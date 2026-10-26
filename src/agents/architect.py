"""ArchitectAgent powered by gemini-2.5-pro for generating ScriptSpecifications."""

from __future__ import annotations

import json
import re
from typing import Any

from google import genai
from google.genai import types

from src.agents.constitution import CONSTITUTION
from src.agents.model_router import (
    ARCHITECT_FALLBACK_CHAIN,
    generate_content_with_fallback,
)
from src.config import DEFAULT_CONFIG
from src.observability.pii_scrubber import PIIScrubber
from src.observability.structured_logging import default_logger
from src.observability.telemetry import telemetry
from src.tools.schemas import ScriptSpecification


class ArchitectAgent:
    """Designs CLI specifications and deterministic TestVectors using gemini-2.5-pro."""

    RESPONSE_SCHEMA = types.Schema(
        type=types.Type.OBJECT,
        required=["tool_name", "description", "arguments", "test_vectors"],
        properties={
            "tool_name": types.Schema(
                type=types.Type.STRING,
                description="Snake-case identifier for the CLI script (without .py extension).",
            ),
            "description": types.Schema(
                type=types.Type.STRING,
                description="Clear summary of what the CLI utility does.",
            ),
            "arguments": types.Schema(
                type=types.Type.ARRAY,
                description="List of CLI argument definitions (flags, types, help strings).",
                items=types.Schema(
                    type=types.Type.OBJECT,
                    required=["flag", "type", "help"],
                    properties={
                        "flag": types.Schema(
                            type=types.Type.STRING,
                            description="CLI flag or positional argument name (e.g. '--input' or '--count').",
                        ),
                        "type": types.Schema(
                            type=types.Type.STRING,
                            description="Argument data type (e.g. 'str', 'int', 'float', 'bool').",
                        ),
                        "required": types.Schema(
                            type=types.Type.BOOLEAN,
                            description="Whether the CLI argument is required.",
                        ),
                        "default": types.Schema(
                            type=types.Type.STRING,
                            nullable=True,
                            description="Optional default value as string.",
                        ),
                        "help": types.Schema(
                            type=types.Type.STRING,
                            description="Help description for this CLI argument.",
                        ),
                    },
                ),
            ),
            "test_vectors": types.Schema(
                type=types.Type.ARRAY,
                description="2 to 4 deterministic test vectors verifying CLI behavior.",
                items=types.Schema(
                    type=types.Type.OBJECT,
                    required=[
                        "name",
                        "cli_args",
                        "expected_exit_code",
                    ],
                    properties={
                        "name": types.Schema(
                            type=types.Type.STRING,
                            description="Descriptive name of the test case.",
                        ),
                        "cli_args": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description="Command-line arguments passed to the script.",
                        ),
                        "stdin": types.Schema(
                            type=types.Type.STRING,
                            nullable=True,
                            description="Optional standard input string piped to the process.",
                        ),
                        "expected_exit_code": types.Schema(
                            type=types.Type.INTEGER,
                            description="Expected integer process exit code.",
                        ),
                        "expected_stdout_substring": types.Schema(
                            type=types.Type.STRING,
                            nullable=True,
                            description="Optional substring expected to appear in stdout.",
                        ),
                    },
                ),
            ),
        },
    )

    SYSTEM_PROMPT = (
        "You are the Lead Systems Architect for PyForge, an autonomous Python CLI tool studio.\n"
        f"{CONSTITUTION.render_prompt_block()}\n\n"
        "Your task is to analyze the user's CLI utility request and conversation context, then "
        "produce a strict JSON object matching the `ScriptSpecification` schema:\n"
        "- `tool_name`: snake_case script name (without `.py`).\n"
        "- `description`: concise explanation of the CLI tool's purpose and behavior.\n"
        "- `arguments`: list of CLI flag/positional definitions (e.g., flag, type, required, help).\n"
        "- `test_vectors`: 2 to 4 deterministic `TestVector` items covering standard usage, flags, "
        "stdin handling (if applicable), and edge cases. Every `TestVector` must define:\n"
        "  - `name`: descriptive test name\n"
        "  - `cli_args`: list of string CLI arguments (self-contained, not requiring external files)\n"
        "  - `stdin`: optional string piped to stdin (or null)\n"
        "  - `expected_exit_code`: expected integer exit code (usually 0 for valid runs)\n"
        "  - `expected_stdout_substring`: deterministic substring expected in stdout\n\n"
        "Return ONLY valid JSON conforming to `ScriptSpecification`."
    )

    def __init__(
        self,
        model: str = DEFAULT_CONFIG.architect_model,
        temperature: float = DEFAULT_CONFIG.architect_temperature,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.api_key = api_key if api_key is not None else DEFAULT_CONFIG.google_api_key
        self._client = client

    def _get_client(self) -> Any:
        """Lazily initialize or return the injected Google GenAI client."""
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise RuntimeError(
                "Missing GOOGLE_API_KEY / GEMINI_API_KEY environment variable for ArchitectAgent."
            )
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    def generate_specification(
        self,
        user_request: str,
        conversation_context: str = "",
    ) -> ScriptSpecification:
        """Generate a validated `ScriptSpecification` from a natural language request.

        Args:
            user_request: Scrubbed natural language description of the desired CLI tool.
            conversation_context: Optional sliding-window memory context.

        Returns:
            Validated `ScriptSpecification` instance.
        """
        scrubbed_request = PIIScrubber.scrub(user_request)
        scrubbed_context = PIIScrubber.scrub(conversation_context)

        prompt_parts = [f"User Request:\n{scrubbed_request}"]
        if scrubbed_context.strip():
            prompt_parts.append(f"Conversation Context:\n{scrubbed_context}")
        prompt = "\n\n".join(prompt_parts)

        with telemetry.trace_agent_routing("architect", self.model):
            default_logger.info(
                layer="agent.architect",
                intent="generate_script_specification",
                outcome="started",
                metadata={"model": self.model, "request_preview": scrubbed_request[:120]},
            )

            client = self._get_client()
            response, resolved_model = generate_content_with_fallback(
                client=client,
                preferred_model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self.SYSTEM_PROMPT,
                    temperature=self.temperature,
                    response_mime_type="application/json",
                    response_schema=self.RESPONSE_SCHEMA,
                ),
                fallback_chain=ARCHITECT_FALLBACK_CHAIN,
                prefer_pro=True,
                layer="agent.architect",
            )
            self.model = resolved_model

            raw_text = (response.text or "").strip()
            cleaned_json = self._extract_json(raw_text)
            spec = ScriptSpecification.model_validate_json(cleaned_json)

            default_logger.info(
                layer="agent.architect",
                intent="generate_script_specification",
                outcome="completed",
                metadata={
                    "tool_name": spec.tool_name,
                    "vector_count": len(spec.test_vectors),
                },
            )
            return spec

    @staticmethod
    def _extract_json(text: str) -> str:
        """Strip optional markdown code fences around JSON output."""
        if not text:
            raise ValueError("ArchitectAgent returned an empty response.")
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence_match:
            return fence_match.group(1).strip()
        # Fallback if valid JSON already
        json.loads(text)
        return text

