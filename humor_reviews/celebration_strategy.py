from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from .settings import ScoringSettings


@dataclass
class SearchPlan:
    query: str
    region: str
    rationale: str


@dataclass
class CelebrationStrategy:
    selected_observances: list[str]
    discarded_observances: list[str]
    notes: str
    searches: list[SearchPlan]


def build_celebration_strategy(
    observances: list[dict[str, str]],
    settings: ScoringSettings,
) -> CelebrationStrategy:
    api_key = os.getenv(settings.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Missing API key env var {settings.api_key_env} for OpenAI strategy."
        )

    client = OpenAI(api_key=api_key)
    payload = observances

    try:
        response = client.chat.completions.create(
            model=settings.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres un planificador de discovery para Google Maps en Espana. "
                        "Selecciona celebraciones relevantes para Espana y genera queries concretas "
                        "que puedan producir lugares con resenas potencialmente graciosas. "
                        "Prioriza lugares con experiencias presenciales, caos, expectativas rotas, "
                        "interaccion humana extrana o actividades propensas a anecdotas absurdas. "
                        "Evita ecommerce generico, tiendas online, academias genericas, "
                        "servicios demasiado tecnicos y negocios donde lo normal sean solo quejas de envio o soporte. "
                        "Devuelve SOLO JSON valido."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Observancias del dia en Espana:\n"
                        f"{json.dumps(payload, ensure_ascii=False)}\n\n"
                        "Devuelve un objeto JSON con:\n"
                        "- selected_observances: array de nombres elegidos\n"
                        "- discarded_observances: array de nombres descartados\n"
                        "- notes: razon corta\n"
                        "- searches: array de maximo 5 objetos con query, region y rationale\n"
                        "Las queries deben ser cortas, aptas para Google Maps y centradas en Espana.\n"
                        "Prefiere categorias y consultas como atracciones, talleres, experiencias, "
                        "museos peculiares, restaurantes tematicos, escape rooms, mercadillos, "
                        "parques tematicos, centros de ocio o lugares fisicos donde una mala experiencia pueda ser ridicula.\n"
                        "No propongas ecommerce, tiendas de regalos online, academias genericas, "
                        "software, soporte tecnico ni negocios dominados por incidencias logisticas.\n"
                        "Si no hay una observancia util, devuelve searches vacio."
                    ),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "celebration_strategy",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "selected_observances": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "discarded_observances": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "notes": {"type": "string"},
                            "searches": {
                                "type": "array",
                                "maxItems": 5,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "query": {"type": "string"},
                                        "region": {"type": "string"},
                                        "rationale": {"type": "string"},
                                    },
                                    "required": ["query", "region", "rationale"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": [
                            "selected_observances",
                            "discarded_observances",
                            "notes",
                            "searches",
                        ],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            temperature=0.2,
            max_completion_tokens=max(settings.max_output_tokens, 400),
        )
        data = _parse_strategy_payload(response)
    except Exception as exc:  # pragma: no cover - network/runtime issues
        raise RuntimeError(
            f"Strategy generation failed: {exc.__class__.__name__}: {exc}"
        ) from exc

    searches = [
        SearchPlan(
            query=str(item.get("query") or "").strip(),
            region=str(item.get("region") or "").strip(),
            rationale=str(item.get("rationale") or "").strip(),
        )
        for item in data.get("searches", [])
        if str(item.get("query") or "").strip()
    ]
    return CelebrationStrategy(
        selected_observances=[str(item).strip() for item in data.get("selected_observances", []) if str(item).strip()],
        discarded_observances=[str(item).strip() for item in data.get("discarded_observances", []) if str(item).strip()],
        notes=str(data.get("notes") or "").strip(),
        searches=searches,
    )


def build_celebration_strategy_from_text(
    celebrations_text: str,
    settings: ScoringSettings,
) -> CelebrationStrategy:
    observances = [
        {
            "name": item,
            "description": "",
            "type": "manual_input",
            "date": "",
            "locations": "Spain",
        }
        for item in _split_celebrations_text(celebrations_text)
    ]
    return build_celebration_strategy(observances, settings)


def _extract_message_content(response: Any) -> str:
    choice = response.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise RuntimeError(
            "Strategy response was truncated by the model token limit. "
            "Increase max_output_tokens for strategy generation."
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
        return json.dumps(
            {
                "selected_observances": [],
                "discarded_observances": [],
                "notes": f"Model refusal: {refusal}",
                "searches": [],
            }
        )
    dumped = _response_to_mapping(response)
    content = _find_text_in_mapping(dumped)
    if content:
        return content
    diagnostic = _response_diagnostic(dumped)
    raise RuntimeError(
        "Strategy response could not be parsed from OpenAI output. "
        f"Response keys: {sorted(dumped.keys())}. {diagnostic}"
    )


def _parse_strategy_payload(response: Any) -> dict[str, Any]:
    content = _extract_message_content(response).strip()
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
    raise RuntimeError(
        "Strategy response was not valid JSON. "
        f"Content preview: {content[:400]!r}"
    )


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


def _split_celebrations_text(celebrations_text: str) -> list[str]:
    raw_parts = re.split(r"[\n;,]+", celebrations_text)
    items: list[str] = []
    for part in raw_parts:
        cleaned = re.sub(r"\s{2,}", " ", part).strip(" -\t\r")
        if cleaned:
            items.append(cleaned)
    return items


def _response_diagnostic(dumped: dict[str, Any]) -> str:
    choices = dumped.get("choices")
    if not isinstance(choices, list) or not choices:
        return "No choices present in response dump."
    first = choices[0]
    if not isinstance(first, dict):
        return f"First choice type: {type(first).__name__}"
    finish_reason = first.get("finish_reason")
    message = first.get("message")
    if not isinstance(message, dict):
        return (
            f"finish_reason={finish_reason!r}, "
            f"message_type={type(message).__name__}, "
            f"choice_keys={sorted(first.keys())}"
        )
    content = message.get("content")
    refusal = message.get("refusal")
    parsed = message.get("parsed")
    preview = repr(content)
    if len(preview) > 300:
        preview = preview[:300] + "..."
    parsed_type = type(parsed).__name__ if parsed is not None else "None"
    return (
        f"finish_reason={finish_reason!r}, "
        f"message_keys={sorted(message.keys())}, "
        f"content_preview={preview}, "
        f"refusal={refusal!r}, "
        f"parsed_type={parsed_type}"
    )
