"""Model registry: per-intent profiles, file-backed so a hot swap reaches every process."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "chat": {
        "provider": "openrouter",
        "model": "google/gemma-4-26b-a4b-it",
        "temperature": 0.2,
        "max_tokens": 1200,
        "fallbacks": ["google/gemma-4-12b-it"],
    },
    "judge": {"provider": "openrouter", "model": "google/gemma-4-12b-it", "temperature": 0.0, "max_tokens": 400, "fallbacks": []},
    "embed": {"provider": "local", "model": "BAAI/bge-base-en-v1.5", "dim": 768},
}


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"profiles": copy.deepcopy(DEFAULT_PROFILES)}
    raw = yaml.safe_load(path.read_text() or "") or {}
    profiles = raw.get("profiles") or {}
    merged = copy.deepcopy(DEFAULT_PROFILES)
    for name, cfg in profiles.items():
        merged.setdefault(name, {}).update(cfg or {})
    return {"profiles": merged}


def load_profiles(path: Path) -> dict[str, dict[str, Any]]:
    return _read(Path(path))["profiles"]


def get_profile(path: Path, name: str = "chat") -> dict[str, Any]:
    profiles = load_profiles(path)
    if name not in profiles:
        raise KeyError(f"unknown model profile {name!r}; have {sorted(profiles)}")
    return profiles[name]


def set_profile(path: Path, name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Write-through update; caller publishes ``model.changed`` afterwards."""
    path = Path(path)
    data = _read(path)
    data["profiles"].setdefault(name, {}).update(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return data["profiles"][name]


def ensure_seed(path: Path) -> None:
    if not Path(path).exists():
        set_profile(Path(path), "chat", copy.deepcopy(DEFAULT_PROFILES["chat"]))
        set_profile(Path(path), "judge", copy.deepcopy(DEFAULT_PROFILES["judge"]))
        set_profile(Path(path), "embed", copy.deepcopy(DEFAULT_PROFILES["embed"]))
