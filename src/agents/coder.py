"""CoderAgent powered by gemini-2.5-flash for generating and healing Python CLI scripts."""

from __future__ import annotations

import re
from typing import Any

from google import genai
from google.genai import types

from src.agents.constitution import CONSTITUTION
from src.agents.model_router import (
    CODER_FALLBACK_CHAIN,
    generate_content_with_fallback,
)
from src.config import DEFAULT_CONFIG
from src.observability.pii_scrubber import PIIScrubber
from src.observability.structured_logging import default_logger
from src.observability.telemetry import telemetry
from src.tools.schemas import ScriptSpecification


class CoderAgent:
    """Generates and self-heals single-file Python CLI utilities using gemini-2.5-flash."""

    SYSTEM_PROMPT = (
        "You are the Lead Python Engineer for PyForge, an autonomous Python CLI tool studio.\n"
        f"{CONSTITUTION.render_prompt_block()}\n\n"
        "Requirements for your Python script output:\n"
        "1. Write a complete, self-contained single-file Python 3 CLI script using `argparse`.\n"
        "2. Include `#!/usr/bin/env python3` at the top and a `if __name__ == '__main__':` entry point.\n"
        "3. Handle EOF, missing stdin, invalid flags, and exceptions gracefully.\n"
        "4. Ensure every `TestVector` in the specification passes with the exact expected exit code "
        "and expected stdout substring.\n"
        "5. Output ONLY pure executable Python code without markdown code fences or commentary."
    )

    def __init__(
        self,
        model: str = DEFAULT_CONFIG.coder_model,
        temperature: float = DEFAULT_CONFIG.coder_temperature,
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
                "Missing GOOGLE_API_KEY / GEMINI_API_KEY environment variable for CoderAgent."
            )
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    def generate_code(
        self,
        spec: ScriptSpecification,
        *,
        previous_code: str | None = None,
        error_feedback: str | None = None,
    ) -> str:
        """Generate or self-heal Python CLI script source code from a `ScriptSpecification`.

        Args:
            spec: The target `ScriptSpecification` designed by ArchitectAgent.
            previous_code: Optional prior code attempt that failed AST or sandbox checks.
            error_feedback: Optional AST violation report or sandbox stderr/failure report.

        Returns:
            Pure Python script source code string.
        """
        prompt_sections: list[str] = [
            "Generate a standalone single-file Python 3 CLI script for the following specification:",
            f"Specification JSON:\n{spec.model_dump_json(indent=2)}",
        ]

        if previous_code:
            prompt_sections.append(
                f"Previous Script Attempt:\n```python\n{previous_code}\n```"
            )
        if error_feedback:
            scrubbed_feedback = PIIScrubber.scrub(error_feedback)
            prompt_sections.append(
                "Self-Healing Diagnostic Feedback (Fix ALL issues below):\n"
                f"{scrubbed_feedback}"
            )

        prompt = "\n\n".join(prompt_sections)
        is_healing = bool(previous_code or error_feedback)

        with telemetry.trace_agent_routing("coder", self.model):
            default_logger.info(
                layer="agent.coder",
                intent="heal_script_code" if is_healing else "generate_script_code",
                outcome="started",
                metadata={
                    "model": self.model,
                    "tool_name": spec.tool_name,
                    "is_healing": is_healing,
                },
            )

            client = self._get_client()
            response, resolved_model = generate_content_with_fallback(
                client=client,
                preferred_model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self.SYSTEM_PROMPT,
                    temperature=self.temperature,
                ),
                fallback_chain=CODER_FALLBACK_CHAIN,
                prefer_pro=False,
                layer="agent.coder",
            )
            self.model = resolved_model

            raw_output = (response.text or "").strip()
            clean_code = self.strip_markdown_fences(raw_output)

            default_logger.info(
                layer="agent.coder",
                intent="heal_script_code" if is_healing else "generate_script_code",
                outcome="completed",
                metadata={
                    "tool_name": spec.tool_name,
                    "code_length": len(clean_code),
                },
            )
            return clean_code

    @staticmethod
    def strip_markdown_fences(text: str) -> str:
        """Extract pure Python source code if wrapped in markdown code blocks."""
        if not text:
            return ""
        fence_match = re.search(r"```(?:python|py)?\s*\n([\s\S]*?)```", text)
        if fence_match:
            return fence_match.group(1).strip() + "\n"

        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned + ("\n" if cleaned else "")

