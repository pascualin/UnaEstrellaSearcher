from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict

from openai import OpenAI

from .api_logging import emit_api_log, sanitize_for_log
from .settings import ScoringSettings


@dataclass
class HumorResult:
    score: int
    notes: str
    tags: list[str]
    summary: str


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
    request_payload = {
        "model": settings.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Devuelve SOLO JSON con: "
                    "score (entero 0-100), notes (string), tags (array de strings), summary (string corto)."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "humor_score",
                "strict": True,
            },
        },
        "temperature": settings.temperature,
        "max_completion_tokens": max(settings.max_output_tokens, 320),
    }
    emit_api_log(
        "api_request",
        {
            "provider": "openai",
            "api": "chat.completions.create",
            "params": request_payload,
        },
    )

    try:
        response = client.chat.completions.create(
            model=request_payload["model"],
            messages=request_payload["messages"],
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
                            "summary": {"type": "string"},
                        },
                        "required": ["score", "notes", "tags", "summary"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            temperature=request_payload["temperature"],
            max_completion_tokens=request_payload["max_completion_tokens"],
        )
        content = _extract_message_content(response)
        payload = _parse_payload(content)
        emit_api_log(
            "api_response",
            {
                "provider": "openai",
                "api": "chat.completions.create",
                "model": settings.model,
                "response": sanitize_for_log(_response_to_mapping(response)),
                "parsed_payload": payload,
            },
        )
        return HumorResult(
            score=_clamp_score(payload.get("score", 0)),
            notes=str(payload.get("notes", "LLM score")).strip() or "LLM score",
            tags=_normalize_tags(payload.get("tags")),
            summary=str(payload.get("summary", "")).strip(),
        )
    except Exception as exc:  # pragma: no cover - network/runtime issues
        message = _redact_secrets(str(exc), [api_key])
        emit_api_log(
            "api_error",
            {
                "provider": "openai",
                "api": "chat.completions.create",
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


def _extract_message_content(response: Any) -> str:
    choice = response.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise RuntimeError(
            "Humor response was truncated by the model token limit. "
            "Increase max_output_tokens for scoring."
        )
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
        try:
            return json.dumps(parsed)
        except TypeError:
            if hasattr(parsed, "model_dump"):
                return json.dumps(parsed.model_dump())
    refusal = getattr(message, "refusal", None)
    if refusal:
        return json.dumps({"notes": f"Model refusal: {refusal}", "score": 0, "tags": ["refusal"], "summary": ""})
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
