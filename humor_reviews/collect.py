from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import requests
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .api_cache import load_cached_json, save_cached_json
from .api_logging import emit_api_log
from .settings import ProviderSettings


@dataclass
class RawReview:
    review_id: str
    place_id: str
    rating: int
    date: str
    reviewer_name: str
    reviewer_profile_url: str
    text: str
    owner_reply: str
    review_url: str


def _serpapi_reviews(
    data_id: str,
    api_key: str,
    hl: str,
    gl: str,
    cache_dir: Path,
    next_page_token: str | None = None,
    sort_by: str | None = "ratingLow",
    num: int | None = None,
) -> dict:
    cache_payload = {
        "data_id": data_id,
        "hl": hl,
        "gl": gl,
        "next_page_token": next_page_token or "",
        "sort_by": sort_by or "",
        "num": int(num or 0),
    }
    cached = load_cached_json(cache_dir, "reviews", cache_payload)
    if isinstance(cached, dict):
        emit_api_log(
            "api_cache_hit",
            {
                "provider": "serpapi",
                "api": "google_maps_reviews",
                "params": {
                    "engine": "google_maps_reviews",
                    "data_id": data_id,
                    "hl": hl,
                    "gl": gl,
                    "sort_by": sort_by or "",
                    "next_page_token": next_page_token or "",
                    "num": int(num or 0),
                },
                "review_count": len(cached.get("reviews", []) or []),
            },
        )
        return cached

    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_maps_reviews",
        "data_id": data_id,
        "api_key": api_key,
        "hl": hl,
        "gl": gl,
    }
    if sort_by:
        params["sort_by"] = sort_by
    if num:
        params["num"] = int(num)
    if next_page_token:
        params["next_page_token"] = next_page_token
    emit_api_log(
        "api_request",
        {
            "provider": "serpapi",
            "api": "google_maps_reviews",
            "method": "GET",
            "url": url,
            "params": params,
        },
    )
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        redacted = _redact_request_url(getattr(exc, "request", None))
        status = getattr(exc.response, "status_code", None)
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
    payload = resp.json()
    reviews = payload.get("reviews", []) or []
    emit_api_log(
        "api_response",
        {
            "provider": "serpapi",
            "api": "google_maps_reviews",
            "status_code": resp.status_code,
            "request_url": _redact_request_url(resp.request),
            "search_metadata_status": (payload.get("search_metadata") or {}).get("status"),
            "review_count": len(reviews),
            "reviews_preview": [
                {
                    "rating": item.get("rating"),
                    "date": item.get("date") or item.get("published_date") or "",
                    "reviewer": (
                        item.get("user", {}) if isinstance(item.get("user"), dict) else item.get("user")
                    ),
                    "snippet": str(
                        item.get("snippet") or item.get("text") or item.get("description") or ""
                    )[:180],
                }
                for item in reviews[:3]
            ],
            "next_page_token": ((payload.get("serpapi_pagination") or {}).get("next_page_token") or ""),
            "response_excerpt": _response_excerpt(resp),
        },
    )
    save_cached_json(cache_dir, "reviews", cache_payload, payload)
    return payload


def collect_reviews(
    data_ids: Iterable[str],
    providers: ProviderSettings,
    max_reviews_per_place: int,
    cache_dir: Path,
) -> Iterable[RawReview]:
    api_key = os.getenv(providers.serpapi_api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Missing API key env var {providers.serpapi_api_key_env}."
        )

    for data_id in data_ids:
        fetched = 0
        next_page_token = None
        while fetched < max_reviews_per_place:
            try:
                payload = _serpapi_reviews(
                    data_id=data_id,
                    api_key=api_key,
                    hl=providers.serpapi_hl,
                    gl=providers.serpapi_gl,
                    cache_dir=cache_dir,
                    next_page_token=next_page_token,
                )
            except Exception as exc:
                _emit_collection_progress(
                    "review_fetch_failed",
                    {"data_id": data_id, "error": str(exc)},
                )
                break

            place_url = payload.get("place_info", {}).get("link", "") or payload.get("search_metadata", {}).get("google_maps_url", "")
            reviews = payload.get("reviews", []) or []
            for review in reviews:
                if fetched >= max_reviews_per_place:
                    break

                rating = int(review.get("rating") or 0)
                review_text = str(review.get("snippet") or review.get("text") or review.get("description") or "").strip()
                reviewer_name, reviewer_profile_url = _extract_reviewer(review)
                review_date_raw = review.get("date") or review.get("published_date") or ""
                review_date = str(review_date_raw) if review_date_raw else ""
                owner_reply = str(review.get("owner_response") or review.get("response") or "").strip()
                review_link = str(review.get("link") or place_url or "")
                review_id = f"{data_id}:{review_link or review_date}:{reviewer_name or 'anon'}"

                if review_date:
                    yield_date = review_date
                else:
                    yield_date = datetime.utcnow().date().isoformat()

                yield RawReview(
                    review_id=review_id,
                    place_id=data_id,
                    rating=rating,
                    date=yield_date,
                    reviewer_name=reviewer_name,
                    reviewer_profile_url=reviewer_profile_url,
                    text=review_text,
                    owner_reply=owner_reply,
                    review_url=review_link,
                )
                fetched += 1

            pagination = payload.get("serpapi_pagination", {}) or {}
            next_page_token = pagination.get("next_page_token")
            if not next_page_token or not reviews:
                break

            time.sleep(1.0)

        time.sleep(1.0)


def _emit_collection_progress(event: str, payload: dict) -> None:
    log_path = os.getenv("PROGRESS_LOG")
    if not log_path:
        return
    record = {"event": event, **payload}
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


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


def _extract_reviewer(review: dict) -> tuple[str, str]:
    user = review.get("user") or review.get("author") or review.get("username") or ""
    if isinstance(user, dict):
        name = str(
            user.get("name")
            or user.get("username")
            or user.get("author")
            or user.get("display_name")
            or ""
        ).strip()
        link = str(user.get("link") or user.get("profile_url") or "").strip()
        return name, link
    if isinstance(user, str):
        return user.strip(), ""
    return "", ""
