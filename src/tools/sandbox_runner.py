"""Sandboxed subprocess test runner with strict timeouts and stripped environment."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src.config import DEFAULT_CONFIG
from src.observability.pii_scrubber import PIIScrubber
from src.observability.structured_logging import default_logger
from src.observability.telemetry import telemetry
from src.tools.schemas import SandboxBatchResult, TestExecutionResult, TestVector


class SandboxTestRunner:
    """Executes generated Python scripts against TestVectors in an isolated subprocess."""

    SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
        "GOOGLE_",
        "GEMINI_",
        "OPENAI_",
        "ANTHROPIC_",
        "AWS_",
        "GITHUB_",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "API_KEY",
        "PRIVATE_KEY",
    )

    SAFE_ENV_KEYS: tuple[str, ...] = (
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
    )

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_CONFIG.sandbox_timeout_seconds,
        python_executable: str = sys.executable,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.python_executable = python_executable

    def _build_restricted_env(self) -> dict[str, str]:
        """Construct a sanitized environment dictionary stripped of sensitive variables."""
        env: dict[str, str] = {}
        for key in self.SAFE_ENV_KEYS:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

        # Ensure defensive safety in case any safe key somehow matched sensitive prefixes
        for key in list(env.keys()):
            upper = key.upper()
            if any(prefix in upper for prefix in self.SENSITIVE_ENV_PREFIXES):
                env.pop(key, None)

        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def run_tests(
        self,
        code: str,
        test_vectors: list[TestVector],
    ) -> SandboxBatchResult:
        """Write `code` to a secure temporary file and execute all `test_vectors`.

        Args:
            code: Complete Python script source code.
            test_vectors: List of TestVector instances to execute.

        Returns:
            SandboxBatchResult summarizing individual test vector results.
        """
        results: list[TestExecutionResult] = []
        stderr_chunks: list[str] = []
        batch_start = time.perf_counter()
        restricted_env = self._build_restricted_env()

        with telemetry.trace_sandbox_execution(len(test_vectors)):
            with tempfile.TemporaryDirectory(prefix="pyforge_sandbox_") as sandbox_dir:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".py",
                    dir=sandbox_dir,
                    delete=False,
                    encoding="utf-8",
                ) as tmp_file:
                    tmp_file.write(code)
                    tmp_file.flush()
                    script_path = Path(tmp_file.name)

                try:
                    os.chmod(script_path, 0o700)
                    for vector in test_vectors:
                        res = self._run_single_vector(
                            script_path=script_path,
                            sandbox_dir=Path(sandbox_dir),
                            vector=vector,
                            env=restricted_env,
                        )
                        results.append(res)
                        if res.stderr:
                            stderr_chunks.append(
                                f"[{vector.name}] {res.stderr.strip()}"
                            )
                finally:
                    if script_path.exists():
                        try:
                            script_path.unlink()
                        except OSError:
                            pass

        total_duration_ms = (time.perf_counter() - batch_start) * 1000.0
        all_passed = bool(results) and all(r.passed for r in results)

        default_logger.info(
            layer="sandbox",
            intent="execute_test_vectors",
            outcome="passed" if all_passed else "failed",
            metadata={
                "total_vectors": len(results),
                "passed": sum(1 for r in results if r.passed),
                "failed": sum(1 for r in results if not r.passed),
                "total_duration_ms": round(total_duration_ms, 2),
            },
        )

        return SandboxBatchResult(
            all_passed=all_passed,
            results=results,
            total_duration_ms=total_duration_ms,
            combined_stderr="\n".join(stderr_chunks),
        )

    def _run_single_vector(
        self,
        *,
        script_path: Path,
        sandbox_dir: Path,
        vector: TestVector,
        env: dict[str, str],
    ) -> TestExecutionResult:
        """Execute a single TestVector against `script_path` with a strict timeout."""
        cmd = [self.python_executable, str(script_path), *vector.cli_args]
        start = time.perf_counter()

        try:
            completed = subprocess.run(
                cmd,
                input=vector.stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                cwd=str(sandbox_dir),
                check=False,
            )
            duration_ms = (time.perf_counter() - start) * 1000.0
            telemetry.sandbox_duration_histogram.record(
                duration_ms, {"vector": vector.name}
            )

            stdout = PIIScrubber.scrub(completed.stdout or "")
            stderr = PIIScrubber.scrub(completed.stderr or "")
            exit_code = int(completed.returncode)

            passed = True
            failure_reasons: list[str] = []

            if exit_code != vector.expected_exit_code:
                passed = False
                failure_reasons.append(
                    f"Expected exit code {vector.expected_exit_code}, got {exit_code}."
                )

            if vector.expected_stdout_substring is not None:
                if vector.expected_stdout_substring not in stdout:
                    passed = False
                    failure_reasons.append(
                        f"Expected stdout substring {vector.expected_stdout_substring!r} "
                        f"not found in output {stdout!r}."
                    )

            return TestExecutionResult(
                vector_name=vector.name,
                passed=passed,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                timed_out=False,
                failure_reason=" ".join(failure_reasons) if failure_reasons else None,
            )

        except subprocess.TimeoutExpired as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            telemetry.sandbox_duration_histogram.record(
                duration_ms, {"vector": vector.name, "timed_out": True}
            )
            raw_stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            raw_stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            timeout_msg = (
                f"Execution timed out after {self.timeout_seconds:.1f} seconds."
            )
            combined_err = f"{raw_stderr}\n{timeout_msg}".strip()

            return TestExecutionResult(
                vector_name=vector.name,
                passed=False,
                exit_code=-1,
                stdout=PIIScrubber.scrub(raw_stdout),
                stderr=PIIScrubber.scrub(combined_err),
                duration_ms=duration_ms,
                timed_out=True,
                failure_reason=timeout_msg,
            )

