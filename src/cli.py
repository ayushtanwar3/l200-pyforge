"""Rich-based CLI interface for PyForge Studio."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table

from src.agents.orchestrator import PyForgeOrchestrator
from src.config import DEFAULT_CONFIG
from src.observability.ast_guardrail import ASTSecurityVisitor
from src.storage.database import SQLiteDatabase
from src.tools.hitl_gate import HumanInTheLoopGate
from src.tools.sandbox_runner import SandboxTestRunner
from src.tools.schemas import ScriptSpecification, TestVector


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level `pyforge` CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pyforge",
        description="PyForge — Self-Healing Multi-Agent CLI Tool & Script Studio",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # pyforge new "<request>"
    new_parser = subparsers.add_parser(
        "new",
        help="Design, code, security-check, test, and self-heal a new Python CLI utility.",
    )
    new_parser.add_argument(
        "request",
        type=str,
        help="Natural language description of the CLI utility to build.",
    )
    new_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Automatically approve and install if all checks pass.",
    )

    # pyforge list
    subparsers.add_parser(
        "list",
        help="Display previously generated CLI utilities stored in SQLite.",
    )

    # pyforge test <script_name>
    test_parser = subparsers.add_parser(
        "test",
        help="Re-run static AST checks and stored test vectors against a script in ./bin/.",
    )
    test_parser.add_argument(
        "script_name",
        type=str,
        help="Name of the script (e.g., csv_stats or csv_stats.py).",
    )

    return parser


def cmd_new(
    request: str,
    *,
    auto_approve: bool = False,
    console: Console | None = None,
    orchestrator: PyForgeOrchestrator | None = None,
) -> int:
    """Execute `pyforge new "<request>"`."""
    ui = console or Console()
    ui.print(
        Panel.fit(
            f"[bold cyan]PyForge Studio[/bold cyan]\n[dim]Request:[/dim] {request}",
            border_style="cyan",
        )
    )

    orch = orchestrator or PyForgeOrchestrator(
        hitl_gate=HumanInTheLoopGate(console=ui)
    )

    spinner = Spinner("dots", text="Initializing PyForge workflow...")
    with Live(spinner, console=ui, refresh_per_second=12, transient=True) as live:
        def update_status(message: str) -> None:
            spinner.update(text=f"[bold cyan]{message}[/bold cyan]")
            live.refresh()

        try:
            # Pause live spinner when entering interactive HITL prompt
            orig_review = orch.hitl_gate.review_and_install

            def wrapped_review(**kwargs):  # type: ignore[no-untyped-def]
                live.stop()
                return orig_review(**kwargs)

            orch.hitl_gate.review_and_install = wrapped_review  # type: ignore[method-assign]
            result = orch.run(
                request,
                auto_approve=auto_approve,
                status_callback=update_status,
            )
        except Exception as exc:
            live.stop()
            ui.print(
                Panel(
                    f"[bold red]Workflow Error:[/bold red] {exc}",
                    title="PyForge Error",
                    border_style="red",
                )
            )
            return 1

    if result.status == "installed" and result.installed_path:
        ui.print(
            Panel(
                f"[bold green]Successfully installed![/bold green]\n"
                f"Executable Path: [underline]{result.installed_path}[/underline]\n"
                f"Self-Healing Iterations Used: {result.healing_attempts_used}",
                title="Installation Complete",
                border_style="green",
            )
        )
        return 0

    if result.status == "aborted":
        ui.print("[yellow]Installation aborted by user.[/yellow]")
        return 0

    ui.print(
        Panel(
            f"[bold red]Failed to verify script after {result.healing_attempts_used} "
            f"self-healing iterations.[/bold red]\n\n"
            f"{result.sandbox_result.format_failure_report()}",
            title="Verification Failed",
            border_style="red",
        )
    )
    return 1


def cmd_list(
    *,
    console: Console | None = None,
    database: SQLiteDatabase | None = None,
) -> int:
    """Execute `pyforge list`."""
    ui = console or Console()
    db = database or SQLiteDatabase()
    scripts = db.list_generated_scripts()

    if not scripts:
        ui.print("[dim]No generated utilities found in SQLite database.[/dim]")
        return 0

    table = Table(
        title="PyForge Generated CLI Utilities",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Name", style="bold green")
    table.add_column("Status")
    table.add_column("Created At", style="magenta")
    table.add_column("Session ID", style="dim")

    for row in scripts:
        status = row["status"]
        badge = (
            f"[green]{status}[/green]"
            if status == "installed"
            else f"[yellow]{status}[/yellow]"
            if status == "aborted"
            else f"[red]{status}[/red]"
        )
        table.add_row(
            str(row["id"]),
            f"{row['name']}.py",
            badge,
            row["created_at"],
            row["session_id"][:8] + "...",
        )

    ui.print(table)
    return 0


def cmd_test(
    script_name: str,
    *,
    console: Console | None = None,
    database: SQLiteDatabase | None = None,
    bin_dir: Path = DEFAULT_CONFIG.bin_dir,
) -> int:
    """Execute `pyforge test <script_name>`."""
    ui = console or Console()
    db = database or SQLiteDatabase()

    stem = script_name[:-3] if script_name.endswith(".py") else script_name
    script_path = Path(bin_dir) / f"{stem}.py"

    record = db.get_generated_script_by_name(stem)
    if not script_path.exists() and record is None:
        ui.print(
            f"[bold red]Error:[/bold red] Script '{stem}.py' not found in {bin_dir} or SQLite."
        )
        return 1

    code = (
        script_path.read_text(encoding="utf-8")
        if script_path.exists()
        else str(record["code"])  # type: ignore[index]
    )

    # 1. Static AST Security Check
    security = ASTSecurityVisitor.validate(code)
    if not security.is_safe:
        ui.print(
            Panel(
                "\n".join(security.violations),
                title="[bold red]AST Security Violations[/bold red]",
                border_style="red",
            )
        )
        return 1

    # 2. Load TestVectors from stored specification (or fallback `--help` smoke test)
    if record and record.get("spec_json"):
        spec = ScriptSpecification.model_validate_json(record["spec_json"])
        vectors = spec.test_vectors
    else:
        vectors = [
            TestVector(
                name="help_flag_smoke_test",
                cli_args=["--help"],
                stdin=None,
                expected_exit_code=0,
                expected_stdout_substring="usage",
            )
        ]

    runner = SandboxTestRunner()
    batch_result = runner.run_tests(code=code, test_vectors=vectors)

    table = Table(
        title=f"Test Results for {stem}.py",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Test Vector", style="cyan")
    table.add_column("Result")
    table.add_column("Exit Code", justify="right")
    table.add_column("Duration (ms)", justify="right")
    table.add_column("Details")

    for res in batch_result.results:
        status_str = (
            "[bold green]PASS[/bold green]"
            if res.passed
            else "[bold red]FAIL[/bold red]"
        )
        details = res.failure_reason or res.stdout.strip().replace("\n", " ")[:60]
        table.add_row(
            res.vector_name,
            status_str,
            str(res.exit_code),
            f"{res.duration_ms:.1f}",
            details,
        )

    ui.print(table)
    return 0 if batch_result.all_passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for `pyforge`."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "new":
        return cmd_new(args.request, auto_approve=bool(args.yes))
    if args.command == "list":
        return cmd_list()
    if args.command == "test":
        return cmd_test(args.script_name)
    return 1


if __name__ == "__main__":
    sys.exit(main())

