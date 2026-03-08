from __future__ import annotations

from datetime import date

from .dedupe import dedupe_reviews
from .settings import AppSettings, CurationSettings
from .storage import Review, Storage


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
def mark_shortlist(storage: Storage, reviews: list[Review]) -> None:
    batch_date = date.today().isoformat()
    for review in reviews:
        storage.mark_shortlist(review.review_id, batch_date, review.humor_score)
        storage.update_status(review.review_id, "selected")
