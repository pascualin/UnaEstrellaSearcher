from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
HOST = "127.0.0.1"
PORT = 5173


HTML_PATH = ROOT / "scripts" / "config_ui.html"


def _load_html() -> str:
    if HTML_PATH.exists():
        return HTML_PATH.read_text(encoding="utf-8")
    return "<h1>Missing config_ui.html</h1>"


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
        "r.rating, r.date, r.humor_score, r.safety_label, r.status, r.updated_at, r.review_url, "
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
        if self.path == "/":
            html = _load_html()
            self._send(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
            return
        if self.path == "/config_ui.css":
            if HTML_PATH.exists():
                css_path = HTML_PATH.with_suffix(".css")
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
        if self.path == "/config":
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
        if self.path.startswith("/db-data"):
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
        if self.path == "/config":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            _write_config(payload)
            self._send(200, b"ok", "text/plain")
            return
        if self.path == "/run-weekly":
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
        if self.path == "/run-dry-run":
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
