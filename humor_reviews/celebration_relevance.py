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


RELEVANCE_ALIASES = {
    "pulpo": ("pulperia", "pulpeira", "octopus"),
    "dislexia": ("dislexia", "logopedia", "dyslexia"),
    "vision": ("optica", "oftalm", "oculista", "optomet"),
    "podologia": ("podolog", "podologo", "podologa", "feet", "foot"),
    "espacio": ("planetario", "astronom", "alien", "cosmic", "cosmico", "nasa"),
    "chocolate": ("chocolate", "chocolateria", "cacao"),
}
AMBIGUOUS_RELEVANCE_TOKENS = {"espacio", "vision", "vista"}


def score_celebration_relevance(
    review_text: str,
    owner_reply: str,
    place_name: str,
    place_category: str,
    observances: list[str],
    settings: ScoringSettings,
) -> RelevanceResult:
    local_result = score_celebration_relevance_local(
        review_text,
        owner_reply,
        place_name,
        place_category,
        observances,
    )
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
            model_result = RelevanceResult(
                score=max(0, min(100, int(payload.get("score") or 0))),
                observance=str(payload.get("observance") or "").strip(),
                notes=str(payload.get("notes") or "").strip(),
            )
            return model_result if model_result.score >= local_result.score else local_result
        except Exception:
            pass
    return local_result


def score_celebration_relevance_local(
    review_text: str,
    owner_reply: str,
    place_name: str,
    place_category: str,
    observances: list[str],
) -> RelevanceResult:
    place_haystack = _normalize_relevance_text(" ".join([place_name, place_category]))
    review_haystack = _normalize_relevance_text(" ".join([review_text, owner_reply]))
    combined_haystack = f"{place_haystack} {review_haystack}".strip()
    stopwords = {
        "dia", "semana", "internacional", "mundial", "global", "nacional",
        "del", "de", "la", "las", "los", "el", "y", "para", "contra",
    }
    place_words = set(place_haystack.split())
    review_words = set(review_haystack.split())
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
        place_matches = [token for token in tokens if token in place_words]
        review_matches = [
            token
            for token in tokens
            if token in review_words and token not in AMBIGUOUS_RELEVANCE_TOKENS
        ]
        alias_matches: list[str] = []
        for token in tokens:
            aliases = RELEVANCE_ALIASES.get(token, ())
            alias_matches.extend(alias for alias in aliases if alias in combined_haystack)
            if (
                token not in AMBIGUOUS_RELEVANCE_TOKENS
                and len(token) >= 5
                and any(word.startswith(token[:4]) for word in place_words)
            ):
                place_matches.append(token)
        place_matches = list(dict.fromkeys(place_matches))
        review_matches = list(dict.fromkeys(review_matches))
        alias_matches = list(dict.fromkeys(alias_matches))
        score = 0
        if tokens and len(place_matches) == len(tokens):
            score = 95
        elif alias_matches:
            score = 90
        elif place_matches:
            score = min(90, 70 + (20 * len(place_matches) // max(1, len(tokens))))
        elif review_matches:
            score = min(90, 65 + (25 * len(review_matches) // max(1, len(tokens))))
        if score > best.score:
            evidence = place_matches or alias_matches or review_matches
            best = RelevanceResult(
                score,
                observance,
                f"Coincidencia temática local: {', '.join(evidence)}.",
            )
    return best


def _normalize_relevance_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text.casefold()).split())


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
