from __future__ import annotations

import hashlib
import html
from urllib.parse import quote_plus
from pathlib import Path
from typing import Iterable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .storage import Place, Review


def capture_screenshots(
    reviews: Iterable[Review],
    output_dir: Path,
    timeout_ms: int,
    max_per_run: int,
    place_map: dict[str, Place],
    debug: bool = False,
    mode: str = "rendered",
) -> dict[str, Path | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path | None] = {}
    captured = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.set_default_timeout(timeout_ms)

        for review in list(reviews):
            target_url = _choose_screenshot_url(review, place_map)
            if not target_url:
                results[review.review_id] = None
                continue

            digest = hashlib.sha256(review.review_id.encode("utf-8")).hexdigest()[:16]
            filename = f"review_{digest}.png"
            path = output_dir / filename
            if path.exists():
                results[review.review_id] = path
                continue
            if captured >= max_per_run:
                results[review.review_id] = None
                continue
            try:
                if mode == "rendered":
                    page.set_content(_render_review_html(review, place_map.get(review.place_id)))
                else:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    _accept_google_consent(page)
                    page.wait_for_timeout(2000)
                page.screenshot(path=str(path), full_page=True)
                results[review.review_id] = path
                captured += 1
            except PlaywrightTimeoutError:
                results[review.review_id] = None
                if debug:
                    print(f"Screenshot timeout for {review.review_id} ({target_url})")
            except Exception as exc:
                results[review.review_id] = None
                if debug:
                    print(f"Screenshot failed for {review.review_id} ({target_url}): {exc}")

        browser.close()

    return results


def _choose_screenshot_url(review: Review, place_map: dict[str, Place]) -> str | None:
    url = (review.review_url or "").strip()
    if url and "maps/reviews/data" not in url:
        return url

    place = place_map.get(review.place_id)
    if not place:
        return url or None

    query_parts = [place.name, place.address]
    query = ", ".join([part for part in query_parts if part])
    if not query:
        return url or None

    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"


def _render_review_html(review: Review, place: Place | None) -> str:
    place_name = place.name if place else "Unknown place"
    place_address = place.address if place else ""
    place_line = f"{place_name} ({place_address})" if place_address else place_name
    owner_reply = review.owner_reply or ""
    review_text = review.text or ""
    return "\n".join(
        [
            "<!doctype html>",
            "<html lang=\"es\">",
            "<head>",
            "  <meta charset=\"utf-8\" />",
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />",
            "  <style>",
            "    body { font-family: Georgia, serif; background: #f6f1e7; color: #1f1b16; margin: 0; }",
            "    .card { max-width: 900px; margin: 24px auto; padding: 24px; background: #fffaf2; border: 1px solid #e3d6c6; }",
            "    h1 { font-size: 22px; margin: 0 0 6px; }",
            "    .meta { font-size: 14px; margin-bottom: 12px; }",
            "    .label { font-weight: bold; }",
            "    .block { white-space: pre-wrap; background: #f1e7da; padding: 12px; }",
            "  </style>",
            "</head>",
            "<body>",
            "  <div class=\"card\">",
            f"    <h1>{html.escape(place_line)}</h1>",
            "    <div class=\"meta\">",
            f"      <span class=\"label\">Reviewer:</span> {html.escape(review.reviewer_name or 'Anonymous')}<br/>",
            f"      <span class=\"label\">Date:</span> {html.escape(review.date)}<br/>",
            f"      <span class=\"label\">Rating:</span> {review.rating} star(s)<br/>",
            f"      <span class=\"label\">Score:</span> {review.humor_score}/100<br/>",
            "    </div>",
            "    <div class=\"label\">Review</div>",
            f"    <div class=\"block\">{html.escape(review_text)}</div>",
            "    <div class=\"label\" style=\"margin-top: 12px;\">Owner reply</div>",
            f"    <div class=\"block\">{html.escape(owner_reply) if owner_reply else '(no owner reply)'}</div>",
            "  </div>",
            "</body>",
            "</html>",
        ]
    )


def _accept_google_consent(page) -> None:
    # Best-effort click on Google consent dialogs (varies by locale).
    button_names = [
        "Aceptar todo",
        "Acepto",
        "Aceptar",
        "Accept all",
        "I agree",
        "Agree",
    ]
    selectors = [
        "button:has-text(\"Aceptar todo\")",
        "button:has-text(\"Acepto\")",
        "button:has-text(\"Aceptar\")",
        "button:has-text(\"Accept all\")",
        "button:has-text(\"I agree\")",
        "button:has-text(\"Agree\")",
        "form button:has-text(\"Aceptar todo\")",
    ]

    # Try in main page.
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=1000):
                page.locator(sel).first.click(timeout=1000)
                return
        except Exception:
            continue

    # Try in iframes (Google consent often appears there).
    for frame in page.frames:
        try:
            for name in button_names:
                locator = frame.get_by_role("button", name=name)
                if locator.first.is_visible(timeout=500):
                    locator.first.click(timeout=500)
                    return
        except Exception:
            continue
