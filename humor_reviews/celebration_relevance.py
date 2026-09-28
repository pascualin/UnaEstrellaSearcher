from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from .settings import ScoringSettings


@dataclass(frozen=True)
class RelevanceResult:
    score: int
    observance: str
    notes: str


def score_celebration_relevance(
    review_text: str,
    owner_reply: str,
    place_name: str,
    place_category: str,
    observances: list[str],
    settings: ScoringSettings,
) -> RelevanceResult:
    if (settings.provider or "").strip().lower() == "openai":
        try:
            api_key, model = openai_planning_config(settings)
            client = OpenAI(api_key=api_key)
            response = client.responses.create(
                model=model,
                instructions=(
                    "Evalúa únicamente la relación temática entre una reseña y las celebraciones "
                    "propuestas para un episodio. No valores si es graciosa. Una relación indirecta "
                    "pero editorialmente defendible puede puntuar alto. Devuelve solo JSON válido."
                ),
                input=json.dumps(
                    {
                        "celebrations": observances,
                        "place_name": place_name,
                        "place_category": place_category,
                        "review": review_text,
                        "owner_reply": owner_reply,
                    },
                    ensure_ascii=False,
                ),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "celebration_relevance",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                                "observance": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                            "required": ["score", "observance", "notes"],
                            "additionalProperties": False,
                        },
                        "strict": True,
                    }
                },
                max_output_tokens=300,
                store=False,
            )
            payload = _response_payload(response)
            return RelevanceResult(
                score=max(0, min(100, int(payload.get("score") or 0))),
                observance=str(payload.get("observance") or "").strip(),
                notes=str(payload.get("notes") or "").strip(),
            )
        except Exception:
            pass
    return _local_relevance(
        review_text,
        owner_reply,
        place_name,
        place_category,
        observances,
    )


def _local_relevance(
    review_text: str,
    owner_reply: str,
    place_name: str,
    place_category: str,
    observances: list[str],
) -> RelevanceResult:
    combined = " ".join([place_name, place_category, review_text, owner_reply])
    decomposed = unicodedata.normalize("NFKD", combined)
    haystack = " ".join(
        re.sub(
            r"[^a-z0-9]+",
            " ",
            "".join(char for char in decomposed if not unicodedata.combining(char)).casefold(),
        ).split()
    )
    stopwords = {
        "dia", "semana", "internacional", "mundial", "global", "nacional",
        "del", "de", "la", "las", "los", "el", "y", "para", "contra",
    }
    best = RelevanceResult(0, "", "No se encontró relación temática directa.")
    for observance in observances:
        normalized = unicodedata.normalize("NFKD", observance)
        normalized = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).casefold()
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", normalized)
            if token not in stopwords and len(token) >= 4
        ]
        matched = [token for token in tokens if token in haystack]
        score = 0
        if tokens and len(matched) == len(tokens):
            score = 95
        elif matched:
            score = min(90, 60 + (30 * len(matched) // max(1, len(tokens))))
        if score > best.score:
            best = RelevanceResult(
                score,
                observance,
                f"Coincidencia temática local: {', '.join(matched)}.",
            )
    return best


def openai_planning_config(settings: ScoringSettings) -> tuple[str, str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key and settings.provider == "openai":
        api_key = os.getenv(settings.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY for celebration relevance scoring.")

    model = os.getenv("OPENAI_PLANNING_MODEL", "").strip()
    if not model:
        model = "gpt-4o-mini"
    return api_key, model


def _response_payload(response: Any) -> dict[str, Any]:
    content = str(getattr(response, "output_text", "") or "").strip()
    if not content:
        raise RuntimeError("Celebration relevance response was empty.")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Celebration relevance response was not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Celebration relevance response was not an object.")
    return payload
