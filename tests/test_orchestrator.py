"""Unit tests for Layer 2 & Layer 3: PyForgeOrchestrator self-healing loop, HITL, SQLite, and Memory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from rich.console import Console

from src.agents.architect import ArchitectAgent
from src.agents.coder import CoderAgent
from src.agents.orchestrator import PyForgeOrchestrator
from src.storage.database import SQLiteDatabase
from src.storage.memory import SlidingWindowMemory
from src.tools.hitl_gate import HumanInTheLoopGate
from src.tools.sandbox_runner import SandboxTestRunner
from src.tools.schemas import ScriptSpecification, TestVector


def _sample_spec() -> ScriptSpecification:
    return ScriptSpecification(
        tool_name="word_counter",
        description="Counts words in standard input or via --text argument.",
        arguments=[{"flag": "--text", "type": "str", "help": "Input text"}],
        test_vectors=[
            TestVector(
                name="count_three_words",
                cli_args=["--text", "one two three"],
                stdin=None,
                expected_exit_code=0,
                expected_stdout_substring="3",
            ),
            TestVector(
                name="count_stdin_words",
                cli_args=[],
                stdin="alpha beta",
                expected_exit_code=0,
                expected_stdout_substring="2",
            ),
        ],
    )


VALID_WORD_COUNTER_CODE = """#!/usr/bin/env python3
import argparse
import sys

def main() -> int:
    parser = argparse.ArgumentParser(description="Word counter")
    parser.add_argument("--text", type=str, default=None)
    args = parser.parse_args()
    content = args.text if args.text is not None else sys.stdin.read()
    words = content.split()
    print(len(words))
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""


def test_orchestrator_self_heals_after_ast_and_runtime_failures(tmp_path: Path) -> None:
    """Orchestrator should self-heal when Coder first emits unsafe code, then buggy code, then valid code."""
    spec = _sample_spec()

    mock_architect = MagicMock(spec=ArchitectAgent)
    mock_architect.generate_specification.return_value = spec

    # Attempt 0: AST Security violation (uses os.system)
    unsafe_code = "import os\nos.system('echo 3')\n"
    # Attempt 1: Passes AST, but fails sandbox test vector (prints wrong number & exits 1)
    buggy_code = """#!/usr/bin/env python3
import sys
sys.stderr.write("RuntimeError: off by one\\n")
print("999")
sys.exit(1)
"""
    # Attempt 2: Valid implementation that passes all test vectors
    mock_coder = MagicMock(spec=CoderAgent)
    mock_coder.generate_code.side_effect = [
        unsafe_code,
        buggy_code,
        VALID_WORD_COUNTER_CODE,
    ]

    db = SQLiteDatabase(db_path=tmp_path / "test_pyforge.db")
    bin_dir = tmp_path / "bin"
    hitl = HumanInTheLoopGate(
        bin_dir=bin_dir,
        console=Console(quiet=True),
        input_fn=lambda _prompt: "A",
    )

    orchestrator = PyForgeOrchestrator(
        architect=mock_architect,
        coder=mock_coder,
        sandbox_runner=SandboxTestRunner(timeout_seconds=5.0),
        hitl_gate=hitl,
        database=db,
        memory=SlidingWindowMemory(max_messages=6),
        max_healing_iterations=3,
    )

    result = orchestrator.run(
        "Create a word counter CLI tool (my key is sk-proj-1234567890abcdefghijklmnop)"
    )

    assert result.status == "installed"
    assert result.healing_attempts_used == 2
    assert result.security_result.is_safe is True
    assert result.sandbox_result.all_passed is True
    assert result.installed_path == bin_dir / "word_counter.py"
    assert result.installed_path.exists()
    # Verify chmod +x was applied
    assert bool(result.installed_path.stat().st_mode & 0o111)

    # Verify PII was scrubbed before ArchitectAgent received the request
    called_kwargs = mock_architect.generate_specification.call_args.kwargs
    assert "sk-proj-" not in called_kwargs["user_request"]
    assert "[REDACTED_OPENAI_KEY]" in called_kwargs["user_request"]

    # Verify feedback was passed to CoderAgent on healing attempts
    second_call_kwargs = mock_coder.generate_code.call_args_list[1].kwargs
    assert "Static AST Security Violations" in second_call_kwargs["error_feedback"]

    third_call_kwargs = mock_coder.generate_code.call_args_list[2].kwargs
    assert "RuntimeError: off by one" in third_call_kwargs["error_feedback"]

    # Verify SQLite persistence
    saved_scripts = db.list_generated_scripts()
    assert len(saved_scripts) == 1
    assert saved_scripts[0]["name"] == "word_counter"
    assert saved_scripts[0]["status"] == "installed"


