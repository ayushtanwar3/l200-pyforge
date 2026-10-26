"""Resilient Gemini model invocation with automatic model discovery and fallback on 404/NOT_FOUND."""

from __future__ import annotations

from typing import Any

from google.genai import types

from src.observability.structured_logging import default_logger

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


def is_model_not_found_error(exc: Exception) -> bool:
    """Return True if an exception indicates a 404 / NOT_FOUND / unsupported model error."""
    msg = str(exc).lower()
    return (
        "404" in msg
        or "not_found" in msg
        or "not found" in msg
        or "not supported for generatecontent" in msg
    )


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
    """Call `client.models.generate_content` with automatic fallback if `preferred_model` is 404.

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
            if not is_model_not_found_error(exc):
                raise
            default_logger.warning(
                layer=layer,
                intent="model_not_found_fallback",
                outcome="retrying_next_model",
                metadata={"unavailable_model": model_name, "error": str(exc)[:200]},
            )

    # Dynamic discovery via ListModels if all static candidates returned 404
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
            if not is_model_not_found_error(exc):
                raise

    assert last_exc is not None
    raise last_exc

