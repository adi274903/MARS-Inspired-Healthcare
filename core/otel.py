"""OpenTelemetry for MARS: request traces plus latency histograms for p50 / p95 / p99.

Off unless ``OTEL_ENABLED=1``; when off every call here is a cheap no-op and ``opentelemetry`` is never imported,
so the offline test suite and local dev need no extra packages (``requirements-otel.txt``).

Exporters (``OTEL_EXPORTER``): ``gcp`` (Cloud Trace + Cloud Monitoring, the Cloud Run default), ``otlp``
(``OTEL_EXPORTER_OTLP_ENDPOINT``), ``console``, or ``memory`` (tests).

What is measured
----------------
* every HTTP request (FastAPI auto-instrumentation): ``http.server.duration`` with latency-sized buckets;
* ``mars.chat.duration_ms``  end-to-end per question (route chat|stream, outcome ok|error|timeout|cancelled);
* ``mars.chat.ttft_ms``      streaming: request -> first answer token;
* ``mars.chat.retrieval_ms`` streaming: request -> retrieval finished (SQL / vector / graph fused);
* ``mars.stage.duration_ms`` per bus stage (sqlite_service, vector_service, graph_service, llm_service, ...).
Stage spans carry ``mars.correlation_id`` (the id threaded through MARS's event log), so a slow question can be
looked up in Cloud Trace by that attribute and in ``GET /api/events/{id}/timeline``.

Percentiles: Cloud Monitoring computes p50 / p95 / p99 from the histograms (dashboard in infra/terraform).
Cloud Run bills CPU only while a request is in flight, so ``flush()`` runs after each question.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any, Iterator

from core.telemetry import log

#: Latency-sized buckets (ms): chat answers run seconds, the OpenTelemetry default tops out at 10 s.
BUCKETS_MS = [25, 50, 100, 250, 500, 1000, 2000, 3000, 5000, 8000, 12000, 20000, 30000, 60000, 120000]

_state: "_Telemetry | None" = None
_last_metric_flush = float("-inf")
#: Cloud Monitoring rejects a series written more often than every 5 s (and drops the whole batch), so metric
#: flushes are rate limited well above that; the periodic reader (OTEL_METRIC_INTERVAL_MS) covers the rest.
METRIC_FLUSH_MIN_S = 20.0


def enabled() -> bool:
    return os.environ.get("OTEL_ENABLED", "").strip().lower() in {"1", "true", "yes"}


class _Telemetry:
    def __init__(self, tracer: Any, meter_provider: Any, tracer_provider: Any, readers: list[Any]) -> None:
        self.tracer = tracer
        self.tracer_provider = tracer_provider
        self.meter_provider = meter_provider
        self.readers = readers
        meter = meter_provider.get_meter("mars")
        self.chat = meter.create_histogram("mars.chat.duration_ms", unit="ms", description="End-to-end question latency")
        self.ttft = meter.create_histogram("mars.chat.ttft_ms", unit="ms", description="Request to first answer token")
        self.retrieval = meter.create_histogram("mars.chat.retrieval_ms", unit="ms", description="Request to retrieval finished")
        self.stage = meter.create_histogram("mars.stage.duration_ms", unit="ms", description="Per bus-stage handler time")


def _views() -> list[Any]:
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    ms = ExplicitBucketHistogramAggregation(BUCKETS_MS)
    seconds = ExplicitBucketHistogramAggregation([b / 1000 for b in BUCKETS_MS])
    return [
        View(instrument_name="mars.*", aggregation=ms),
        View(instrument_name="http.server.duration", aggregation=ms),
        View(instrument_name="http.server.request.duration", aggregation=seconds),
    ]


def setup(app: Any = None, *, exporter: str | None = None, extra_readers: list[Any] | None = None,
          extra_span_processors: list[Any] | None = None) -> bool:
    """Configure providers (and instrument ``app``). Returns False when disabled or when setup failed."""
    global _state
    if _state is not None:
        return True
    if not enabled() and exporter is None:
        return False
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        kind = (exporter or os.environ.get("OTEL_EXPORTER", "gcp")).strip().lower()
        resource = Resource.create({
            "service.name": os.environ.get("K_SERVICE", "mars-healthcare"),
            "service.version": os.environ.get("K_REVISION", "local"),
            "service.instance.id": os.environ.get("K_REVISION", "local") + "-" + str(os.getpid()),
        })
        readers: list[Any] = list(extra_readers or [])
        span_processors: list[Any] = list(extra_span_processors or [])
        interval = int(os.environ.get("OTEL_METRIC_INTERVAL_MS", "60000"))

        if kind == "gcp":
            from opentelemetry.exporter.cloud_monitoring import CloudMonitoringMetricsExporter
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            readers.append(PeriodicExportingMetricReader(CloudMonitoringMetricsExporter(add_unique_identifier=True),
                                                         export_interval_millis=interval))
            span_processors.append(BatchSpanProcessor(CloudTraceSpanExporter(), schedule_delay_millis=1000))
        elif kind == "otlp":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=interval))
            span_processors.append(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
        elif kind == "console":
            from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

            readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=interval))
            span_processors.append(SimpleSpanProcessor(ConsoleSpanExporter()))

        tracer_provider = TracerProvider(resource=resource)
        for processor in span_processors:
            tracer_provider.add_span_processor(processor)
        meter_provider = MeterProvider(resource=resource, metric_readers=readers, views=_views())
        _state = _Telemetry(tracer_provider.get_tracer("mars"), meter_provider, tracer_provider, readers)

        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(
                app, tracer_provider=tracer_provider, meter_provider=meter_provider,
                excluded_urls="healthz,^https?://[^/]+/?$",
            )
        log("otel", "enabled", exporter=kind)
        return True
    except Exception as exc:  # telemetry must never take the service down
        log("otel", "setup_failed", error=f"{type(exc).__name__}: {exc}")
        _state = None
        return False


def shutdown() -> None:
    """Drop the configured providers (tests)."""
    global _state
    if _state is not None:
        with contextlib.suppress(Exception):
            _state.meter_provider.shutdown()
            _state.tracer_provider.shutdown()
    _state = None


def flush(*, force_metrics: bool = False) -> None:
    """Push buffered spans now. Metrics are NOT flushed on the request path: the periodic reader exports them every
    OTEL_METRIC_INTERVAL_MS, and a second write to the same series within 5 s makes Cloud Monitoring reject the batch.
    ``force_metrics`` is for shutdown only (histograms are cumulative, so the next periodic export catches up)."""
    global _last_metric_flush
    if _state is None:
        return
    with contextlib.suppress(Exception):
        _state.tracer_provider.force_flush(timeout_millis=3000)
    if force_metrics:
        _last_metric_flush = time.monotonic()
        with contextlib.suppress(Exception):
            _state.meter_provider.force_flush(timeout_millis=3000)


def flush_soon() -> None:
    """flush() on a worker thread: the export does network I/O and must never block the event loop."""
    if _state is None:
        return
    try:
        asyncio.get_running_loop().run_in_executor(None, flush)
    except RuntimeError:  # no running loop (sync caller)
        flush()


@contextlib.contextmanager
def stage(name: str, event: Any = None) -> Iterator[None]:
    """Time one bus-stage handler: a span plus the ``mars.stage.duration_ms`` histogram (outcome ok|error)."""
    if _state is None:
        yield
        return
    cid = getattr(event, "correlation_id", None)
    etype = getattr(event, "type", None)
    started = time.perf_counter()
    outcome = "ok"
    with _state.tracer.start_as_current_span(f"mars.stage.{name}") as span:
        span.set_attribute("mars.stage", name)
        if cid:
            span.set_attribute("mars.correlation_id", str(cid))
        if etype:
            span.set_attribute("mars.event_type", str(etype))
        try:
            yield
        except BaseException:
            outcome = "error"
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            span.set_attribute("mars.duration_ms", round(elapsed_ms, 1))
            _state.stage.record(elapsed_ms, {"stage": name, "outcome": outcome})


def record_chat(route: str, outcome: str, total_ms: float, *, ttft_ms: float | None = None,
                retrieval_ms: float | None = None, correlation_id: str | None = None) -> None:
    """Record one finished question. ``route`` is chat|stream; ``outcome`` ok|error|timeout|cancelled."""
    if _state is None:
        return
    attrs = {"route": route, "outcome": outcome}
    _state.chat.record(total_ms, attrs)
    if ttft_ms is not None:
        _state.ttft.record(ttft_ms, attrs)
    if retrieval_ms is not None:
        _state.retrieval.record(retrieval_ms, attrs)
    with _state.tracer.start_as_current_span("mars.chat") as span:  # a point-in-time span carrying the numbers
        span.set_attribute("mars.route", route)
        span.set_attribute("mars.outcome", outcome)
        span.set_attribute("mars.total_ms", round(total_ms, 1))
        if ttft_ms is not None:
            span.set_attribute("mars.ttft_ms", round(ttft_ms, 1))
        if retrieval_ms is not None:
            span.set_attribute("mars.retrieval_ms", round(retrieval_ms, 1))
        if correlation_id:
            span.set_attribute("mars.correlation_id", str(correlation_id))
