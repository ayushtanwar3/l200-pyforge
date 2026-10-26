"""Unit tests for Layer 1: AST Security Guardrail, PII Scrubber, and Structured Logging."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from src.observability.ast_guardrail import (
    ASTSecurityVisitor,
    validate_code_security,
)
from src.observability.pii_scrubber import PIIScrubber
from src.observability.structured_logging import StructuredJSONFormatter


def test_valid_stdlib_cli_script_is_safe() -> None:
    """A clean standard-library argparse script should pass AST validation."""
    code = """#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser(description="Sample tool")
    parser.add_argument("--name", default="world")
    args = parser.parse_args()
    print(json.dumps({"greeting": f"Hello, {args.name}!"}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""
    result = validate_code_security(code)
    assert result.is_safe is True
    assert result.violations == []


@pytest.mark.parametrize(
    "forbidden_call",
    [
        "eval('1 + 1')",
        "exec('print(1)')",
        "compile('x = 1', '<string>', 'exec')",
        "__import__('os')",
        "globals()",
        "locals()",
    ],
)
def test_prohibited_builtins_are_blocked(forbidden_call: str) -> None:
    """All dynamic execution and scope inspection builtins must be rejected."""
    code = f"import sys\nx = {forbidden_call}\n"
    result = ASTSecurityVisitor.validate(code)
    assert result.is_safe is False
    assert any("Prohibited builtin call" in v for v in result.violations)


@pytest.mark.parametrize(
    "import_stmt",
    [
        "import socket",
        "import urllib.request",
        "from urllib import parse",
        "import requests",
        "import httpx",
        "import aiohttp",
        "from ftplib import FTP",
    ],
)
def test_prohibited_network_modules_are_blocked(import_stmt: str) -> None:
    """Unauthorized network modules must be flagged by ASTSecurityVisitor."""
    code = f"{import_stmt}\nprint('hello')\n"
    result = ASTSecurityVisitor.validate(code)
    assert result.is_safe is False
    assert any("Prohibited network module" in v for v in result.violations)


@pytest.mark.parametrize(
    "unsafe_snippet",
    [
        "import os\nos.system('ls -la')",
        "from os import system\nsystem('whoami')",
        "import pty\npty.spawn('/bin/sh')",
        "import subprocess\nsubprocess.Popen('ls', shell=True)",
        "import subprocess\nsubprocess.run(['ls'], shell=True)",
        "import shutil\nshutil.rmtree('/')",
    ],
)
def test_prohibited_shells_and_destructive_calls_blocked(unsafe_snippet: str) -> None:
    """Shell execution (`os.system`, `pty`, `shell=True`) and `shutil.rmtree('/')` must be blocked."""
    result = ASTSecurityVisitor.validate(unsafe_snippet)
    assert result.is_safe is False
    assert len(result.violations) >= 1


def test_subprocess_without_shell_true_is_allowed() -> None:
    """Safe subprocess calls with shell=False (or omitted) are permitted."""
    code = """
import subprocess
subprocess.run(["echo", "hello"], shell=False, check=True)
"""
    result = ASTSecurityVisitor.validate(code)
    assert result.is_safe is True


def test_syntax_error_fails_validation() -> None:
    """Invalid Python syntax must return is_safe=False with a SyntaxError violation."""
    result = ASTSecurityVisitor.validate("def broken(:\n    pass")
    assert result.is_safe is False
    assert any("SyntaxError" in v for v in result.violations)


def test_pii_scrubber_redacts_secrets_and_pii() -> None:
    """PIIScrubber must redact API keys, bearer tokens, emails, IPs, and private keys."""
    sample = (
        "OpenAI key sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456 "
        "Google AIzaSyDaGmWKa4JsXZ-0123456789abcdefghijk "
        "GitHub ghp_1234567890abcdefghijklmnopqrstuv "
        "AWS AKIAIOSFODNN7EXAMPLE "
        "Authorization: Bearer my-secret-token-value-12345 "
        "Contact user@example.com from 192.168.1.42 "
        "password='supersecretpassword'"
    )
    scrubbed = PIIScrubber.scrub(sample)
    assert "sk-proj-" not in scrubbed
    assert "AIzaSy" not in scrubbed
    assert "ghp_" not in scrubbed
    assert "AKIAIOSFODNN7EXAMPLE" not in scrubbed
    assert "my-secret-token-value-12345" not in scrubbed
    assert "user@example.com" not in scrubbed
    assert "192.168.1.42" not in scrubbed
    assert "supersecretpassword" not in scrubbed
    assert "[REDACTED_OPENAI_KEY]" in scrubbed
    assert "[REDACTED_GOOGLE_KEY]" in scrubbed
    assert "[REDACTED_GITHUB_TOKEN]" in scrubbed
    assert "[REDACTED_AWS_KEY]" in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_IP]" in scrubbed


def test_structured_json_formatter_outputs_required_fields() -> None:
    """StructuredJSONFormatter must output all required fields and scrub PII."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredJSONFormatter())

    test_logger = logging.getLogger("pyforge.test.json")
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    test_logger.info(
        "user prompt with sk-proj-1234567890abcdefghijklmnop",
        extra={
            "layer": "observability",
            "intent": "scrub_key sk-proj-1234567890abcdefghijklmnop",
            "outcome": "redacted",
            "metadata": {"email": "alice@example.com"},
        },
    )

    output = stream.getvalue().strip()
    record = json.loads(output)

    for required_key in (
        "timestamp",
        "level",
        "trace_id",
        "span_id",
        "layer",
        "intent",
        "outcome",
        "metadata",
    ):
        assert required_key in record

    assert "sk-proj-" not in record["intent"]
    assert record["metadata"]["email"] == "[REDACTED_EMAIL]"

