from __future__ import annotations

from datetime import date

from .dedupe import dedupe_reviews
from .settings import AppSettings
from .storage import Review, Storage


def build_shortlist(
    storage: Storage,
    app: AppSettings,
) -> list[Review]:
    candidates = storage.fetch_candidates(app.humor_threshold, app.allow_repeat_suggestions)
    deduped = dedupe_reviews(candidates)

    shortlist: list[Review] = []
    for item in deduped:
        if item.is_duplicate:
            continue

        shortlist.append(item.review)

    return shortlist
def mark_shortlist(storage: Storage, reviews: list[Review]) -> None:
    batch_date = date.today().isoformat()
    for review in reviews:
        storage.mark_shortlist(review.review_id, batch_date, review.humor_score)
        storage.update_status(review.review_id, "")
