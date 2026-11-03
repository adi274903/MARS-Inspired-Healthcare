"""Control plane tests: admin auth, registry reads and hot-swapped profiles."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core.bus import MemoryBus
from core.config import get_settings
from services.common import make_app
from services.control_plane.main import build as build_control_plane
from tests.support import build_fixture_db

TOKEN = {"Authorization": "Bearer test-token"}


def make_settings(tmp_path: Path, monkeypatch):
    db = build_fixture_db(tmp_path)
    monkeypatch.setenv("MONOLITH", "1")
    monkeypatch.setenv("BUS", "memory")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("MODELS_YAML", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events" / "events.db"))
    return get_settings()


@pytest.mark.asyncio
async def test_admin_surface(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    bus = MemoryBus()
    await bus.start()
    spec = build_control_plane(settings, bus)
    assert spec.name == "control_plane"
    app = make_app("control_plane", settings, spec.router)
    for hook in spec.on_start:
        await hook()
    yaml_path = Path(settings.models_yaml)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as client:
        # auth
        no_token = await client.get("/api/admin/models")
        assert no_token.status_code == 401
        bad_token = await client.get("/api/admin/models", headers={"Authorization": "Bearer nope"})
        assert bad_token.status_code == 401

        models = await client.get("/api/admin/models", headers=TOKEN)
        assert models.status_code == 200
        body = models.json()
        assert body["yaml_path"] == str(yaml_path)
        assert set(body["profiles"]) >= {"chat", "judge", "embed"}
        assert body["profiles"]["chat"]["model"] == "google/gemma-4-26b-a4b-it"
        assert yaml_path.exists(), "ensure_seed must materialise the registry"

        # a valid change: persisted, returned and announced on the bus
        updated = await client.post(
            "/api/admin/profiles/chat",
            json={"model": "mock/tiny", "temperature": 0.7},
            headers=TOKEN,
        )
        assert updated.status_code == 200
        cfg = updated.json()
        assert cfg["model"] == "mock/tiny"
        assert cfg["temperature"] == 0.7
        assert cfg["provider"] == "openrouter"  # untouched keys survive

        changed = [e for e in await bus.history() if e.type == "model.changed"]
        assert len(changed) == 1
        assert changed[0].producer == "control_plane"
        assert changed[0].payload["profile"] == "chat"
        assert changed[0].payload["config"]["model"] == "mock/tiny"

        on_disk = yaml_path.read_text()
        assert "mock/tiny" in on_disk
        assert "profiles:" in on_disk

        # unknown keys / unknown profile names are rejected
        unknown_key = await client.post("/api/admin/profiles/chat", json={"nope": 1}, headers=TOKEN)
        assert unknown_key.status_code == 400
        unknown_profile = await client.post("/api/admin/profiles/router", json={"model": "x/y"}, headers=TOKEN)
        assert unknown_profile.status_code == 400
        assert len([e for e in await bus.history() if e.type == "model.changed"]) == 1

        health = await client.get("/api/admin/health")
        assert health.status_code == 200
        assert health.json()["registry_ok"] is True
        assert health.json()["profiles"] == ["chat", "embed", "judge"]

    await bus.stop()
