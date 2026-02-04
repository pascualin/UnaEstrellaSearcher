from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from .storage import Review


@dataclass
class DedupedReview:
    review: Review
    is_duplicate: bool
    matched_id: str | None


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def dedupe_reviews(reviews: Iterable[Review], threshold: float = 0.85) -> list[DedupedReview]:
    seen: list[Review] = []
    result: list[DedupedReview] = []

    for review in reviews:
        duplicate_of = None
        for prior in seen:
            if _similar(review.text, prior.text) >= threshold:
                duplicate_of = prior.review_id
                break

        if duplicate_of:
            result.append(DedupedReview(review=review, is_duplicate=True, matched_id=duplicate_of))
        else:
            seen.append(review)
            result.append(DedupedReview(review=review, is_duplicate=False, matched_id=None))

    return result
