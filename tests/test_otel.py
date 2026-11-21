"""OpenTelemetry wiring: stage spans, chat histograms and HTTP auto-instrumentation (in-memory exporters only)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from core import otel  # noqa: E402


@pytest.fixture
def tel():
    reader = InMemoryMetricReader()
    spans = InMemorySpanExporter()
    assert otel.setup(exporter="none", extra_readers=[reader], extra_span_processors=[SimpleSpanProcessor(spans)])
    yield reader, spans
    otel.shutdown()


def _points(reader, name):
    data = reader.get_metrics_data()
    out = []
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    out.extend(metric.data.data_points)
    return out


def test_disabled_is_a_noop(monkeypatch):
    otel.shutdown()
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    assert otel.enabled() is False
    assert otel.setup() is False
    with otel.stage("vector_service", SimpleNamespace(correlation_id="c0", type="retrieval.requested")):
        pass
    otel.record_chat("chat", "ok", 12.0)
    otel.flush()


def test_record_chat_fills_the_latency_histograms(tel):
    reader, spans = tel
    otel.record_chat("stream", "ok", 1234.0, ttft_ms=800.0, retrieval_ms=300.0, correlation_id="c1")
    total = _points(reader, "mars.chat.duration_ms")
    assert len(total) == 1 and total[0].count == 1 and total[0].sum == pytest.approx(1234.0)
    assert dict(total[0].attributes) == {"route": "stream", "outcome": "ok"}
    assert list(total[0].explicit_bounds) == otel.BUCKETS_MS  # p95/p99 need buckets past 10 s
    assert _points(reader, "mars.chat.ttft_ms")[0].sum == pytest.approx(800.0)
    assert _points(reader, "mars.chat.retrieval_ms")[0].sum == pytest.approx(300.0)
    span = spans.get_finished_spans()[-1]
    assert span.name == "mars.chat" and span.attributes["mars.correlation_id"] == "c1"


def test_stage_records_a_span_and_a_histogram_and_marks_errors(tel):
    reader, spans = tel
    event = SimpleNamespace(correlation_id="c9", type="retrieval.requested")
    with otel.stage("vector_service", event):
        pass
    with pytest.raises(ValueError):
        with otel.stage("llm_service", event):
            raise ValueError("boom")
    by_stage = {dict(p.attributes)["stage"]: dict(p.attributes)["outcome"] for p in _points(reader, "mars.stage.duration_ms")}
    assert by_stage == {"vector_service": "ok", "llm_service": "error"}
    names = [s.name for s in spans.get_finished_spans()]
    assert "mars.stage.vector_service" in names and "mars.stage.llm_service" in names
    assert all(s.attributes["mars.correlation_id"] == "c9" for s in spans.get_finished_spans())


def test_http_requests_are_instrumented_but_health_checks_are_not():
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    reader = InMemoryMetricReader()
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    assert otel.setup(app, exporter="none", extra_readers=[reader])
    try:
        client = TestClient(app)
        assert client.get("/ping").status_code == 200
        assert client.get("/healthz").status_code == 200
        http = [p for name in ("http.server.duration", "http.server.request.duration") for p in _points(reader, name)]
        assert sum(p.count for p in http) == 1  # /ping only
    finally:
        otel.shutdown()


def test_request_path_never_flushes_metrics(tel, monkeypatch):
    """Cloud Monitoring rejects (and drops) a batch when a series is written twice within 5 s."""
    calls = []
    monkeypatch.setattr(otel._state.meter_provider, "force_flush", lambda timeout_millis=0: calls.append(1))
    for _ in range(5):
        otel.flush()
    assert calls == []
    otel.flush(force_metrics=True)  # shutdown only
    assert len(calls) == 1


async def test_flush_soon_does_not_block_the_event_loop(tel, monkeypatch):
    import threading

    ran = threading.Event()
    monkeypatch.setattr(otel, "flush", lambda **kw: ran.set())
    otel.flush_soon()
    assert ran.wait(2.0)
