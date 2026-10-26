"""Unit tests for Layer 4: SandboxTestRunner subprocess execution, timeouts, and env isolation."""

from __future__ import annotations

import os

from src.tools.sandbox_runner import SandboxTestRunner
from src.tools.schemas import TestVector


def test_sandbox_runner_passes_valid_cli_script() -> None:
    """SandboxTestRunner should pass when exit code and stdout substring match."""
    code = """#!/usr/bin/env python3
import argparse
import sys

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upper", action="store_true")
    args = parser.parse_args()
    data = sys.stdin.read().strip()
    print(data.upper() if args.upper else data)
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""
    vectors = [
        TestVector(
            name="uppercase_stdin",
            cli_args=["--upper"],
            stdin="hello pyforge",
            expected_exit_code=0,
            expected_stdout_substring="HELLO PYFORGE",
        ),
        TestVector(
            name="passthrough_stdin",
            cli_args=[],
            stdin="unchanged",
            expected_exit_code=0,
            expected_stdout_substring="unchanged",
        ),
    ]

    runner = SandboxTestRunner(timeout_seconds=5.0)
    batch = runner.run_tests(code=code, test_vectors=vectors)

    assert batch.all_passed is True
    assert batch.passed_count == 2
    assert batch.failed_count == 0


def test_sandbox_runner_captures_failure_and_stderr() -> None:
    """SandboxTestRunner should capture non-zero exit codes and stderr output."""
    code = """#!/usr/bin/env python3
import sys
sys.stderr.write("ValueError: invalid input format\\n")
sys.exit(2)
"""
    vectors = [
        TestVector(
            name="should_fail",
            cli_args=[],
            stdin=None,
            expected_exit_code=0,
            expected_stdout_substring="ok",
        )
    ]

    runner = SandboxTestRunner(timeout_seconds=5.0)
    batch = runner.run_tests(code=code, test_vectors=vectors)

    assert batch.all_passed is False
    assert batch.failed_count == 1
    assert batch.results[0].exit_code == 2
    assert "ValueError: invalid input format" in batch.results[0].stderr
    assert "ValueError: invalid input format" in batch.combined_stderr


def test_sandbox_runner_enforces_timeout() -> None:
    """Scripts exceeding the timeout must be terminated and marked timed_out=True."""
    code = """#!/usr/bin/env python3
import time
time.sleep(10)
"""
    vectors = [
        TestVector(
            name="infinite_sleep",
            cli_args=[],
            stdin=None,
            expected_exit_code=0,
            expected_stdout_substring=None,
        )
    ]

    runner = SandboxTestRunner(timeout_seconds=0.3)
    batch = runner.run_tests(code=code, test_vectors=vectors)

    assert batch.all_passed is False
    assert len(batch.results) == 1
    res = batch.results[0]
    assert res.passed is False
    assert res.timed_out is True
    assert res.exit_code == -1
    assert "timed out" in res.stderr.lower()


def test_sandbox_runner_strips_sensitive_environment_variables(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """GOOGLE_API_KEY and other secrets in parent env must not leak into sandboxed subprocess."""
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSySecretKeyShouldNotLeak1234567890123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-secretShouldNotLeak1234567890")

    code = """#!/usr/bin/env python3
import os
print("GOOGLE=" + os.environ.get("GOOGLE_API_KEY", "MISSING"))
print("OPENAI=" + os.environ.get("OPENAI_API_KEY", "MISSING"))
"""
    vectors = [
        TestVector(
            name="check_env_stripped",
            cli_args=[],
            stdin=None,
            expected_exit_code=0,
            expected_stdout_substring="GOOGLE=MISSING",
        )
    ]

    runner = SandboxTestRunner(timeout_seconds=5.0)
    batch = runner.run_tests(code=code, test_vectors=vectors)

    assert batch.all_passed is True
    assert "OPENAI=MISSING" in batch.results[0].stdout
    assert "AIzaSySecretKey" not in batch.results[0].stdout

