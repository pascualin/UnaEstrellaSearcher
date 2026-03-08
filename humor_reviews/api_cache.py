from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def load_cached_json(cache_dir: Path, kind: str, payload: dict[str, Any]) -> Any | None:
    path = _cache_path(cache_dir, kind, payload)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_cached_json(cache_dir: Path, kind: str, payload: dict[str, Any], value: Any) -> None:
    path = _cache_path(cache_dir, kind, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    except OSError:
        return


def _cache_path(cache_dir: Path, kind: str, payload: dict[str, Any]) -> Path:
    key = _cache_key(kind, payload)
    return cache_dir / kind / f"{key}.json"


def _cache_key(kind: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
