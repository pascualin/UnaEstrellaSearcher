from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class Place:
    place_id: str
    data_id: str
    name: str
    address: str
    category: str
    total_reviews: int
    last_review_date: Optional[str]
    provider: str
    place_url: Optional[str] = None


@dataclass
class Review:
    review_id: str
    place_id: str
    rating: int
    date: str
    reviewer_name: str
    reviewer_profile_url: str
    text: str
    summary: str
    owner_reply: str
    review_url: str
    humor_score: int
    humor_notes: str
    safety_label: str
    safety_notes: str
    tags: str
    translated_text: str = ""
    translated_owner_reply: str = ""
    original_text_language: str = ""
    original_owner_reply_language: str = ""


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS places (
                    place_id TEXT PRIMARY KEY,
                    data_id TEXT,
                    name TEXT,
                    address TEXT,
                    category TEXT,
                    total_reviews INTEGER,
                    last_review_date TEXT,
                    provider TEXT,
                    place_url TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    place_id TEXT,
                    rating INTEGER,
                    date TEXT,
                    reviewer_name TEXT,
                    reviewer_profile_url TEXT,
                    text TEXT,
                    owner_reply TEXT,
                    review_url TEXT,
                    humor_score INTEGER,
                    humor_notes TEXT,
                    safety_label TEXT,
                    safety_notes TEXT,
                    tags TEXT,
                    status TEXT DEFAULT 'new',
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shortlist (
                    review_id TEXT PRIMARY KEY,
                    batch_date TEXT,
                    score INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT,
                    count INTEGER,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS celebration_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    year INTEGER,
                    month INTEGER,
                    day INTEGER,
                    target_funny_reviews INTEGER,
                    humor_threshold INTEGER,
                    observances_json TEXT,
                    strategy_json TEXT,
                    discovered_count INTEGER,
                    collected_count INTEGER,
                    funny_count INTEGER,
                    created_at TEXT
                )
                """
            )
            self._ensure_place_columns(conn)
            self._ensure_review_columns(conn)

    def _ensure_place_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(places)").fetchall()}
        if "data_id" not in columns:
            conn.execute("ALTER TABLE places ADD COLUMN data_id TEXT")
        if "place_url" not in columns:
            conn.execute("ALTER TABLE places ADD COLUMN place_url TEXT")

    def _ensure_review_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(reviews)").fetchall()}
        if "reviewer_profile_url" not in columns:
            conn.execute("ALTER TABLE reviews ADD COLUMN reviewer_profile_url TEXT")
        if "summary" not in columns:
            conn.execute("ALTER TABLE reviews ADD COLUMN summary TEXT")
        if "translated_text" not in columns:
            conn.execute("ALTER TABLE reviews ADD COLUMN translated_text TEXT")
        if "translated_owner_reply" not in columns:
            conn.execute("ALTER TABLE reviews ADD COLUMN translated_owner_reply TEXT")
        if "original_text_language" not in columns:
            conn.execute("ALTER TABLE reviews ADD COLUMN original_text_language TEXT")
        if "original_owner_reply_language" not in columns:
            conn.execute("ALTER TABLE reviews ADD COLUMN original_owner_reply_language TEXT")
        self._migrate_legacy_review_status(conn, columns)

    def _migrate_legacy_review_status(
        self,
        conn: sqlite3.Connection,
        columns: set[str] | None = None,
    ) -> None:
        if columns is None:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(reviews)").fetchall()}
        has_reviewed = "reviewed" in columns
        has_selected = "selected" in columns
        if not has_reviewed and not has_selected:
            return

        selected_expr = "COALESCE(selected, 0) = 1" if has_selected else "0"
        reviewed_expr = "COALESCE(reviewed, 0) = 1" if has_reviewed else "0"
        conn.execute(
            f"""
            UPDATE reviews
            SET status = CASE
                WHEN {selected_expr} THEN 'accepted'
                WHEN {reviewed_expr} THEN 'rejected'
                ELSE ''
            END
            WHERE LOWER(COALESCE(status, '')) IN ('', 'new')
            """
        )

    def upsert_place(self, place: Place) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO places (
                    place_id, data_id, name, address, category, total_reviews,
                    last_review_date, provider, place_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(place_id) DO UPDATE SET
                    data_id=excluded.data_id,
                    name=excluded.name,
                    address=excluded.address,
                    category=excluded.category,
                    total_reviews=excluded.total_reviews,
                    last_review_date=excluded.last_review_date,
                    provider=excluded.provider,
                    place_url=excluded.place_url
                """,
                (
                    place.place_id,
                    place.data_id,
                    place.name,
                    place.address,
                    place.category,
                    place.total_reviews,
                    place.last_review_date,
                    place.provider,
                    place.place_url,
                ),
            )

    def upsert_review(self, review: Review) -> None:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO reviews (
                    review_id, place_id, rating, date, reviewer_name, reviewer_profile_url, text, summary, owner_reply,
                    translated_text, translated_owner_reply, original_text_language, original_owner_reply_language,
                    review_url, humor_score, humor_notes, safety_label, safety_notes, tags, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                    rating=excluded.rating,
                    date=excluded.date,
                    reviewer_name=excluded.reviewer_name,
                    reviewer_profile_url=excluded.reviewer_profile_url,
                    text=excluded.text,
                    summary=excluded.summary,
                    owner_reply=excluded.owner_reply,
                    translated_text=excluded.translated_text,
                    translated_owner_reply=excluded.translated_owner_reply,
                    original_text_language=excluded.original_text_language,
                    original_owner_reply_language=excluded.original_owner_reply_language,
                    review_url=excluded.review_url,
                    humor_score=excluded.humor_score,
                    humor_notes=excluded.humor_notes,
                    safety_label=excluded.safety_label,
                    safety_notes=excluded.safety_notes,
                    tags=excluded.tags,
                    updated_at=excluded.updated_at
                """,
                (
                    review.review_id,
                    review.place_id,
                    review.rating,
                    review.date,
                    review.reviewer_name,
                    review.reviewer_profile_url,
                    review.text,
                    review.summary,
                    review.owner_reply,
                    review.translated_text,
                    review.translated_owner_reply,
                    review.original_text_language,
                    review.original_owner_reply_language,
                    review.review_url,
                    review.humor_score,
                    review.humor_notes,
                    review.safety_label,
                    review.safety_notes,
                    review.tags,
                    now,
                    now,
                ),
            )

    def mark_shortlist(self, review_id: str, batch_date: str, score: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO shortlist (review_id, batch_date, score)
                VALUES (?, ?, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                    batch_date=excluded.batch_date,
                    score=excluded.score
                """,
                (review_id, batch_date, score),
            )

    def update_status(self, review_id: str, status: str) -> None:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE reviews SET status=?, updated_at=? WHERE review_id=?
                """,
                (status, now, review_id),
            )

    def update_reviewed(self, review_id: str, reviewed: bool) -> None:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE reviews SET reviewed=?, updated_at=? WHERE review_id=?
                """,
                (1 if reviewed else 0, now, review_id),
            )

    def update_selected(self, review_id: str, selected: bool) -> None:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE reviews SET selected=?, updated_at=? WHERE review_id=?
                """,
                (1 if selected else 0, now, review_id),
            )

    def record_stat(self, event: str, count: int) -> None:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO ingest_stats (event, count, created_at)
                VALUES (?, ?, ?)
                """,
                (event, count, now),
            )

    def record_celebration_run(
        self,
        year: int | None,
        month: int | None,
        day: int | None,
        target_funny_reviews: int,
        humor_threshold: int,
        observances_json: str,
        strategy_json: str,
        discovered_count: int,
        collected_count: int,
        funny_count: int,
    ) -> None:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO celebration_runs (
                    year, month, day, target_funny_reviews, humor_threshold,
                    observances_json, strategy_json, discovered_count, collected_count,
                    funny_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    year,
                    month,
                    day,
                    target_funny_reviews,
                    humor_threshold,
                    observances_json,
                    strategy_json,
                    discovered_count,
                    collected_count,
                    funny_count,
                    now,
                ),
            )

    def fetch_candidates(self, humor_threshold: int, allow_repeat: bool) -> list[Review]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if allow_repeat:
                rows = conn.execute(
                    """
                    SELECT * FROM reviews
                    WHERE humor_score >= ?
                      AND LOWER(COALESCE(status, '')) NOT IN ('rejected', 'rechazada', 'discarded')
                    ORDER BY humor_score DESC
                    """,
                    (humor_threshold,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM reviews
                    WHERE humor_score >= ?
                      AND LOWER(COALESCE(status, '')) IN ('', 'new')
                      AND review_id NOT IN (SELECT review_id FROM shortlist)
                    ORDER BY humor_score DESC
                    """,
                    (humor_threshold,),
                ).fetchall()

        return [
            Review(
                review_id=row["review_id"],
                place_id=row["place_id"],
                rating=row["rating"],
                date=row["date"],
                reviewer_name=row["reviewer_name"],
                reviewer_profile_url=row["reviewer_profile_url"] or "",
                text=row["text"],
                summary=row["summary"] or "",
                owner_reply=row["owner_reply"],
                translated_text=row["translated_text"] or "",
                translated_owner_reply=row["translated_owner_reply"] or "",
                original_text_language=row["original_text_language"] or "",
                original_owner_reply_language=row["original_owner_reply_language"] or "",
                review_url=row["review_url"],
                humor_score=row["humor_score"],
                humor_notes=row["humor_notes"],
                safety_label=row["safety_label"],
                safety_notes=row["safety_notes"],
                tags=row["tags"],
            )
            for row in rows
        ]

    def fetch_reviews_with_tag(self, tag: str) -> list[Review]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reviews
                WHERE tags LIKE ?
                ORDER BY updated_at DESC
                """,
                (f"%{tag}%",),
            ).fetchall()

        return [
            Review(
                review_id=row["review_id"],
                place_id=row["place_id"],
                rating=row["rating"],
                date=row["date"],
                reviewer_name=row["reviewer_name"],
                reviewer_profile_url=row["reviewer_profile_url"] or "",
                text=row["text"],
                summary=row["summary"] or "",
                owner_reply=row["owner_reply"],
                translated_text=row["translated_text"] or "",
                translated_owner_reply=row["translated_owner_reply"] or "",
                original_text_language=row["original_text_language"] or "",
                original_owner_reply_language=row["original_owner_reply_language"] or "",
                review_url=row["review_url"],
                humor_score=row["humor_score"],
                humor_notes=row["humor_notes"],
                safety_label=row["safety_label"],
                safety_notes=row["safety_notes"],
                tags=row["tags"],
            )
            for row in rows
        ]

    def fetch_reviews_needing_rescore(self) -> list[Review]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reviews
                WHERE tags LIKE '%llm_error%'
                   OR humor_notes LIKE 'LLM error:%'
                   OR humor_notes = 'Parse failure'
                   OR (humor_score = 0 AND tags = 'misc')
                ORDER BY updated_at DESC
                """
            ).fetchall()

        return [
            Review(
                review_id=row["review_id"],
                place_id=row["place_id"],
                rating=row["rating"],
                date=row["date"],
                reviewer_name=row["reviewer_name"],
                reviewer_profile_url=row["reviewer_profile_url"] or "",
                text=row["text"],
                summary=row["summary"] or "",
                owner_reply=row["owner_reply"],
                translated_text=row["translated_text"] or "",
                translated_owner_reply=row["translated_owner_reply"] or "",
                original_text_language=row["original_text_language"] or "",
                original_owner_reply_language=row["original_owner_reply_language"] or "",
                review_url=row["review_url"],
                humor_score=row["humor_score"],
                humor_notes=row["humor_notes"],
                safety_label=row["safety_label"],
                safety_notes=row["safety_notes"],
                tags=row["tags"],
            )
            for row in rows
        ]

    def review_exists(self, review_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT review_id FROM reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            return row is not None

    def get_place_ids(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT place_id, data_id FROM places").fetchall()
            return [row[1] or row[0] for row in rows]

    def get_place_map(self) -> dict[str, Place]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM places").fetchall()
            place_map: dict[str, Place] = {
                row["place_id"]: Place(
                    place_id=row["place_id"],
                    data_id=row["data_id"],
                    name=row["name"],
                    address=row["address"],
                    category=row["category"],
                    total_reviews=row["total_reviews"],
                    last_review_date=row["last_review_date"],
                    provider=row["provider"],
                    place_url=row["place_url"],
                )
                for row in rows
            }
            # Also map by data_id for lookups from reviews.
            for row in rows:
                data_id = row["data_id"]
                if data_id and data_id not in place_map:
                    place_map[data_id] = place_map[row["place_id"]]
            return place_map

    def iter_reviews(self) -> Iterable[Review]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM reviews").fetchall()
            for row in rows:
                yield Review(
                    review_id=row["review_id"],
                    place_id=row["place_id"],
                    rating=row["rating"],
                    date=row["date"],
                    reviewer_name=row["reviewer_name"],
                    reviewer_profile_url=row["reviewer_profile_url"] or "",
                    text=row["text"],
                    summary=row["summary"] or "",
                    owner_reply=row["owner_reply"],
                    translated_text=row["translated_text"] or "",
                    translated_owner_reply=row["translated_owner_reply"] or "",
                    original_text_language=row["original_text_language"] or "",
                    original_owner_reply_language=row["original_owner_reply_language"] or "",
                    review_url=row["review_url"],
                    humor_score=row["humor_score"],
                    humor_notes=row["humor_notes"],
                    safety_label=row["safety_label"],
                    safety_notes=row["safety_notes"],
                    tags=row["tags"],
                )
