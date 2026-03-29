from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .api_cache import load_cached_json, save_cached_json
from .api_logging import emit_api_log
from .settings import DiscoverySettings, ProviderSettings
from .storage import Place

GENERAL_SEARCH_SEEDS = [
    "restaurant",
    "bar",
    "cafe",
    "museum",
    "tourist attraction",
    "hotel",
]


@dataclass
class DiscoveredPlace:
    place: Place


@dataclass
class SearchQuery:
    query: str
    region: str = ""
    category: str = ""


def _serpapi_maps_search(
    query: str,
    api_key: str,
    hl: str,
    gl: str,
    cache_dir: Path,
    no_cache: bool = False,
    location: str | None = None,
    _retry_without_location: bool = True,
) -> tuple[list[dict], bool]:
    cache_payload = {
        "query": query,
        "hl": hl,
        "gl": gl,
        "location": location or "",
    }
    cached = load_cached_json(cache_dir, "discover", cache_payload)
    if isinstance(cached, list):
        emit_api_log(
            "api_cache_hit",
            {
                "provider": "serpapi",
                "api": "google_maps_search",
                "params": {
                    "engine": "google_maps",
                    "type": "search",
                    "q": query,
                    "hl": hl,
                    "gl": gl,
                    "location": location or "",
                },
                "result_count": len(cached),
            },
        )
        return cached, bool(location)

    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_maps",
        "type": "search",
        "q": query,
        "hl": hl,
        "gl": gl,
        "api_key": api_key,
    }
    if no_cache:
        params["no_cache"] = "true"
    if location:
        params["location"] = location
    emit_api_log(
        "api_request",
        {
            "provider": "serpapi",
            "api": "google_maps_search",
            "method": "GET",
            "url": url,
            "params": params,
        },
    )
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 400 and location and _retry_without_location:
            # Some generic locations (e.g. country-only) are rejected by SerpApi.
            return _serpapi_maps_search(
                query=query,
                api_key=api_key,
                hl=hl,
                gl=gl,
                cache_dir=cache_dir,
                no_cache=no_cache,
                location=None,
                _retry_without_location=False,
            )
        redacted = _redact_request_url(getattr(exc, "request", None))
        body = _response_excerpt(getattr(exc, "response", None))
        raise RuntimeError(
            f"SerpApi HTTP error {status} for {redacted}. Response: {body}"
        ) from exc
    except requests.RequestException as exc:
        redacted = _redact_request_url(getattr(exc, "request", None))
        raise RuntimeError(
            f"SerpApi request failed for {redacted}. "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    data = resp.json()
    results = data.get("local_results", []) or []
    emit_api_log(
        "api_response",
        {
            "provider": "serpapi",
            "api": "google_maps_search",
            "status_code": resp.status_code,
            "request_url": _redact_request_url(resp.request),
            "search_metadata_status": (data.get("search_metadata") or {}).get("status"),
            "result_count": len(results),
            "results_preview": [
                {
                    "title": item.get("title") or item.get("name") or "",
                    "address": item.get("address") or item.get("formatted_address") or "",
                    "place_id": item.get("place_id") or "",
                    "data_id": item.get("data_id") or "",
                }
                for item in results[:5]
            ],
            "response_excerpt": _response_excerpt(resp),
        },
    )
    save_cached_json(cache_dir, "discover", cache_payload, results)
    return results, bool(location)


def _build_place_url(place_raw: dict) -> str:
    link = str(place_raw.get("link") or place_raw.get("place_link") or "").strip()
    if link:
        return link
    place_id = str(place_raw.get("place_id") or "").strip()
    if place_id:
        return f"https://www.google.com/maps/place/?q=place_id:{place_id}"
    return ""


def _redact_request_url(request: requests.PreparedRequest | None) -> str:
    if not request or not request.url:
        return "request_url_unavailable"
    parts = urlsplit(request.url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in {"api_key", "key", "apikey"}:
            query.append((key, "REDACTED"))
        else:
            query.append((key, value))
    redacted_query = urlencode(query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, redacted_query, parts.fragment))


def _response_excerpt(response: requests.Response | None) -> str:
    if response is None:
        return "response_unavailable"
    text = (response.text or "").strip().replace("\n", " ")
    if not text:
        return "empty_response"
    return text[:300]


def discover_places(
    discovery: DiscoverySettings,
    providers: ProviderSettings,
    cache_dir: Path,
) -> Iterable[DiscoveredPlace]:
    api_key = os.getenv(providers.serpapi_api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Missing API key env var {providers.serpapi_api_key_env}."
        )

    min_recent_date = datetime.utcnow() - timedelta(days=discovery.require_recent_days)
    categories = _effective_categories(discovery.categories)

    regions = discovery.regions or [""]
    for region in regions:
        for category in categories:
            query_category = _normalize_category(category)
            effective_location = region or _country_search_term(discovery.country)
            query = _build_query(query_category, discovery.name_contains, effective_location)
            try:
                results, location_used = _serpapi_maps_search(
                    query=query,
                    api_key=api_key,
                    hl=providers.serpapi_hl,
                    gl=providers.serpapi_gl,
                    cache_dir=cache_dir,
                    no_cache=False,
                    location=effective_location or None,
                )
            except Exception as exc:
                _emit_discovery_progress(
                    "search_failed",
                    {
                        "region": effective_location or "global",
                        "category": _progress_category_label(category, query),
                        "query": query,
                        "error": str(exc),
                    },
                )
                time.sleep(0.6)
                continue
            _emit_discovery_progress(
                "search_query",
                {
                    "region": effective_location or "global",
                    "category": _progress_category_label(category, query),
                    "query": query,
                    "raw_results": len(results),
                },
            )
            if not results:
                _emit_discovery_progress(
                    "no_results",
                    {
                        "region": effective_location or "global",
                        "category": _progress_category_label(category, query),
                        "query": query,
                    },
                )
                time.sleep(0.6)
                continue

            kept = 0
            skipped_no_ids = 0
            skipped_min_reviews = 0
            skipped_region = 0
            skipped_recent = 0
            for place_raw in results:
                place_id = str(place_raw.get("place_id") or "")
                data_id = str(place_raw.get("data_id") or "")
                if not place_id and not data_id:
                    skipped_no_ids += 1
                    continue
                if region and not _region_matches(place_raw, region):
                    skipped_region += 1
                    _emit_discovery_progress(
                        "region_filtered_out",
                        {
                            "region": region,
                            "category": _progress_category_label(category, query),
                            "query": query,
                            "place_name": place_raw.get("title") or place_raw.get("name") or "Unknown",
                            "address": place_raw.get("address") or place_raw.get("formatted_address") or "",
                        },
                    )
                    continue

                total_reviews = int(place_raw.get("reviews") or 0)
                if total_reviews < discovery.min_total_reviews:
                    skipped_min_reviews += 1
                    continue

                last_review_date = None
                last_seen = place_raw.get("reviewed_at") or place_raw.get("last_review_date")
                if last_seen:
                    last_review_date = str(last_seen)
                    parsed = _parse_iso_date(last_review_date)
                    if parsed and parsed < min_recent_date.date():
                        skipped_recent += 1
                        continue

                place = Place(
                    place_id=place_id or data_id,
                    data_id=data_id,
                    name=place_raw.get("title") or place_raw.get("name") or "Unknown",
                    address=place_raw.get("address") or place_raw.get("formatted_address") or "",
                    category=_stored_category(category),
                    total_reviews=total_reviews,
                    last_review_date=last_review_date,
                    provider="serpapi",
                    place_url=_build_place_url(place_raw),
                )

                yield DiscoveredPlace(place=place)
                kept += 1

            if kept == 0:
                _emit_discovery_progress(
                    "no_results",
                    {
                        "region": effective_location or "global",
                        "category": _progress_category_label(category, query),
                        "query": query,
                        "raw_results": len(results),
                        "reason": "filtered_out",
                        "skipped_no_ids": skipped_no_ids,
                        "skipped_min_reviews": skipped_min_reviews,
                        "skipped_region": skipped_region,
                        "skipped_recent": skipped_recent,
                    },
                )

            _ = location_used
            time.sleep(1.2)


def discover_places_for_queries(
    queries: list[SearchQuery],
    discovery: DiscoverySettings,
    providers: ProviderSettings,
    cache_dir: Path,
) -> Iterable[DiscoveredPlace]:
    api_key = os.getenv(providers.serpapi_api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Missing API key env var {providers.serpapi_api_key_env}."
        )

    min_recent_date = datetime.utcnow() - timedelta(days=discovery.require_recent_days)
    for item in queries:
        try:
            results, _ = _serpapi_maps_search(
                query=item.query,
                api_key=api_key,
                hl=providers.serpapi_hl,
                gl=providers.serpapi_gl,
                cache_dir=cache_dir,
                no_cache=False,
                location=item.region or None,
            )
        except Exception as exc:
            _emit_discovery_progress(
                "search_failed",
                {
                    "region": item.region or "global",
                    "category": _progress_category_label(item.category, item.query),
                    "query": item.query,
                    "error": str(exc),
                },
            )
            continue

        _emit_discovery_progress(
            "search_query",
            {
                "region": item.region or "global",
                "category": _progress_category_label(item.category, item.query),
                "query": item.query,
                "raw_results": len(results),
            },
        )
        for place_raw in results:
            place = _build_place(place_raw, item.category, min_recent_date.date(), discovery.min_total_reviews)
            if place is None:
                continue
            yield DiscoveredPlace(place=place)
        time.sleep(1.2)


def _parse_iso_date(value: str) -> datetime.date | None:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _normalize_category(category: str) -> str:
    return str(category or "").strip().lower().replace("_", " ")


def _effective_categories(categories: list[str] | None) -> list[str]:
    cleaned = [str(item or "").strip() for item in (categories or []) if str(item or "").strip()]
    if cleaned:
        return cleaned
    return GENERAL_SEARCH_SEEDS.copy()


def _stored_category(category: str) -> str:
    value = str(category or "").strip()
    if value:
        return value
    return "general"


def _build_query(category: str, name_contains: str, region: str) -> str:
    category = str(category or "").strip()
    name_contains = str(name_contains or "").strip()
    region = str(region or "").strip()
    tokens = [part for part in [category, name_contains] if part]
    if tokens:
        return f"{' '.join(tokens)} in {region}" if region else " ".join(tokens)
    if region:
        return f"places in {region}"
    return "places"


def _progress_category_label(category: str, query: str) -> str:
    label = str(category or "").strip()
    if label:
        return label
    query_text = str(query or "").strip()
    if query_text and not query_text.lower().startswith("places in "):
        return query_text
    return "búsqueda general"


def _country_search_term(country: str) -> str:
    code = str(country or "").strip().upper()
    aliases = {
        "US": "United States",
        "ES": "Spain",
        "GB": "United Kingdom",
        "UK": "United Kingdom",
        "FR": "France",
        "IT": "Italy",
        "DE": "Germany",
        "PT": "Portugal",
        "MX": "Mexico",
        "AR": "Argentina",
        "JP": "Japan",
        "CN": "China",
        "HK": "Hong Kong",
    }
    return aliases.get(code, code)


def _build_place(
    place_raw: dict,
    category: str,
    min_recent_date: datetime.date,
    min_total_reviews: int,
) -> Place | None:
    place_id = str(place_raw.get("place_id") or "")
    data_id = str(place_raw.get("data_id") or "")
    if not place_id and not data_id:
        return None

    total_reviews = int(place_raw.get("reviews") or 0)
    if total_reviews < min_total_reviews:
        return None

    last_review_date = None
    last_seen = place_raw.get("reviewed_at") or place_raw.get("last_review_date")
    if last_seen:
        last_review_date = str(last_seen)
        parsed = _parse_iso_date(last_review_date)
        if parsed and parsed < min_recent_date:
            return None

    return Place(
        place_id=place_id or data_id,
        data_id=data_id,
        name=place_raw.get("title") or place_raw.get("name") or "Unknown",
        address=place_raw.get("address") or place_raw.get("formatted_address") or "",
        category=category,
        total_reviews=total_reviews,
        last_review_date=last_review_date,
        provider="serpapi",
        place_url=_build_place_url(place_raw),
    )


def _category_aliases(category: str) -> list[str]:
    norm = _normalize_category(category)
    aliases = {
        "sports stadium": [
            "stadium",
            "sports stadium",
            "sports complex",
            "arena",
            "estadio",
            "estadio deportivo",
            "estádio",
            "estadio de fútbol",
            "estadio de futbol",
            "estadio de futebol",
        ],
        "stadium": [
            "stadium",
            "sports stadium",
            "sports complex",
            "arena",
            "estadio",
            "estadio deportivo",
            "estádio",
            "estadio de fútbol",
            "estadio de futbol",
            "estadio de futebol",
        ],
        "stadiums": [
            "stadium",
            "sports stadium",
            "sports complex",
            "arena",
            "estadio",
            "estadio deportivo",
            "estádio",
            "estadio de fútbol",
            "estadio de futbol",
            "estadio de futebol",
        ],
        "estadio deportivo": [
            "stadium",
            "sports stadium",
            "sports complex",
            "arena",
            "estadio",
            "estadio deportivo",
            "estádio",
            "estadio de fútbol",
            "estadio de futbol",
            "estadio de futebol",
        ],
        "estadios deportivos": [
            "stadium",
            "sports stadium",
            "sports complex",
            "arena",
            "estadio",
            "estadio deportivo",
            "estádio",
            "estadio de fútbol",
            "estadio de futbol",
            "estadio de futebol",
        ],
        "tourist attraction": ["tourist attraction", "tourist", "attraction", "atraccion turistica", "atracción turística"],
        "hotel": ["hotel", "lodging", "hostal", "hostel"],
        "restaurant": ["restaurant", "restaurante", "food", "comida"],
        "parking": ["parking", "estacionamiento", "aparcamiento"],
        "car wash": ["car wash", "lavado de coches", "autolavado"],
        "spa": ["spa", "balneario"],
    }
    return aliases.get(norm, [norm])


def _category_matches(place_raw: dict, category: str) -> bool:
    norm_cat = _normalize_category(category)
    aliases = _category_aliases(category)
    raw_types = place_raw.get("types") or place_raw.get("type") or []
    if isinstance(raw_types, str):
        types = [raw_types]
    else:
        try:
            types = list(raw_types)
        except TypeError:
            types = []

    raw_category = place_raw.get("category") or place_raw.get("category_name") or ""
    raw_categories = place_raw.get("categories") or []
    if isinstance(raw_categories, str):
        raw_categories = [raw_categories]

    tokens = []
    tokens.extend(types)
    if raw_category:
        tokens.append(raw_category)
    tokens.extend(raw_categories)

    # Fall back to title matching only if we have no structured category signal.
    norm_tokens = [str(t).lower().replace("_", " ") for t in tokens]
    title = str(place_raw.get("title") or place_raw.get("name") or "").lower()

    for alias in aliases:
        if any(alias in token for token in norm_tokens):
            return True
        if alias in title:
            return True
    return False


def _region_matches(place_raw: dict, region: str) -> bool:
    if not region:
        return True
    address = _normalize_region_text(
        str(
        place_raw.get("address")
        or place_raw.get("formatted_address")
        or place_raw.get("location")
        or ""
        )
    )
    if not address:
        return True
    parts = _region_parts(region)
    if not parts:
        return True

    # Use the most specific location term first. This avoids false negatives like
    # "Estepona. Malaga" and also avoids requiring "Spain" to appear in addresses.
    primary = parts[0]
    if primary in address:
        return True

    # Fall back to exact phrase match for the normalized region string.
    normalized_region = _normalize_region_text(region)
    if normalized_region and normalized_region in address:
        return True

    return False


def _region_parts(region: str) -> list[str]:
    ignored = {"espana", "spain", "es", "malaga province"}
    normalized = _normalize_region_text(region)
    if not normalized:
        return []
    parts = [
        part.strip()
        for part in re.split(r"[,.;:/\-]+", normalized)
        if part.strip()
    ]
    return [part for part in parts if part not in ignored]


def _normalize_region_text(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(ch for ch in ascii_value if not unicodedata.combining(ch))
    ascii_value = ascii_value.lower()
    ascii_value = re.sub(r"[^a-z0-9\s,.;:/-]+", " ", ascii_value)
    return re.sub(r"\s+", " ", ascii_value).strip()


def _emit_discovery_progress(event: str, payload: dict) -> None:
    log_path = os.getenv("PROGRESS_LOG")
    if not log_path:
        return
    record = {"event": event, **payload}
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return
