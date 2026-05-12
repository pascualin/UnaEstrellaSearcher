from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from .celebration_strategy import CelebrationStrategy, build_celebration_strategy_from_text
from .collect import collect_reviews
from .discover import SearchQuery, discover_places, discover_places_for_queries
from .humor import score_review
from .safety import assess_safety
from .settings import load_settings
from .shortlist import build_shortlist, mark_shortlist
from .storage import Place, Review, Storage


def run_discovery(storage: Storage, settings) -> tuple[int, list[str], dict[str, str]]:
    count = 0
    place_ids: list[str] = []
    place_names: dict[str, str] = {}
    cache_dir = settings.app.data_dir / "api_cache"
    for discovered in discover_places(settings.discovery, settings.providers, cache_dir):
        storage.upsert_place(discovered.place)
        count += 1
        place_key = discovered.place.data_id or discovered.place.place_id
        place_ids.append(place_key)
        place_names[place_key] = discovered.place.name
        _emit_progress(
            "discovered_place",
            {
                "place_id": place_key,
                "place_name": discovered.place.name,
                "category": discovered.place.category,
            },
        )
        _emit_progress("sites_found", {"count": count})
        if count >= settings.app.max_places_per_run:
            break
    return count, place_ids, place_names


def run_collection(
    storage: Storage,
    settings,
    place_ids: list[str] | None = None,
    place_names: dict[str, str] | None = None,
) -> int:
    count = 0
    skipped_empty = 0
    skipped_existing = 0
    per_place_counts: dict[str, int] = {}
    if place_ids is None:
        place_ids = storage.get_place_ids()
    place_map = storage.get_place_map()
    cache_dir = settings.app.data_dir / "api_cache"
    for place_id in place_ids:
        place_name = place_id
        if place_names and place_id in place_names:
            place_name = place_names[place_id]
        else:
            place = place_map.get(place_id)
            place_name = place.name if place else place_id

        place_data_id = place_id
        place = place_map.get(place_id)
        if place and place.data_id:
            place_data_id = place.data_id

        _emit_progress("place_start", {"place_id": place_id, "place_name": place_name})
        place_scores: list[int] = []
        place_count = 0
        try:
            for raw in collect_reviews(
                [place_data_id],
                settings.providers,
                settings.app.max_reviews_per_place,
                cache_dir,
            ):
                if not (raw.text or "").strip():
                    skipped_empty += 1
                    continue
                if raw.rating > 2:
                    continue

                per_place_counts.setdefault(raw.place_id, 0)
                if per_place_counts[raw.place_id] >= settings.app.max_reviews_per_place:
                    continue
                if storage.review_exists(raw.review_id):
                    skipped_existing += 1
                    continue

                humor = score_review(raw.text, raw.owner_reply, raw.rating, settings.scoring)
                safety = assess_safety(raw.text, raw.owner_reply, settings.safety)

                review = Review(
                    review_id=raw.review_id,
                    place_id=raw.place_id,
                    rating=raw.rating,
                    date=raw.date,
                    reviewer_name=raw.reviewer_name,
                    reviewer_profile_url=raw.reviewer_profile_url,
                    text=raw.text,
                    summary=humor.summary,
                    owner_reply=raw.owner_reply,
                    review_url=raw.review_url,
                    humor_score=humor.score,
                    humor_notes=humor.notes,
                    safety_label=safety.label,
                    safety_notes=safety.notes,
                    tags=",".join(humor.tags),
                )

                storage.upsert_review(review)
                if humor.score < settings.app.humor_threshold:
                    storage.update_status(review.review_id, "rejected")
                per_place_counts[raw.place_id] += 1
                count += 1
                place_count += 1
                place_scores.append(humor.score)
                _emit_progress(
                    "review_scored",
                    {
                        "review_id": raw.review_id,
                        "place_id": place_id,
                        "place_name": place_name,
                        "review_count": place_count,
                        "total_collected": count,
                        "score": humor.score,
                        "reviewer_name": raw.reviewer_name,
                    },
                )
        except Exception as exc:
            _emit_progress(
                "place_failed",
                {
                    "place_id": place_id,
                    "place_name": place_name,
                    "error": str(exc),
                },
            )

        _emit_progress(
            "place_done",
            {
                "place_id": place_id,
                "place_name": place_name,
                "review_count": place_count,
                "total_collected": count,
                "scores": place_scores,
            },
        )

    storage.record_stat("empty_reviews_skipped", skipped_empty)
    storage.record_stat("existing_reviews_skipped", skipped_existing)
    return count


def run_shortlist(storage: Storage, settings, dry_run: bool = False) -> None:
    shortlist = build_shortlist(storage, settings.app)
    if not dry_run:
        mark_shortlist(storage, shortlist)

    suffix = " (dry-run)" if dry_run else ""
    print(f"Shortlist built with {len(shortlist)} reviews{suffix}")


