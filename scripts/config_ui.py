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
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List
from uuid import uuid4

from openai import OpenAI
import requests
import yaml

from humor_reviews.celebration_calendar import fetch_observances
from humor_reviews.celebration_strategy import observance_exclusion_reason
from humor_reviews.collect import RawReview, _serpapi_reviews, collect_reviews
from humor_reviews.humor import score_review
from humor_reviews.notion_sync import NotionSyncError, append_review_image, sync_place_reviews_page
from humor_reviews.openai_models import openai_model_catalog
from humor_reviews.place_metadata import country_name, place_location
from humor_reviews.safety import assess_safety
from humor_reviews.settings import load_settings
from humor_reviews.storage import Place, Review, Storage
from humor_reviews.translation import translate_review_to_spanish, translate_reviews_to_spanish


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
HOST = os.getenv("CONFIG_UI_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = 5173


CONFIG_HTML_PATH = ROOT / "scripts" / "config_view.html"
RUN_HTML_PATH = ROOT / "scripts" / "run_view.html"
IMPORT_REVIEW_HTML_PATH = ROOT / "scripts" / "import_review_view.html"
DB_HTML_PATH = ROOT / "scripts" / "db_view.html"
REVIEW_HTML_PATH = ROOT / "scripts" / "review_detail.html"
PLACE_HTML_PATH = ROOT / "scripts" / "place_detail.html"
REVIEW_IMPORT_MAX_PAGES = 24
REVIEW_IMPORT_SORT_ORDERS = [
    None,
    "newestFirst",
    "ratingLow",
    "ratingHigh",
]
GOOGLE_REVIEW_HOSTS = {
    "google.com",
    "maps.google.com",
    "maps.app.goo.gl",
    "goo.gl",
}
IMAGE_IMPORT_MAX_BYTES = 12 * 1024 * 1024
IMAGE_IMPORT_MAX_FILES = 8
IMAGE_IMPORT_TOTAL_MAX_BYTES = 32 * 1024 * 1024
NOTION_CAPTURE_MAX_BYTES = 12 * 1024 * 1024
NOTION_CAPTURE_MAX_FILES = 50
NOTION_CAPTURE_TOTAL_MAX_BYTES = 64 * 1024 * 1024
OPENAI_MODEL_CACHE_TTL_SECONDS = 300
_openai_model_cache: dict[str, Any] = {"key": "", "expires_at": 0.0, "payload": None}
_place_metadata_lookups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
_active_run_lock = threading.Lock()
_active_run: dict[str, Any] = {
    "process": None,
    "mode": "",
    "cancel_requested": False,
}
_review_import_lock = threading.Lock()
_review_import_job: dict[str, Any] = {}


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


def _openai_scoring_models() -> dict[str, Any]:
    cfg = _load_config()
    scoring = cfg.get("scoring", {}) or {}
    api_key_env = str(scoring.get("api_key_env") or "OPENAI_API_KEY").strip()
    api_key = os.getenv(api_key_env, "").strip()
    cache_key = f"{api_key_env}:{bool(api_key)}"
    if (
        _openai_model_cache["key"] == cache_key
        and _openai_model_cache["expires_at"] > time.monotonic()
        and _openai_model_cache["payload"]
    ):
        return _openai_model_cache["payload"]
    if not api_key:
        payload = {
            "models": openai_model_catalog(),
            "source": "catalog",
            "warning": f"No se encontro {api_key_env}; se muestra el catalogo general.",
        }
        _cache_openai_models(cache_key, payload)
        return payload
    try:
        client = OpenAI(api_key=api_key, timeout=10.0, max_retries=0)
        available_ids = [model.id for model in client.models.list()]
        models = openai_model_catalog(available_ids)
        if models:
            payload = {"models": models, "source": "account", "warning": ""}
            _cache_openai_models(cache_key, payload)
            return payload
    except Exception:
        pass
    payload = {
        "models": openai_model_catalog(),
        "source": "catalog",
        "warning": "No se pudo consultar la cuenta; se muestra el catalogo general.",
    }
    _cache_openai_models(cache_key, payload)
    return payload


def _cache_openai_models(cache_key: str, payload: dict[str, Any]) -> None:
    _openai_model_cache["key"] = cache_key
    _openai_model_cache["expires_at"] = time.monotonic() + OPENAI_MODEL_CACHE_TTL_SECONDS
    _openai_model_cache["payload"] = payload


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


def _active_run_status() -> dict[str, Any]:
    with _active_run_lock:
        process = _active_run.get("process")
        active = process is not None and process.poll() is None
        return {
            "active": active,
            "mode": str(_active_run.get("mode") or "") if active else "",
            "stopping": bool(_active_run.get("cancel_requested")) if active else False,
        }


def _monitor_run_process(process, mode: str, log_path: Path) -> None:
    stdout, stderr = process.communicate()
    if stdout:
        _append_progress_log(log_path, "process_output", {"stream": "stdout", "text": stdout})
    if stderr:
        _append_progress_log(log_path, "process_output", {"stream": "stderr", "text": stderr})

    with _active_run_lock:
        is_current = _active_run.get("process") is process
        cancelled = is_current and bool(_active_run.get("cancel_requested"))
        if is_current:
            _active_run.update(
                {"process": None, "mode": "", "cancel_requested": False}
            )

    if cancelled:
        _append_progress_log(log_path, "run_cancelled", {"mode": mode})
    elif process.returncode != 0:
        _append_progress_log(
            log_path,
            "run_failed",
            {
                "returncode": process.returncode,
                "message": _friendly_process_failure(stderr or ""),
            },
        )


def _start_run_process(command: list[str], mode: str, log_path: Path) -> bool:
    with _active_run_lock:
        current = _active_run.get("process")
        if current is not None and current.poll() is None:
            return False
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        env = os.environ.copy()
        env["PROGRESS_LOG"] = str(log_path)
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        _active_run.update(
            {"process": process, "mode": mode, "cancel_requested": False}
        )

    threading.Thread(
        target=_monitor_run_process,
        args=(process, mode, log_path),
        daemon=True,
    ).start()
    return True


def _stop_active_run(log_path: Path) -> bool:
    with _active_run_lock:
        process = _active_run.get("process")
        if process is None or process.poll() is not None:
            _active_run.update(
                {"process": None, "mode": "", "cancel_requested": False}
            )
            return False
        mode = str(_active_run.get("mode") or "")
        _active_run["cancel_requested"] = True
        process.terminate()

    _append_progress_log(log_path, "run_cancel_requested", {"mode": mode})
    return True


def _friendly_process_failure(stderr: str) -> str:
    message = str(stderr or "").casefold()
    if "insufficient_quota" in message or "credit_balance_exhausted" in message:
        return (
            "OpenAI no tiene saldo de API. Añade créditos o configura TypeSafe Jev "
            "con TYPESAFE_API_KEY."
        )
    if "missing openai_api_key" in message:
        return "Falta OPENAI_API_KEY para usar el modelo de OpenAI seleccionado."
    if "missing typesafe_api_key" in message:
        return "Falta TYPESAFE_API_KEY para usar TypeSafe Jev."
    return "La ejecución se interrumpió. Consulta el detalle técnico del registro."


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


def _ensure_place_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(places)").fetchall()}
    if "average_rating" not in columns:
        conn.execute("ALTER TABLE places ADD COLUMN average_rating REAL")
    if "country" not in columns:
        conn.execute("ALTER TABLE places ADD COLUMN country TEXT")
    if "notion_page_id" not in columns:
        conn.execute("ALTER TABLE places ADD COLUMN notion_page_id TEXT")
    if "notion_page_url" not in columns:
        conn.execute("ALTER TABLE places ADD COLUMN notion_page_url TEXT")
    if "notion_exported_at" not in columns:
        conn.execute("ALTER TABLE places ADD COLUMN notion_exported_at TEXT")
    if "processed_at" not in columns:
        conn.execute("ALTER TABLE places ADD COLUMN processed_at TEXT")


def _cached_place_metadata(place_ids: list[str]) -> dict[str, Any]:
    settings = load_settings(CONFIG_PATH)
    cache_dir = settings.app.data_dir / "api_cache"
    cache_key = str(cache_dir.resolve())
    identifiers = tuple(sorted({place_id for place_id in place_ids if place_id}))
    lookup_key = (cache_key, identifiers)
    if lookup_key in _place_metadata_lookups:
        return _place_metadata_lookups[lookup_key]

    identifier_set = set(identifiers)
    for path in (cache_dir / "discover").glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            item_ids = {str(item.get("place_id") or ""), str(item.get("data_id") or "")}
            if identifier_set.intersection(item_ids):
                _place_metadata_lookups[lookup_key] = item
                return item

    if any(identifier.startswith("0x") for identifier in identifiers):
        for path in (cache_dir / "reviews").glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            data_id = str((payload.get("search_parameters") or {}).get("data_id") or "")
            place_info = payload.get("place_info") or {}
            if data_id in identifier_set and isinstance(place_info, dict):
                _place_metadata_lookups[lookup_key] = place_info
                return place_info

    _place_metadata_lookups[lookup_key] = {}
    return {}


def _hydrate_place_metadata(conn: sqlite3.Connection, place_row: sqlite3.Row) -> sqlite3.Row:
    if place_row["average_rating"] is not None and str(place_row["country"] or "").strip():
        return place_row
    identifiers = [str(place_row[key] or "") for key in ("place_id", "data_id")]
    metadata = _cached_place_metadata(identifiers)
    address = str(metadata.get("address") or place_row["address"] or "").strip()
    try:
        average_rating = float(metadata.get("rating"))
    except (TypeError, ValueError):
        average_rating = None
    fallback_country = country_name(load_settings(CONFIG_PATH).discovery.country)
    country = place_location(address, fallback_country)[2]
    conn.execute(
        """
        UPDATE places
        SET address = CASE WHEN ? <> '' THEN ? ELSE address END,
            average_rating = COALESCE(?, average_rating),
            country = CASE WHEN ? <> '' THEN ? ELSE country END
        WHERE place_id = ?
        """,
        (address, address, average_rating, country, country, place_row["place_id"]),
    )
    return conn.execute(
        """
        SELECT place_id, data_id, name, address, category, place_url,
               average_rating, country, notion_page_id, notion_page_url,
               notion_exported_at, processed_at
        FROM places WHERE place_id = ?
        """,
        (place_row["place_id"],),
    ).fetchone()


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


def _place_processed_where(processed_filter: str) -> str:
    normalized = str(processed_filter or "unprocessed").strip().lower()
    if normalized == "processed":
        return "WHERE COALESCE(p.processed_at, '') <> ''"
    if normalized == "all":
        return ""
    return "WHERE COALESCE(p.processed_at, '') = ''"


def _fetch_db_snapshot(sort_by: str, processed_filter: str) -> Dict[str, Any]:
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
        _ensure_place_columns(conn)

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
        "processed_places": _scalar("SELECT COUNT(*) as count FROM places WHERE COALESCE(processed_at, '') <> ''"),
        "unprocessed_places": _scalar("SELECT COUNT(*) as count FROM places WHERE COALESCE(processed_at, '') = ''"),
    }
    summary["empty_reviews_skipped_total"] = _scalar(
        "SELECT COALESCE(SUM(count), 0) as count FROM ingest_stats WHERE event = 'empty_reviews_skipped'"
    )
    summary["empty_reviews_skipped_last"] = _scalar(
        "SELECT count as count FROM ingest_stats WHERE event = 'empty_reviews_skipped' ORDER BY created_at DESC LIMIT 1"
    )
    order_by = "last_review_updated_at DESC, top_humor_score DESC"
    if sort_by == "humor_score":
        order_by = "top_humor_score DESC, last_review_updated_at DESC"
    processed_where = _place_processed_where(processed_filter)

    places = _rows(
        "SELECT "
        "COALESCE(p.place_id, r.place_id) AS place_id, "
        "COALESCE(p.name, 'Sitio') AS place_name, "
        "COALESCE(p.address, '') AS place_address, "
        "COALESCE(p.category, '') AS place_category, "
        "COALESCE(p.place_url, '') AS place_url, "
        "COALESCE(p.notion_page_url, '') AS notion_page_url, "
        "COALESCE(p.processed_at, '') AS processed_at, "
        "COUNT(r.review_id) AS review_count, "
        "MAX(COALESCE(r.humor_score, 0)) AS top_humor_score, "
        "SUM(CASE WHEN LOWER(COALESCE(r.status, '')) IN ('', 'new') THEN 1 ELSE 0 END) AS pending_count, "
        "SUM(CASE WHEN LOWER(COALESCE(r.status, '')) IN ('accepted', 'aceptada', 'selected', 'used') THEN 1 ELSE 0 END) AS accepted_count, "
        "SUM(CASE WHEN LOWER(COALESCE(r.status, '')) IN ('rejected', 'rechazada', 'discarded') THEN 1 ELSE 0 END) AS rejected_count, "
        "MAX(r.updated_at) AS updated_at, "
        "MAX(r.updated_at) AS last_review_updated_at "
        "FROM reviews r "
        "LEFT JOIN places p ON (p.place_id = r.place_id OR p.data_id = r.place_id) "
        f"{processed_where} "
        "GROUP BY COALESCE(p.place_id, r.place_id), p.name, p.address, p.category, p.place_url, p.notion_page_url, p.processed_at "
        f"ORDER BY {order_by} LIMIT 200"
    )
    shortlist = _rows(
        "SELECT review_id, batch_date, score FROM shortlist ORDER BY batch_date DESC LIMIT 200"
    )
    return {
        "summary": summary,
        "places": places,
        "reviews": [],
        "shortlist": shortlist,
    }


