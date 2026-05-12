from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI
import yaml

from .api_logging import emit_api_log, sanitize_for_log


@dataclass
class TranslationResult:
    review_text_es: str
    owner_reply_es: str
    review_language: str
    owner_reply_language: str


def translate_review_to_spanish(review_text: str, owner_reply_text: str) -> TranslationResult:
    review_text = str(review_text or "").strip()
    owner_reply_text = str(owner_reply_text or "").strip()
    if not review_text and not owner_reply_text:
        return TranslationResult(
            review_text_es="",
            owner_reply_es="",
            review_language="",
            owner_reply_language="",
        )

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return TranslationResult(
            review_text_es=review_text,
            owner_reply_es=owner_reply_text,
            review_language="",
            owner_reply_language="",
        )

    model = _translation_model()
    client = OpenAI(api_key=api_key)
    try:
        review_payload = _translate_single_text_to_spanish(client, model, review_text, "review")
        owner_payload = _translate_single_text_to_spanish(client, model, owner_reply_text, "owner_reply")
        result = TranslationResult(
            review_text_es=str(review_payload.get("translated_text_es") or review_text).strip() or review_text,
            owner_reply_es=str(owner_payload.get("translated_text_es") or owner_reply_text).strip() or owner_reply_text,
            review_language=str(review_payload.get("original_language") or "").strip().lower(),
            owner_reply_language=str(owner_payload.get("original_language") or "").strip().lower(),
        )
        emit_api_log(
            "api_response",
            {
                "provider": "openai",
                "api": "chat.completions.create",
                "model": model,
                "parsed_payload": {
                    "review_text_es": result.review_text_es,
                    "owner_reply_es": result.owner_reply_es,
                    "review_language": result.review_language,
                    "owner_reply_language": result.owner_reply_language,
                },
            },
        )
        return result
    except Exception as exc:
        emit_api_log(
            "api_error",
            {
                "provider": "openai",
                "api": "chat.completions.create",
                "model": model,
                "error_type": exc.__class__.__name__,
                "error": str(exc).replace(api_key, "REDACTED"),
            },
        )
        return TranslationResult(
            review_text_es=review_text,
            owner_reply_es=owner_reply_text,
            review_language="",
            owner_reply_language="",
        )


def _translate_single_text_to_spanish(
    client: OpenAI,
    model: str,
    text: str,
    field_name: str,
) -> dict[str, str]:
    text = str(text or "").strip()
    if not text:
        return {"translated_text_es": "", "original_language": ""}

    request_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Tu tarea es detectar el idioma dominante de un texto y traducirlo al castellano si hace falta. "
                    "Si el texto ya está en castellano natural y mayoritario, devuélvelo igual. "
                    "Si el texto está en inglés, portugués, francés, italiano o cualquier idioma distinto del castellano, "
                    "tradúcelo completamente al castellano manteniendo tono, insultos, ironía y humor. "
                    "Si el texto mezcla castellano con otro idioma, pero el idioma dominante no es castellano, tradúcelo completo al castellano. "
                    "Una frase de saludo en castellano no convierte el texto en castellano si el resto está en otro idioma. "
                    "Devuelve el idioma original con código ISO 639-1 en minúsculas: es, en, pt, fr, it, de, etc. "
                    "Devuelve solo JSON válido."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "field": field_name,
                        "text": text,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": f"{field_name}_translation",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "translated_text_es": {"type": "string"},
                        "original_language": {"type": "string"},
                    },
                    "required": ["translated_text_es", "original_language"],
                    "additionalProperties": False,
                },
            },
        },
        "temperature": 0,
        "max_completion_tokens": 1400,
    }
    emit_api_log(
        "api_request",
        {
            "provider": "openai",
            "api": "chat.completions.create",
            "params": request_payload,
        },
    )
    response = _create_completion_with_retries(client, request_payload)
    content = _extract_message_content(response)
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise RuntimeError("Translation payload is not an object.")
    emit_api_log(
        "api_response",
        {
            "provider": "openai",
            "api": "chat.completions.create",
            "model": model,
            "response": sanitize_for_log(_response_to_mapping(response)),
            "parsed_payload": payload,
        },
    )
    return {
        "translated_text_es": str(payload.get("translated_text_es") or text).strip() or text,
        "original_language": str(payload.get("original_language") or "").strip().lower(),
    }


def _create_completion_with_retries(client: OpenAI, request_payload: dict[str, Any]) -> Any:
    attempts = 3
    delay_seconds = 1.5
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _create_completion(client, request_payload, include_temperature=True)
        except Exception as exc:  # pragma: no cover - network/runtime issues
            if _is_unsupported_temperature_error(exc):
                return _create_completion(client, request_payload, include_temperature=False)
            last_exc = exc
            if attempt == attempts or not _is_retryable_openai_error(exc):
                raise
            time.sleep(delay_seconds * attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Translation request failed without raising an exception.")


def _is_unsupported_temperature_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "unsupported value" in message and "temperature" in message


def _create_completion(
    client: OpenAI,
    request_payload: dict[str, Any],
    include_temperature: bool,
) -> Any:
    kwargs: dict[str, Any] = {
        "model": request_payload["model"],
        "messages": request_payload["messages"],
        "response_format": request_payload["response_format"],
        "max_completion_tokens": request_payload["max_completion_tokens"],
    }
    if include_temperature:
        kwargs["temperature"] = request_payload["temperature"]
    return client.chat.completions.create(**kwargs)


def _is_retryable_openai_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    retryable_tokens = (
        "connection",
        "timeout",
        "rate",
        "429",
        "500",
        "502",
        "503",
        "504",
        "apierror",
        "server",
    )
    return any(token in name or token in text for token in retryable_tokens)


def _extract_message_content(response: Any) -> str:
    choice = response.choices[0]
    message = choice.message
    content = getattr(message, "content", "") or ""
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    parsed = getattr(message, "parsed", None)
    if parsed:
        if hasattr(parsed, "model_dump"):
            parsed = parsed.model_dump()
        return json.dumps(parsed, ensure_ascii=False)
    raise RuntimeError("Translation response could not be parsed from OpenAI output.")


def _response_to_mapping(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(response, dict):
        return response
    return {"repr": repr(response)}


def _translation_model() -> str:
    explicit = os.getenv("OPENAI_TRANSLATION_MODEL", "").strip()
    if explicit:
        return explicit
    configured = _config_scoring_model()
    if configured:
        return configured
    fallback = os.getenv("OPENAI_MODEL", "").strip()
    if fallback:
        return fallback
    return "gpt-5.2"


def _config_scoring_model() -> str:
    config_path = Path("config.yaml")
    if not config_path.exists():
        return ""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    scoring = raw.get("scoring") or {}
    return str(scoring.get("model") or "").strip()