def run_weekly(storage: Storage, settings) -> None:
    _emit_progress("run_started", {"message": "weekly"})
    discovered, discovered_place_ids, discovered_place_names = run_discovery(storage, settings)
    if not discovered_place_ids:
        _emit_progress("run_complete", {"discovered": discovered, "collected": 0})
        print(f"Discovered {discovered} places, collected 0 reviews")
        return
    collected = run_collection(
        storage,
        settings,
        discovered_place_ids,
        discovered_place_names or None,
    )
    run_shortlist(storage, settings)
    _emit_progress("sites_found", {"count": discovered})
    _emit_progress("run_complete", {"discovered": discovered, "collected": collected})
    print(f"Discovered {discovered} places, collected {collected} reviews")


def run_add_place(storage: Storage, place_id: str) -> None:
    placeholder = Place(
        place_id=place_id,
        data_id=place_id,
        name="Manual",
        address="",
        category="manual",
        total_reviews=0,
        last_review_date=None,
        provider="serpapi",
    )
    storage.upsert_place(placeholder)
    print(f"Added place {place_id}")

def run_set_status(storage: Storage, review_id: str, status: str) -> None:
    storage.update_status(review_id, status)
    print(f"Updated {review_id} to {status}")


def run_rescore_llm_errors(storage: Storage, settings, limit: int | None = None) -> None:
    reviews = storage.fetch_reviews_needing_rescore()
    if limit is not None:
        reviews = reviews[:limit]

    rescored = 0
    for review in reviews:
        humor = score_review(review.text, review.owner_reply, review.rating, settings.scoring)
        if "llm_error" in humor.tags:
            continue
        updated = Review(
            review_id=review.review_id,
            place_id=review.place_id,
            rating=review.rating,
            date=review.date,
            reviewer_name=review.reviewer_name,
            reviewer_profile_url=review.reviewer_profile_url,
            text=review.text,
            summary=humor.summary,
            owner_reply=review.owner_reply,
            review_url=review.review_url,
            humor_score=humor.score,
            humor_notes=humor.notes,
            safety_label=review.safety_label,
            safety_notes=review.safety_notes,
            tags=",".join(humor.tags),
        )
        storage.upsert_review(updated)
        storage.update_status(review.review_id, "new")
        rescored += 1

    print(f"Rescored {rescored} reviews")