def _place_review_translations(
    review_rows: list[sqlite3.Row],
) -> dict[str, tuple[str, str, str, str]]:
    translations: dict[str, tuple[str, str, str, str]] = {}
    pending: list[dict[str, str]] = []
    for row in review_rows:
        review_id = str(row["review_id"] or "")
        review_text = str(row["text"] or "").strip()
        translated_text = str(row["translated_text"] or "").strip()
        review_language = str(row["original_text_language"] or "").strip().lower()
        owner_reply_text, _ = _split_owner_reply(str(row["owner_reply"] or ""))
        translated_owner_reply = str(row["translated_owner_reply"] or "").strip()
        owner_reply_language = str(row["original_owner_reply_language"] or "").strip().lower()
        if (
            translated_text
            and review_language
            and (not owner_reply_text or (translated_owner_reply and owner_reply_language))
        ):
            translations[review_id] = (
                translated_text,
                translated_owner_reply,
                review_language,
                owner_reply_language,
            )
            continue
        pending.append(
            {
                "review_id": review_id,
                "review_text": review_text,
                "owner_reply_text": owner_reply_text,
            }
        )

    if not pending:
        return translations

    translated = translate_reviews_to_spanish(pending)
    with sqlite3.connect(_db_path()) as conn:
        _ensure_review_columns(conn)
        for item in pending:
            review_id = item["review_id"]
            result = translated.get(review_id)
            review_text_es = str(result.review_text_es if result else item["review_text"]).strip()
            owner_reply_es = str(result.owner_reply_es if result else item["owner_reply_text"]).strip()
            review_language = str(result.review_language if result else "").strip().lower()
            owner_reply_language = str(result.owner_reply_language if result else "").strip().lower()
            translations[review_id] = (
                review_text_es or item["review_text"],
                owner_reply_es or item["owner_reply_text"],
                review_language,
                owner_reply_language,
            )
            _store_review_translation(conn, review_id, *translations[review_id])
    return translations


