"""Control plane: owns the model registry an operator can hot-swap.

Profiles live in ``settings.models_yaml`` so a change reaches every process on
the next read; a ``model.changed`` event tells the LLM service to drop its
cached provider. Nothing here touches the network.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

from core import registry
from core.bus import Bus
from core.envelope import new_event
from core.telemetry import log
from services.common import ServiceSpec

KNOWN_PROFILES = ("chat", "judge", "embed")
ALLOWED_KEYS = ("provider", "model", "temperature", "max_tokens", "fallbacks", "dim")


def _check_token(authorization: str | None, settings) -> None:
    expected = f"Bearer {settings.admin_token}"
    if not authorization or authorization.strip() != expected:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


def build(settings, bus: Bus) -> ServiceSpec:
    yaml_path = Path(settings.models_yaml)
    router = APIRouter()

    async def on_start() -> None:
        registry.ensure_seed(yaml_path)

    @router.get("/api/admin/models")
    async def models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _check_token(authorization, settings)
        return {"profiles": registry.load_profiles(yaml_path), "yaml_path": str(yaml_path)}

    @router.post("/api/admin/profiles/{name}")
    async def update_profile(
        name: str,
        payload: dict[str, Any] = Body(default_factory=dict),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_token(authorization, settings)
        if name not in KNOWN_PROFILES:
            raise HTTPException(400, f"unknown profile {name!r}; expected one of {list(KNOWN_PROFILES)}")
        unknown = sorted(set(payload) - set(ALLOWED_KEYS))
        if unknown:
            raise HTTPException(400, f"unknown keys: {unknown}; allowed: {list(ALLOWED_KEYS)}")
        if not payload:
            raise HTTPException(400, "empty profile update")
        cfg: dict[str, Any] = {k: v for k, v in payload.items() if v is not None}
        updated = registry.set_profile(yaml_path, name, cfg)
        await bus.publish(new_event("model.changed", {"profile": name, "config": updated}, producer="control_plane"))
        log("control_plane", "model.changed", profile=name, keys=sorted(cfg))
        return updated

    @router.get("/api/admin/health")
    async def health() -> dict[str, Any]:
        try:
            names = sorted(registry.load_profiles(yaml_path))
            ok = True
        except Exception as exc:  # a broken YAML must not 500 a probe
            log("control_plane", "registry.unreadable", error=f"{type(exc).__name__}: {exc}")
            names = []
            ok = False
        return {"registry_ok": ok, "profiles": names}

    return ServiceSpec(
        name="control_plane",
        router=router,
        port=settings.service_ports.get("control_plane", 8090),
        on_start=[on_start],
        meta={"yaml_path": str(yaml_path)},
    )


def main() -> None:  # ``python -m services.control_plane.main``
    from services.common import run

    run("services.control_plane.main")


if __name__ == "__main__":
    main()
