"""PyForgeOrchestrator coordinating the Architect, Coder, AST Guardrail, Sandbox, and HITL Gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.agents.architect import ArchitectAgent
from src.agents.coder import CoderAgent
from src.agents.constitution import CONSTITUTION
from src.config import DEFAULT_CONFIG
from src.observability.ast_guardrail import (
    ASTSecurityVisitor,
    SecurityValidationResult,
)
from src.observability.pii_scrubber import PIIScrubber
from src.observability.structured_logging import default_logger
from src.observability.telemetry import telemetry
from src.storage.database import SQLiteDatabase
from src.storage.memory import SlidingWindowMemory
from src.tools.hitl_gate import HITLDecision, HITLOutcome, HumanInTheLoopGate
from src.tools.sandbox_runner import SandboxTestRunner
from src.tools.schemas import SandboxBatchResult, ScriptSpecification


@dataclass
class OrchestrationResult:
    """Comprehensive outcome of a PyForge end-to-end orchestration workflow."""

    session_id: str
    spec: ScriptSpecification
    code: str
    security_result: SecurityValidationResult
    sandbox_result: SandboxBatchResult
    healing_attempts_used: int
    status: str
    installed_path: Path | None = None
    history: list[str] = field(default_factory=list)


class PyForgeOrchestrator:
    """Coordinates specification design, coding, static AST safety, sandbox testing, and self-healing."""

    def __init__(
        self,
        *,
        architect: ArchitectAgent | None = None,
        coder: CoderAgent | None = None,
        sandbox_runner: SandboxTestRunner | None = None,
        hitl_gate: HumanInTheLoopGate | None = None,
        database: SQLiteDatabase | None = None,
        memory: SlidingWindowMemory | None = None,
        max_healing_iterations: int = DEFAULT_CONFIG.max_healing_iterations,
    ) -> None:
        self.architect = architect or ArchitectAgent()
        self.coder = coder or CoderAgent()
        self.sandbox_runner = sandbox_runner or SandboxTestRunner()
        self.hitl_gate = hitl_gate or HumanInTheLoopGate()
        self.database = database or SQLiteDatabase()
        self.memory = memory or SlidingWindowMemory()
        self.max_healing_iterations = max_healing_iterations

    def run(
        self,
        user_request: str,
        *,
        auto_approve: bool = False,
        status_callback: Callable[[str], None] | None = None,
    ) -> OrchestrationResult:
        """Execute the full PyForge workflow with up to `max_healing_iterations` self-healing retries.

        Workflow Steps:
        1. Scrub input prompt through `PIIScrubber`.
        2. Query `ArchitectAgent` to generate `ScriptSpecification`.
        3. Query `CoderAgent` to generate Python code.
        4. Run `ASTSecurityVisitor`. If violations found, re-prompt Coder with violation report.
        5. Run `SandboxTestRunner` against test vectors.
        6. Self-Healing Loop: Up to 3 automated healing iterations on AST or sandbox failure.
        7. Invoke `HumanInTheLoopGate` when all tests pass and security is clear.
        8. Persist script and session state in SQLite.
        """
        notify = status_callback or (lambda _msg: None)
        history: list[str] = []

        # Step 1: Scrub user input through PIIScrubber
        scrubbed_request = PIIScrubber.scrub(user_request)
        session_id = self.database.create_session(title=scrubbed_request[:100])
        self.database.add_message(session_id, "user", scrubbed_request)
        self.memory.add_message("user", scrubbed_request)

        default_logger.info(
            layer="orchestrator",
            intent="start_workflow",
            outcome="session_created",
            metadata={"session_id": session_id},
        )

        # Step 2: Query ArchitectAgent to generate ScriptSpecification
        notify("Architect (gemini-2.5-pro) designing CLI specification & test vectors...")
        context_str = self.memory.get_context_string()
        spec = self.architect.generate_specification(
            user_request=scrubbed_request,
            conversation_context=context_str,
        )
        history.append(f"Specification generated for '{spec.tool_name}'.")
        self.memory.add_message("assistant", spec.model_dump_json())

        # Step 3-6: Coding, AST Security Check, Sandbox Execution, and Self-Healing Loop
        previous_code: str | None = None
        error_feedback: str | None = None
        code = ""
        security_result = SecurityValidationResult(is_safe=False, violations=[])
        sandbox_result = SandboxBatchResult(all_passed=False, results=[])
        healing_attempts_used = 0

        while True:
            for attempt in range(self.max_healing_iterations + 1):
                if attempt > 0:
                    healing_attempts_used += 1
                    telemetry.healing_iterations_counter.add(
                        1, {"tool_name": spec.tool_name, "attempt": attempt}
                    )
                    notify(
                        f"Self-healing iteration {attempt}/{self.max_healing_iterations} "
                        f"with Coder (gemini-2.5-flash)..."
                    )
                else:
                    notify(
                        f"Coder (gemini-2.5-flash) generating '{spec.tool_name}.py'..."
                    )

                code = self.coder.generate_code(
                    spec,
                    previous_code=previous_code,
                    error_feedback=error_feedback,
                )

                # Step 4: Run ASTSecurityVisitor + Constitution check
                notify("Running static AST security guardrail...")
                with telemetry.trace_ast_check():
                    ast_check = ASTSecurityVisitor.validate(code)
                    raw_violations = CONSTITUTION.check_raw_violations(code)
                    all_violations = [*ast_check.violations, *raw_violations]
                    security_result = SecurityValidationResult(
                        is_safe=len(all_violations) == 0,
                        violations=all_violations,
                    )

                if not security_result.is_safe:
                    telemetry.ast_violations_counter.add(
                        len(security_result.violations),
                        {"tool_name": spec.tool_name},
                    )
                    violation_report = "\n".join(
                        f"- {v}" for v in security_result.violations
                    )
                    history.append(
                        f"Attempt {attempt}: AST security violation(s) detected."
                    )
                    default_logger.warning(
                        layer="orchestrator",
                        intent="ast_security_validation",
                        outcome="violations_detected",
                        metadata={
                            "attempt": attempt,
                            "violations": security_result.violations,
                        },
                    )
                    previous_code = code
                    error_feedback = (
                        "Static AST Security Violations Detected (Forbidden by Constitution):\n"
                        f"{violation_report}\n"
                        "Rewrite the script without any prohibited imports or functions."
                    )
                    sandbox_result = SandboxBatchResult(
                        all_passed=False,
                        results=[],
                        combined_stderr=error_feedback,
                    )
                    continue

                # Step 5: Run SandboxTestRunner against test vectors
                notify(
                    f"Executing {len(spec.test_vectors)} test vector(s) in isolated sandbox..."
                )
                sandbox_result = self.sandbox_runner.run_tests(
                    code=code,
                    test_vectors=spec.test_vectors,
                )

                if sandbox_result.all_passed:
                    history.append(
                        f"Attempt {attempt}: All {len(spec.test_vectors)} sandbox tests passed."
                    )
                    break

                # Step 6: Prepare failure trace & stderr for next self-healing iteration
                failure_report = sandbox_result.format_failure_report()
                history.append(
                    f"Attempt {attempt}: {sandbox_result.failed_count} test vector(s) failed."
                )
                default_logger.warning(
                    layer="orchestrator",
                    intent="sandbox_test_verification",
                    outcome="failed",
                    metadata={
                        "attempt": attempt,
                        "failed_count": sandbox_result.failed_count,
                        "stderr": sandbox_result.combined_stderr,
                    },
                )
                previous_code = code
                error_feedback = (
                    f"Sandbox Test Vectors Failed:\n{failure_report}\n\n"
                    f"Captured Stderr:\n{sandbox_result.combined_stderr}"
                )

            # Check if verification succeeded after the loop
            if not (security_result.is_safe and sandbox_result.all_passed):
                status = "failed"
                self.database.save_generated_script(
                    session_id=session_id,
                    name=spec.tool_name,
                    spec_json=spec.model_dump_json(),
                    code=code,
                    status=status,
                )
                default_logger.error(
                    layer="orchestrator",
                    intent="complete_workflow",
                    outcome="failed_after_max_healing",
                    metadata={
                        "session_id": session_id,
                        "tool_name": spec.tool_name,
                        "healing_attempts_used": healing_attempts_used,
                    },
                )
                return OrchestrationResult(
                    session_id=session_id,
                    spec=spec,
                    code=code,
                    security_result=security_result,
                    sandbox_result=sandbox_result,
                    healing_attempts_used=healing_attempts_used,
                    status=status,
                    installed_path=None,
                    history=history,
                )

            # Step 7: Human-in-the-Loop Gate
            hitl_outcome: HITLOutcome = self.hitl_gate.review_and_install(
                spec=spec,
                code=code,
                sandbox_result=sandbox_result,
                auto_approve=auto_approve,
            )

            if hitl_outcome.decision == HITLDecision.EDIT:
                # Re-enter Coder generation with user's edit request
                history.append(
                    f"User requested edit: {hitl_outcome.edit_feedback}"
                )
                self.database.add_message(
                    session_id, "user", f"Edit request: {hitl_outcome.edit_feedback}"
                )
                self.memory.add_message(
                    "user", f"Edit request: {hitl_outcome.edit_feedback}"
                )
                previous_code = code
                error_feedback = (
                    f"User Requested Refinement:\n{hitl_outcome.edit_feedback}\n"
                    "Ensure all existing test vectors still pass."
                )
                continue

            if hitl_outcome.decision == HITLDecision.APPROVE:
                status = "installed"
                self.database.save_generated_script(
                    session_id=session_id,
                    name=spec.tool_name,
                    spec_json=spec.model_dump_json(),
                    code=code,
                    status=status,
                )
                self.database.add_message(
                    session_id,
                    "assistant",
                    f"Installed {spec.tool_name}.py to {hitl_outcome.installed_path}",
                )
                return OrchestrationResult(
                    session_id=session_id,
                    spec=spec,
                    code=code,
                    security_result=security_result,
                    sandbox_result=sandbox_result,
                    healing_attempts_used=healing_attempts_used,
                    status=status,
                    installed_path=hitl_outcome.installed_path,
                    history=history,
                )

            # User selected Abort
            status = "aborted"
            self.database.save_generated_script(
                session_id=session_id,
                name=spec.tool_name,
                spec_json=spec.model_dump_json(),
                code=code,
                status=status,
            )
            return OrchestrationResult(
                session_id=session_id,
                spec=spec,
                code=code,
                security_result=security_result,
                sandbox_result=sandbox_result,
                healing_attempts_used=healing_attempts_used,
                status=status,
                installed_path=None,
                history=history,
            )