def _fetch_place_detail(place_id: str) -> Dict[str, Any] | None:
    db_path = _db_path()
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_review_columns(conn)
        _ensure_place_columns(conn)
        place_row = conn.execute(
            """
            SELECT place_id, data_id, name, address, category, place_url,
                   average_rating, country, notion_page_id, notion_page_url,
                   notion_exported_at, processed_at
            FROM places
            WHERE place_id = ? OR data_id = ?
            LIMIT 1
            """,
            (place_id, place_id),
        ).fetchone()
        if place_row:
            place_row = _hydrate_place_metadata(conn, place_row)
        review_place_ids = [place_id]
        if place_row:
            review_place_ids = [
                value
                for value in dict.fromkeys(
                    [str(place_row["place_id"] or ""), str(place_row["data_id"] or "")]
                )
                if value
            ]
        placeholders = ", ".join("?" for _ in review_place_ids)
        review_rows = conn.execute(
            f"""
            SELECT review_id, rating, date, reviewer_name, reviewer_profile_url,
                   text, translated_text, original_text_language,
                   owner_reply, translated_owner_reply, original_owner_reply_language,
                   review_url, submitted_by, humor_score, humor_notes, safety_label,
                   safety_notes, tags, status, updated_at
            FROM reviews
            WHERE place_id IN ({placeholders})
            ORDER BY COALESCE(humor_score, 0) DESC, updated_at DESC
            """,
            tuple(review_place_ids),
        ).fetchall()
    if not review_rows:
        return None

    translations = _place_review_translations(review_rows)
    reviews: list[dict[str, Any]] = []
    for row in review_rows:
        reviewer_name = str(row["reviewer_name"] or "Anónimo").strip() or "Anónimo"
        reviewer_url = str(row["reviewer_profile_url"] or "").strip()
        if reviewer_name.startswith("{"):
            parsed = _parse_reviewer_payload(reviewer_name)
            if parsed:
                reviewer_name, reviewer_url = parsed
        owner_reply_text, owner_reply_date = _split_owner_reply(str(row["owner_reply"] or ""))
        translated_text, translated_owner_reply, review_language, owner_reply_language = translations[
            str(row["review_id"] or "")
        ]
        reviews.append(
            {
                "review_id": row["review_id"],
                "rating": row["rating"] or 0,
                "date": row["date"] or "",
                "reviewer_name": reviewer_name,
                "reviewer_profile_url": reviewer_url,
                "review_text": translated_text or row["text"] or "",
                "review_language": review_language,
                "owner_reply_text": translated_owner_reply or owner_reply_text,
                "owner_reply_language": owner_reply_language,
                "owner_reply_date": owner_reply_date,
                "review_url": row["review_url"] or "",
                "submitted_by": row["submitted_by"] or "",
                "humor_score": row["humor_score"] or 0,
                "humor_notes": row["humor_notes"] or "",
                "safety_label": row["safety_label"] or "",
                "safety_notes": row["safety_notes"] or "",
                "tags": row["tags"] or "",
                "status": _normalize_status(row["status"]),
                "status_label": _status_label(row["status"]),
                "updated_at": row["updated_at"] or "",
            }
        )

    first_review = reviews[0]
    place = {
        "place_id": str((place_row["place_id"] if place_row else place_id) or place_id),
        "place_name": str((place_row["name"] if place_row else "Sitio") or "Sitio"),
        "place_address": str((place_row["address"] if place_row else "") or ""),
        "average_rating": float((place_row["average_rating"] if place_row else 0) or 0),
        "place_country": str((place_row["country"] if place_row else "") or ""),
        "place_category": str((place_row["category"] if place_row else "") or ""),
        "place_url": str((place_row["place_url"] if place_row else first_review["review_url"]) or ""),
        "notion_page_id": str((place_row["notion_page_id"] if place_row else "") or ""),
        "notion_page_url": str((place_row["notion_page_url"] if place_row else "") or ""),
        "notion_exported_at": str((place_row["notion_exported_at"] if place_row else "") or ""),
        "processed_at": str((place_row["processed_at"] if place_row else "") or ""),
        "processed": bool((place_row["processed_at"] if place_row else "") or ""),
        "review_count": len(reviews),
        "accepted_count": sum(review["status"] == "accepted" for review in reviews),
        "rejected_count": sum(review["status"] == "rejected" for review in reviews),
        "pending_count": sum(not review["status"] for review in reviews),
        "top_humor_score": first_review["humor_score"],
    }
    return {"place": place, "reviews": reviews}


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


