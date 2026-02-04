from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict

from openai import OpenAI

from .settings import ScoringSettings


@dataclass
class HumorResult:
    score: int
    notes: str
    tags: list[str]


def score_review(
    text: str,
    owner_reply: str,
    rating: int,
    settings: ScoringSettings,
) -> HumorResult:
    api_key = os.getenv(settings.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Missing API key env var {settings.api_key_env} for OpenAI scoring."
        )

    client = OpenAI(api_key=api_key)
    prompt = settings.prompt.format(
        review_text=(text or "").strip(),
        owner_reply=(owner_reply or "").strip(),
        rating=rating,
    )

    try:
        response = client.chat.completions.create(
            model=settings.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Devuelve SOLO JSON con: "
                        "score (entero 0-100), notes (string), tags (array de strings)."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "humor_score",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "notes": {"type": "string"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["score", "notes", "tags"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
        )
        content = response.choices[0].message.content or ""
        payload = _parse_payload(content)
        return HumorResult(
            score=_clamp_score(payload.get("score", 0)),
            notes=str(payload.get("notes", "LLM score")).strip() or "LLM score",
            tags=_normalize_tags(payload.get("tags")),
        )
    except Exception as exc:  # pragma: no cover - network/runtime issues
        message = _redact_secrets(str(exc), [api_key])
        return HumorResult(
            score=0,
            notes=f"LLM error: {exc.__class__.__name__} - {message}" if message else f"LLM error: {exc.__class__.__name__}",
            tags=["llm_error"],
        )


def _parse_payload(content: str) -> Dict[str, Any]:
    content = content.strip()
    try:
        payload = json.loads(content)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    match = re.search(r"\b(\d{1,3})\b", content)
    if match:
        return {"score": int(match.group(1)), "notes": "Parsed score", "tags": ["misc"]}

    return {"score": 0, "notes": "Parse failure", "tags": ["misc"]}


def _clamp_score(value: int) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        tags = [str(tag).strip() for tag in value if str(tag).strip()]
        return tags or ["misc"]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return ["misc"]


def _redact_secrets(message: str, secrets: list[str | None]) -> str:
    if not message:
        return ""
    redacted = message
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "REDACTED")
    return redacted