def test_orchestrator_fails_after_exhausting_max_healing_iterations(tmp_path: Path) -> None:
    """If Coder fails on initial attempt + all 3 healing retries, status must be 'failed'."""
    spec = _sample_spec()

    mock_architect = MagicMock(spec=ArchitectAgent)
    mock_architect.generate_specification.return_value = spec

    always_broken_code = """#!/usr/bin/env python3
import sys
sys.stderr.write("Broken implementation\\n")
sys.exit(1)
"""
    mock_coder = MagicMock(spec=CoderAgent)
    mock_coder.generate_code.return_value = always_broken_code

    db = SQLiteDatabase(db_path=tmp_path / "test_fail.db")
    hitl = HumanInTheLoopGate(
        bin_dir=tmp_path / "bin",
        console=Console(quiet=True),
        input_fn=lambda _prompt: "A",
    )

    orchestrator = PyForgeOrchestrator(
        architect=mock_architect,
        coder=mock_coder,
        sandbox_runner=SandboxTestRunner(timeout_seconds=5.0),
        hitl_gate=hitl,
        database=db,
        max_healing_iterations=3,
    )

    result = orchestrator.run("Build word counter")

    assert result.status == "failed"
    assert result.healing_attempts_used == 3
    assert result.installed_path is None
    assert mock_coder.generate_code.call_count == 4  # 1 initial + 3 healing


def test_sliding_window_memory_compacts_when_budget_exceeded() -> None:
    """SlidingWindowMemory must compact older messages into system_summary when exceeding max_messages."""
    memory = SlidingWindowMemory(
        max_messages=4,
        api_key="",
        custom_summarizer=lambda older, prev: f"Summary({len(older)} turns, prev={bool(prev)})",
    )

    for i in range(5):
        memory.add_message("user", f"Turn {i}")

    assert len(memory.messages) <= 4
    assert "Summary(" in memory.system_summary
    assert "### Conversation Summary" in memory.get_context_string()


def test_sliding_window_memory_async_background_worker() -> None:
    """SlidingWindowMemory async worker should consolidate older messages in the background without blocking."""
    memory = SlidingWindowMemory(
        max_messages=4,
        api_key="",
        custom_summarizer=lambda older, prev: f"AsyncSummary({len(older)} turns)",
    )

    async def _run_async_memory() -> None:
        await memory.start_worker()
        for i in range(6):
            await memory.add_message_async("user", f"Async turn {i}")
        await memory.wait_for_compaction()
        await memory.stop_worker()

    asyncio.run(_run_async_memory())
    assert len(memory.messages) <= 4
    assert "AsyncSummary(" in memory.system_summary



def test_architect_response_schema_is_gemini_api_compatible() -> None:
    """ArchitectAgent.RESPONSE_SCHEMA must serialize for Gemini API without additionalProperties errors."""
    from google.genai import Client, models, types

    client = Client(api_key="test-key")
    params = types._GenerateContentParameters(
        model="gemini-2.5-pro",
        contents="build a tool",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ArchitectAgent.RESPONSE_SCHEMA,
        ),
    )
    payload = models._GenerateContentParameters_to_mldev(client._api_client, params)
    assert "generationConfig" in payload
    assert "responseSchema" in payload["generationConfig"]


def test_architect_automatically_falls_back_on_404_not_found() -> None:
    """If gemini-2.5-pro returns 404 NOT_FOUND, ArchitectAgent should fall back to an available model."""
    spec = _sample_spec()
    fake_client = MagicMock()

    def _side_effect(*, model: str, contents: str, config: object) -> object:
        if model == "gemini-2.5-pro":
            raise RuntimeError(
                "404 NOT_FOUND. {'error': {'code': 404, 'message': 'models/gemini-2.5-pro is not found'}}"
            )
        resp = MagicMock()
        resp.text = spec.model_dump_json()
        return resp

    fake_client.models.generate_content.side_effect = _side_effect

    agent = ArchitectAgent(model="gemini-2.5-pro", client=fake_client)
    returned_spec = agent.generate_specification("Build a word counter")

    assert returned_spec.tool_name == "word_counter"
    assert agent.model != "gemini-2.5-pro"
    assert fake_client.models.generate_content.call_count == 2



def test_sqlite_async_worker_persists_records(tmp_path: Path) -> None:

    """SQLiteDatabase async background worker queue should persist sessions, messages, and scripts."""
    db = SQLiteDatabase(db_path=tmp_path / "async_worker.db")

    async def _exercise_worker() -> None:
        await db.start_worker()
        sid = await db.create_session_async("Async Session")
        await db.add_message_async(sid, "user", "Hello async")
        await db.save_generated_script_async(
            session_id=sid,
            name="async_tool",
            spec_json="{}",
            code="print('ok')",
            status="installed",
        )
        await db.stop_worker()

        msgs = db.get_session_messages(sid)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello async"

        script = db.get_generated_script_by_name("async_tool")
        assert script is not None
        assert script["status"] == "installed"

    asyncio.run(_exercise_worker())

