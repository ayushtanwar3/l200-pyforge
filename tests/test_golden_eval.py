"""Automated Golden Dataset evaluation harness to statically measure agent regressions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from rich.console import Console

from src.agents.architect import ArchitectAgent
from src.agents.coder import CoderAgent
from src.agents.orchestrator import GuardrailPolicyPlugin, PyForgeOrchestrator
from src.config import PROJECT_ROOT
from src.storage.database import SQLiteDatabase
from src.tools.hitl_gate import HumanInTheLoopGate
from src.tools.sandbox_runner import SandboxTestRunner
from src.tools.schemas import ScriptSpecification

GOLDEN_DATASET_PATH = PROJECT_ROOT / "configs" / "golden_dataset.json"


def load_golden_dataset() -> dict:
    """Load the golden evaluation dataset from `configs/golden_dataset.json`."""
    raw = GOLDEN_DATASET_PATH.read_text(encoding="utf-8")
    return json.loads(raw)


def test_golden_dataset_regression_suite(tmp_path: Path) -> None:
    """Evaluate all golden dataset benchmark cases against PyForge's guardrails and orchestrator."""
    dataset = load_golden_dataset()
    assert "cases" in dataset and len(dataset["cases"]) >= 3

    runner = SandboxTestRunner(timeout_seconds=5.0)
    plugin = GuardrailPolicyPlugin(runner)

    for case in dataset["cases"]:
        spec = ScriptSpecification.model_validate(case["expected_spec"])
        code = case["reference_code"]

        # 1. Verify AST & Constitution Guardrail matches golden expectation
        security = plugin.evaluate_security(code)
        assert security.is_safe is case["expected_ast_safe"], (
            f"Golden case {case['id']} AST safety mismatch: {security.violations}"
        )

        # 2. If AST-safe, verify Sandbox execution matches golden expectation
        if security.is_safe:
            batch = plugin.evaluate_test_vectors(code, spec)
            assert batch.all_passed is case["expected_all_tests_pass"], (
                f"Golden case {case['id']} sandbox result mismatch: "
                f"{batch.format_failure_report()}"
            )

            # 3. Verify end-to-end Orchestrator workflow on valid golden cases
            mock_arch = MagicMock(spec=ArchitectAgent)
            mock_arch.generate_specification.return_value = spec
            mock_coder = MagicMock(spec=CoderAgent)
            mock_coder.generate_code.return_value = code

            orch = PyForgeOrchestrator(
                architect=mock_arch,
                coder=mock_coder,
                sandbox_runner=runner,
                hitl_gate=HumanInTheLoopGate(
                    bin_dir=tmp_path / "bin",
                    console=Console(quiet=True),
                    input_fn=lambda _p: "A",
                ),
                database=SQLiteDatabase(db_path=tmp_path / f"{case['id']}.db"),
            )
            result = orch.run(case["user_prompt"], auto_approve=True)
            assert result.status == "installed"
            assert result.installed_path is not None and result.installed_path.exists()

