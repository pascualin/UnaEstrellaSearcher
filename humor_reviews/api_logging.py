from __future__ import annotations

import json
import os
from typing import Any


_SECRET_KEYS = {"api_key", "key", "apikey", "authorization"}


def emit_api_log(event: str, payload: dict[str, Any]) -> None:
    log_path = os.getenv("PROGRESS_LOG")
    if not log_path:
        return
    record = {"event": event, **sanitize_for_log(payload)}
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def sanitize_for_log(value: Any, *, max_string: int = 600, max_items: int = 25) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                sanitized[key] = "REDACTED"
            else:
                sanitized[key] = sanitize_for_log(
                    item,
                    max_string=max_string,
                    max_items=max_items,
                )
        return sanitized
    if isinstance(value, list):
        trimmed = value[:max_items]
        sanitized = [
            sanitize_for_log(item, max_string=max_string, max_items=max_items)
            for item in trimmed
        ]
        if len(value) > max_items:
            sanitized.append({"truncated_items": len(value) - max_items})
        return sanitized
    if isinstance(value, tuple):
        return [
            sanitize_for_log(item, max_string=max_string, max_items=max_items)
            for item in value
        ]
    if isinstance(value, str):
        if len(value) <= max_string:
            return value
        return f"{value[:max_string]}... [truncated {len(value) - max_string} chars]"
    return value
