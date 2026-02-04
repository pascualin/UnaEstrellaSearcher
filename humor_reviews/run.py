from __future__ import annotations

import argparse
import os
from pathlib import Path
from .collect import collect_reviews
from .discover import discover_places
from .humor import score_review
from .safety import assess_safety
from .settings import load_settings
from .shortlist import build_shortlist, export_shortlist, export_shortlist_html, mark_shortlist
from .storage import Place, Review, Storage


def run_discovery(storage: Storage, settings) -> int:
    count = 0
    for discovered in discover_places(settings.discovery, settings.providers):
        storage.upsert_place(discovered.place)
        count += 1
        if count >= settings.app.max_places_per_run:
            break
    return count


def run_collection(storage: Storage, settings) -> int:
    count = 0
    per_place_counts: dict[str, int] = {}
    place_ids = storage.get_place_ids()
    for raw in collect_reviews(
        place_ids,
        settings.providers,
        settings.app.max_reviews_per_place,
    ):
        if raw.rating > 2:
            continue

        per_place_counts.setdefault(raw.place_id, 0)
        if per_place_counts[raw.place_id] >= settings.app.max_reviews_per_place:
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
            owner_reply=raw.owner_reply,
            review_url=raw.review_url,
            humor_score=humor.score,
            humor_notes=humor.notes,
            safety_label=safety.label,
            safety_notes=safety.notes,
            tags=",".join(humor.tags),
        )

        storage.upsert_review(review)
        per_place_counts[raw.place_id] += 1
        count += 1

    return count


def run_shortlist(storage: Storage, settings, dry_run: bool = False) -> None:
    shortlist = build_shortlist(storage, settings.app, settings.curation)
    screenshot_map = {}
    place_map = storage.get_place_map()
    if settings.app.enable_screenshots:
        try:
            from .screenshots import capture_screenshots

            screenshot_map = capture_screenshots(
                shortlist,
                settings.app.screenshot_dir,
                settings.app.screenshot_timeout_ms,
                settings.app.screenshot_max_per_run,
                place_map,
                settings.app.screenshot_debug,
                settings.app.screenshot_mode,
            )
        except Exception as exc:
            print(f"Screenshots disabled due to error: {exc}")

    json_path, md_path = export_shortlist(shortlist, settings.app, settings.app.output_dir)
    html_path = export_shortlist_html(
        shortlist,
        settings.app,
        settings.app.output_dir,
        screenshot_map,
        place_map,
    )
    if not dry_run:
        mark_shortlist(storage, shortlist)

    suffix = " (dry-run)" if dry_run else ""
    print(f"Shortlist saved to {json_path}, {md_path}, and {html_path}{suffix}")


def run_weekly(storage: Storage, settings) -> None:
    discovered = run_discovery(storage, settings)
    collected = run_collection(storage, settings)
    run_shortlist(storage, settings)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Humorous Review Scout")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover")
    sub.add_parser("collect")
    shortlist = sub.add_parser("shortlist")
    shortlist.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate HTML/JSON/MD from existing DB without fetching new data.",
    )
    sub.add_parser("weekly")

    add_place = sub.add_parser("add-place")
    add_place.add_argument("place_id")

    set_status = sub.add_parser("set-status")
    set_status.add_argument("review_id")
    set_status.add_argument("status", choices=["new", "selected", "used", "discarded"])

    args = parser.parse_args()

    _load_env(Path(".env"))
    settings = load_settings()
    settings.app.output_dir.mkdir(parents=True, exist_ok=True)
    settings.app.data_dir.mkdir(parents=True, exist_ok=True)

    storage = Storage(settings.app.data_dir / "humor_reviews.db")

    if args.command == "discover":
        count = run_discovery(storage, settings)
        print(f"Discovered {count} places")
    elif args.command == "collect":
        count = run_collection(storage, settings)
        print(f"Collected {count} reviews")
    elif args.command == "shortlist":
        run_shortlist(storage, settings, dry_run=args.dry_run)
    elif args.command == "weekly":
        run_weekly(storage, settings)
    elif args.command == "add-place":
        run_add_place(storage, args.place_id)
    elif args.command == "set-status":
        run_set_status(storage, args.review_id, args.status)


if __name__ == "__main__":
    main()
