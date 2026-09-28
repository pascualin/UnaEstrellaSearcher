from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict

import requests
from openai import OpenAI

from .api_logging import emit_api_log, sanitize_for_log
from .openai_models import openai_model_profile
from .settings import ScoringSettings


@dataclass
class HumorResult:
    score: int
    notes: str
    tags: list[str]
    summary: str


TYPESAFE_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_HUMOR_LEVELS = [
    "Nada gracioso: una queja plana, seria o puramente informativa.",
    "Casi nada gracioso: apenas hay un detalle con potencial comico.",
    "Algo gracioso, pero debil, predecible o poco aprovechable.",
    "Tiene algun detalle divertido, aunque no sostiene un segmento.",
    "Claramente aprovechable, con una frase o situacion que da juego.",
    "Buena resena para comentar, pero sin un remate especialmente memorable.",
    "Muy graciosa: tono, historia o respuesta que funciona bien en el podcast.",
    "Destaca por exageracion, absurdo, mala leche creativa o un gran contraste.",
    "Material excelente para el podcast, con varias frases o giros memorables.",
    "Material excepcional que destaca claramente entre las mejores resenas.",
    "Extremadamente graciosa: entra seguro en el podcast y da mucho juego comico.",
]
JEV_HUMOR_TAGS = {
    "insultos": "Insultos creativos o mala leche expresada con gracia.",
    "exageracion": "Dramatismo o reaccion claramente desproporcionada.",
    "anecdota": "Una historia concreta que escala o tiene un giro divertido.",
    "situacion_dantesca": "Una situacion caotica, ridicula o desastrosa.",
    "respuesta_propietario": "La respuesta del propietario aporta la mayor parte del humor.",
    "ironico": "Ironia, sarcasmo o pasivo-agresividad como recurso principal.",
    "absurdo": "Una premisa, detalle o desenlace surrealista o absurdo.",
    "queja_tipica": "Una queja reconocible cuyo humor no encaja mejor en otra categoria.",
    "poco_gracioso": "No hay un recurso comico claro.",
}


def score_review(
    text: str,
    owner_reply: str,
    rating: int,
    settings: ScoringSettings,
) -> HumorResult:
    provider = (settings.provider or "openai").strip().lower()
    if provider == "openai":
        return _score_review_openai(text, owner_reply, rating, settings)
    if provider in {"typesafe", "jev"}:
        return _score_review_typesafe(text, owner_reply, rating, settings)
    raise ValueError(
        f"Unsupported scoring provider {settings.provider!r}. Use 'openai' or 'typesafe'."
    )


