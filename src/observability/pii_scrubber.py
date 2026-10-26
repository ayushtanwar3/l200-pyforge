"""PII and secret scrubber using compiled regular expressions."""

from __future__ import annotations

import re
from typing import Any, Pattern


class PIIScrubber:
    """Detects and redacts sensitive secrets, tokens, keys, and PII from text and payloads."""

    _PATTERNS: list[tuple[str, Pattern[str], str]] = [
        (
            "private_key",
            re.compile(
                r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
                r"[\s\S]*?"
                r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
                re.MULTILINE,
            ),
            "[REDACTED_PRIVATE_KEY]",
        ),
        (
            "private_key_header",
            re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
            "[REDACTED_PRIVATE_KEY]",
        ),
        (
            "openai_api_key",
            re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,}\b"),
            "[REDACTED_OPENAI_KEY]",
        ),
        (
            "google_api_key",
            re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
            "[REDACTED_GOOGLE_KEY]",
        ),
        (
            "github_token",
            re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
            "[REDACTED_GITHUB_TOKEN]",
        ),
        (
            "aws_access_key",
            re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
            "[REDACTED_AWS_KEY]",
        ),
        (
            "jwt_token",
            re.compile(
                r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
            ),
            "[REDACTED_JWT]",
        ),
        (
            "bearer_token",
            re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b"),
            "Bearer [REDACTED_BEARER_TOKEN]",
        ),
        (
            "password_assignment",
            re.compile(
                r"(?i)\b(password|passwd|pwd|secret|api_key|apikey|access_token)\b"
                r"(\s*[:=]\s*['\"]?)([^'\"\s,;}{]+)(['\"]?)"
            ),
            r"\1\2[REDACTED_SECRET]\4",
        ),
        (
            "email",
            re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
            "[REDACTED_EMAIL]",
        ),
        (
            "credit_card",
            re.compile(r"\b(?!0{4})(?:\d{4}[-\s]?){3}\d{4}\b"),
            "[REDACTED_CREDIT_CARD]",
        ),
        (
            "ipv4_address",
            re.compile(
                r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
            ),
            "[REDACTED_IP]",
        ),
    ]

    @classmethod
    def scrub(cls, text: str) -> str:
        """Scrub all recognized PII, API keys, tokens, and secrets from a string.

        Args:
            text: Input string potentially containing sensitive tokens or PII.

        Returns:
            Sanitized string with sensitive patterns replaced by redaction placeholders.
        """
        if not isinstance(text, str) or not text:
            return text

        scrubbed = text
        for _, pattern, replacement in cls._PATTERNS:
            scrubbed = pattern.sub(replacement, scrubbed)
        return scrubbed

    @classmethod
    def scrub_value(cls, value: Any) -> Any:
        """Recursively scrub strings inside dictionaries, lists, tuples, or arbitrary objects.

        Args:
            value: Arbitrary data structure to sanitize.

        Returns:
            Sanitized copy of the input data structure.
        """
        if isinstance(value, str):
            return cls.scrub(value)
        if isinstance(value, dict):
            return {
                cls.scrub(str(k)) if isinstance(k, str) else k: cls.scrub_value(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [cls.scrub_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls.scrub_value(item) for item in value)
        return value
