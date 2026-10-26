"""Sliding-window conversation memory with automatic Gemini-backed compaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from google import genai
from google.genai import types

from src.config import DEFAULT_CONFIG
from src.observability.pii_scrubber import PIIScrubber
from src.observability.structured_logging import default_logger


@dataclass
class ConversationMessage:
    """Single conversation turn stored in sliding window memory."""

    role: str
    content: str


class SlidingWindowMemory:
    """Maintains a bounded window of recent messages and compacts older turns into a summary."""

    def __init__(
        self,
        max_messages: int = DEFAULT_CONFIG.memory_max_messages,
        max_token_budget: int = 4000,
        summarizer_model: str = DEFAULT_CONFIG.coder_model,
        api_key: str | None = None,
        custom_summarizer: Callable[[list[ConversationMessage], str], str] | None = None,
    ) -> None:
        if max_messages < 2:
            raise ValueError("max_messages must be at least 2")
        self.max_messages = max_messages
        self.max_token_budget = max_token_budget
        self.summarizer_model = summarizer_model
        self.api_key = api_key if api_key is not None else DEFAULT_CONFIG.google_api_key
        self.custom_summarizer = custom_summarizer

        self.system_summary: str = ""
        self.messages: list[ConversationMessage] = []

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Approximate token count (~4 characters per token)."""
        return max(1, len(text) // 4)

    def total_estimated_tokens(self) -> int:
        """Compute total estimated tokens across summary and active window."""
        total = self.estimate_tokens(self.system_summary) if self.system_summary else 0
        for msg in self.messages:
            total += self.estimate_tokens(msg.content)
        return total

    def add_message(self, role: str, content: str) -> None:
        """Add a message to memory and compact older messages if budget is exceeded."""
        clean_content = PIIScrubber.scrub(content)
        self.messages.append(ConversationMessage(role=role, content=clean_content))

        if (
            len(self.messages) > self.max_messages
            or self.total_estimated_tokens() > self.max_token_budget
        ):
            self._compact()

    def _compact(self) -> None:
        """Compact the oldest messages into `self.system_summary`."""
        keep_count = max(1, self.max_messages // 2)
        if len(self.messages) <= keep_count:
            return

        to_summarize = self.messages[:-keep_count]
        self.messages = self.messages[-keep_count:]

        new_summary = self._summarize_turns(to_summarize, self.system_summary)
        self.system_summary = PIIScrubber.scrub(new_summary)

        default_logger.info(
            layer="memory",
            intent="compact_sliding_window",
            outcome="compacted",
            metadata={
                "compacted_turns": len(to_summarize),
                "remaining_turns": len(self.messages),
                "summary_length": len(self.system_summary),
            },
        )

    def _summarize_turns(
        self,
        older_messages: list[ConversationMessage],
        existing_summary: str,
    ) -> str:
        """Summarize older dialogue turns using `gemini-2.5-flash` (or fallback when offline)."""
        if self.custom_summarizer is not None:
            return self.custom_summarizer(older_messages, existing_summary)

        formatted_turns = "\n".join(
            f"{msg.role.upper()}: {msg.content}" for msg in older_messages
        )

        if self.api_key:
            try:
                client = genai.Client(api_key=self.api_key)
                prompt = (
                    "Summarize the following prior conversation turns concisely, preserving "
                    "CLI requirements, flags, constraints, and error context.\n\n"
                )
                if existing_summary:
                    prompt += f"Existing Summary:\n{existing_summary}\n\n"
                prompt += f"Older Dialogue Turns:\n{formatted_turns}"

                response = client.models.generate_content(
                    model=self.summarizer_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                    ),
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as exc:
                default_logger.warning(
                    layer="memory",
                    intent="gemini_summarize_turns",
                    outcome="fallback_local_summary",
                    metadata={"error": str(exc)},
                )

        # Deterministic local fallback compaction
        snippets = [
            f"[{m.role}] {m.content[:160].strip()}" for m in older_messages
        ]
        combined = " | ".join(snippets)
        if existing_summary:
            return f"{existing_summary} | {combined}"
        return f"Compacted context: {combined}"

    def get_context_string(self) -> str:
        """Render the current system summary and recent message window as a context block."""
        sections: list[str] = []
        if self.system_summary:
            sections.append(f"### Conversation Summary\n{self.system_summary}")
        if self.messages:
            turns = "\n".join(
                f"{msg.role.upper()}: {msg.content}" for msg in self.messages
            )
            sections.append(f"### Recent Messages\n{turns}")
        return "\n\n".join(sections)

