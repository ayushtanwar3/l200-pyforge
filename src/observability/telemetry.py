"""OpenTelemetry tracer and metrics setup for PyForge."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    InMemoryMetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, Status, StatusCode

from src.config import DEFAULT_CONFIG
from src.observability.pii_scrubber import PIIScrubber


class TelemetryManager:
    """Manages OpenTelemetry tracing and metrics across PyForge layers."""

    def __init__(
        self,
        service_name: str = DEFAULT_CONFIG.service_name,
        enable_console_export: bool = DEFAULT_CONFIG.enable_console_telemetry,
    ) -> None:
        resource = Resource.create({"service.name": service_name})

        self.span_exporter = InMemorySpanExporter()
        self.tracer_provider = TracerProvider(resource=resource)
        self.tracer_provider.add_span_processor(
            SimpleSpanProcessor(self.span_exporter)
        )
        if enable_console_export:
            self.tracer_provider.add_span_processor(
                BatchSpanProcessor(ConsoleSpanExporter())
            )

        self.metric_reader = InMemoryMetricReader()
        readers = [self.metric_reader]
        if enable_console_export:
            readers.append(
                PeriodicExportingMetricReader(ConsoleMetricExporter())
            )
        self.meter_provider = MeterProvider(
            resource=resource, metric_readers=readers
        )

        # Register global providers if none configured yet
        try:
            trace.set_tracer_provider(self.tracer_provider)
        except Exception:
            pass
        try:
            metrics.set_meter_provider(self.meter_provider)
        except Exception:
            pass

        self.tracer = self.tracer_provider.get_tracer("pyforge.tracer")
        self.meter = self.meter_provider.get_meter("pyforge.meter")

        # Metrics instruments
        self.agent_calls_counter = self.meter.create_counter(
            name="pyforge.agent.calls",
            description="Number of agent invocations by role",
            unit="1",
        )
        self.ast_violations_counter = self.meter.create_counter(
            name="pyforge.ast.violations",
            description="Number of static AST security violations detected",
            unit="1",
        )
        self.sandbox_runs_counter = self.meter.create_counter(
            name="pyforge.sandbox.runs",
            description="Number of sandbox test vector executions",
            unit="1",
        )
        self.healing_iterations_counter = self.meter.create_counter(
            name="pyforge.healing.iterations",
            description="Number of self-healing iterations executed",
            unit="1",
        )
        self.sandbox_duration_histogram = self.meter.create_histogram(
            name="pyforge.sandbox.duration_ms",
            description="Execution duration of sandbox test runs in milliseconds",
            unit="ms",
        )

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Start an OpenTelemetry span with sanitized attributes and error capture."""
        with self.tracer.start_as_current_span(name) as active_span:
            if attributes:
                for key, value in attributes.items():
                    safe_val = PIIScrubber.scrub_value(value)
                    if isinstance(safe_val, (bool, int, float, str)):
                        active_span.set_attribute(key, safe_val)
                    else:
                        active_span.set_attribute(key, str(safe_val))
            try:
                yield active_span
                active_span.set_status(Status(StatusCode.OK))
            except Exception as exc:
                active_span.record_exception(exc)
                active_span.set_status(
                    Status(StatusCode.ERROR, PIIScrubber.scrub(str(exc)))
                )
                raise

    @contextmanager
    def trace_agent_routing(self, agent_name: str, model: str) -> Iterator[Span]:
        """Create a span around an agent routing invocation."""
        self.agent_calls_counter.add(1, {"agent": agent_name, "model": model})
        with self.span(
            f"agent.{agent_name}",
            attributes={"agent.name": agent_name, "agent.model": model},
        ) as active_span:
            yield active_span

    @contextmanager
    def trace_ast_check(self) -> Iterator[Span]:
        """Create a span around static AST security verification."""
        with self.span("security.ast_guardrail") as active_span:
            yield active_span

    @contextmanager
    def trace_sandbox_execution(self, vector_count: int) -> Iterator[Span]:
        """Create a span around sandbox subprocess test execution."""
        self.sandbox_runs_counter.add(vector_count)
        with self.span(
            "sandbox.execution",
            attributes={"sandbox.vector_count": vector_count},
        ) as active_span:
            yield active_span


telemetry = TelemetryManager()
tracer = telemetry.tracer
meter = telemetry.meter