def _fetch_place_statuses(place_ids: List[str]) -> Dict[str, bool]:
    normalized_ids: List[str] = []
    seen: set[str] = set()
    for place_id in place_ids:
        value = str(place_id or "").strip()
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
        _ensure_place_columns(conn)
        rows = conn.execute(
            f"""
            SELECT place_id, data_id, processed_at
            FROM places
            WHERE place_id IN ({placeholders}) OR data_id IN ({placeholders})
            """,
            tuple(normalized_ids) + tuple(normalized_ids),
        ).fetchall()

    statuses: Dict[str, bool] = {}
    requested = set(normalized_ids)
    for row in rows:
        processed = bool(str(row["processed_at"] or "").strip())
        for alias in (str(row["place_id"] or "").strip(), str(row["data_id"] or "").strip()):
            if alias in requested:
                statuses[alias] = processed
    return statuses


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


def _set_place_processed(place_id: str, processed: bool) -> bool:
    db_path = _db_path()
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        _ensure_place_columns(conn)
        processed_at = datetime.utcnow().isoformat() if processed else None
        cur = conn.execute(
            "UPDATE places SET processed_at=? WHERE place_id=? OR data_id=?",
            (processed_at, place_id, place_id),
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


def _store_place_notion_page(
    place_id: str,
    page_id: str,
    page_url: str,
    review_ids: list[str],
    image_review_ids: list[str],
) -> None:
    db_path = _db_path()
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        _ensure_place_columns(conn)
        _ensure_review_columns(conn)
        now = datetime.utcnow().isoformat()
        conn.execute(
            """
            UPDATE places
            SET notion_page_id=?, notion_page_url=?, notion_exported_at=?
            WHERE place_id=? OR data_id=?
            """,
            (page_id, page_url, now, place_id, place_id),
        )
        if review_ids:
            placeholders = ", ".join("?" for _ in review_ids)
            conn.execute(
                f"UPDATE reviews SET notion_page_id=?, notion_page_url=? WHERE review_id IN ({placeholders})",
                (page_id, page_url, *review_ids),
            )
        if image_review_ids:
            placeholders = ", ".join("?" for _ in image_review_ids)
            conn.execute(
                f"UPDATE reviews SET notion_image_uploaded_at=? WHERE review_id IN ({placeholders})",
                (now, *image_review_ids),
            )


def _decode_notion_review_images(raw_images: Any) -> dict[str, bytes]:
    if not isinstance(raw_images, list) or not raw_images:
        raise ValueError("No se recibieron las capturas de las reseñas aceptadas.")
    if len(raw_images) > NOTION_CAPTURE_MAX_FILES:
        raise ValueError(f"Demasiadas capturas. Usa como máximo {NOTION_CAPTURE_MAX_FILES}.")

    images: dict[str, bytes] = {}
    total_bytes = 0
    for raw_image in raw_images:
        if not isinstance(raw_image, dict):
            raise ValueError("El formato de una captura no es válido.")
        review_id = str(raw_image.get("review_id") or "").strip()
        image_data = str(raw_image.get("image_data") or "").strip()
        if not review_id or not image_data:
            raise ValueError("Cada captura debe indicar su reseña y contener una imagen.")
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]
        try:
            image_bytes = base64.b64decode(image_data, validate=True)
        except Exception as exc:
            raise ValueError("Una de las capturas no se pudo leer.") from exc
        if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Las capturas para Notion deben estar en formato PNG.")
        if len(image_bytes) > NOTION_CAPTURE_MAX_BYTES:
            raise ValueError("Una captura es demasiado grande para subirla a Notion.")
        total_bytes += len(image_bytes)
        if total_bytes > NOTION_CAPTURE_TOTAL_MAX_BYTES:
            raise ValueError("El conjunto de capturas es demasiado grande para subirlo a Notion.")
        images[review_id] = image_bytes
    return images