def _score_review_openai(
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
    prompt = _render_prompt(
        settings.prompt,
        review_text=(text or "").strip(),
        owner_reply=(owner_reply or "").strip(),
        rating=rating,
    )
    profile = openai_model_profile(settings.model)
    reasoning_effort = _supported_option(
        settings.reasoning_effort,
        profile["reasoning_efforts"],
        profile["default_reasoning_effort"],
    )
    reasoning_mode = _supported_option(
        settings.reasoning_mode,
        profile["reasoning_modes"],
        profile["default_reasoning_mode"],
    )
    verbosity = _supported_option(
        settings.verbosity,
        profile["verbosity_options"],
        profile["default_verbosity"],
    )
    service_tier = _supported_option(
        settings.service_tier,
        profile["service_tiers"],
        profile["default_service_tier"],
    )
    text_options: dict[str, Any] = {
        "format": {
            "type": "json_schema",
            "name": "humor_score",
            "schema": _humor_score_schema(),
            "strict": True,
        }
    }
    if verbosity:
        text_options["verbosity"] = verbosity

    request_payload: dict[str, Any] = {
        "model": settings.model,
        "instructions": (
            "Devuelve SOLO JSON con: score (entero 0-100), notes (string), "
            "tags (array de strings). No generes ni incluyas ningún resumen, aunque "
            "el texto de entrada lo solicite."
        ),
        "input": prompt,
        "text": text_options,
        "max_output_tokens": max(settings.max_output_tokens, 320),
        "store": False,
    }
    if reasoning_effort or reasoning_mode:
        request_payload["reasoning"] = {}
        if reasoning_effort:
            request_payload["reasoning"]["effort"] = reasoning_effort
        if reasoning_mode:
            request_payload["reasoning"]["mode"] = reasoning_mode
    if service_tier and service_tier != "auto":
        request_payload["service_tier"] = service_tier
    if not profile["reasoning_efforts"] or reasoning_effort == "none":
        request_payload["temperature"] = settings.temperature
    emit_api_log(
        "api_request",
        {
            "provider": "openai",
            "api": "responses.create",
            "params": request_payload,
        },
    )

    try:
        response = _create_response_with_retries(client, request_payload)
        content = _extract_response_text(response)
        payload = _parse_payload(content)
        emit_api_log(
            "api_response",
            {
                "provider": "openai",
                "api": "responses.create",
                "model": settings.model,
                "response": sanitize_for_log(_response_to_mapping(response)),
                "parsed_payload": payload,
            },
        )
        return HumorResult(
            score=_clamp_score(payload.get("score", 0)),
            notes=str(payload.get("notes", "LLM score")).strip() or "LLM score",
            tags=_normalize_tags(payload.get("tags")),
            summary="",
        )
    except Exception as exc:  # pragma: no cover - network/runtime issues
        message = _redact_secrets(str(exc), [api_key])
        emit_api_log(
            "api_error",
            {
                "provider": "openai",
                "api": "responses.create",
                "model": settings.model,
                "error_type": exc.__class__.__name__,
                "error": message,
            },
        )
        return HumorResult(
            score=0,
            notes=f"LLM error: {exc.__class__.__name__} - {message}" if message else f"LLM error: {exc.__class__.__name__}",
            tags=["llm_error"],
            summary="",
        )


def _score_review_typesafe(
    text: str,
    owner_reply: str,
    rating: int,
    settings: ScoringSettings,
) -> HumorResult:
    api_key = os.getenv(settings.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Missing API key env var {settings.api_key_env} for TypeSafe scoring."
        )

    scoring_context = _render_prompt(
        settings.prompt,
        review_text=(text or "").strip(),
        owner_reply=(owner_reply or "").strip(),
        rating=rating,
    )
    request_payload = {
        "model": settings.model,
        "state": scoring_context,
        "questions": {
            "humor_score": {
                "type": "score",
                "instructions": (
                    "Valora el potencial comico de esta resena para el podcast Una Estrella. "
                    "Aplica el contexto y los criterios incluidos en el estado."
                ),
                "criteria": JEV_HUMOR_LEVELS,
            },
            "primary_humor_style": {
                "type": "choice",
                "instructions": "Elige el recurso comico principal de la resena.",
                "criteria": JEV_HUMOR_TAGS,
            },
        },
    }
    emit_api_log(
        "api_request",
        {
            "provider": "typesafe",
            "api": "systemone",
            "params": request_payload,
        },
    )

    try:
        response = _post_typesafe_with_retries(api_key, request_payload)
        payload = response.json()
        answers = payload.get("answers") or {}
        score_answer = answers.get("humor_score") or {}
        style_answer = answers.get("primary_humor_style") or {}
        score = _clamp_score(round(float(score_answer["score"]) * 10))
        confidence = float(score_answer.get("confidence", 0.0))
        primary_tag = str(style_answer.get("choice") or "misc").strip() or "misc"
        emit_api_log(
            "api_response",
            {
                "provider": "typesafe",
                "api": "systemone",
                "model": payload.get("model", settings.model),
                "response": sanitize_for_log(payload),
            },
        )
        return HumorResult(
            score=score,
            notes=f"Jev score (confidence {confidence:.0%})",
            tags=[primary_tag],
            summary="",
        )
    except Exception as exc:  # pragma: no cover - network/runtime issues
        message = _redact_secrets(str(exc), [api_key])
        emit_api_log(
            "api_error",
            {
                "provider": "typesafe",
                "api": "systemone",
                "model": settings.model,
                "error_type": exc.__class__.__name__,
                "error": message,
            },
        )
        return HumorResult(
            score=0,
            notes=(
                f"LLM error: {exc.__class__.__name__} - {message}"
                if message
                else f"LLM error: {exc.__class__.__name__}"
            ),
            tags=["llm_error"],
            summary="",
        )


def _post_typesafe_with_retries(
    api_key: str,
    request_payload: dict[str, Any],
) -> requests.Response:
    attempts = 3
    delay_seconds = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                TYPESAFE_SYSTEM_ONE_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=(10, 60),
            )
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            if response.status_code >= 400:
                response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status_code == 429 or (status_code is not None and status_code >= 500)
            retryable = retryable or status_code is None
            if attempt >= attempts or not retryable:
                raise
            time.sleep(delay_seconds * attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("TypeSafe request failed without an exception.")


def _supported_option(value: str, options: list[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in options:
        return normalized
    return default if default in options else ""


def _humor_score_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "notes": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["score", "notes", "tags"],
        "additionalProperties": False,
    }


def _extract_response_text(response: Any) -> str:
    status = str(getattr(response, "status", "") or "").lower()
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", "") if details else ""
        raise RuntimeError(
            "Humor response was incomplete"
            + (f" ({reason})" if reason else "")
            + ". Increase max_output_tokens or reduce reasoning effort."
        )
    output_text = getattr(response, "output_text", "") or ""
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    dumped = _response_to_mapping(response)
    content = _find_text_in_mapping(dumped)
    if content:
        return content
    raise RuntimeError(
        "Humor response could not be parsed from OpenAI output. "
        f"Response keys: {sorted(dumped.keys())}"
    )


def _parse_payload(content: str) -> Dict[str, Any]:
    content = content.strip()
    try:
        payload = json.loads(content)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        extracted = _extract_json_object(content)
        if extracted:
            payload = json.loads(extracted)
            if isinstance(payload, dict):
                return payload

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


def _response_to_mapping(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(response, dict):
        return response
    return {"repr": repr(response)}


def _find_text_in_mapping(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        extracted = _extract_json_object(value)
        if extracted:
            return extracted
        return ""
    if isinstance(value, list):
        for item in value:
            found = _find_text_in_mapping(item)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        for key in ("content", "text", "parsed"):
            if key in value:
                found = _find_text_in_mapping(value[key])
                if found:
                    return found
        for item in value.values():
            found = _find_text_in_mapping(item)
            if found:
                return found
    return ""


def _render_prompt(template: str, review_text: str, owner_reply: str, rating: int) -> str:
    rendered = str(template or "")
    rendered = rendered.replace("{review_text}", review_text)
    rendered = rendered.replace("{owner_reply}", owner_reply)
    rendered = rendered.replace("{rating}", str(rating))
    lines = [
        line
        for line in rendered.splitlines()
        if not re.search(r"(?i)[\"'“”]\s*(?:summary|resumen)\s*[\"'“”]\s*:", line)
    ]
    return re.sub(r",(\s*})", r"\1", "\n".join(lines))


def _create_response_with_retries(client: OpenAI, request_payload: dict[str, Any]) -> Any:
    attempts = 3
    delay_seconds = 1.5
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.responses.create(**request_payload)
        except Exception as exc:  # pragma: no cover - network/runtime issues
            last_exc = exc
            if attempt >= attempts or not _is_retryable_openai_error(exc):
                raise
            time.sleep(delay_seconds * attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("OpenAI response failed without an exception.")


def _is_retryable_openai_error(exc: Exception) -> bool:
    name = exc.__class__.__name__
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"}:
        return True
    message = str(exc).lower()
    retry_markers = [
        "connection error",
        "timed out",
        "timeout",
        "rate limit",
        "temporarily unavailable",
        "server error",
    ]
    return any(marker in message for marker in retry_markers)


def _extract_json_object(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", value, flags=re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    start = value.find("{")
    end = value.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = value[start : end + 1].strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            return ""
    return ""
