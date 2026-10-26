"""Sliding-window conversation memory with non-blocking asynchronous background compaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
    """Maintains a bounded window of recent messages and compacts older turns asynchronously."""

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
        self._compaction_queue: asyncio.Queue[list[ConversationMessage] | None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        """Lazily initialize asyncio.Lock within the active event loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

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

    def _exceeds_budget(self) -> bool:
        """Return True if message count or token budget exceeds configured thresholds."""
        return (
            len(self.messages) > self.max_messages
            or self.total_estimated_tokens() > self.max_token_budget
        )

    async def start_worker(self) -> None:
        """Start the background asyncio.Queue compaction worker to prevent UI blocking."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._compaction_queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._compaction_worker())
        default_logger.info(
            layer="memory",
            intent="start_async_compaction_worker",
            outcome="started",
            metadata={"max_messages": self.max_messages},
        )

    async def stop_worker(self) -> None:
        """Drain pending background compactions and stop the async worker."""
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
        if self._compaction_queue is not None and self._worker_task is not None:
            await self._compaction_queue.put(None)
            await self._worker_task
            self._worker_task = None
            self._compaction_queue = None
            self._lock = None
            default_logger.info(
                layer="memory",
                intent="stop_async_compaction_worker",
                outcome="stopped",
            )

    async def _compaction_worker(self) -> None:
        """Background worker that consolidates older dialogue batches asynchronously."""
        assert self._compaction_queue is not None
        while True:
            batch = await self._compaction_queue.get()
            if batch is None:
                self._compaction_queue.task_done()
                break
            try:
                async with self._get_lock():
                    new_summary = await self._summarize_turns_async(
                        batch, self.system_summary
                    )
                    self.system_summary = PIIScrubber.scrub(new_summary)
                    default_logger.info(
                        layer="memory",
                        intent="async_compact_sliding_window",
                        outcome="compacted",
                        metadata={
                            "compacted_turns": len(batch),
                            "remaining_turns": len(self.messages),
                            "summary_length": len(self.system_summary),
                        },
                    )
            except Exception as exc:
                default_logger.error(
                    layer="memory",
                    intent="async_compact_sliding_window",
                    outcome="error",
                    metadata={"error": str(exc)},
                )
            finally:
                self._compaction_queue.task_done()

    def _slice_for_compaction(self) -> list[ConversationMessage]:
        """Slice oldest messages out of the active window for background consolidation."""
        keep_count = max(1, self.max_messages // 2)
        if len(self.messages) <= keep_count:
            return []
        to_summarize = self.messages[:-keep_count]
        self.messages = self.messages[-keep_count:]
        return to_summarize

    async def add_message_async(self, role: str, content: str) -> None:
        """Add a message and trigger non-blocking asynchronous background compaction."""
        clean_content = PIIScrubber.scrub(content)
        self.messages.append(ConversationMessage(role=role, content=clean_content))

        if self._exceeds_budget():
            to_summarize = self._slice_for_compaction()
            if not to_summarize:
                return
            if (
                self._compaction_queue is not None
                and self._worker_task is not None
                and not self._worker_task.done()
            ):
                await self._compaction_queue.put(to_summarize)
            else:
                task = asyncio.create_task(self._compact_batch_async(to_summarize))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def _compact_batch_async(
        self, to_summarize: list[ConversationMessage]
    ) -> None:
        """Consolidate a batch of older messages asynchronously without blocking UI."""
        async with self._get_lock():
            new_summary = await self._summarize_turns_async(
                to_summarize, self.system_summary
            )
            self.system_summary = PIIScrubber.scrub(new_summary)
            default_logger.info(
                layer="memory",
                intent="async_compact_sliding_window",
                outcome="compacted",
                metadata={
                    "compacted_turns": len(to_summarize),
                    "remaining_turns": len(self.messages),
                    "summary_length": len(self.system_summary),
                },
            )

    async def wait_for_compaction(self) -> None:
        """Await all in-flight asynchronous memory compaction tasks."""
        if self._compaction_queue is not None:
            await self._compaction_queue.join()
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

    def add_message(self, role: str, content: str) -> None:
        """Add a message to memory and schedule background async compaction if budget exceeded."""
        clean_content = PIIScrubber.scrub(content)
        self.messages.append(ConversationMessage(role=role, content=clean_content))

        if self._exceeds_budget():
            to_summarize = self._slice_for_compaction()
            if not to_summarize:
                return
            try:
                loop = asyncio.get_running_loop()
                if (
                    self._compaction_queue is not None
                    and self._worker_task is not None
                    and not self._worker_task.done()
                ):
                    loop.call_soon_threadsafe(
                        self._compaction_queue.put_nowait, to_summarize
                    )
                else:
                    task = loop.create_task(self._compact_batch_async(to_summarize))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            except RuntimeError:
                # No running event loop in current thread; run async compaction via asyncio.run
                self._lock = None
                asyncio.run(self._compact_batch_async(to_summarize))
                self._lock = None

    async def _summarize_turns_async(
        self,
        older_messages: list[ConversationMessage],
        existing_summary: str,
    ) -> str:
        """Asynchronously summarize older dialogue turns using Gemini async client (`client.aio`)."""
        if self.custom_summarizer is not None:
            return await asyncio.to_thread(
                self.custom_summarizer, older_messages, existing_summary
            )

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

                response = await client.aio.models.generate_content(
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
                    intent="gemini_async_summarize_turns",
                    outcome="fallback_local_summary",
                    metadata={"error": str(exc)},
                )

        return await asyncio.to_thread(
            self._local_fallback_summary, older_messages, existing_summary
        )

    @staticmethod
    def _local_fallback_summary(
        older_messages: list[ConversationMessage],
        existing_summary: str,
    ) -> str:
        """Deterministic fallback summary when offline or no API key is configured."""
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
