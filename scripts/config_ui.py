from __future__ import annotations

import ast
import base64
import hashlib
import html as html_lib
import json
import os
import re
import socket
import sqlite3
import threading
from datetime import datetime
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

from openai import OpenAI
import requests
import yaml

from humor_reviews.collect import _serpapi_reviews
from humor_reviews.humor import score_review
from humor_reviews.notion_sync import NotionSyncError, append_review_image, create_review_page
from humor_reviews.safety import assess_safety
from humor_reviews.settings import load_settings
from humor_reviews.storage import Place, Review, Storage
from humor_reviews.translation import translate_review_to_spanish


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
HOST = os.getenv("CONFIG_UI_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = 5173


CONFIG_HTML_PATH = ROOT / "scripts" / "config_view.html"
RUN_HTML_PATH = ROOT / "scripts" / "run_view.html"
IMPORT_REVIEW_HTML_PATH = ROOT / "scripts" / "import_review_view.html"
DB_HTML_PATH = ROOT / "scripts" / "db_view.html"
REVIEW_HTML_PATH = ROOT / "scripts" / "review_detail.html"
REVIEW_IMPORT_MAX_PAGES = 24
REVIEW_IMPORT_SORT_ORDERS = [
    None,
    "newestFirst",
    "ratingLow",
    "ratingHigh",
]
GOOGLE_REVIEW_HOSTS = {
    "google.com",
    "www.google.com",
    "maps.google.com",
    "www.maps.google.com",
    "maps.app.goo.gl",
}
IMAGE_IMPORT_MAX_BYTES = 12 * 1024 * 1024
IMAGE_IMPORT_MAX_FILES = 8
IMAGE_IMPORT_TOTAL_MAX_BYTES = 32 * 1024 * 1024


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


def _load_html(path: Path, fallback: str) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"<h1>{fallback}</h1>"


def _load_config() -> Dict[str, Any]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return raw or {}


def _write_config(payload: Dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _db_path() -> Path:
    cfg = _load_config()
    data_dir = (cfg.get("app", {}) or {}).get("data_dir", "data")
    return (ROOT / data_dir / "humor_reviews.db").resolve()


def _storage() -> Storage:
    return Storage(_db_path())


def _progress_log_path() -> Path:
    cfg = _load_config()
    data_dir = (cfg.get("app", {}) or {}).get("data_dir", "data")
    return (ROOT / data_dir / "progress.log").resolve()


def _append_progress_log(path: Path, event: str, payload: dict) -> None:
    record = {"event": event, **payload}
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def _ui_auth_credentials() -> tuple[str, str] | None:
    username = os.getenv("CONFIG_UI_USERNAME", "").strip()
    password = os.getenv("CONFIG_UI_PASSWORD", "").strip()
    if username and password:
        return username, password
    return None


def _is_authorized(headers) -> bool:
    credentials = _ui_auth_credentials()
    if not credentials:
        return True
    provided = str(headers.get("Authorization") or "").strip()
    if not provided.startswith("Basic "):
        return False
    encoded = provided[6:].strip()
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return False
    return decoded == f"{credentials[0]}:{credentials[1]}"


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def _ensure_review_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reviews)").fetchall()}
    if "summary" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN summary TEXT")
    if "submitted_by" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN submitted_by TEXT")
    if "notion_page_id" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN notion_page_id TEXT")
    if "notion_page_url" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN notion_page_url TEXT")
    if "notion_image_uploaded_at" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN notion_image_uploaded_at TEXT")
    if "translated_text" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN translated_text TEXT")
    if "translated_owner_reply" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN translated_owner_reply TEXT")
    if "original_text_language" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN original_text_language TEXT")
    if "original_owner_reply_language" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN original_owner_reply_language TEXT")
    _migrate_legacy_review_status(conn, columns)


def _migrate_legacy_review_status(conn: sqlite3.Connection, columns: set[str] | None = None) -> None:
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


def _normalize_status(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"accepted", "aceptada", "selected", "used"}:
        return "accepted"
    if value in {"rejected", "rechazada", "discarded"}:
        return "rejected"
    return ""


def _status_label(raw: str | None) -> str:
    normalized = _normalize_status(raw)
    if normalized == "accepted":
        return "Aceptada"
    if normalized == "rejected":
        return "Rechazada"
    return "Vacío"


def _status_filter_sql(status_filter: str) -> tuple[str, tuple]:
    normalized = str(status_filter or "pending").strip().lower()
    if normalized == "accepted":
        return "WHERE LOWER(COALESCE(r.status, '')) IN ('accepted', 'aceptada', 'selected', 'used')", ()
    if normalized == "rejected":
        return "WHERE LOWER(COALESCE(r.status, '')) IN ('rejected', 'rechazada', 'discarded')", ()
    if normalized == "all":
        return "", ()
    return "WHERE LOWER(COALESCE(r.status, '')) IN ('', 'new')", ()


def _fetch_db_snapshot(sort_by: str, status_filter: str) -> Dict[str, Any]:
    db_path = _db_path()
    if not db_path.exists():
        return {
            "summary": {"places": 0, "reviews": 0, "shortlist": 0, "pending": 0, "accepted": 0, "rejected": 0},
            "places": [],
            "reviews": [],
            "shortlist": [],
        }

    def _rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.OperationalError:
                return []

    with sqlite3.connect(db_path) as conn:
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
        _ensure_review_columns(conn)

    def _scalar(sql: str) -> int:
        rows = _rows(sql)
        if rows and "count" in rows[0]:
            return int(rows[0]["count"])
        return 0

    summary = {
        "places": _rows("SELECT COUNT(*) as count FROM places")[0]["count"],
        "reviews": _rows("SELECT COUNT(*) as count FROM reviews")[0]["count"],
        "shortlist": _rows("SELECT COUNT(*) as count FROM shortlist")[0]["count"],
        "pending": _scalar("SELECT COUNT(*) as count FROM reviews WHERE LOWER(COALESCE(status, '')) IN ('', 'new')"),
        "accepted": _scalar("SELECT COUNT(*) as count FROM reviews WHERE LOWER(COALESCE(status, '')) IN ('accepted', 'aceptada', 'selected', 'used')"),
        "rejected": _scalar("SELECT COUNT(*) as count FROM reviews WHERE LOWER(COALESCE(status, '')) IN ('rejected', 'rechazada', 'discarded')"),
    }
    summary["empty_reviews_skipped_total"] = _scalar(
        "SELECT COALESCE(SUM(count), 0) as count FROM ingest_stats WHERE event = 'empty_reviews_skipped'"
    )
    summary["empty_reviews_skipped_last"] = _scalar(
        "SELECT count as count FROM ingest_stats WHERE event = 'empty_reviews_skipped' ORDER BY created_at DESC LIMIT 1"
    )
    order_by = "r.updated_at DESC"
    if sort_by == "humor_score":
        order_by = "r.humor_score DESC, r.updated_at DESC"
    review_filter, review_params = _status_filter_sql(status_filter)

    reviews = _rows(
        "SELECT "
        "r.review_id, r.rating, r.date, r.humor_score, r.safety_label, r.status, r.updated_at, r.review_url, "
        "p.name as place_name, p.address as place_locality "
        "FROM reviews r "
        "LEFT JOIN places p ON (p.place_id = r.place_id OR p.data_id = r.place_id) "
        f"{review_filter} "
        f"ORDER BY {order_by} LIMIT 200",
        review_params,
    )
    for row in reviews:
        row["status_label"] = _status_label(row.get("status"))
    shortlist = _rows(
        "SELECT review_id, batch_date, score FROM shortlist ORDER BY batch_date DESC LIMIT 200"
    )
    return {
        "summary": summary,
        "reviews": reviews,
        "shortlist": shortlist,
    }


def _fetch_review_statuses(review_ids: List[str]) -> Dict[str, str]:
    normalized_ids: List[str] = []
    seen: set[str] = set()
    for review_id in review_ids:
        value = str(review_id or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized_ids.append(value)
    if not normalized_ids:
        return {}

    db_path = _db_path()
    if not db_path.exists():
        return {}

    placeholders = ", ".join("?" for _ in normalized_ids)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_review_columns(conn)
        rows = conn.execute(
            f"SELECT review_id, status FROM reviews WHERE review_id IN ({placeholders})",
            tuple(normalized_ids),
        ).fetchall()
    return {str(row["review_id"]): _normalize_status(row["status"]) for row in rows}


def _review_filters(sort_by: str, status_filter: str) -> tuple[str, str, tuple]:
    order_by = "r.updated_at DESC"
    if sort_by == "humor_score":
        order_by = "r.humor_score DESC, r.updated_at DESC"
    review_filter, params = _status_filter_sql(status_filter)
    return order_by, review_filter, params


def _review_navigation(
    review_id: str,
    sort_by: str,
    status_filter: str,
) -> tuple[str, str]:
    db_path = _db_path()
    if not db_path.exists():
        return "", ""
    order_by, review_filter, review_params = _review_filters(sort_by, status_filter)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT r.review_id FROM reviews r "
            f"{review_filter} "
            f"ORDER BY {order_by} LIMIT 200",
            review_params,
        ).fetchall()
    ids = [str(row["review_id"]) for row in rows]
    try:
        index = ids.index(review_id)
    except ValueError:
        return "", ""
    prev_id = ids[index - 1] if index > 0 else ""
    next_id = ids[index + 1] if index + 1 < len(ids) else ""
    return prev_id, next_id


def _render_review_detail(
    review_id: str,
    sort_by: str = "updated_at",
    status_filter: str = "pending",
) -> str | None:
    db_path = _db_path()
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_review_columns(conn)
        try:
            row = conn.execute(
                """
                SELECT
                    r.review_id, r.place_id, r.rating, r.date, r.reviewer_name, r.reviewer_profile_url,
                    r.text, r.translated_text, r.original_text_language, r.summary, r.owner_reply, r.translated_owner_reply, r.original_owner_reply_language, r.review_url, r.humor_score, r.humor_notes,
                    r.safety_label, r.safety_notes, r.tags, r.status, r.updated_at, r.notion_page_url,
                    p.name as place_name, p.address as place_address, p.category as place_category
                FROM reviews r
                LEFT JOIN places p ON (p.place_id = r.place_id OR p.data_id = r.place_id)
                WHERE r.review_id = ?
                """,
                (review_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such column: r.summary" not in str(exc):
                raise
            conn.execute("ALTER TABLE reviews ADD COLUMN summary TEXT")
            row = conn.execute(
                """
                SELECT
                    r.review_id, r.place_id, r.rating, r.date, r.reviewer_name, r.reviewer_profile_url,
                    r.text, r.translated_text, r.original_text_language, r.summary, r.owner_reply, r.translated_owner_reply, r.original_owner_reply_language, r.review_url, r.humor_score, r.humor_notes,
                    r.safety_label, r.safety_notes, r.tags, r.status, r.updated_at, r.notion_page_url,
                    p.name as place_name, p.address as place_address, p.category as place_category
                FROM reviews r
                LEFT JOIN places p ON (p.place_id = r.place_id OR p.data_id = r.place_id)
                WHERE r.review_id = ?
                """,
                (review_id,),
            ).fetchone()
        if not row:
            return None
        translated_review_text, translated_owner_reply_text, original_review_language, original_owner_reply_language = _ensure_translated_texts(
            conn,
            review_id=str(row["review_id"]),
            review_text=str(row["text"] or ""),
            translated_review_text=str(row["translated_text"] or ""),
            original_review_language=str(row["original_text_language"] or ""),
            owner_reply_raw=str(row["owner_reply"] or ""),
            translated_owner_reply_text=str(row["translated_owner_reply"] or ""),
            original_owner_reply_language=str(row["original_owner_reply_language"] or ""),
        )

    def _esc(value: Any) -> str:
        return html_lib.escape(str(value or ""))

    place_name_raw = str(row["place_name"] or "Sitio").strip()
    place_name = _esc(place_name_raw)
    place_address_raw = str(row["place_address"] or "").strip()
    place_address = _esc(place_address_raw)
    place_category = _esc(str(row["place_category"] or "Lugar").strip() or "Lugar")
    review_url = _esc(row["review_url"] or "")
    notion_page_url = _esc(row["notion_page_url"] or "")
    reviewer_raw = str(row["reviewer_name"] or "Anonymous").strip()
    reviewer_url_raw = str(row["reviewer_profile_url"] or "").strip()
    if reviewer_raw.startswith("{"):
        parsed = _parse_reviewer_payload(reviewer_raw)
        if parsed:
            reviewer_raw, reviewer_url_raw = parsed
    reviewer = _esc(reviewer_raw)
    reviewer_url = _esc(reviewer_url_raw)
    summary = _esc(row["summary"] or "")
    review_text = _esc(translated_review_text or row["text"] or "")
    owner_reply_raw = str(row["owner_reply"] or "").strip()
    owner_reply_text_raw, owner_reply_date_raw = _split_owner_reply(owner_reply_raw)
    owner_reply_display_text = translated_owner_reply_text or owner_reply_text_raw
    owner_reply = _esc(owner_reply_display_text)
    review_language_badge = _translation_badge_html(original_review_language)
    owner_reply_language_badge = _translation_badge_html(original_owner_reply_language)
    owner_reply_date = _esc(owner_reply_date_raw)
    tags_raw = str(row["tags"] or "").strip() or "misc"
    tags = _esc(tags_raw)
    tags_title = _esc(tags_raw)
    reviewer_html = f'<a href="{reviewer_url}">{reviewer}</a>' if reviewer_url else reviewer
    maps_link = (
        f'<a class="gm-place-link" href="{review_url}" target="_blank" rel="noopener noreferrer">'
        "DETALLES DEL LUGAR</a>"
        if review_url
        else ""
    )
    if notion_page_url:
        maps_link += (
            (' · ' if maps_link else '')
            + f'<a class="gm-place-link" href="{notion_page_url}" target="_blank" rel="noopener noreferrer">NOTION</a>'
        )
    place_avatar = _esc(_avatar_text(place_name_raw))
    reviewer_avatar = _esc(_avatar_text(reviewer_raw))
    rating_stars = _render_stars(int(row["rating"] or 0))
    summary_html = ""
    if summary and len(str(row["text"] or "")) >= 420:
        summary_html = (
            '<section class="gm-summary-card">'
            '<h3>Resumen de la IA</h3>'
            f'<div class="gm-summary-text">{summary}</div>'
            "</section>"
        )
    owner_reply_html = (
        '<section class="gm-owner-reply" id="owner-reply-card">'
        f'<h3>Respuesta del propietario{owner_reply_language_badge}</h3>'
        f'<div class="gm-owner-reply-text" id="owner-reply-text">{owner_reply}</div>'
        f'<div class="gm-owner-reply-date" id="owner-reply-date">{owner_reply_date}</div>'
        "</section>"
        if owner_reply
        else ""
    )
    copy_review_payload = _esc(
        json.dumps(
            {
                "owner_reply_text": owner_reply_text_raw,
                "owner_reply_text_translated": owner_reply_display_text,
                "owner_reply_date": owner_reply_date_raw,
            },
            ensure_ascii=False,
        )
    )
    status_value = _normalize_status(row["status"])
    status_label = _status_label(row["status"])
    prev_review_id, next_review_id = _review_navigation(
        review_id,
        sort_by,
        status_filter,
    )

    template = _load_html(REVIEW_HTML_PATH, "Missing review_detail.html")
    updated_at = _format_datetime(str(row["updated_at"] or ""))
    nav_base = (
        f"&sort={quote(sort_by, safe='')}"
        f"&status={quote(status_filter, safe='')}"
    )
    prev_review_href = f"/review?id={quote(prev_review_id, safe='')}{nav_base}" if prev_review_id else "#"
    next_review_href = f"/review?id={quote(next_review_id, safe='')}{nav_base}" if next_review_id else "#"
    html = (
        template.replace("{{place_name}}", place_name)
        .replace("{{prev_review_href}}", prev_review_href)
        .replace("{{next_review_href}}", next_review_href)
        .replace("{{prev_review_class}}", "" if prev_review_id else "is-disabled")
        .replace("{{next_review_class}}", "" if next_review_id else "is-disabled")
        .replace("{{place_category}}", place_category)
        .replace("{{place_address}}", place_address or "Sin dirección")
        .replace("{{place_avatar}}", place_avatar)
        .replace("{{reviewer_avatar}}", reviewer_avatar)
        .replace("{{rating_stars}}", rating_stars)
        .replace("{{summary_html}}", summary_html)
        .replace("{{review_text}}", review_text or "(sin texto)")
        .replace("{{review_language_badge}}", review_language_badge)
        .replace("{{owner_reply_html}}", owner_reply_html)
        .replace("{{copy_review_payload}}", copy_review_payload)
        .replace("{{humor_score}}", _esc(row["humor_score"]))
        .replace("{{safety_label}}", _esc(row["safety_label"]))
        .replace("{{safety_notes}}", _esc(row["safety_notes"]))
        .replace("{{tags}}", tags or "misc")
        .replace("{{tags_title}}", tags_title)
        .replace("{{humor_notes}}", _esc(row["humor_notes"]) or "Sin nota adicional.")
        .replace("{{date}}", _esc(row["date"]))
        .replace("{{rating}}", _esc(row["rating"]))
        .replace("{{status}}", _esc(row["status"]))
        .replace("{{status_label}}", status_label)
        .replace("{{status_value}}", status_value)
        .replace("{{review_id}}", _esc(row["review_id"]))
        .replace("{{reviewer_html}}", reviewer_html)
        .replace("{{updated_at}}", _esc(updated_at))
        .replace("{{maps_link}}", maps_link)
    )
    return html.replace("{{copy_review_payload}}", "{}")


def _set_review_status(review_id: str, status: str) -> bool:
    db_path = _db_path()
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        _ensure_review_columns(conn)
        now = datetime.utcnow().isoformat()
        cur = conn.execute(
            "UPDATE reviews SET status=?, updated_at=? WHERE review_id=?",
            (status, now, review_id),
        )
        return cur.rowcount > 0


def _fetch_review_for_notion(review_id: str) -> dict[str, Any] | None:
    db_path = _db_path()
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_review_columns(conn)
        row = conn.execute(
            """
            SELECT
                r.review_id, r.reviewer_name, r.text, r.translated_text, r.original_text_language, r.owner_reply, r.translated_owner_reply, r.original_owner_reply_language, r.review_url,
                r.submitted_by, r.humor_score, r.safety_label, r.tags, r.notion_page_id, r.notion_page_url,
                r.notion_image_uploaded_at,
                p.name as place_name, p.address as place_address
            FROM reviews r
            LEFT JOIN places p ON (p.place_id = r.place_id OR p.data_id = r.place_id)
            WHERE r.review_id = ?
            """,
            (review_id,),
        ).fetchone()
    if not row:
        return None
    owner_reply_text, _ = _split_owner_reply(str(row["owner_reply"] or ""))
    with sqlite3.connect(db_path) as conn:
        _ensure_review_columns(conn)
        translated_review_text, translated_owner_reply_text, original_review_language, original_owner_reply_language = _ensure_translated_texts(
            conn,
            review_id=str(row["review_id"]),
            review_text=str(row["text"] or ""),
            translated_review_text=str(row["translated_text"] or ""),
            original_review_language=str(row["original_text_language"] or ""),
            owner_reply_raw=str(row["owner_reply"] or ""),
            translated_owner_reply_text=str(row["translated_owner_reply"] or ""),
            original_owner_reply_language=str(row["original_owner_reply_language"] or ""),
        )
    return {
        "review_id": row["review_id"],
        "reviewer_name": row["reviewer_name"] or "",
        "submitted_by": row["submitted_by"] or "",
        "review_text": translated_review_text or row["text"] or "",
        "owner_reply_text": translated_owner_reply_text or owner_reply_text,
        "review_language": original_review_language,
        "owner_reply_language": original_owner_reply_language,
        "review_url": row["review_url"] or "",
        "humor_score": row["humor_score"] or 0,
        "safety_label": row["safety_label"] or "",
        "tags": row["tags"] or "",
        "place_name": row["place_name"] or "",
        "place_address": row["place_address"] or "",
        "notion_page_id": row["notion_page_id"] or "",
        "notion_page_url": row["notion_page_url"] or "",
        "notion_image_uploaded_at": row["notion_image_uploaded_at"] or "",
    }


def _store_notion_page(review_id: str, page_id: str, page_url: str) -> None:
    db_path = _db_path()
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        _ensure_review_columns(conn)
        now = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE reviews SET notion_page_id=?, notion_page_url=?, updated_at=? WHERE review_id=?",
            (page_id, page_url, now, review_id),
        )


def _store_notion_image_uploaded(review_id: str) -> None:
    db_path = _db_path()
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        _ensure_review_columns(conn)
        now = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE reviews SET notion_image_uploaded_at=?, updated_at=? WHERE review_id=?",
            (now, now, review_id),
        )


def _store_review_translation(
    conn: sqlite3.Connection,
    review_id: str,
    translated_text: str,
    translated_owner_reply: str,
    original_text_language: str,
    original_owner_reply_language: str,
) -> None:
    now = datetime.utcnow().isoformat()
    conn.execute(
        """
        UPDATE reviews
        SET translated_text=?, translated_owner_reply=?, original_text_language=?, original_owner_reply_language=?, updated_at=?
        WHERE review_id=?
        """,
        (translated_text, translated_owner_reply, original_text_language, original_owner_reply_language, now, review_id),
    )


def _ensure_translated_texts(
    conn: sqlite3.Connection,
    review_id: str,
    review_text: str,
    translated_review_text: str,
    original_review_language: str,
    owner_reply_raw: str,
    translated_owner_reply_text: str,
    original_owner_reply_language: str,
) -> tuple[str, str, str, str]:
    review_text = str(review_text or "").strip()
    translated_review_text = str(translated_review_text or "").strip()
    original_review_language = str(original_review_language or "").strip().lower()
    owner_reply_text, _ = _split_owner_reply(str(owner_reply_raw or ""))
    translated_owner_reply_text = str(translated_owner_reply_text or "").strip()
    original_owner_reply_language = str(original_owner_reply_language or "").strip().lower()

    if (
        translated_review_text
        and original_review_language
        and (owner_reply_text == "" or (translated_owner_reply_text and original_owner_reply_language))
    ):
        return (
            translated_review_text,
            translated_owner_reply_text,
            original_review_language,
            original_owner_reply_language,
        )

    result = translate_review_to_spanish(review_text, owner_reply_text)
    final_review_text = str(result.review_text_es or review_text).strip() or review_text
    final_owner_reply_text = str(result.owner_reply_es or owner_reply_text).strip() or owner_reply_text
    final_review_language = str(result.review_language or "").strip().lower()
    final_owner_reply_language = str(result.owner_reply_language or "").strip().lower()
    _store_review_translation(
        conn,
        review_id,
        final_review_text,
        final_owner_reply_text,
        final_review_language,
        final_owner_reply_language,
    )
    return final_review_text, final_owner_reply_text, final_review_language, final_owner_reply_language


def _translation_badge_html(language_code: str) -> str:
    code = str(language_code or "").strip().lower()
    if not code or code == "es":
        return ""
    flag, label = _language_badge_parts(code)
    return f' <span class="gm-translation-badge" title="Traducido del {html_lib.escape(label)}">{flag}</span>'


def _language_badge_parts(language_code: str) -> tuple[str, str]:
    mapping = {
        "en": ("🇬🇧", "inglés"),
        "fr": ("🇫🇷", "francés"),
        "de": ("🇩🇪", "alemán"),
        "it": ("🇮🇹", "italiano"),
        "pt": ("🇵🇹", "portugués"),
        "zh": ("🇨🇳", "chino"),
        "zh-cn": ("🇨🇳", "chino"),
        "zh-tw": ("🇹🇼", "chino tradicional"),
        "ja": ("🇯🇵", "japonés"),
        "ko": ("🇰🇷", "coreano"),
        "ru": ("🇷🇺", "ruso"),
        "ar": ("🇸🇦", "árabe"),
        "nl": ("🇳🇱", "neerlandés"),
        "pl": ("🇵🇱", "polaco"),
        "tr": ("🇹🇷", "turco"),
    }
    return mapping.get(language_code, ("🌐", language_code.upper()))


def _sanitize_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-._")
    return cleaned or "review"


def _normalize_free_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _data_uri_from_image_bytes(image_bytes: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _openai_api_key(settings) -> str:
    return os.getenv(settings.scoring.api_key_env, "").strip()


def _manual_place_id(place_name: str, review_url: str) -> str:
    base = _normalize_free_text(place_name).lower() or _normalize_review_url(review_url) or "manual-image"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    return f"manual-image-place:{digest}"


def _manual_review_id(
    place_name: str,
    reviewer_name: str,
    review_text: str,
    date: str,
    rating: int,
    review_url: str,
) -> str:
    if review_url:
        digest_source = _normalize_review_url(review_url)
    else:
        digest_source = " | ".join(
            [
                _normalize_free_text(place_name).lower(),
                _normalize_free_text(reviewer_name).lower(),
                _normalize_free_text(review_text).lower(),
                _normalize_free_text(date).lower(),
                str(int(rating or 0)),
            ]
        )
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:24]
    return f"manual-image-review:{digest}"


def _owner_reply_storage_value(text: str, date: str) -> str:
    reply_text = str(text or "").strip()
    reply_date = str(date or "").strip()
    if not reply_text:
        return ""
    if reply_date:
        return str({"text": reply_text, "date": reply_date})
    return reply_text


def _infer_image_mime_type(image_bytes: bytes, provided: str = "") -> str:
    provided = str(provided or "").strip().lower()
    if provided.startswith("image/"):
        return provided
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _extract_review_from_images(
    images: list[dict[str, Any]],
    review_url: str = "",
) -> dict[str, Any]:
    if not images:
        raise RuntimeError("No hay capturas para analizar.")
    settings = load_settings(CONFIG_PATH)
    api_key = _openai_api_key(settings)
    if not api_key:
        raise RuntimeError(f"Falta la variable de entorno {settings.scoring.api_key_env}.")

    client = OpenAI(api_key=api_key)
    prompt_text = (
        "Extrae los datos visibles de una o varias capturas consecutivas de una misma reseña de Google Maps. "
        "Todas las imágenes pertenecen a la misma reseña y pueden ser partes distintas de la misma pantalla. "
        "Combina la información de todas sin duplicar texto. "
        "Devuelve solo JSON válido. "
        "Si un dato no es visible, devuelve cadena vacía. "
        "No inventes información. "
        "Usa estos campos exactos: "
        "place_name, reviewer_name, rating, date, review_text, owner_reply_text, owner_reply_date, place_address. "
        "rating debe ser entero entre 1 y 5 si se ve claramente; si no se ve, devuelve 0."
    )
    request_payload = {
        "model": settings.scoring.model,
        "messages": [
            {
                "role": "system",
                "content": "Eres un extractor OCR preciso. Devuelve solo JSON.",
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt_text}]
                + [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": _data_uri_from_image_bytes(
                                item["bytes"],
                                _infer_image_mime_type(item["bytes"], str(item.get("mime_type") or "")),
                            )
                        },
                    }
                    for item in images
                ],
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "google_maps_review_from_image",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "place_name": {"type": "string"},
                        "reviewer_name": {"type": "string"},
                        "rating": {"type": "integer", "minimum": 0, "maximum": 5},
                        "date": {"type": "string"},
                        "review_text": {"type": "string"},
                        "owner_reply_text": {"type": "string"},
                        "owner_reply_date": {"type": "string"},
                        "place_address": {"type": "string"},
                    },
                    "required": [
                        "place_name",
                        "reviewer_name",
                        "rating",
                        "date",
                        "review_text",
                        "owner_reply_text",
                        "owner_reply_date",
                        "place_address",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "temperature": 0,
        "max_completion_tokens": 1200,
    }
    response = client.chat.completions.create(
        model=request_payload["model"],
        messages=request_payload["messages"],
        response_format=request_payload["response_format"],
        temperature=request_payload["temperature"],
        max_completion_tokens=request_payload["max_completion_tokens"],
    )
    content = response.choices[0].message.content or ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        content = "\n".join(parts)
    payload = json.loads(str(content).strip())
    if not isinstance(payload, dict):
        raise RuntimeError("No se pudo interpretar la reseña desde la captura.")

    place_name = _normalize_free_text(payload.get("place_name"))
    reviewer_name = _normalize_free_text(payload.get("reviewer_name"))
    review_text = str(payload.get("review_text") or "").strip()
    owner_reply_text = str(payload.get("owner_reply_text") or "").strip()
    owner_reply_date = _normalize_free_text(payload.get("owner_reply_date"))
    review_date = _normalize_free_text(payload.get("date"))
    place_address = _normalize_free_text(payload.get("place_address"))
    try:
        rating = int(payload.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    rating = max(0, min(5, rating))

    if not review_text:
        raise RuntimeError("No pude leer el texto de la reseña en la captura.")
    if rating <= 0:
        raise RuntimeError("No pude identificar claramente la puntuación en estrellas.")
    if not place_name:
        place_name = "Sitio importado desde captura"
    if not reviewer_name:
        reviewer_name = "Autor desconocido"
    if not review_date:
        review_date = datetime.utcnow().date().isoformat()

    return {
        "place_name": place_name,
        "reviewer_name": reviewer_name,
        "rating": rating,
        "date": review_date,
        "review_text": review_text,
        "owner_reply_text": owner_reply_text,
        "owner_reply_date": owner_reply_date,
        "place_address": place_address,
        "review_url": _normalize_review_url(review_url),
    }


def _import_review_from_images(
    images: list[dict[str, Any]],
    review_url: str = "",
    submitted_by: str = "",
) -> dict[str, Any]:
    if not images:
        raise ValueError("Selecciona una captura antes de importar.")
    if len(images) > IMAGE_IMPORT_MAX_FILES:
        raise ValueError(f"Demasiadas capturas. Usa como máximo {IMAGE_IMPORT_MAX_FILES}.")
    total_bytes = 0
    for item in images:
        image_bytes = bytes(item.get("bytes") or b"")
        if not image_bytes:
            raise ValueError("Una de las capturas no se pudo leer correctamente.")
        if len(image_bytes) > IMAGE_IMPORT_MAX_BYTES:
            raise ValueError("Una de las capturas es demasiado grande. Usa imágenes de menos de 12 MB.")
        total_bytes += len(image_bytes)
    if total_bytes > IMAGE_IMPORT_TOTAL_MAX_BYTES:
        raise ValueError("Las capturas pesan demasiado en conjunto. Reduce la cantidad o el tamaño.")

    extracted = _extract_review_from_images(images, review_url=review_url)
    submitted_by = _normalize_free_text(submitted_by)
    settings = load_settings(CONFIG_PATH)
    owner_reply = _owner_reply_storage_value(
        extracted["owner_reply_text"],
        extracted["owner_reply_date"],
    )
    humor = score_review(extracted["review_text"], owner_reply, extracted["rating"], settings.scoring)
    safety = assess_safety(extracted["review_text"], owner_reply, settings.safety)

    place_id = _manual_place_id(extracted["place_name"], extracted["review_url"])
    review_id = _manual_review_id(
        extracted["place_name"],
        extracted["reviewer_name"],
        extracted["review_text"],
        extracted["date"],
        extracted["rating"],
        extracted["review_url"],
    )

    storage = _storage()
    storage.upsert_place(
        Place(
            place_id=place_id,
            data_id=place_id,
            name=extracted["place_name"],
            address=extracted["place_address"],
            category="manual_image",
            total_reviews=0,
            last_review_date=extracted["date"],
            provider="manual_image",
            place_url="",
        )
    )
    already_exists = storage.review_exists(review_id)
    storage.upsert_review(
        Review(
            review_id=review_id,
            place_id=place_id,
            rating=extracted["rating"],
            date=extracted["date"],
            reviewer_name=extracted["reviewer_name"],
            reviewer_profile_url="",
            text=extracted["review_text"],
            summary=humor.summary,
            owner_reply=owner_reply,
            review_url=extracted["review_url"],
            humor_score=humor.score,
            humor_notes=humor.notes,
            safety_label=safety.label,
            safety_notes=safety.notes,
            tags=",".join(humor.tags),
            submitted_by=submitted_by,
        )
    )
    if humor.score < settings.app.humor_threshold:
        storage.update_status(review_id, "rejected")

    return {
        "ok": True,
        "already_exists": already_exists,
        "review_id": review_id,
        "place_name": extracted["place_name"],
        "reviewer_name": extracted["reviewer_name"],
        "submitted_by": submitted_by,
        "rating": extracted["rating"],
        "humor_score": humor.score,
        "detail_url": f"/review?id={quote(review_id, safe='')}",
        "source": "image",
    }


def _normalize_google_host(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized


def _is_google_review_link(url: str) -> bool:
    parts = urlsplit(str(url or "").strip())
    return _normalize_google_host(parts.netloc) in {
        "google.com",
        "maps.google.com",
        "maps.app.goo.gl",
    }


def _normalize_review_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    scheme = "https"
    host = _normalize_google_host(parts.netloc)
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query, keep_blank_values=True)
    normalized_query = []
    hl = str((query.get("hl") or [""])[0] or "").strip()
    if hl:
        normalized_query.append(("hl", hl))
    return urlunsplit((scheme, host, path, "&".join(f"{key}={quote(value, safe='')}" for key, value in normalized_query), ""))


def _review_url_key(url: str) -> str:
    normalized = _normalize_review_url(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    return f"{_normalize_google_host(parts.netloc)}{parts.path.rstrip('/')}"


def _extract_review_id_from_url(url: str) -> str:
    normalized = _normalize_review_url(url)
    if not normalized:
        return ""
    match = re.search(r"!1s([^!]+)!", normalized)
    if match:
        return match.group(1).strip()
    return ""


def _extract_cid_from_review_url(url: str) -> str:
    normalized = _normalize_review_url(url)
    if not normalized:
        return ""
    match = re.search(r"!2m1!1s(0x[0-9a-f]+:0x[0-9a-f]+)!", normalized, re.IGNORECASE)
    if not match:
        return ""
    value = match.group(1).strip().lower()
    if value.startswith("0x0:"):
        return value.split(":", 1)[1]
    return value


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


def _resolve_review_url(raw_url: str) -> tuple[str, str]:
    normalized = _normalize_review_url(raw_url)
    if not normalized:
        raise ValueError("Pega un enlace de Google Maps válido.")
    if not _is_google_review_link(normalized):
        raise ValueError("El enlace debe ser de una reseña de Google Maps.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(normalized, headers=headers, timeout=20, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException:
        return normalized, ""
    return _normalize_review_url(response.url or normalized), response.text or ""


def _extract_place_data_id_from_text(text: str, cid_hint: str = "") -> str:
    if not text:
        return ""
    candidates = list(dict.fromkeys(re.findall(r"0x[0-9a-f]+:0x[0-9a-f]+", text, flags=re.IGNORECASE)))
    if not candidates:
        return ""
    cid_hint = str(cid_hint or "").strip().lower()
    if cid_hint:
        matching = [item for item in candidates if item.lower().endswith(f":{cid_hint}")]
        if matching:
            matching.sort(key=lambda item: item.lower().startswith("0x0:"))
            return matching[0]
    non_zero = [item for item in candidates if not item.lower().startswith("0x0:")]
    if non_zero:
        return non_zero[0]
    return candidates[0]


def _resolve_place_data_id_from_db(review_url: str) -> str:
    db_path = _db_path()
    if not db_path.exists():
        return ""
    target_key = _review_url_key(review_url)
    cid_hint = _extract_cid_from_review_url(review_url)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if target_key:
            rows = conn.execute(
                "SELECT place_id, review_url FROM reviews WHERE COALESCE(review_url, '') <> ''"
            ).fetchall()
            for row in rows:
                if _review_url_key(str(row["review_url"] or "")) == target_key:
                    return str(row["place_id"] or "").strip()
        if cid_hint:
            rows = conn.execute(
                """
                SELECT place_id, data_id
                FROM places
                WHERE LOWER(COALESCE(place_id, '')) LIKE ?
                   OR LOWER(COALESCE(data_id, '')) LIKE ?
                LIMIT 1
                """,
                (f"%:{cid_hint.lower()}", f"%:{cid_hint.lower()}"),
            ).fetchall()
            if rows:
                row = rows[0]
                return str(row["data_id"] or row["place_id"] or "").strip()
    return ""


def _resolve_place_data_id(review_url: str) -> tuple[str, str]:
    resolved_url, response_text = _resolve_review_url(review_url)
    data_id = _resolve_place_data_id_from_db(resolved_url)
    if data_id:
        return resolved_url, data_id

    cid_hint = _extract_cid_from_review_url(resolved_url)
    data_id = _extract_place_data_id_from_text(response_text, cid_hint)
    if data_id:
        return resolved_url, data_id
    data_id = _extract_place_data_id_from_text(resolved_url, cid_hint)
    if data_id:
        return resolved_url, data_id
    raise RuntimeError(
        "No pude identificar el sitio de esa reseña. Prueba con el enlace completo de Google Maps."
    )


def _build_review_from_serpapi(place_data_id: str, review_payload: dict, fallback_review_url: str) -> Review:
    rating = int(review_payload.get("rating") or 0)
    review_text = str(
        review_payload.get("snippet")
        or review_payload.get("text")
        or review_payload.get("description")
        or ""
    ).strip()
    reviewer_name, reviewer_profile_url = _extract_reviewer(review_payload)
    review_date_raw = review_payload.get("date") or review_payload.get("published_date") or review_payload.get("iso_date") or ""
    review_date = str(review_date_raw).strip()
    owner_reply = str(review_payload.get("owner_response") or review_payload.get("response") or "").strip()
    review_url = str(review_payload.get("link") or fallback_review_url or "").strip()
    review_id = f"{place_data_id}:{review_url or review_date}:{reviewer_name or 'anon'}"

    settings = load_settings(CONFIG_PATH)
    humor = score_review(review_text, owner_reply, rating, settings.scoring)
    safety = assess_safety(review_text, owner_reply, settings.safety)

    return Review(
        review_id=review_id,
        place_id=place_data_id,
        rating=rating,
        date=review_date or datetime.utcnow().date().isoformat(),
        reviewer_name=reviewer_name,
        reviewer_profile_url=reviewer_profile_url,
        text=review_text,
        summary=humor.summary,
        owner_reply=owner_reply,
        review_url=review_url,
        humor_score=humor.score,
        humor_notes=humor.notes,
        safety_label=safety.label,
        safety_notes=safety.notes,
        tags=",".join(humor.tags),
    )


def _upsert_place_from_reviews_payload(storage: Storage, place_data_id: str, payload: dict, review: Review) -> None:
    place_info = payload.get("place_info") or {}
    search_metadata = payload.get("search_metadata") or {}
    total_reviews = place_info.get("reviews") or place_info.get("total_reviews") or 0
    try:
        total_reviews = int(total_reviews)
    except (TypeError, ValueError):
        total_reviews = 0
    place = Place(
        place_id=place_data_id,
        data_id=place_data_id,
        name=str(place_info.get("title") or place_info.get("name") or "Importado manualmente").strip(),
        address=str(place_info.get("address") or "").strip(),
        category=str(place_info.get("type") or "manual").strip() or "manual",
        total_reviews=total_reviews,
        last_review_date=review.date,
        provider="serpapi",
        place_url=str(place_info.get("link") or search_metadata.get("google_maps_url") or "").strip(),
    )
    storage.upsert_place(place)


def _find_review_in_serpapi(place_data_id: str, review_url: str) -> tuple[dict, dict]:
    settings = load_settings(CONFIG_PATH)
    cache_dir = settings.app.data_dir / "api_cache"
    api_key = os.getenv(settings.providers.serpapi_api_key_env, "").strip()
    target_key = _review_url_key(review_url)
    target_review_id = _extract_review_id_from_url(review_url)
    search_errors: list[str] = []

    for sort_by in REVIEW_IMPORT_SORT_ORDERS:
        next_page_token = None
        seen_tokens: set[str] = set()
        for _ in range(REVIEW_IMPORT_MAX_PAGES):
            try:
                payload = _serpapi_reviews(
                    data_id=place_data_id,
                    api_key=api_key,
                    hl=settings.providers.serpapi_hl,
                    gl=settings.providers.serpapi_gl,
                    cache_dir=cache_dir,
                    next_page_token=next_page_token,
                    sort_by=sort_by,
                    num=20 if next_page_token else None,
                )
            except Exception as exc:
                label = sort_by or "default"
                search_errors.append(f"{label}: {exc}")
                break

            for item in payload.get("reviews", []) or []:
                item_review_id = str(item.get("review_id") or "").strip()
                item_key = _review_url_key(str(item.get("link") or ""))
                if target_review_id and item_review_id and item_review_id == target_review_id:
                    return payload, item
                if target_key and item_key and item_key == target_key:
                    return payload, item

            pagination = payload.get("serpapi_pagination") or {}
            next_page_token = str(pagination.get("next_page_token") or "").strip()
            if not next_page_token or next_page_token in seen_tokens:
                break
            seen_tokens.add(next_page_token)

    if search_errors and len(search_errors) == len(REVIEW_IMPORT_SORT_ORDERS):
        raise RuntimeError(
            "SerpAPI devolvió error al buscar la reseña: " + " | ".join(search_errors[:3])
        )
    raise RuntimeError(
        "La reseña no apareció en SerpAPI ni buscando por relevancia, recientes y puntuación."
    )


def _import_review_from_url(review_url: str) -> dict[str, Any]:
    normalized_url = _normalize_review_url(review_url)
    if not normalized_url:
        raise ValueError("Pega un enlace de reseña antes de importar.")

    settings = load_settings(CONFIG_PATH)
    api_key = os.getenv(settings.providers.serpapi_api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(
            f"Falta la variable de entorno {settings.providers.serpapi_api_key_env}."
        )

    resolved_url, place_data_id = _resolve_place_data_id(normalized_url)
    payload, raw_review = _find_review_in_serpapi(place_data_id, resolved_url)
    review = _build_review_from_serpapi(place_data_id, raw_review, resolved_url)
    if not review.text:
        raise RuntimeError("La reseña existe, pero no tiene texto para puntuar.")

    storage = _storage()
    already_exists = storage.review_exists(review.review_id)
    _upsert_place_from_reviews_payload(storage, place_data_id, payload, review)
    storage.upsert_review(review)
    if review.humor_score < settings.app.humor_threshold:
        storage.update_status(review.review_id, "rejected")

    return {
        "ok": True,
        "already_exists": already_exists,
        "review_id": review.review_id,
        "place_name": (payload.get("place_info") or {}).get("title") or "",
        "reviewer_name": review.reviewer_name,
        "rating": review.rating,
        "humor_score": review.humor_score,
        "detail_url": f"/review?id={quote(review.review_id, safe='')}",
    }


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


def _format_owner_reply(raw: str) -> str:
    text, date = _split_owner_reply(raw)
    if text and date:
        return f"{text}\n\n{date}"
    return text


def _split_owner_reply(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if not raw:
        return "", ""
    if raw.startswith("{") and raw.endswith("}"):
        try:
            payload = ast.literal_eval(raw)
        except Exception:
            return raw, ""
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("snippet") or payload.get("response") or "").strip()
            date = str(payload.get("date") or payload.get("published_date") or "").strip()
            if text:
                return text, date
    return raw, ""


def _format_datetime(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%d-%m-%Y %H:%M")
    except ValueError:
        return raw


def _avatar_text(value: str) -> str:
    parts = [part for part in re.split(r"\s+", (value or "").strip()) if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


def _render_stars(rating: int) -> str:
    rating = max(0, min(5, int(rating or 0)))
    stars = []
    for idx in range(5):
        cls = "gm-star-filled" if idx < rating else "gm-star-empty"
        stars.append(f'<span class="gm-star {cls}">★</span>')
    return "".join(stars)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if _is_authorized(self.headers):
            return True
        self._send(
            401,
            b"Authentication required",
            "text/plain; charset=utf-8",
            headers={"WWW-Authenticate": 'Basic realm="Humorous Review Scout"'},
        )
        return False

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        if self.path == "/config_ui.css":
            css_path = ROOT / "scripts" / "config_ui.css"
            if css_path.exists():
                self._send(
                    200,
                    css_path.read_bytes(),
                    "text/css; charset=utf-8",
                    headers={"Cache-Control": "no-store"},
                )
                return
            self._send(404, b"Not found", "text/plain")
            return
        if self.path == "/config_ui.js":
            js_path = ROOT / "scripts" / "config_ui.js"
            if js_path.exists():
                self._send(
                    200,
                    js_path.read_bytes(),
                    "application/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-store"},
                )
                return
            self._send(404, b"Not found", "text/plain")
            return
        if self.path == "/" or self.path in {"/config", "/config/"}:
            html = _load_html(CONFIG_HTML_PATH, "Missing config_view.html")
            self._send(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
            return
        if self.path in {"/run", "/run/"}:
            html = _load_html(RUN_HTML_PATH, "Missing run_view.html")
            self._send(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
            return
        if self.path in {"/import-review", "/import-review/"}:
            html = _load_html(IMPORT_REVIEW_HTML_PATH, "Missing import_review_view.html")
            self._send(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
            return
        if self.path in {"/db", "/db/"}:
            html = _load_html(DB_HTML_PATH, "Missing db_view.html")
            self._send(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
            return
        if self.path.startswith("/review"):
            review_id = ""
            sort_by = "updated_at"
            status_filter = "pending"
            if "?" in self.path:
                query = urlsplit(self.path).query
                parsed = parse_qs(query)
                review_id = (parsed.get("id") or [""])[0]
                sort_by = (parsed.get("sort") or ["updated_at"])[0]
                status_filter = (parsed.get("status") or ["pending"])[0]
            if not review_id:
                self._send(400, b"Missing review id", "text/plain")
                return
            html = _render_review_detail(review_id, sort_by, status_filter)
            if html is None:
                self._send(404, b"Review not found", "text/plain")
                return
            self._send(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
            return
        if self.path == "/api/config":
            cfg = _load_config()
            self._send(200, json.dumps(cfg).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/api/progress"):
            log_path = _progress_log_path()
            offset = 0
            if "?" in self.path:
                query = urlsplit(self.path).query
                parsed = parse_qs(query)
                try:
                    offset = int((parsed.get("offset") or ["0"])[0])
                except ValueError:
                    offset = 0
            if not log_path.exists():
                payload = {"lines": [], "next_offset": 0}
                self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
                return
            data = log_path.read_bytes()
            if offset < 0 or offset > len(data):
                offset = 0
            chunk = data[offset:]
            text = chunk.decode("utf-8", errors="ignore")
            lines = [line for line in text.splitlines() if line.strip()]
            payload = {"lines": lines, "next_offset": len(data)}
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/api/db-data"):
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            sort_by = "updated_at"
            status_filter = "pending"
            for part in query.split("&"):
                if not part:
                    continue
                key, _, value = part.partition("=")
                if key == "sort":
                    sort_by = value
                if key == "status":
                    status_filter = value or "pending"
            payload = _fetch_db_snapshot(sort_by, status_filter)
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/api/review-statuses"):
            parsed = parse_qs(urlsplit(self.path).query)
            raw_ids = parsed.get("ids") or []
            review_ids: List[str] = []
            for batch in raw_ids:
                review_ids.extend(part.strip() for part in str(batch).split(","))
            payload = {"statuses": _fetch_review_statuses(review_ids)}
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        if self.path == "/api/config":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            _write_config(payload)
            self._send(200, b"ok", "text/plain")
            return
        if self.path == "/api/run-weekly":
            import subprocess

            log_path = _progress_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")

            def _runner() -> None:
                env = os.environ.copy()
                env["PROGRESS_LOG"] = str(log_path)
                result = subprocess.run(
                    ["python3", "-m", "humor_reviews.run", "weekly"],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    env=env,
                )
                if result.stdout:
                    _append_progress_log(log_path, "process_output", {"stream": "stdout", "text": result.stdout})
                if result.stderr:
                    _append_progress_log(log_path, "process_output", {"stream": "stderr", "text": result.stderr})
                if result.returncode != 0:
                    _append_progress_log(
                        log_path,
                        "run_failed",
                        {"returncode": result.returncode},
                    )

            threading.Thread(target=_runner, daemon=True).start()
            self._send(202, b"started", "text/plain; charset=utf-8")
            return
        if self.path == "/api/run-dry-run":
            import subprocess

            result = subprocess.run(
                ["python3", "-m", "humor_reviews.run", "shortlist", "--dry-run"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            output = (result.stdout or "") + (result.stderr or "")
            body = output.encode("utf-8")
            self._send(200, body, "text/plain; charset=utf-8")
            return
        if self.path == "/api/import-review":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            review_url = str(payload.get("review_url") or "").strip()
            try:
                result = _import_review_from_url(review_url)
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "message": str(exc)}).encode("utf-8"),
                    "application/json",
                )
                return
            except Exception as exc:
                self._send(
                    502,
                    json.dumps({"ok": False, "message": str(exc)}).encode("utf-8"),
                    "application/json",
                )
                return
            self._send(200, json.dumps(result).encode("utf-8"), "application/json")
            return
        if self.path == "/api/import-review-image":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            review_url = str(payload.get("review_url") or "").strip()
            submitted_by = str(payload.get("submitted_by") or "").strip()
            images_payload = payload.get("images") or []
            if not isinstance(images_payload, list) or not images_payload:
                self._send(
                    400,
                    json.dumps({"ok": False, "message": "Selecciona al menos una captura antes de importar."}).encode("utf-8"),
                    "application/json",
                )
                return
            images: list[dict[str, Any]] = []
            try:
                for raw_item in images_payload:
                    if not isinstance(raw_item, dict):
                        continue
                    image_data = str(raw_item.get("image_data") or "").strip()
                    mime_type = str(raw_item.get("mime_type") or "").strip()
                    if not image_data:
                        continue
                    if "," in image_data:
                        image_data = image_data.split(",", 1)[1]
                    images.append(
                        {
                            "bytes": base64.b64decode(image_data),
                            "mime_type": mime_type,
                        }
                    )
            except Exception:
                self._send(
                    400,
                    json.dumps({"ok": False, "message": "La imagen no se pudo leer correctamente."}).encode("utf-8"),
                    "application/json",
                )
                return
            try:
                result = _import_review_from_images(images, review_url=review_url, submitted_by=submitted_by)
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "message": str(exc)}).encode("utf-8"),
                    "application/json",
                )
                return
            except Exception as exc:
                self._send(
                    502,
                    json.dumps({"ok": False, "message": str(exc)}).encode("utf-8"),
                    "application/json",
                )
                return
            self._send(200, json.dumps(result).encode("utf-8"), "application/json")
            return
        if self.path == "/api/review-status":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            review_id = str(payload.get("review_id") or "").strip()
            status = _normalize_status(str(payload.get("status") or ""))
            if not review_id:
                self._send(400, b"missing review_id", "text/plain")
                return
            if not _set_review_status(review_id, status):
                self._send(404, b"review not found", "text/plain")
                return
            message = ""
            notion_url = ""
            if status == "accepted":
                review = _fetch_review_for_notion(review_id)
                if review:
                    if review.get("notion_page_url"):
                        notion_url = str(review["notion_page_url"])
                        message = "Estado actualizado. La página de Notion ya existía."
                    else:
                        try:
                            page = create_review_page(review)
                            _store_notion_page(review_id, page.page_id, page.url)
                            notion_url = page.url
                            message = "Estado actualizado y página creada en Notion."
                        except NotionSyncError as exc:
                            message = f"Estado actualizado, pero Notion no se pudo sincronizar: {exc}"
                else:
                    message = "Estado actualizado."
            else:
                message = "Estado actualizado."
            self._send(
                200,
                json.dumps({"ok": True, "status": status, "message": message, "notion_url": notion_url}).encode("utf-8"),
                "application/json",
            )
            return
        if self.path == "/api/review-notion-image":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            review_id = str(payload.get("review_id") or "").strip()
            image_data = str(payload.get("image_data") or "").strip()
            if not review_id or not image_data:
                self._send(400, b"missing review_id or image_data", "text/plain")
                return
            review = _fetch_review_for_notion(review_id)
            if not review:
                self._send(404, b"review not found", "text/plain")
                return
            if not review.get("notion_page_id"):
                self._send(400, b"review has no notion page", "text/plain")
                return
            if review.get("notion_image_uploaded_at"):
                self._send(
                    200,
                    json.dumps({"ok": True, "message": "La imagen de Notion ya existía."}).encode("utf-8"),
                    "application/json",
                )
                return
            try:
                if "," in image_data:
                    image_data = image_data.split(",", 1)[1]
                image_bytes = base64.b64decode(image_data)
            except Exception:
                self._send(400, b"invalid image_data", "text/plain")
                return
            filename = _sanitize_filename(
                f"{review.get('place_name') or 'review'}-{review.get('reviewer_name') or 'anonimo'}.png"
            )
            if not filename.lower().endswith(".png"):
                filename += ".png"
            try:
                append_review_image(str(review["notion_page_id"]), image_bytes, filename)
                _store_notion_image_uploaded(review_id)
                self._send(
                    200,
                    json.dumps({"ok": True, "message": "Captura añadida en Notion."}).encode("utf-8"),
                    "application/json",
                )
            except NotionSyncError as exc:
                self._send(
                    502,
                    json.dumps({"ok": False, "message": f"No se pudo subir la captura a Notion: {exc}"}).encode("utf-8"),
                    "application/json",
                )
            return
        self._send(404, b"Not found", "text/plain")


def main() -> None:
    _load_env(ROOT / ".env")
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Config UI running at http://{HOST}:{PORT}")
    if HOST == "0.0.0.0":
        print(f"LAN URL: http://{_local_ip()}:{PORT}")
    if _ui_auth_credentials():
        print("Basic auth enabled for Config UI.")
    server.serve_forever()
if __name__ == "__main__":
    main()
