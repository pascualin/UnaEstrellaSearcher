from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

import requests
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .settings import DiscoverySettings, ProviderSettings
from .storage import Place


@dataclass
class DiscoveredPlace:
    place: Place


def _serpapi_maps_search(query: str, api_key: str, hl: str, gl: str) -> list[dict]:
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_maps",
        "type": "search",
        "q": query,
        "hl": hl,
        "gl": gl,
        "api_key": api_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        redacted = _redact_request_url(getattr(exc, "request", None))
        raise RuntimeError(f"SerpApi request failed: {redacted}") from exc
    data = resp.json()
    return data.get("local_results", []) or []


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


def discover_places(
    discovery: DiscoverySettings,
    providers: ProviderSettings,
) -> Iterable[DiscoveredPlace]:
    api_key = os.getenv(providers.serpapi_api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Missing API key env var {providers.serpapi_api_key_env}."
        )

    min_recent_date = datetime.utcnow() - timedelta(days=discovery.require_recent_days)

    for region in discovery.regions:
        for category in discovery.categories:
            query = f"{category} in {region}"
            results = _serpapi_maps_search(
                query=query,
                api_key=api_key,
                hl=providers.serpapi_hl,
                gl=providers.serpapi_gl,
            )

            for place_raw in results:
                place_id = str(place_raw.get("place_id") or "")
                data_id = str(place_raw.get("data_id") or "")
                if not place_id and not data_id:
                    continue

                total_reviews = int(place_raw.get("reviews") or 0)
                if total_reviews < discovery.min_total_reviews:
                    continue

                last_review_date = None
                last_seen = place_raw.get("reviewed_at") or place_raw.get("last_review_date")
                if last_seen:
                    last_review_date = str(last_seen)
                    parsed = _parse_iso_date(last_review_date)
                    if parsed and parsed < min_recent_date.date():
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

            time.sleep(1.2)


def _parse_iso_date(value: str) -> datetime.date | None:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None
