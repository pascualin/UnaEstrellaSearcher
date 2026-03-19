from __future__ import annotations

import ast
import base64
import html as html_lib
import json
import os
import re
import socket
import sqlite3
import threading
from datetime import datetime
from urllib.parse import parse_qs, quote, urlsplit
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

import yaml

from humor_reviews.notion_sync import NotionSyncError, append_review_image, create_review_page
from humor_reviews.translation import translate_review_to_spanish


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
HOST = os.getenv("CONFIG_UI_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = 5173


CONFIG_HTML_PATH = ROOT / "scripts" / "config_view.html"
DB_HTML_PATH = ROOT / "scripts" / "db_view.html"
REVIEW_HTML_PATH = ROOT / "scripts" / "review_detail.html"


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
                r.humor_score, r.safety_label, r.tags, r.notion_page_id, r.notion_page_url,
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
        if self.path == "/" or self.path in {"/config", "/config/"}:
            html = _load_html(CONFIG_HTML_PATH, "Missing config_view.html")
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
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Config UI running at http://{HOST}:{PORT}")
    if HOST == "0.0.0.0":
        print(f"LAN URL: http://{_local_ip()}:{PORT}")
    if _ui_auth_credentials():
        print("Basic auth enabled for Config UI.")
    server.serve_forever()
if __name__ == "__main__":
    main()
