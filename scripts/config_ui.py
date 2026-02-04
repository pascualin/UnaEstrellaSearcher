from __future__ import annotations

import ast
import html as html_lib
import json
import sqlite3
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


def _fetch_db_snapshot(sort_by: str) -> Dict[str, Any]:
    db_path = _db_path()
    if not db_path.exists():
        return {
            "summary": {"places": 0, "reviews": 0, "shortlist": 0},
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

    def _scalar(sql: str) -> int:
        rows = _rows(sql)
        if rows and "count" in rows[0]:
            return int(rows[0]["count"])
        return 0

    summary = {
        "places": _rows("SELECT COUNT(*) as count FROM places")[0]["count"],
        "reviews": _rows("SELECT COUNT(*) as count FROM reviews")[0]["count"],
        "shortlist": _rows("SELECT COUNT(*) as count FROM shortlist")[0]["count"],
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

    reviews = _rows(
        "SELECT "
        "r.review_id, r.rating, r.date, r.humor_score, r.safety_label, r.status, r.updated_at, r.review_url, "
        "p.name as place_name, p.address as place_locality "
        "FROM reviews r "
        "LEFT JOIN places p ON (p.place_id = r.place_id OR p.data_id = r.place_id) "
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
        try:
            row = conn.execute(
                """
                SELECT
                    r.review_id, r.place_id, r.rating, r.date, r.reviewer_name, r.reviewer_profile_url,
                    r.text, r.summary, r.owner_reply, r.review_url, r.humor_score, r.humor_notes,
                    r.safety_label, r.safety_notes, r.tags, r.status, r.updated_at,
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

    place_name = _esc(row["place_name"] or "Sitio")
    place_address = _esc(row["place_address"] or "")
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
    owner_reply = _esc(row["owner_reply"] or "")
    tags = _esc(row["tags"] or "")
    place_line = f"{place_name} {'· ' + place_address if place_address else ''}".strip()
    reviewer_html = f'<a href="{reviewer_url}">{reviewer}</a>' if reviewer_url else reviewer
    maps_link = (
        f'<a class="cta" href="{review_url}" target="_blank" rel="noopener noreferrer">'
        "Abrir en Google Maps</a>"
        if review_url
        else ""
    )
    summary_html = (
        f'<div class="review-text"><div class="label">Resumen</div><div class="block">{summary}</div></div>'
        if summary
        else ""
    )
    owner_reply_html = (
        f'<div class="review-text"><div class="label">Respuesta del propietario</div><div class="block">{owner_reply}</div></div>'
        if owner_reply
        else ""
    )

    template = _load_html(REVIEW_HTML_PATH, "Missing review_detail.html")
    updated_at = _format_datetime(str(row["updated_at"] or ""))
    return (
        template.replace("{{place_line}}", place_line)
        .replace("{{summary_html}}", summary_html)
        .replace("{{review_text}}", review_text or "(sin texto)")
        .replace("{{owner_reply_html}}", owner_reply_html)
        .replace("{{humor_score}}", _esc(row["humor_score"]))
        .replace("{{safety_label}}", _esc(row["safety_label"]))
        .replace("{{safety_notes}}", _esc(row["safety_notes"]))
        .replace("{{tags}}", tags or "misc")
        .replace("{{humor_notes}}", _esc(row["humor_notes"]) or "Sin nota adicional.")
        .replace("{{date}}", _esc(row["date"]))
        .replace("{{rating}}", _esc(row["rating"]))
        .replace("{{status}}", _esc(row["status"]))
        .replace("{{reviewer_html}}", reviewer_html)
        .replace("{{updated_at}}", _esc(updated_at))
        .replace("{{maps_link}}", maps_link)
    )


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


def _format_datetime(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%d-%m-%Y %H:%M")
    except ValueError:
        return raw


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
        if self.path == "/latest-html":
            latest = _find_latest_html()
            if latest:
                self.send_response(302)
                self.send_header("Location", f"/outputs/{latest.name}")
                self.end_headers()
            else:
                self._send(404, b"No HTML output found", "text/plain")
            return
        if self.path.startswith("/api/db-data"):
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            sort_by = "updated_at"
            for part in query.split("&"):
                if not part:
                    continue
                key, _, value = part.partition("=")
                if key == "sort":
                    sort_by = value
            payload = _fetch_db_snapshot(sort_by)
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/outputs/"):
            name = self.path.replace("/outputs/", "", 1)
            file_path = ROOT / "out" / name
            if file_path.exists() and file_path.is_file():
                self._send(200, file_path.read_bytes(), "text/html; charset=utf-8")
                return
            self._send(404, b"Not found", "text/plain")
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

            result = subprocess.run(
                ["python3", "-m", "humor_reviews.run", "weekly"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            output = (result.stdout or "") + (result.stderr or "")
            body = output.encode("utf-8")
            self._send(200, body, "text/plain; charset=utf-8")
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
        self._send(404, b"Not found", "text/plain")


def main() -> None:
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Config UI running at http://{HOST}:{PORT}")
    server.serve_forever()


def _find_latest_html() -> Path | None:
    out_dir = ROOT / "out"
    if not out_dir.exists():
        return None
    candidates = sorted(out_dir.glob("weekly_shortlist_*.html"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


if __name__ == "__main__":
    main()
