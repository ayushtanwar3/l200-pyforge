"""Strict Pydantic v2 models for PyForge tool specifications and execution results."""

from __future__ import annotations

import re
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TestVector(BaseModel):
    """Single deterministic CLI test vector for verifying a generated script."""

    __test__ = False
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, description="Descriptive name of the test case.")
    cli_args: list[str] = Field(
        default_factory=list,
        description="Command-line arguments passed to the script.",
    )
    stdin: Optional[str] = Field(
        default=None,
        description="Optional standard input string piped to the process.",
    )
    expected_exit_code: int = Field(
        default=0,
        description="Expected integer process exit code.",
    )
    expected_stdout_substring: Optional[str] = Field(
        default=None,
        description="Optional substring expected to appear in stdout.",
    )


class ScriptSpecification(BaseModel):
    """Architectural specification for a single-file Python CLI utility."""

    model_config = ConfigDict(extra="ignore")

    tool_name: str = Field(
        ...,
        min_length=1,
        description="Snake-case identifier for the CLI script (without .py extension).",
    )
    description: str = Field(
        ...,
        min_length=1,
        description="Clear summary of what the CLI utility does.",
    )
    arguments: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of CLI argument definitions (flags, types, help strings).",
    )
    test_vectors: list[TestVector] = Field(
        ...,
        min_length=1,
        description="2-4 deterministic test vectors verifying CLI behavior.",
    )

    @field_validator("tool_name")
    @classmethod
    def sanitize_tool_name(cls, value: str) -> str:
        """Normalize tool_name into a safe snake_case filename stem."""
        cleaned = value.strip().lower()
        if cleaned.endswith(".py"):
            cleaned = cleaned[:-3]
        cleaned = re.sub(r"[^a-z0-9_]+", "_", cleaned).strip("_")
        if not cleaned:
            raise ValueError("tool_name must contain at least one alphanumeric character")
        return cleaned


class TestExecutionResult(BaseModel):
    """Execution result of running a single TestVector inside the sandbox."""

    __test__ = False
    model_config = ConfigDict(extra="ignore")

    vector_name: str
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    failure_reason: Optional[str] = None


class SandboxBatchResult(BaseModel):
    """Aggregated outcome of running a batch of TestVectors in the sandbox."""

    model_config = ConfigDict(extra="ignore")

    all_passed: bool
    results: list[TestExecutionResult] = Field(default_factory=list)
    total_duration_ms: float = 0.0
    combined_stderr: str = ""

    @property
    def passed_count(self) -> int:
        """Number of test vectors that passed."""
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        """Number of test vectors that failed."""
        return sum(1 for r in self.results if not r.passed)

    def format_failure_report(self) -> str:
        """Format failed test vector details for Coder self-healing feedback."""
        lines: list[str] = []
        for res in self.results:
            if not res.passed:
                lines.append(
                    f"- Test '{res.vector_name}' FAILED "
                    f"(exit_code={res.exit_code}, timed_out={res.timed_out}): "
                    f"{res.failure_reason or 'Check stderr/stdout.'}"
                )
                if res.stdout.strip():
                    lines.append(f"  stdout: {res.stdout.strip()}")
                if res.stderr.strip():
                    lines.append(f"  stderr: {res.stderr.strip()}")
        return "\n".join(lines) if lines else "All tests passed."