def _export_place_to_notion(
    place_id: str,
    review_images: dict[str, bytes],
) -> dict[str, Any]:
    detail = _fetch_place_detail(place_id)
    if not detail:
        raise LookupError("Sitio no encontrado.")
    place = detail["place"]
    accepted_candidates = [
        review for review in detail["reviews"] if review["status"] == "accepted"
    ]
    if not accepted_candidates:
        raise ValueError("Acepta al menos una reseña antes de exportar.")
    accepted_ids = {str(review["review_id"]) for review in accepted_candidates}
    missing_images = accepted_ids.difference(review_images)
    if missing_images:
        raise ValueError("No se pudo generar la captura de todas las reseñas aceptadas.")
    accepted_reviews = [
        review
        for candidate in accepted_candidates
        if (review := _fetch_review_for_notion(str(candidate["review_id"]))) is not None
    ]
    page = sync_place_reviews_page(
        place,
        accepted_reviews,
        page_id=str(place.get("notion_page_id") or ""),
        page_url=str(place.get("notion_page_url") or ""),
        review_images={
            str(review["review_id"]): review_images[str(review["review_id"])]
            for review in accepted_reviews
        },
    )
    _store_place_notion_page(
        place_id,
        page.page_id,
        page.url,
        [str(review["review_id"]) for review in accepted_reviews],
        [str(review["review_id"]) for review in accepted_reviews],
    )
    return {
        "ok": True,
        "notion_url": page.url,
        "exported_reviews": len(accepted_reviews),
        "message": (
            f"Página de Notion actualizada con {len(accepted_reviews)} "
            f"{'reseña' if len(accepted_reviews) == 1 else 'reseñas'} aceptadas y sus capturas."
        ),
    }


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
    place_id = _manual_place_id(extracted["place_name"], extracted["review_url"])
    storage = _storage()
    if place_id in storage.get_processed_place_ids():
        raise ValueError("Este sitio ya está marcado como procesado y no se evaluará de nuevo.")
    submitted_by = _normalize_free_text(submitted_by)
    settings = load_settings(CONFIG_PATH)
    owner_reply = _owner_reply_storage_value(
        extracted["owner_reply_text"],
        extracted["owner_reply_date"],
    )
    humor = score_review(extracted["review_text"], owner_reply, extracted["rating"], settings.scoring)
    safety = assess_safety(extracted["review_text"], owner_reply, settings.safety)

    review_id = _manual_review_id(
        extracted["place_name"],
        extracted["reviewer_name"],
        extracted["review_text"],
        extracted["date"],
        extracted["rating"],
        extracted["review_url"],
    )

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
    host = _normalize_google_host(parts.netloc)
    if host == "goo.gl":
        return bool(re.fullmatch(r"/maps/[^/]+/?", parts.path))
    return host in GOOGLE_REVIEW_HOSTS


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

    # Google can finish at consent instead of Maps; retain the actual review URL.
    for hop in reversed([*response.history, response]):
        hop_url = str(hop.url or "")
        parts = urlsplit(hop_url)
        candidate_url = hop_url
        if _normalize_google_host(parts.netloc) == "consent.google.com":
            candidate_url = str((parse_qs(parts.query).get("continue") or [""])[0])
        candidate = _normalize_review_url(candidate_url)
        if (
            _is_google_review_link(candidate)
            and _normalize_google_host(urlsplit(candidate).netloc) in {"google.com", "maps.google.com"}
        ):
            return candidate, (hop.text or "") if candidate_url == hop_url else ""
    raise RuntimeError("El enlace no redirige a una reseña de Google Maps válida.")


def _extract_place_data_id_from_text(text: str, cid_hint: str = "") -> str:
    if not text:
        return ""
    candidates = list(dict.fromkeys(re.findall(r"0x[0-9a-f]+:0x[0-9a-f]+", text, flags=re.IGNORECASE)))
    if not candidates:
        return ""
    cid_hint = str(cid_hint or "").strip().lower()
    if cid_hint:
        matching = [item for item in candidates if item.lower().endswith(f":{cid_hint}")]
        matching.sort(key=lambda item: item.lower().startswith("0x0:"))
        return matching[0] if matching else ""
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


def _raw_review_from_serpapi(
    place_data_id: str,
    review_payload: dict,
    fallback_review_url: str,
) -> RawReview:
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

    return RawReview(
        review_id=review_id,
        place_id=place_data_id,
        rating=rating,
        date=review_date or datetime.utcnow().date().isoformat(),
        reviewer_name=reviewer_name,
        reviewer_profile_url=reviewer_profile_url,
        text=review_text,
        owner_reply=owner_reply,
        review_url=review_url,
    )


