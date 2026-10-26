"""Human-in-the-Loop (HITL) interactive approval gate before script installation."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from src.config import DEFAULT_CONFIG
from src.observability.structured_logging import default_logger
from src.tools.schemas import SandboxBatchResult, ScriptSpecification


class HITLDecision(str, Enum):
    """User decision outcomes at the Human-in-the-Loop approval gate."""

    APPROVE = "approve"
    EDIT = "edit"
    ABORT = "abort"


@dataclass(frozen=True)
class HITLOutcome:
    """Result of the Human-in-the-Loop interactive gate."""

    decision: HITLDecision
    installed_path: Path | None = None
    edit_feedback: str | None = None


class HumanInTheLoopGate:
    """Interactive gate displaying syntax, test results, and destination path before saving."""

    def __init__(
        self,
        bin_dir: Path = DEFAULT_CONFIG.bin_dir,
        console: Console | None = None,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.bin_dir = Path(bin_dir)
        self.console = console or Console()
        self.input_fn = input_fn or self.console.input

    def review_and_install(
        self,
        *,
        spec: ScriptSpecification,
        code: str,
        sandbox_result: SandboxBatchResult,
        auto_approve: bool = False,
    ) -> HITLOutcome:
        """Display script details and prompt user for `[Approve & Install / Edit Request / Abort]`.

        Args:
            spec: The ScriptSpecification for the utility.
            code: Verified Python script code.
            sandbox_result: Result of running all TestVectors in the sandbox.
            auto_approve: If True, automatically approve and install without interactive prompt.

        Returns:
            HITLOutcome with the user's decision, installed path, or edit request feedback.
        """
        destination = self.bin_dir / f"{spec.tool_name}.py"

        self._render_review_ui(
            spec=spec,
            code=code,
            sandbox_result=sandbox_result,
            destination=destination,
        )

        if auto_approve:
            installed = self._install_script(code=code, destination=destination)
            return HITLOutcome(
                decision=HITLDecision.APPROVE,
                installed_path=installed,
            )

        while True:
            raw_choice = self.input_fn(
                "\n[bold cyan]Action[/bold cyan] "
                "[[bold green]A[/bold green]pprove & Install / "
                "[bold yellow]E[/bold yellow]dit Request / "
                "[bold red]Q[/bold red] Abort]: "
            ).strip()

            normalized = raw_choice.lower()
            if normalized in {"a", "approve", "approve & install", "y", "yes"}:
                installed = self._install_script(code=code, destination=destination)
                default_logger.info(
                    layer="hitl",
                    intent="approve_and_install_script",
                    outcome="installed",
                    metadata={"tool_name": spec.tool_name, "path": str(installed)},
                )
                return HITLOutcome(
                    decision=HITLDecision.APPROVE,
                    installed_path=installed,
                )

            if normalized in {"e", "edit", "edit request"}:
                feedback = self.input_fn(
                    "[bold yellow]Enter requested changes or refinements:[/bold yellow] "
                ).strip()
                default_logger.info(
                    layer="hitl",
                    intent="request_script_edit",
                    outcome="edit_requested",
                    metadata={"tool_name": spec.tool_name, "feedback": feedback},
                )
                return HITLOutcome(
                    decision=HITLDecision.EDIT,
                    edit_feedback=feedback or "Please refine the implementation.",
                )

            if normalized in {"q", "abort", "n", "no", "cancel"}:
                default_logger.info(
                    layer="hitl",
                    intent="abort_script_installation",
                    outcome="aborted",
                    metadata={"tool_name": spec.tool_name},
                )
                return HITLOutcome(decision=HITLDecision.ABORT)

            self.console.print(
                "[yellow]Invalid selection. Choose 'A' (Approve & Install), "
                "'E' (Edit Request), or 'Q' (Abort).[/yellow]"
            )

    def _render_review_ui(
        self,
        *,
        spec: ScriptSpecification,
        code: str,
        sandbox_result: SandboxBatchResult,
        destination: Path,
    ) -> None:
        """Render syntax-highlighted script, test results table, and destination path."""
        syntax = Syntax(code, "python", theme="monokai", line_numbers=True)
        self.console.print(
            Panel(
                syntax,
                title=f"[bold green]Generated Script: {spec.tool_name}.py[/bold green]",
                subtitle=f"Proposed Path: {destination}",
                border_style="green",
            )
        )

        table = Table(
            title="Sandbox Test Vector Outcomes",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Test Vector", style="cyan")
        table.add_column("Status")
        table.add_column("Exit Code", justify="right")
        table.add_column("Duration (ms)", justify="right")
        table.add_column("Stdout Preview")

        for res in sandbox_result.results:
            status_badge = (
                "[bold green]PASS[/bold green]"
                if res.passed
                else "[bold red]FAIL[/bold red]"
            )
            preview = res.stdout.strip().replace("\n", " ")[:60]
            table.add_row(
                res.vector_name,
                status_badge,
                str(res.exit_code),
                f"{res.duration_ms:.1f}",
                preview,
            )

        self.console.print(table)
        self.console.print(
            f"[bold]Proposed Destination:[/bold] [underline]{destination}[/underline]"
        )

    def _install_script(self, *, code: str, destination: Path) -> Path:
        """Write the approved script to `destination` and apply `chmod +x` (`0o755`)."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = code
        if not payload.startswith("#!"):
            payload = "#!/usr/bin/env python3\n" + payload
        destination.write_text(payload, encoding="utf-8")

        current_mode = destination.stat().st_mode
        os.chmod(
            destination,
            current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
        )
        return destination

