from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
import json
import html as html_lib
import ast

from .dedupe import dedupe_reviews
from .settings import AppSettings, CurationSettings
from .storage import Place, Review, Storage


def _review_to_markdown(review: Review, reason: str, tags: list[str]) -> str:
    safety_map = {
        "safe": "Safe to use",
        "caution": "Use with caution",
        "not_recommended": "Not recommended",
    }
    safety_label = safety_map.get(review.safety_label, review.safety_label)
    lines = [
        f"### {review.humor_score}/100 - {', '.join(tags)}",
        f"**Reviewer:** {review.reviewer_name or 'Anonymous'}",
        f"**Date:** {review.date}",
        f"**Rating:** {review.rating} star(s)",
        "",
        "**Review:**",
        review.text or "(no text)",
    ]

    if review.owner_reply:
        lines.extend(["", "**Owner reply:**", review.owner_reply])

    lines.extend([
        "",
        f"**Why selected:** {reason}",
        f"**Safety:** {safety_label} ({review.safety_notes})",
        f"**Link:** {review.review_url}",
    ])

    return "\n".join(lines)


def build_shortlist(
    storage: Storage,
    app: AppSettings,
    curation: CurationSettings,
) -> list[Review]:
    candidates = storage.fetch_candidates(app.humor_threshold, app.allow_repeat_suggestions)
    deduped = dedupe_reviews(candidates)

    theme_limits = curation.theme_limits or {}
    theme_counts = {key: 0 for key in theme_limits}

    shortlist: list[Review] = []
    for item in deduped:
        if item.is_duplicate:
            continue

        review = item.review
        tags = [t.strip() for t in review.tags.split(",") if t.strip()]
        chosen_tag = tags[0] if tags else "misc"

        if theme_limits:
            limit = theme_limits.get(chosen_tag, theme_limits.get("misc", app.weekly_target_count))
            if theme_counts.get(chosen_tag, 0) >= limit:
                continue

        shortlist.append(review)
        if chosen_tag in theme_counts:
            theme_counts[chosen_tag] += 1

        if len(shortlist) >= app.weekly_target_count:
            break

    return shortlist


