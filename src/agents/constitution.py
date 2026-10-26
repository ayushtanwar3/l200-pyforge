"""Immutable agent constitution, core boundaries, and system rules for PyForge."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConstitution:
    """Immutable constitution governing all PyForge agents and generated scripts."""

    ALLOWED_STDLIB_MODULES: tuple[str, ...] = (
        "argparse",
        "sys",
        "pathlib",
        "json",
        "csv",
        "re",
        "os",
        "math",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "hashlib",
        "base64",
        "statistics",
        "textwrap",
        "io",
        "typing",
        "shutil",
        "tempfile",
        "time",
        "uuid",
    )

    FORBIDDEN_MODULES: tuple[str, ...] = (
        "socket",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "pty",
    )

    FORBIDDEN_BUILTINS: tuple[str, ...] = (
        "eval",
        "exec",
        "compile",
        "__import__",
        "globals",
        "locals",
    )

    CORE_RULES: tuple[str, ...] = (
        "1. Standalone Single-File Python: Code MUST be a single, self-contained Python 3 script.",
        "2. Standard Library Only: Use Python standard libraries only (e.g., argparse, sys, pathlib, json, csv, re). No external pip dependencies are allowed unless explicitly specified.",
        "3. Strict CLI Ergonomics: Always use `argparse` for command-line interface parsing, handle `--help`, invalid flags, EOF, and runtime exceptions gracefully with informative error messages and proper exit codes.",
        "4. No Dynamic Code Execution: Never use `eval()`, `exec()`, `compile()`, `__import__()`, `globals()`, or `locals()`.",
        "5. No Unauthorized Network Access: Never import or use `socket`, `urllib`, `requests`, `httpx`, `aiohttp`, or `ftplib`.",
        "6. No Arbitrary Shell Execution: Never use `os.system`, `pty`, or `subprocess` with `shell=True`.",
        "7. No Destructive Filesystem Wipes: Never emit harmful filesystem wipe commands such as `rm -rf /` or `shutil.rmtree('/')`.",
    )

    _DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"rm\s+-[rfRF]+\s+/(?:[\s'\"`;)]|$|\*)"),
        re.compile(r"shutil\.rmtree\(\s*['\"]\/['\"]\s*\)"),
    )

    @classmethod
    def render_prompt_block(cls) -> str:
        """Render the constitution into a system prompt section for LLM agents."""
        rules_text = "\n".join(cls.CORE_RULES)
        return (
            "=== PYFORGE CONSTITUTION (IMMUTABLE BOUNDARIES) ===\n"
            f"{rules_text}\n"
            "==================================================="
        )

    @classmethod
    def check_raw_violations(cls, text: str) -> list[str]:
        """Check raw text or source code for destructive filesystem commands."""
        violations: list[str] = []
        for pattern in cls._DESTRUCTIVE_PATTERNS:
            if pattern.search(text):
                violations.append(
                    f"Constitution violation: Destructive filesystem pattern matching '{pattern.pattern}'."
                )
        return violations


CONSTITUTION = AgentConstitution()

