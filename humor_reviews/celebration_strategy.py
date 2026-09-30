from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from .settings import ScoringSettings
from .celebration_relevance import openai_planning_config


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


LOCAL_QUERY_RULES = [
    (("pulpo",), ("restaurante de pulpo", "marisquería")),
    (("dislexia",), ("asociación de dislexia", "centro de apoyo a la dislexia")),
    (("vision", "vista"), ("óptica", "clínica oftalmológica")),
    (("podolog",), ("podólogo", "clínica de podología")),
    (("espacio", "astronom"), ("planetario", "museo del espacio")),
    (("chocolate",), ("chocolatería", "tienda de chocolate")),
    (("paella",), ("restaurante de paella", "arrocería")),
    (("cafe",), ("cafetería", "tostador de café")),
    (("libro", "bibliotec"), ("librería", "biblioteca")),
    (("musica",), ("sala de conciertos", "tienda de música")),
    (("turismo", "viaje"), ("atracción turística", "visita guiada")),
]
UNSUITABLE_TOPIC_MARKERS = ("sindrome", "deficiencia")
SENSITIVE_TOPIC_MARKERS = (
    "cancer",
    "suicidio",
    "violencia",
    "maltrato",
    "duelo",
    "enfermedad grave",
    "victimas",
    "discapacidad",
)


def build_celebration_strategy(
    observances: list[dict[str, str]],
    settings: ScoringSettings,
) -> CelebrationStrategy:
    searchable_observances, prediscarded_observances = _partition_searchable_observances(
        observances
    )
    payload = searchable_observances

    if (settings.provider or "").strip().lower() not in {"openai"}:
        return _build_local_strategy(
            searchable_observances,
            "Planificación local para el proveedor elegido.",
            prediscarded_observances,
        )

    if not searchable_observances:
        return _build_local_strategy(
            [],
            "No hay celebraciones adecuadas para buscar lugares.",
            prediscarded_observances,
        )

    try:
        api_key, model = openai_planning_config(settings)
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
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
                        "Descarta celebraciones sensibles relacionadas con violencia, victimas, suicidio, "
                        "enfermedades graves, duelo o discapacidad: no deben usarse para buscar humor. "
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
                        "- searches: array de maximo 12 objetos con query, region y rationale\n"
                        "Las queries deben ser cortas, aptas para Google Maps y centradas en Espana.\n"
                        "Incluye al menos una query para cada celebracion seleccionada antes de "
                        "anadir queries adicionales para cualquiera de ellas.\n"
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
                                "maxItems": 12,
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
        if _is_openai_quota_error(exc):
            raise RuntimeError(
                "OpenAI no tiene saldo de API. Añade créditos o configura TypeSafe Jev "
                "con TYPESAFE_API_KEY para puntuar las reseñas."
            ) from exc
        return _build_local_strategy(
            searchable_observances,
            f"Planificación local porque OpenAI no estaba disponible ({exc.__class__.__name__}).",
            prediscarded_observances,
        )

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
        discarded_observances=list(
            dict.fromkeys(
                prediscarded_observances
                + [
                    str(item).strip()
                    for item in data.get("discarded_observances", [])
                    if str(item).strip()
                ]
            )
        ),
        notes=str(data.get("notes") or "").strip(),
        searches=searches,
    )


def _build_local_strategy(
    observances: list[dict[str, str]],
    notes: str,
    discarded_observances: list[str] | None = None,
) -> CelebrationStrategy:
    names = list(
        dict.fromkeys(
            str(item.get("name") or "").strip()
            for item in observances
            if str(item.get("name") or "").strip()
        )
    )
    searches: list[SearchPlan] = []
    seen_queries: set[str] = set()
    query_groups: list[tuple[str, tuple[str, ...]]] = []
    for name in names:
        topic = _observance_topic(name)
        normalized_topic = _normalize(topic)
        queries: tuple[str, ...] = ()
        for keywords, candidates in LOCAL_QUERY_RULES:
            if any(keyword in normalized_topic for keyword in keywords):
                queries = candidates
                break
        if not queries:
            queries = (topic, f"museo {topic}")
        query_groups.append((name, queries))

    max_queries_per_observance = max(
        (len(queries) for _name, queries in query_groups),
        default=0,
    )
    for query_index in range(max_queries_per_observance):
        for name, queries in query_groups:
            if query_index >= len(queries):
                continue
            query = queries[query_index]
            normalized_query = _normalize(query)
            if not normalized_query or normalized_query in seen_queries:
                continue
            seen_queries.add(normalized_query)
            searches.append(
                SearchPlan(
                    query=query,
                    region="Spain",
                    rationale=f"Búsqueda relacionada con {name}.",
                )
            )
            if len(searches) >= 12:
                break
        if len(searches) >= 12:
            break
    return CelebrationStrategy(
        selected_observances=names,
        discarded_observances=list(discarded_observances or []),
        notes=notes,
        searches=searches,
    )


def _partition_searchable_observances(
    observances: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    searchable: list[dict[str, str]] = []
    discarded: list[str] = []
    for observance in observances:
        name = str(observance.get("name") or "").strip()
        if observance_exclusion_reason(name):
            if name:
                discarded.append(name)
            continue
        searchable.append(observance)
    return searchable, list(dict.fromkeys(discarded))


def observance_exclusion_reason(name: str) -> str:
    normalized_topic = _normalize(_observance_topic(name))
    has_query_rule = any(
        keyword in normalized_topic
        for keywords, _queries in LOCAL_QUERY_RULES
        for keyword in keywords
    )
    if any(marker in normalized_topic for marker in SENSITIVE_TOPIC_MARKERS):
        return "Tema sensible"
    if not has_query_rule and any(
        marker in normalized_topic for marker in UNSUITABLE_TOPIC_MARKERS
    ):
        return "No produce una búsqueda adecuada de lugares"
    return ""


def _observance_topic(name: str) -> str:
    topic = re.sub(
        r"^(?:día|semana|noche|jornada)\s+"
        r"(?:(?:internacional|mundial|global|europe[oa]|nacional)\s+)*"
        r"(?:(?:de|del|de la|de los|de las)\s+)?",
        "",
        str(name or "").strip(),
        flags=re.IGNORECASE,
    ).strip(" .#")
    return topic or str(name or "").strip()


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text.casefold()).split())


def _is_openai_quota_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return "insufficient_quota" in message or "credit_balance_exhausted" in message


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