def _score_imported_review(raw: RawReview, settings, submitted_by: str = "") -> Review:
    humor = score_review(raw.text, raw.owner_reply, raw.rating, settings.scoring)
    safety = assess_safety(raw.text, raw.owner_reply, settings.safety)
    return Review(
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
        submitted_by=submitted_by,
    )


def _upsert_place_from_reviews_payload(
    storage: Storage,
    place_data_id: str,
    payload: dict,
    review: RawReview,
) -> Place:
    existing = storage.get_place_map().get(place_data_id)
    place_info = payload.get("place_info") or {}
    search_metadata = payload.get("search_metadata") or {}
    total_reviews = place_info.get("reviews") or place_info.get("total_reviews") or (existing.total_reviews if existing else 0)
    try:
        total_reviews = int(total_reviews)
    except (TypeError, ValueError):
        total_reviews = 0
    try:
        average_rating = float(place_info.get("rating"))
    except (TypeError, ValueError):
        average_rating = None
    address = str(place_info.get("address") or (existing.address if existing else "")).strip()
    country = place_location(
        address,
        country_name(load_settings(CONFIG_PATH).discovery.country),
    )[2]
    place = Place(
        place_id=existing.place_id if existing else place_data_id,
        data_id=place_data_id,
        name=str(place_info.get("title") or place_info.get("name") or (existing.name if existing else "Importado manualmente")).strip(),
        address=address,
        category=str(place_info.get("type") or (existing.category if existing else "manual")).strip() or "manual",
        total_reviews=total_reviews,
        last_review_date=review.date,
        provider="serpapi",
        place_url=str(place_info.get("link") or search_metadata.get("google_maps_url") or (existing.place_url if existing else "") or "").strip(),
        average_rating=average_rating,
        country=country,
    )
    storage.upsert_place(place)
    return place


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


