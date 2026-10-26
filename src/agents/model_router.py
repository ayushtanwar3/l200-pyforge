"""Strategic Model Router with task-based routing (Pro vs. Flash), 503 backoff, and 404 fallback."""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from google.genai import types

from src.config import DEFAULT_CONFIG
from src.observability.structured_logging import default_logger


class TaskComplexity(str, Enum):
    """Task complexity tiers for strategic model routing."""

    PLANNING = "planning"          # High-reasoning architectural specification (Pro)
    FAST_EXECUTION = "execution"   # Fast code synthesis, self-healing, compaction (Flash)


ARCHITECT_FALLBACK_CHAIN: tuple[str, ...] = (
    "gemini-2.5-pro",
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-pro-latest",
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-2.0-flash",
)

CODER_FALLBACK_CHAIN: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-pro-latest",
)

_EXCLUDED_MODEL_SUBSTRINGS: tuple[str, ...] = (
    "embedding",
    "imagen",
    "veo",
    "aqa",
    "tts",
    "audio",
    "vision",
    "bisect",
    "learnlm",
    "robotics",
    "computer-use",
)


class StrategicModelRouter:
    """Routes agent requests to the optimal Gemini model tier based on task complexity.

    - Routes `TaskComplexity.PLANNING` (requirement analysis, edge-case & test-vector design)
      to `gemini-2.5-pro`.
    - Routes `TaskComplexity.FAST_EXECUTION` (code synthesis, iterative self-healing,
      and memory summarization) to `gemini-2.5-flash`.
    """

    def __init__(
        self,
        planning_model: str = DEFAULT_CONFIG.architect_model,
        execution_model: str = DEFAULT_CONFIG.coder_model,
    ) -> None:
        self.planning_model = planning_model
        self.execution_model = execution_model

    def select_model(self, complexity: TaskComplexity) -> str:
        """Select the optimal model ID for a given task complexity tier."""
        selected = (
            self.planning_model
            if complexity == TaskComplexity.PLANNING
            else self.execution_model
        )
        default_logger.info(
            layer="agent.router",
            intent="strategic_model_routing",
            outcome="model_selected",
            metadata={"complexity": complexity.value, "selected_model": selected},
        )
        return selected


def is_model_not_found_error(exc: Exception) -> bool:
    """Return True if an exception indicates a 404 / NOT_FOUND / unsupported model error."""
    msg = str(exc).lower()
    return (
        "404" in msg
        or "not_found" in msg
        or "not found" in msg
        or "not supported for generatecontent" in msg
    )


def is_transient_unavailable_error(exc: Exception) -> bool:
    """Return True if an exception indicates a transient 503 UNAVAILABLE / 429 spike."""
    msg = str(exc).lower()
    return "503" in msg or "unavailable" in msg or "high demand" in msg


def discover_available_models(client: Any, prefer_pro: bool = False) -> list[str]:
    """Query `client.models.list()` for text generation Gemini models available to this API key."""
    discovered: list[str] = []
    try:
        for model_obj in client.models.list():
            name = getattr(model_obj, "name", "") or ""
            if name.startswith("models/"):
                name = name[len("models/") :]
            if not name.startswith("gemini"):
                continue
            lower_name = name.lower()
            if any(excl in lower_name for excl in _EXCLUDED_MODEL_SUBSTRINGS):
                continue

            actions = getattr(model_obj, "supported_actions", None)
            if actions and "generateContent" not in actions:
                continue

            discovered.append(name)
    except Exception as exc:
        default_logger.warning(
            layer="agent.router",
            intent="list_available_models",
            outcome="failed",
            metadata={"error": str(exc)},
        )
        return []

    def _sort_key(model_name: str) -> tuple[int, str]:
        m = model_name.lower()
        if prefer_pro:
            if "pro" in m:
                return (0, m)
            if "flash" in m and "lite" not in m:
                return (1, m)
            return (2, m)
        else:
            if "flash" in m and "lite" not in m:
                return (0, m)
            if "pro" in m:
                return (1, m)
            return (2, m)

    discovered.sort(key=_sort_key)
    return discovered


def generate_content_with_fallback(
    *,
    client: Any,
    preferred_model: str,
    contents: Any,
    config: types.GenerateContentConfig,
    fallback_chain: tuple[str, ...],
    prefer_pro: bool,
    layer: str,
) -> tuple[Any, str]:
    """Call `client.models.generate_content` with automatic fallback on 404 or 503 errors.

    Returns:
        Tuple of `(response, resolved_model_name)`.
    """
    candidates: list[str] = [preferred_model]
    for candidate in fallback_chain:
        if candidate not in candidates:
            candidates.append(candidate)

    last_exc: Exception | None = None
    tried: set[str] = set()

    for idx, model_name in enumerate(candidates):
        tried.add(model_name)
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            if idx > 0:
                default_logger.info(
                    layer=layer,
                    intent="model_fallback_succeeded",
                    outcome="resolved",
                    metadata={
                        "preferred_model": preferred_model,
                        "resolved_model": model_name,
                    },
                )
            return response, model_name
        except Exception as exc:
            last_exc = exc
            if is_transient_unavailable_error(exc):
                default_logger.warning(
                    layer=layer,
                    intent="model_503_unavailable_fallback",
                    outcome="retrying_next_model",
                    metadata={"unavailable_model": model_name, "error": str(exc)[:200]},
                )
                time.sleep(0.2)
                continue
            if not is_model_not_found_error(exc):
                raise
            default_logger.warning(
                layer=layer,
                intent="model_not_found_fallback",
                outcome="retrying_next_model",
                metadata={"unavailable_model": model_name, "error": str(exc)[:200]},
            )

    # Dynamic discovery via ListModels if all static candidates returned 404/503
    for dynamic_model in discover_available_models(client, prefer_pro=prefer_pro):
        if dynamic_model in tried:
            continue
        tried.add(dynamic_model)
        try:
            response = client.models.generate_content(
                model=dynamic_model,
                contents=contents,
                config=config,
            )
            default_logger.info(
                layer=layer,
                intent="dynamic_model_discovery_succeeded",
                outcome="resolved",
                metadata={
                    "preferred_model": preferred_model,
                    "resolved_model": dynamic_model,
                },
            )
            return response, dynamic_model
        except Exception as exc:
            last_exc = exc
            if not (is_model_not_found_error(exc) or is_transient_unavailable_error(exc)):
                raise

    assert last_exc is not None
    raise last_exc
