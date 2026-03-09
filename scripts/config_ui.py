from __future__ import annotations

import ast
import html as html_lib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
HOST = "127.0.0.1"
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


def _ensure_review_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reviews)").fetchall()}
    if "summary" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN summary TEXT")
    if "reviewed" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN reviewed INTEGER DEFAULT 0")
    if "selected" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN selected INTEGER DEFAULT 0")


def _fetch_db_snapshot(sort_by: str, show_reviewed: bool, only_selected: bool) -> Dict[str, Any]:
    db_path = _db_path()
    if not db_path.exists():
        return {
            "summary": {"places": 0, "reviews": 0, "shortlist": 0, "reviewed": 0, "pending_review": 0, "selected": 0},
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
        "reviewed": _scalar("SELECT COUNT(*) as count FROM reviews WHERE COALESCE(reviewed, 0) = 1"),
        "pending_review": _scalar("SELECT COUNT(*) as count FROM reviews WHERE COALESCE(reviewed, 0) = 0"),
        "selected": _scalar("SELECT COUNT(*) as count FROM reviews WHERE COALESCE(selected, 0) = 1"),
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
    filters: list[str] = []
    if not show_reviewed:
        filters.append("COALESCE(r.reviewed, 0) = 0")
    if only_selected:
        filters.append("COALESCE(r.selected, 0) = 1")
    review_filter = f"WHERE {' AND '.join(filters)}" if filters else ""

    reviews = _rows(
        "SELECT "
        "r.review_id, r.rating, r.date, r.humor_score, r.safety_label, r.status, r.updated_at, r.review_url, "
        "COALESCE(r.reviewed, 0) as reviewed, COALESCE(r.selected, 0) as selected, "
        "p.name as place_name, p.address as place_locality "
        "FROM reviews r "
        "LEFT JOIN places p ON (p.place_id = r.place_id OR p.data_id = r.place_id) "
        f"{review_filter} "
        f"ORDER BY {order_by} LIMIT 200"
    )
    shortlist = _rows(
        "SELECT review_id, batch_date, score FROM shortlist ORDER BY batch_date DESC LIMIT 200"
    )
    return {
        "summary": summary,
        "reviews": reviews,
        "shortlist": shortlist,
    }


def _render_review_detail(review_id: str) -> str | None:
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
                    r.text, r.summary, r.owner_reply, r.review_url, r.humor_score, r.humor_notes,
                    r.safety_label, r.safety_notes, r.tags, r.status, r.updated_at,
                    COALESCE(r.reviewed, 0) as reviewed, COALESCE(r.selected, 0) as selected,
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
                    r.text, r.summary, r.owner_reply, r.review_url, r.humor_score, r.humor_notes,
                    r.safety_label, r.safety_notes, r.tags, r.status, r.updated_at,
                    COALESCE(r.reviewed, 0) as reviewed, COALESCE(r.selected, 0) as selected,
                    p.name as place_name, p.address as place_address, p.category as place_category
                FROM reviews r
                LEFT JOIN places p ON (p.place_id = r.place_id OR p.data_id = r.place_id)
                WHERE r.review_id = ?
                """,
                (review_id,),
            ).fetchone()
        if not row:
            return None

    def _esc(value: Any) -> str:
        return html_lib.escape(str(value or ""))

    place_name_raw = str(row["place_name"] or "Sitio").strip()
    place_name = _esc(place_name_raw)
    place_address_raw = str(row["place_address"] or "").strip()
    place_address = _esc(place_address_raw)
    place_category = _esc(str(row["place_category"] or "Lugar").strip() or "Lugar")
    review_url = _esc(row["review_url"] or "")
    reviewer_raw = str(row["reviewer_name"] or "Anonymous").strip()
    reviewer_url_raw = str(row["reviewer_profile_url"] or "").strip()
    if reviewer_raw.startswith("{"):
        parsed = _parse_reviewer_payload(reviewer_raw)
        if parsed:
            reviewer_raw, reviewer_url_raw = parsed
    reviewer = _esc(reviewer_raw)
    reviewer_url = _esc(reviewer_url_raw)
    summary = _esc(row["summary"] or "")
    review_text = _esc(row["text"] or "")
    owner_reply_raw = str(row["owner_reply"] or "").strip()
    owner_reply = _esc(_format_owner_reply(owner_reply_raw))
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
        f'<section class="gm-owner-reply"><h3>Respuesta del propietario</h3><div class="gm-owner-reply-text">{owner_reply}</div></section>'
        if owner_reply
        else ""
    )
    reviewed = bool(row["reviewed"])
    reviewed_label = "Revisada" if reviewed else "Pendiente de revisar"
    reviewed_button = "Quitar marca de revisada" if reviewed else "Marcar como revisada"
    selected = bool(row["selected"])
    selected_label = "Seleccionada" if selected else "No seleccionada"
    selected_button = "Quitar selección" if selected else "Marcar como seleccionada"

    template = _load_html(REVIEW_HTML_PATH, "Missing review_detail.html")
    updated_at = _format_datetime(str(row["updated_at"] or ""))
    return (
        template.replace("{{place_name}}", place_name)
        .replace("{{place_category}}", place_category)
        .replace("{{place_address}}", place_address or "Sin dirección")
        .replace("{{place_avatar}}", place_avatar)
        .replace("{{reviewer_avatar}}", reviewer_avatar)
        .replace("{{rating_stars}}", rating_stars)
        .replace("{{summary_html}}", summary_html)
        .replace("{{review_text}}", review_text or "(sin texto)")
        .replace("{{owner_reply_html}}", owner_reply_html)
        .replace("{{humor_score}}", _esc(row["humor_score"]))
        .replace("{{safety_label}}", _esc(row["safety_label"]))
        .replace("{{safety_notes}}", _esc(row["safety_notes"]))
        .replace("{{tags}}", tags or "misc")
        .replace("{{tags_title}}", tags_title)
        .replace("{{humor_notes}}", _esc(row["humor_notes"]) or "Sin nota adicional.")
        .replace("{{date}}", _esc(row["date"]))
        .replace("{{rating}}", _esc(row["rating"]))
        .replace("{{status}}", _esc(row["status"]))
        .replace("{{review_id}}", _esc(row["review_id"]))
        .replace("{{reviewed}}", "true" if reviewed else "false")
        .replace("{{reviewed_label}}", reviewed_label)
        .replace("{{reviewed_button}}", reviewed_button)
        .replace("{{selected}}", "true" if selected else "false")
        .replace("{{selected_label}}", selected_label)
        .replace("{{selected_button}}", selected_button)
        .replace("{{reviewer_html}}", reviewer_html)
        .replace("{{updated_at}}", _esc(updated_at))
        .replace("{{maps_link}}", maps_link)
    )


def _set_reviewed(review_id: str, reviewed: bool) -> bool:
    db_path = _db_path()
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        _ensure_review_columns(conn)
        now = datetime.utcnow().isoformat()
        cur = conn.execute(
            "UPDATE reviews SET reviewed=?, updated_at=? WHERE review_id=?",
            (1 if reviewed else 0, now, review_id),
        )
        return cur.rowcount > 0


def _set_selected(review_id: str, selected: bool) -> bool:
    db_path = _db_path()
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        _ensure_review_columns(conn)
        now = datetime.utcnow().isoformat()
        cur = conn.execute(
            "UPDATE reviews SET selected=?, updated_at=? WHERE review_id=?",
            (1 if selected else 0, now, review_id),
        )
        return cur.rowcount > 0


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
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("{") and raw.endswith("}"):
        try:
            payload = ast.literal_eval(raw)
        except Exception:
            return raw
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("snippet") or payload.get("response") or "").strip()
            date = str(payload.get("date") or payload.get("published_date") or "").strip()
            if text and date:
                return f"{text}\n\n{date}"
            if text:
                return text
    return raw


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

    def do_GET(self) -> None:
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
            if "?" in self.path:
                query = urlsplit(self.path).query
                parsed = parse_qs(query)
                review_id = (parsed.get("id") or [""])[0]
            if not review_id:
                self._send(400, b"Missing review id", "text/plain")
                return
            html = _render_review_detail(review_id)
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
            show_reviewed = False
            only_selected = False
            for part in query.split("&"):
                if not part:
                    continue
                key, _, value = part.partition("=")
                if key == "sort":
                    sort_by = value
                if key == "show_reviewed":
                    show_reviewed = value.lower() in {"1", "true", "yes", "on"}
                if key == "only_selected":
                    only_selected = value.lower() in {"1", "true", "yes", "on"}
            payload = _fetch_db_snapshot(sort_by, show_reviewed, only_selected)
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
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
        if self.path == "/api/review-reviewed":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            review_id = str(payload.get("review_id") or "").strip()
            reviewed = bool(payload.get("reviewed"))
            if not review_id:
                self._send(400, b"missing review_id", "text/plain")
                return
            if not _set_reviewed(review_id, reviewed):
                self._send(404, b"review not found", "text/plain")
                return
            self._send(200, json.dumps({"ok": True, "reviewed": reviewed}).encode("utf-8"), "application/json")
            return
        if self.path == "/api/review-selected":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            review_id = str(payload.get("review_id") or "").strip()
            selected = bool(payload.get("selected"))
            if not review_id:
                self._send(400, b"missing review_id", "text/plain")
                return
            if not _set_selected(review_id, selected):
                self._send(404, b"review not found", "text/plain")
                return
            self._send(200, json.dumps({"ok": True, "selected": selected}).encode("utf-8"), "application/json")
            return
        self._send(404, b"Not found", "text/plain")


def main() -> None:
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Config UI running at http://{HOST}:{PORT}")
    server.serve_forever()
if __name__ == "__main__":
    main()