def export_shortlist(
    reviews: list[Review],
    app: AppSettings,
    output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_date = date.today().isoformat()

    json_path = output_dir / f"weekly_shortlist_{batch_date}.json"
    md_path = output_dir / f"weekly_shortlist_{batch_date}.md"

    payload = [asdict(review) for review in reviews]
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    md_sections = [f"# Weekly Shortlist ({batch_date})", ""]
    for review in reviews:
        reason = review.humor_notes or "High humor score"
        tags = [t.strip() for t in review.tags.split(",") if t.strip()]
        md_sections.append(_review_to_markdown(review, reason, tags))
        md_sections.append("")

    md_path.write_text("\n".join(md_sections), encoding="utf-8")
    return json_path, md_path


def export_shortlist_html(
    reviews: list[Review],
    app: AppSettings,
    output_dir: Path,
    screenshot_map: dict[str, Path | None],
    place_map: dict[str, Place],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_date = date.today().isoformat()
    batch_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    html_path = output_dir / f"weekly_shortlist_{batch_stamp}.html"

    def _rel_path(path: Path | None) -> str:
        if not path:
            return ""
        try:
            return path.relative_to(output_dir).as_posix()
        except ValueError:
            return path.as_posix()

    grouped: dict[str, list[Review]] = {}
    for review in reviews:
        grouped.setdefault(review.place_id, []).append(review)

    # Sort groups by top score desc, then place name.
    def _group_key(item: tuple[str, list[Review]]) -> tuple[int, str]:
        place_id, items = item
        top_score = max((r.humor_score for r in items), default=0)
        place = place_map.get(place_id)
        place_name = (place.name if place else "Unknown place").lower()
        return (-top_score, place_name)

    rows = []
    for place_id, place_reviews in sorted(grouped.items(), key=_group_key):
        place_reviews = sorted(place_reviews, key=lambda r: r.humor_score, reverse=True)
        place = place_map.get(place_id)
        place_name = place.name if place else "Unknown place"
        place_address = place.address if place else ""
        place_line_raw = (
            f"{place_name} · {place_address}"
            if place_address
            else place_name
        )
        place_line = html_lib.escape(place_line_raw)
        place_url_raw = ""
        if place:
            place_url_raw = place.place_url or ""
            if not place_url_raw and place.place_id:
                place_url_raw = f"https://www.google.com/maps/place/?q=place_id:{place.place_id}"
        place_url = html_lib.escape(place_url_raw, quote=True) if place_url_raw else ""
        group_title = (
            f'<a href="{place_url}">{place_line}</a>'
            if place_url
            else place_line
        )
        rows.append(
            "\n".join(
                [
                    '<section class="group">',
                    '  <div class="group-header">',
                    f'    <h2 class="group-title">{group_title}</h2>',
                    '    <div class="group-meta">Reseñas ordenadas por score (desc)</div>',
                    "  </div>",
                ]
            )
        )

        for review in place_reviews:
            place_html = group_title
            screenshot_rel = _rel_path(screenshot_map.get(review.review_id))
            screenshot_html = (
                f'<img src="{screenshot_rel}" alt="Screenshot" />'
                if screenshot_rel
                else "<em>Screenshot not available</em>"
            )
            tags = ", ".join([t.strip() for t in review.tags.split(",") if t.strip()]) or "misc"
            reviewer_name_raw = review.reviewer_name or "Anonymous"
            reviewer_link_raw = review.reviewer_profile_url or ""
            if reviewer_link_raw == "" and reviewer_name_raw.strip().startswith("{"):
                parsed = _parse_reviewer_payload(reviewer_name_raw)
                if parsed:
                    reviewer_name_raw, reviewer_link_raw = parsed
            reviewer_name = html_lib.escape(reviewer_name_raw or "Anonymous")
            reviewer_link = html_lib.escape(reviewer_link_raw or "", quote=True)
            reviewer_html = (
                f'<a href="{reviewer_link}">{reviewer_name}</a>'
                if reviewer_link
                else reviewer_name
            )
            score_link = html_lib.escape(review.review_url or "", quote=True)
            score_html = (
                f'<a class="score" href="{score_link}">{review.humor_score}/100</a>'
                if score_link
                else f'<span class="score">{review.humor_score}/100</span>'
            )
            safe_notes = html_lib.escape(review.humor_notes or "")
            safe_safety = html_lib.escape(review.safety_notes or "")
            safe_review_text = html_lib.escape(review.text or "(no text)")
            owner_reply_display = _format_owner_reply(review.owner_reply)

            rows.append(
                "\n".join(
                    [
                        '<article class="review">',
                        f"  <div class=\"score-wrap\">{score_html}</div>",
                        "  <div class=\"meta\">",
                        f"    <span><strong>Reviewer:</strong> {reviewer_html}</span>",
                        f"    <span><strong>Date:</strong> {review.date}</span>",
                        f"    <span><strong>Rating:</strong> {review.rating} star(s)</span>",
                        "  </div>",
                        "  <div class=\"meta\">",
                        f"    <span><strong>Safety:</strong> {review.safety_label} ({safe_safety})</span>",
                        f"    <span><strong>Why selected:</strong> {safe_notes}</span>",
                        "  </div>",
                        "  <div class=\"tags\">",
                        "    " + " ".join([f'<span class="tag">{t}</span>' for t in tags.split(", ")]),
                        "  </div>",
                        "  <section class=\"review-text\">",
                        "    <div class=\"label\">Review</div>",
                        f"    <div class=\"block\">{safe_review_text}</div>",
                        "  </section>",
                        (
                            "  <section class=\"review-text\">"
                            "    <div class=\"label\">Owner reply</div>"
                            f"    <div class=\"block\">{owner_reply_display}</div>"
                            "  </section>"
                            if review.owner_reply
                            else ""
                        ),
                        (
                            f'  <div class="links"><a class="cta" href="{score_link}">Ver reseña en Google Maps</a></div>'
                            if review.review_url
                            else ""
                        ),
                        f"<div class=\"screenshot\">{screenshot_html}</div>" if screenshot_rel else "",
                        "</article>",
                    ]
                )
            )
        rows.append("</section>")

    html = "\n".join(
        [
            "<!doctype html>",
            "<html lang=\"es\">",
            "<head>",
            "  <meta charset=\"utf-8\" />",
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />",
            f"  <title>Weekly Shortlist {batch_date}</title>",
            "  <style>",
            "    @import url(\"https://fonts.googleapis.com/css2?family=Fraunces:wght@500;700&family=DM+Sans:wght@400;500;700&display=swap\");",
            "    :root {",
            "      --bg: #eef3f9;",
            "      --card: #f9fbff;",
            "      --ink: #0d1b2a;",
            "      --muted: #5b6b7d;",
            "      --accent: #1b6aa9;",
            "      --accent-2: #7dc2ff;",
            "      --border: #d6e2f0;",
            "      --shadow: 0 18px 40px rgba(13, 27, 42, 0.12);",
            "    }",
            "    * { box-sizing: border-box; }",
            "    body {",
            "      font-family: \"DM Sans\", sans-serif;",
            "      background: radial-gradient(circle at top, #f8f2e6 0%, var(--bg) 55%, #efe6d6 100%);",
            "      color: var(--ink);",
            "      margin: 0;",
            "      min-height: 100vh;",
            "    }",
            "    header {",
            "      padding: 36px 24px 32px;",
            "      background: linear-gradient(120deg, #0b1f36 0%, #163b5c 55%, #0f263b 100%);",
            "      color: #eef5ff;",
            "      border-bottom: 4px solid var(--accent);",
            "    }",
            "    header h1 {",
            "      margin: 0;",
            "      font-family: \"Fraunces\", serif;",
            "      font-size: 32px;",
            "      letter-spacing: 0.4px;",
            "    }",
            "    header p { margin: 8px 0 0; color: #cddff0; }",
            "    main { padding: 28px 20px 48px; max-width: 1020px; margin: 0 auto; }",
            "    .summary {",
            "      display: grid;",
            "      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));",
            "      gap: 16px;",
            "      margin-bottom: 26px;",
            "    }",
            "    .summary-card {",
            "      background: #f2f7ff;",
            "      border: 1px solid var(--border);",
            "      border-radius: 14px;",
            "      padding: 16px;",
            "      box-shadow: 0 10px 22px rgba(20, 16, 12, 0.08);",
            "    }",
            "    .summary-card h3 { margin: 0 0 8px; font-size: 13px; color: var(--muted); }",
            "    .summary-card .value { font-family: \"Fraunces\", serif; font-size: 24px; }",
            "    .group {",
            "      margin-bottom: 34px;",
            "      border: 1px solid var(--border);",
            "      border-radius: 18px;",
            "      padding: 18px;",
            "      background: #f4f8ff;",
            "    }",
            "    .group-header {",
            "      border-bottom: 1px solid var(--border);",
            "      padding-bottom: 14px;",
            "      margin-bottom: 18px;",
            "    }",
            "    .group-title {",
            "      font-family: \"Fraunces\", serif;",
            "      font-size: 26px;",
            "      margin: 0 0 6px;",
            "      color: var(--ink);",
            "    }",
            "    .group-meta { color: var(--muted); }",
            "    .review {",
            "      background: var(--card);",
            "      border: 1px solid var(--border);",
            "      padding: 26px;",
            "      margin-bottom: 26px;",
            "      box-shadow: var(--shadow);",
            "      border-radius: 16px;",
            "    }",
            "    .score {",
            "      font-family: \"Fraunces\", serif;",
            "      font-size: 28px;",
            "      font-weight: 700;",
            "      color: var(--accent);",
            "      text-decoration: none;",
            "    }",
            "    .score-wrap {",
            "      display: inline-flex;",
            "      align-items: center;",
            "      gap: 6px;",
            "      background: #e3f0ff;",
            "      padding: 6px 14px;",
            "      border-radius: 999px;",
            "      border: 1px solid #c6dcf5;",
            "      box-shadow: inset 0 1px 0 rgba(255,255,255,0.8);",
            "      margin-bottom: 12px;",
            "    }",
            "    .place { font-size: 15px; color: var(--muted); }",
            "    .meta { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 12px; color: var(--muted); font-size: 14px; }",
            "    .meta strong { color: var(--ink); }",
            "    .tags { margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap; }",
            "    .tag { background: #dcecff; color: #1f4a7a; padding: 6px 12px; border-radius: 999px; font-size: 12px; font-weight: 600; }",
            "    .review-text { margin-top: 18px; }",
            "    .label { font-weight: 700; margin-bottom: 8px; font-family: \"Fraunces\", serif; }",
            "    .block { white-space: pre-wrap; background: #eff5ff; padding: 14px; border: 1px solid var(--border); border-radius: 10px; }",
            "    .links { margin-top: 14px; }",
            "    .links a { color: var(--accent); text-decoration: none; font-weight: 700; }",
            "    .links .cta {",
            "      display: inline-flex;",
            "      align-items: center;",
            "      gap: 8px;",
            "      background: var(--accent);",
            "      color: #eef5ff;",
            "      padding: 10px 16px;",
            "      border-radius: 999px;",
            "      box-shadow: 0 10px 18px rgba(27, 106, 169, 0.2);",
            "    }",
            "    .links a:hover, .score:hover { text-decoration: underline; }",
            "    .screenshot img { max-width: 100%; border: 1px solid var(--border); margin-top: 16px; border-radius: 12px; }",
            "    a { color: var(--accent); }",
            "  </style>",
            "</head>",
            "<body>",
            f"  <header><h1>Weekly Shortlist ({batch_date})</h1><p>Selección semanal de reseñas con mayor potencial humorístico.</p></header>",
            "  <main>",
            "    <section class=\"summary\">",
            f"      <div class=\"summary-card\"><h3>Reseñas seleccionadas</h3><div class=\"value\">{len(reviews)}</div></div>",
            f"      <div class=\"summary-card\"><h3>Puntuación media</h3><div class=\"value\">{round(sum(r.humor_score for r in reviews) / len(reviews), 1) if reviews else 0}</div></div>",
            f"      <div class=\"summary-card\"><h3>Top score</h3><div class=\"value\">{max((r.humor_score for r in reviews), default=0)}</div></div>",
            f"      <div class=\"summary-card\"><h3>Fecha</h3><div class=\"value\">{batch_date}</div></div>",
            "    </section>",
            "\n".join(rows) if rows else "<p>No reviews selected.</p>",
            "  </main>",
            "</body>",
            "</html>",
        ]
    )

    html_path.write_text(html, encoding="utf-8")
    return html_path


def _parse_reviewer_payload(raw: str) -> tuple[str, str] | None:
    try:
        payload = ast.literal_eval(raw)
    except Exception:
        return None
    if isinstance(payload, dict):
        name = str(payload.get("name") or payload.get("username") or "").strip()
        link = str(payload.get("link") or payload.get("profile_url") or "").strip()
        if name or link:
            return name or "Anonymous", link
    return None


def _format_owner_reply(raw: str | None) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            payload = ast.literal_eval(raw)
        except Exception:
            return html_lib.escape(raw)
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("snippet") or payload.get("response") or "").strip()
            date = str(payload.get("date") or payload.get("published_date") or "").strip()
            if text and date:
                return html_lib.escape(f"{text}\n\n{date}")
            if text:
                return html_lib.escape(text)
    return html_lib.escape(raw)


def mark_shortlist(storage: Storage, reviews: list[Review]) -> None:
    batch_date = date.today().isoformat()
    for review in reviews:
        storage.mark_shortlist(review.review_id, batch_date, review.humor_score)
        storage.update_status(review.review_id, "selected")