def _import_review_from_url(
    review_url: str,
    submitted_by: str = "",
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    normalized_url = _normalize_review_url(review_url)
    if not normalized_url:
        raise ValueError("Pega un enlace de reseña antes de importar.")

    settings = load_settings(CONFIG_PATH)
    api_key = os.getenv(settings.providers.serpapi_api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(
            f"Falta la variable de entorno {settings.providers.serpapi_api_key_env}."
        )

    if on_progress:
        on_progress({"message": "Identificando el sitio de la reseña…"})
    resolved_url, place_data_id = _resolve_place_data_id(normalized_url)
    storage = _storage()
    if place_data_id in storage.get_processed_place_ids():
        raise ValueError("Este sitio ya está marcado como procesado y no se evaluará de nuevo.")
    if on_progress:
        on_progress({"message": "Localizando la reseña del enlace…"})
    payload, raw_payload = _find_review_in_serpapi(place_data_id, resolved_url)
    raw_review = _raw_review_from_serpapi(place_data_id, raw_payload, resolved_url)
    if not raw_review.text:
        raise RuntimeError("La reseña existe, pero no tiene texto para puntuar.")
    if place_data_id in storage.get_processed_place_ids():
        raise ValueError("Este sitio ya está marcado como procesado y no se evaluará de nuevo.")

    place = _upsert_place_from_reviews_payload(storage, place_data_id, payload, raw_review)
    with sqlite3.connect(storage.db_path) as conn:
        conn.row_factory = sqlite3.Row
        existing_rows = conn.execute(
            "SELECT review_id, review_url, humor_score FROM reviews WHERE place_id IN (?, ?)",
            (place.place_id, place.data_id),
        ).fetchall()
    existing_by_id = {str(row["review_id"]): dict(row) for row in existing_rows}
    existing_by_url = {
        _review_url_key(str(row["review_url"])): dict(row)
        for row in existing_rows
        if _extract_review_id_from_url(str(row["review_url"] or ""))
    }
    target_key = _review_url_key(raw_review.review_url)
    existing = existing_by_id.get(raw_review.review_id) or existing_by_url.get(target_key)
    already_exists = existing is not None
    submitted_by = _normalize_free_text(submitted_by)
    if existing:
        review_id = str(existing["review_id"])
        humor_score = int(existing["humor_score"] or 0)
        if submitted_by:
            with sqlite3.connect(storage.db_path) as conn:
                conn.execute(
                    "UPDATE reviews SET submitted_by=? WHERE review_id=?",
                    (submitted_by, review_id),
                )
    else:
        review = _score_imported_review(raw_review, settings, submitted_by)
        storage.upsert_review(review)
        if review.humor_score < settings.app.humor_threshold:
            storage.update_status(review.review_id, "rejected")
        review_id = review.review_id
        humor_score = review.humor_score

    result = {
        "ok": True,
        "already_exists": already_exists,
        "review_id": review_id,
        "place_id": place.place_id,
        "place_name": place.name,
        "reviewer_name": raw_review.reviewer_name,
        "submitted_by": submitted_by,
        "rating": raw_review.rating,
        "humor_score": humor_score,
        "detail_url": f"/place?id={quote(place.place_id, safe='')}",
        "review_count": len(existing_by_id) + (0 if already_exists else 1),
        "new_reviews": 0 if already_exists else 1,
        "additional_reviews": 0,
        "inspected_reviews": 0,
        "max_reviews": max(1, settings.app.max_reviews_per_place),
        "warning": "",
    }
    known_ids = set(existing_by_id) | {raw_review.review_id, review_id}
    known_urls = set(existing_by_url) | {target_key}

    def report() -> None:
        if on_progress:
            on_progress({
                **result,
                "message": (
                    f"{place.name}: {result['additional_reviews']} reseñas adicionales analizadas · "
                    f"{result['inspected_reviews']}/{result['max_reviews']} revisadas."
                ),
            })

    report()
    try:
        for raw in collect_reviews(
            [place_data_id],
            settings.providers,
            result["max_reviews"],
            settings.app.data_dir / "api_cache",
            raise_on_error=True,
        ):
            if place_data_id in storage.get_processed_place_ids():
                result["warning"] = "El sitio se ha marcado como procesado. Se ha detenido el análisis adicional."
                break
            result["inspected_reviews"] += 1
            url_key = _review_url_key(raw.review_url)
            has_review_url = bool(_extract_review_id_from_url(raw.review_url))
            if (
                not (raw.text or "").strip()
                or raw.rating > 2
                or raw.review_id in known_ids
                or (has_review_url and url_key in known_urls)
                or storage.review_exists(raw.review_id)
            ):
                report()
                continue
            review = _score_imported_review(raw, settings)
            storage.upsert_review(review)
            if review.humor_score < settings.app.humor_threshold:
                storage.update_status(review.review_id, "rejected")
            known_ids.add(raw.review_id)
            if has_review_url:
                known_urls.add(url_key)
            result["additional_reviews"] += 1
            result["new_reviews"] += 1
            result["review_count"] += 1
            report()
    except Exception as exc:
        result["warning"] = f"El análisis adicional se interrumpió: {exc}. Las reseñas guardadas están disponibles en el sitio."
    return result


def _review_import_status() -> dict[str, Any]:
    with _review_import_lock:
        return dict(_review_import_job)


def _run_review_import(job_id: str, review_url: str, submitted_by: str) -> None:
    def update(fields: dict[str, Any]) -> None:
        with _review_import_lock:
            if _review_import_job.get("id") == job_id:
                _review_import_job.update(fields)

    try:
        result = _import_review_from_url(review_url, submitted_by, on_progress=update)
        update({
            **result,
            "status": "completed",
            "message": (
                f"{result['place_name']}: {result['new_reviews']} reseñas nuevas analizadas · "
                f"{result['review_count']} reseñas disponibles en el sitio."
            ),
        })
    except Exception as exc:
        update({"ok": False, "status": "failed", "message": str(exc)})


def _start_review_import(review_url: str, submitted_by: str) -> dict[str, Any] | None:
    normalized_url = _normalize_review_url(review_url)
    if not normalized_url or not _is_google_review_link(normalized_url):
        raise ValueError("Pega un enlace de una reseña de Google Maps válido.")
    with _review_import_lock:
        if _review_import_job.get("status") == "running":
            return None
        job = {
            "id": uuid4().hex,
            "ok": True,
            "status": "running",
            "message": "Identificando el sitio de la reseña…",
        }
        _review_import_job.clear()
        _review_import_job.update(job)
    threading.Thread(
        target=_run_review_import,
        args=(job["id"], normalized_url, submitted_by),
        daemon=True,
    ).start()
    return job


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
        if self.path == "/review_capture.js":
            js_path = ROOT / "scripts" / "review_capture.js"
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
        if self.path.startswith("/place"):
            parsed = parse_qs(urlsplit(self.path).query)
            place_id = (parsed.get("id") or [""])[0]
            if not place_id:
                self._send(400, b"Missing place id", "text/plain")
                return
            if _fetch_place_detail(place_id) is None:
                self._send(404, b"Place not found", "text/plain")
                return
            html = _load_html(PLACE_HTML_PATH, "Missing place_detail.html")
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
        if self.path == "/api/scoring-models":
            payload = _openai_scoring_models()
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
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
                payload = {
                    "lines": [],
                    "next_offset": 0,
                    "run": _active_run_status(),
                }
                self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
                return
            data = log_path.read_bytes()
            if offset < 0 or offset > len(data):
                offset = 0
            chunk = data[offset:]
            text = chunk.decode("utf-8", errors="ignore")
            lines = [line for line in text.splitlines() if line.strip()]
            payload = {
                "lines": lines,
                "next_offset": len(data),
                "run": _active_run_status(),
            }
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/api/db-data"):
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            sort_by = "updated_at"
            processed_filter = "unprocessed"
            for part in query.split("&"):
                if not part:
                    continue
                key, _, value = part.partition("=")
                if key == "sort":
                    sort_by = value
                if key in {"processed", "status"}:
                    processed_filter = value or "unprocessed"
            payload = _fetch_db_snapshot(sort_by, processed_filter)
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/api/place-detail"):
            parsed = parse_qs(urlsplit(self.path).query)
            place_id = (parsed.get("id") or [""])[0]
            if not place_id:
                self._send(400, b"Missing place id", "text/plain")
                return
            payload = _fetch_place_detail(place_id)
            if payload is None:
                self._send(404, b"Place not found", "text/plain")
                return
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
        if self.path.startswith("/api/import-review-status"):
            parsed = parse_qs(urlsplit(self.path).query)
            job_id = (parsed.get("id") or [""])[0]
            payload = _review_import_status()
            if job_id and payload.get("id") != job_id:
                self._send(
                    404,
                    json.dumps({"ok": False, "message": "El análisis ya no está disponible. Abre el sitio desde la base de datos."}).encode("utf-8"),
                    "application/json",
                )
                return
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/api/place-statuses"):
            parsed = parse_qs(urlsplit(self.path).query)
            raw_ids = parsed.get("ids") or []
            place_ids: List[str] = []
            for batch in raw_ids:
                place_ids.extend(part.strip() for part in str(batch).split(","))
            payload = {"statuses": _fetch_place_statuses(place_ids)}
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        if self.path == "/api/stop-run":
            stopped = _stop_active_run(_progress_log_path())
            self._send(
                202 if stopped else 200,
                json.dumps(
                    {
                        "ok": True,
                        "stopped": stopped,
                        "message": (
                            "Deteniendo la búsqueda activa."
                            if stopped
                            else "No hay ninguna búsqueda activa."
                        ),
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json",
            )
            return
        if self.path == "/api/config":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            _write_config(payload)
            self._send(200, b"ok", "text/plain")
            return
        if self.path == "/api/run-weekly":
            log_path = _progress_log_path()
            try:
                started = _start_run_process(
                    [sys.executable, "-m", "humor_reviews.run", "weekly"],
                    "weekly",
                    log_path,
                )
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"ok": False, "message": str(exc)}).encode("utf-8"),
                    "application/json",
                )
                return
            if not started:
                self._send(
                    409,
                    json.dumps(
                        {"ok": False, "message": "Ya hay una búsqueda en curso."}
                    ).encode("utf-8"),
                    "application/json",
                )
                return
            self._send(202, b"started", "text/plain; charset=utf-8")
            return
        if self.path == "/api/episode-observances":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            episode_date = str(payload.get("date") or "").strip()
            try:
                requested_date = date.fromisoformat(episode_date)
            except ValueError:
                self._send(
                    400,
                    json.dumps(
                        {"ok": False, "message": "Selecciona una fecha válida."}
                    ).encode("utf-8"),
                    "application/json",
                )
                return
            try:
                settings = load_settings(CONFIG_PATH)
                observances = fetch_observances(
                    requested_date,
                    settings.app.data_dir / "api_cache",
                )
            except Exception as exc:
                self._send(
                    502,
                    json.dumps(
                        {"ok": False, "message": str(exc)}
                    ).encode("utf-8"),
                    "application/json",
                )
                return
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "date": episode_date,
                        "observances": [
                            {
                                "name": observance.name,
                                "source_url": observance.source_url,
                                "exclusion_reason": observance_exclusion_reason(
                                    observance.name
                                ),
                            }
                            for observance in observances
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json",
            )
            return
        if self.path == "/api/run-episode":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            episode_date = str(payload.get("date") or "").strip()
            requested_observances = payload.get("observances")
            selected_observances = list(
                dict.fromkeys(
                    str(name).strip()
                    for name in requested_observances
                    if str(name).strip()
                )
            ) if isinstance(requested_observances, list) else []
            try:
                date.fromisoformat(episode_date)
                target = max(1, min(20, int(payload.get("target") or 5)))
                humor_threshold = max(0, min(100, int(payload.get("humor_threshold") or 60)))
                relevance_threshold = max(
                    0, min(100, int(payload.get("relevance_threshold") or 60))
                )
            except (TypeError, ValueError):
                self._send(
                    400,
                    json.dumps({"ok": False, "message": "Fecha o umbrales no válidos."}).encode(
                        "utf-8"
                    ),
                    "application/json",
                )
                return

            if not selected_observances:
                self._send(
                    400,
                    json.dumps(
                        {"ok": False, "message": "Selecciona al menos una celebración."}
                    ).encode("utf-8"),
                    "application/json",
                )
                return
            selected_observances = selected_observances[:50]

            log_path = _progress_log_path()
            command = [
                sys.executable,
                "-m",
                "humor_reviews.run",
                "episode-search",
                "--date",
                episode_date,
                "--target",
                str(target),
                "--humor-threshold",
                str(humor_threshold),
                "--relevance-threshold",
                str(relevance_threshold),
            ]
            for observance in selected_observances:
                command.extend(["--observance", observance])
            try:
                started = _start_run_process(command, "episode", log_path)
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"ok": False, "message": str(exc)}).encode("utf-8"),
                    "application/json",
                )
                return
            if not started:
                self._send(
                    409,
                    json.dumps(
                        {"ok": False, "message": "Ya hay una búsqueda en curso."}
                    ).encode("utf-8"),
                    "application/json",
                )
                return
            self._send(
                202,
                json.dumps(
                    {
                        "ok": True,
                        "date": episode_date,
                        "target": target,
                        "observances": selected_observances,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json",
            )
            return
        if self.path == "/api/run-dry-run":
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
            submitted_by = str(payload.get("submitted_by") or "").strip()
            try:
                result = _start_review_import(review_url, submitted_by)
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
            if result is None:
                self._send(
                    409,
                    json.dumps({"ok": False, "message": "Ya hay un análisis de sitio en curso."}).encode("utf-8"),
                    "application/json",
                )
                return
            self._send(202, json.dumps(result).encode("utf-8"), "application/json")
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
            self._send(
                200,
                json.dumps({"ok": True, "status": status, "message": "Estado actualizado."}).encode("utf-8"),
                "application/json",
            )
            return
        if self.path == "/api/place-processed":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            place_id = str(payload.get("place_id") or "").strip()
            processed = payload.get("processed") is True
            if not place_id:
                self._send(400, b"missing place_id", "text/plain")
                return
            if not _set_place_processed(place_id, processed):
                self._send(404, b"place not found", "text/plain")
                return
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "processed": processed,
                        "message": "Sitio marcado como procesado." if processed else "Sitio reabierto.",
                    }
                ).encode("utf-8"),
                "application/json",
            )
            return
        if self.path == "/api/place-notion-export":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            place_id = str(payload.get("place_id") or "").strip()
            if not place_id:
                self._send(400, b"missing place_id", "text/plain")
                return
            try:
                review_images = _decode_notion_review_images(payload.get("captures"))
                result = _export_place_to_notion(place_id, review_images)
            except LookupError as exc:
                self._send(404, json.dumps({"ok": False, "message": str(exc)}).encode("utf-8"), "application/json")
                return
            except ValueError as exc:
                self._send(400, json.dumps({"ok": False, "message": str(exc)}).encode("utf-8"), "application/json")
                return
            except NotionSyncError as exc:
                self._send(502, json.dumps({"ok": False, "message": f"No se pudo sincronizar Notion: {exc}"}).encode("utf-8"), "application/json")
                return
            self._send(200, json.dumps(result).encode("utf-8"), "application/json")
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
