"""Structured Intent vs. Outcome JSON logger with automatic PII scrubbing."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any

from opentelemetry import trace

from src.observability.pii_scrubber import PIIScrubber


class StructuredJSONFormatter(logging.Formatter):
    """Formats Python log records into strict Intent vs. Outcome JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a LogRecord into a PII-scrubbed JSON string."""
        span = trace.get_current_span()
        span_context = span.get_span_context() if span else None

        default_trace_id = (
            f"{span_context.trace_id:032x}"
            if span_context and span_context.is_valid
            else "0" * 32
        )
        default_span_id = (
            f"{span_context.span_id:016x}"
            if span_context and span_context.is_valid
            else "0" * 16
        )

        trace_id = getattr(record, "trace_id", None) or default_trace_id
        span_id = getattr(record, "span_id", None) or default_span_id
        layer = getattr(record, "layer", "system")
        intent = getattr(record, "intent", record.getMessage())
        outcome = getattr(record, "outcome", "logged")
        metadata = getattr(record, "metadata", {}) or {}

        if record.exc_info:
            metadata = dict(metadata)
            metadata["exception"] = self.formatException(record.exc_info)

        payload: dict[str, Any] = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "trace_id": PIIScrubber.scrub(str(trace_id)),
            "span_id": PIIScrubber.scrub(str(span_id)),
            "layer": PIIScrubber.scrub(str(layer)),
            "intent": PIIScrubber.scrub(str(intent)),
            "outcome": PIIScrubber.scrub(str(outcome)),
            "metadata": PIIScrubber.scrub_value(metadata),
        }
        return json.dumps(payload, ensure_ascii=False)


class StructuredLogger:
    """High-level wrapper for emitting structured Intent vs. Outcome logs."""

    def __init__(self, name: str = "pyforge", level: int = logging.INFO) -> None:
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.logger.propagate = False

        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(StructuredJSONFormatter())
            self.logger.addHandler(handler)

    def log_event(
        self,
        *,
        level: int = logging.INFO,
        layer: str,
        intent: str,
        outcome: str,
        metadata: dict[str, Any] | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> None:
        """Emit a structured JSON log record with PII scrubbing."""
        extra: dict[str, Any] = {
            "layer": layer,
            "intent": intent,
            "outcome": outcome,
            "metadata": metadata or {},
        }
        if trace_id is not None:
            extra["trace_id"] = trace_id
        if span_id is not None:
            extra["span_id"] = span_id

        self.logger.log(level, intent, extra=extra)

    def info(
        self,
        *,
        layer: str,
        intent: str,
        outcome: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Emit an INFO-level structured log event."""
        self.log_event(
            level=logging.INFO,
            layer=layer,
            intent=intent,
            outcome=outcome,
            metadata=metadata,
        )

    def warning(
        self,
        *,
        layer: str,
        intent: str,
        outcome: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Emit a WARNING-level structured log event."""
        self.log_event(
            level=logging.WARNING,
            layer=layer,
            intent=intent,
            outcome=outcome,
            metadata=metadata,
        )

    def error(
        self,
        *,
        layer: str,
        intent: str,
        outcome: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Emit an ERROR-level structured log event."""
        self.log_event(
            level=logging.ERROR,
            layer=layer,
            intent=intent,
            outcome=outcome,
            metadata=metadata,
        )


default_logger = StructuredLogger()

