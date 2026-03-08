from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .api_cache import load_cached_json, save_cached_json
from .settings import DiscoverySettings, ProviderSettings
from .storage import Place


@dataclass
class DiscoveredPlace:
    place: Place


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
    categories = discovery.categories or [""]

    regions = discovery.regions or [""]
    for region in regions:
        for category in categories:
            query_category = _normalize_category(category)
            query = _build_query(query_category, discovery.name_contains, region)
            try:
                results, location_used = _serpapi_maps_search(
                    query=query,
                    api_key=api_key,
                    hl=providers.serpapi_hl,
                    gl=providers.serpapi_gl,
                    cache_dir=cache_dir,
                    no_cache=False,
                    location=region or None,
                )
            except Exception as exc:
                _emit_discovery_progress(
                    "search_failed",
                    {
                        "region": region or "global",
                        "category": category,
                        "query": query,
                        "error": str(exc),
                    },
                )
                time.sleep(0.6)
                continue
            _emit_discovery_progress(
                "search_query",
                {"region": region or "global", "category": category, "query": query, "raw_results": len(results)},
            )
            if not results:
                _emit_discovery_progress(
                    "no_results",
                    {"region": region or "global", "category": category, "query": query},
                )
                time.sleep(0.6)
                continue

            kept = 0
            skipped_no_ids = 0
            skipped_min_reviews = 0
            skipped_region = 0
            skipped_recent = 0
            for place_raw in results:
                # Region filtering disabled to avoid over-filtering when SerpApi returns
                # locations with inconsistent address formatting.
                place_id = str(place_raw.get("place_id") or "")
                data_id = str(place_raw.get("data_id") or "")
                if not place_id and not data_id:
                    skipped_no_ids += 1
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
                    category=category,
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
                        "region": region or "global",
                        "category": category,
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


def _parse_iso_date(value: str) -> datetime.date | None:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _normalize_category(category: str) -> str:
    return str(category or "").strip().lower().replace("_", " ")


def _build_query(category: str, name_contains: str, region: str) -> str:
    tokens = [part for part in [category.strip(), str(name_contains or "").strip()] if part]
    if tokens:
        return f"{' '.join(tokens)} in {region}" if region else " ".join(tokens)
    return region or ""


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
    address = str(
        place_raw.get("address")
        or place_raw.get("formatted_address")
        or place_raw.get("location")
        or ""
    ).lower()
    if not address:
        return True
    parts = [p.strip().lower() for p in region.split(",") if p.strip()]
    return all(part in address for part in parts)


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