def run_themed_celebrations(
    storage: Storage,
    settings,
    celebrations_text: str,
    target_funny_reviews: int,
    humor_threshold: int,
    max_searches: int,
    max_places: int,
    max_reviews_per_place: int,
) -> None:
    cache_dir = settings.app.data_dir / "api_cache"
    if not celebrations_text.strip():
        print("No celebrations text provided.")
        return

    strategy = build_celebration_strategy_from_text(celebrations_text, settings.scoring)
    if not strategy.searches:
        print("No viable search strategy generated for those celebrations.")
        return

    queries = [
        SearchQuery(query=item.query, region=item.region, category="themed_day")
        for item in strategy.searches[:max_searches]
    ]

    discovered_count = 0
    collected_count = 0
    funny_count = 0
    seen_places: set[str] = set()
    place_ids: list[str] = []
    place_names: dict[str, str] = {}

    for discovered in discover_places_for_queries(queries, settings.discovery, settings.providers, cache_dir):
        place_key = discovered.place.data_id or discovered.place.place_id
        if place_key in seen_places:
            continue
        storage.upsert_place(discovered.place)
        seen_places.add(place_key)
        place_ids.append(place_key)
        place_names[place_key] = discovered.place.name
        discovered_count += 1
        if discovered_count >= max_places:
            break

    place_map = storage.get_place_map()
    for place_id in place_ids:
        place = place_map.get(place_id)
        place_data_id = place.data_id if place and place.data_id else place_id
        for raw in collect_reviews(
            [place_data_id],
            settings.providers,
            max_reviews_per_place,
            cache_dir,
        ):
            if not (raw.text or "").strip():
                continue
            if raw.rating > 2:
                continue
            if storage.review_exists(raw.review_id):
                continue

            humor = score_review(raw.text, raw.owner_reply, raw.rating, settings.scoring)
            safety = assess_safety(raw.text, raw.owner_reply, settings.safety)
            review = Review(
                review_id=raw.review_id,
                place_id=raw.place_id,
                rating=raw.rating,
                date=raw.date,
                reviewer_name=raw.reviewer_name,
                reviewer_profile_url=raw.reviewer_profile_url,
                text=raw.text,
                summary=humor.summary,
                owner_reply=raw.owner_reply,
                review_url=raw.review_url,
                humor_score=humor.score,
                humor_notes=humor.notes,
                safety_label=safety.label,
                safety_notes=safety.notes,
                tags=",".join(humor.tags),
            )
            storage.upsert_review(review)
            if humor.score < humor_threshold:
                storage.update_status(review.review_id, "rejected")
            collected_count += 1
            if humor.score > humor_threshold:
                funny_count += 1
            if funny_count >= target_funny_reviews:
                break
        if funny_count >= target_funny_reviews:
            break

    storage.record_celebration_run(
        year=None,
        month=None,
        day=None,
        target_funny_reviews=target_funny_reviews,
        humor_threshold=humor_threshold,
        observances_json=json.dumps(
            [line.strip() for line in celebrations_text.splitlines() if line.strip()],
            ensure_ascii=False,
        ),
        strategy_json=json.dumps(_strategy_to_dict(strategy), ensure_ascii=False),
        discovered_count=discovered_count,
        collected_count=collected_count,
        funny_count=funny_count,
    )
    print(
        f"Themed celebrations run completed: observances={len([line for line in celebrations_text.splitlines() if line.strip()])}, "
        f"discovered={discovered_count}, collected={collected_count}, funny={funny_count}"
    )


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _emit_progress(event: str, payload: dict) -> None:
    log_path = os.getenv("PROGRESS_LOG")
    if not log_path:
        return
    record = {"event": event, **payload}
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Humorous Review Scout")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover")
    discover.add_argument(
        "--no-api",
        action="store_true",
        help="Skip external API calls (SerpApi/OpenAI) for rehearsal runs.",
    )
    collect = sub.add_parser("collect")
    collect.add_argument(
        "--no-api",
        action="store_true",
        help="Skip external API calls (SerpApi/OpenAI) for rehearsal runs.",
    )
    shortlist = sub.add_parser("shortlist")
    shortlist.add_argument(
        "--dry-run",
        action="store_true",
        help="Build shortlist from existing DB without fetching new data.",
    )
    weekly = sub.add_parser("weekly")
    weekly.add_argument(
        "--no-api",
        action="store_true",
        help="Run weekly rehearsal from existing DB without external API calls.",
    )

    add_place = sub.add_parser("add-place")
    add_place.add_argument("place_id")

    set_status = sub.add_parser("set-status")
    set_status.add_argument("review_id")
    set_status.add_argument("status", choices=["new", "selected", "used", "discarded"])

    rescore_llm_errors = sub.add_parser("rescore-llm-errors")
    rescore_llm_errors.add_argument("--limit", type=int)

    themed_celebrations = sub.add_parser("themed-celebrations")
    themed_celebrations.add_argument("--celebrations")
    themed_celebrations.add_argument("--celebrations-file")
    themed_celebrations.add_argument("--target", type=int, default=10)
    themed_celebrations.add_argument("--threshold", type=int, default=60)
    themed_celebrations.add_argument("--max-searches", type=int, default=15)
    themed_celebrations.add_argument("--max-places", type=int, default=12)
    themed_celebrations.add_argument("--max-reviews-per-place", type=int, default=10)

    args = parser.parse_args()

    _load_env(Path(".env"))
    settings = load_settings()
    settings.app.data_dir.mkdir(parents=True, exist_ok=True)

    storage = Storage(settings.app.data_dir / "humor_reviews.db")

    if args.command == "discover":
        if args.no_api:
            print("Discovery skipped (--no-api).")
            return
        count, _, _ = run_discovery(storage, settings)
        print(f"Discovered {count} places")
    elif args.command == "collect":
        if args.no_api:
            print("Collection skipped (--no-api).")
            return
        count = run_collection(storage, settings)
        print(f"Collected {count} reviews")
    elif args.command == "shortlist":
        run_shortlist(storage, settings, dry_run=args.dry_run)
    elif args.command == "weekly":
        if args.no_api:
            _emit_progress("run_started", {"message": "weekly_no_api"})
            run_shortlist(storage, settings, dry_run=True)
            _emit_progress("run_complete", {"discovered": 0, "collected": 0, "mode": "no_api"})
            print("Weekly rehearsal complete (--no-api). No external API calls were made.")
            return
        run_weekly(storage, settings)
    elif args.command == "add-place":
        run_add_place(storage, args.place_id)
    elif args.command == "set-status":
        run_set_status(storage, args.review_id, args.status)
    elif args.command == "rescore-llm-errors":
        run_rescore_llm_errors(storage, settings, limit=args.limit)
    elif args.command == "themed-celebrations":
        celebrations_text = ""
        if args.celebrations:
            celebrations_text = args.celebrations
        elif args.celebrations_file:
            celebrations_text = Path(args.celebrations_file).read_text(encoding="utf-8")
        run_themed_celebrations(
            storage,
            settings,
            celebrations_text=celebrations_text,
            target_funny_reviews=args.target,
            humor_threshold=args.threshold,
            max_searches=args.max_searches,
            max_places=args.max_places,
            max_reviews_per_place=args.max_reviews_per_place,
        )


def _strategy_to_dict(strategy: CelebrationStrategy) -> dict:
    return {
        "selected_observances": strategy.selected_observances,
        "discarded_observances": strategy.discarded_observances,
        "notes": strategy.notes,
        "searches": [
            {"query": item.query, "region": item.region, "rationale": item.rationale}
            for item in strategy.searches
        ],
    }


if __name__ == "__main__":
    main()
