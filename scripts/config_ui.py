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


HTML = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Config Studio</title>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Spectral:wght@500;700&display=swap");
    :root {
      --bg: #eef4fb;
      --panel: #f9fbff;
      --ink: #0d1b2a;
      --muted: #5d6b7a;
      --accent: #1b6aa9;
      --accent-2: #7cc2ff;
      --border: #d6e2f0;
      --shadow: 0 18px 40px rgba(13, 27, 42, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Space Grotesk", sans-serif;
      background: radial-gradient(circle at top, #f4f8ff 0%, var(--bg) 60%, #e6effa 100%);
      color: var(--ink);
      min-height: 100vh;
    }
    .layout {
      display: grid;
      grid-template-columns: 240px 1fr;
      min-height: 100vh;
    }
    aside {
      background: #0b1f36;
      color: #eaf2ff;
      padding: 22px 18px;
      border-right: 1px solid rgba(255, 255, 255, 0.08);
    }
    .brand {
      font-family: "Spectral", serif;
      font-size: 20px;
      letter-spacing: 0.3px;
      margin-bottom: 20px;
    }
    nav {
      display: grid;
      gap: 10px;
    }
    .nav-item {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.06);
      color: #eaf2ff;
      text-decoration: none;
      font-weight: 600;
      border: 1px solid transparent;
      cursor: pointer;
    }
    .nav-item.active {
      background: #1b6aa9;
      border-color: rgba(255, 255, 255, 0.2);
      box-shadow: 0 8px 18px rgba(27, 106, 169, 0.35);
    }
    .content {
      display: flex;
      flex-direction: column;
    }
    header {
      padding: 28px 24px 22px;
      background: linear-gradient(120deg, #0b1f36 0%, #163b5c 55%, #0f263b 100%);
      color: #eef5ff;
      border-bottom: 4px solid var(--accent);
    }
    header h1 {
      margin: 0;
      font-family: "Spectral", serif;
      font-size: 30px;
      letter-spacing: 0.3px;
    }
    header p { margin: 8px 0 0; color: #cddff0; }
    main {
      padding: 26px 20px 40px;
      max-width: 1100px;
      margin: 0 auto;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 18px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px;
      box-shadow: var(--shadow);
    }
    .card h2 {
      margin: 0 0 12px;
      font-size: 18px;
      font-family: "Spectral", serif;
    }
    label { display: block; font-weight: 600; margin: 10px 0 6px; }
    input[type="text"], input[type="number"], textarea, select {
      width: 100%;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #eef5ff;
      font: inherit;
      color: var(--ink);
    }
    textarea { min-height: 120px; resize: vertical; }
    .row { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
    .toggle { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
    .actions {
      margin-top: 18px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }
    .side-actions {
      margin-top: 22px;
      display: grid;
      gap: 10px;
    }
    .side-actions button {
      width: 100%;
      justify-content: center;
    }
    button {
      border: 0;
      padding: 10px 16px;
      border-radius: 999px;
      font-weight: 700;
      cursor: pointer;
      background: var(--accent);
      color: #eef5ff;
      box-shadow: 0 10px 18px rgba(27, 106, 169, 0.2);
    }
    button.secondary {
      background: #e3f0ff;
      color: var(--accent);
      box-shadow: none;
    }
    .hint { color: var(--muted); font-size: 13px; }
    .banner { margin-top: 12px; font-weight: 600; }
    .view { display: none; }
    .view.active { display: block; }
    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: #fdfefe;
    }
    table {
      border-collapse: collapse;
      width: 100%;
      font-size: 13px;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
    }
    th {
      background: #eef5ff;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
    }
    .db-grid {
      display: grid;
      gap: 18px;
    }
    .db-summary {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    }
    .db-summary-card {
      background: #f4f8ff;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
    }
    .db-summary-card .label { color: var(--muted); font-size: 12px; }
    .db-summary-card .value { font-size: 20px; font-weight: 700; }
    .db-section h3 { margin: 0 0 10px; font-family: "Spectral", serif; }
    .db-controls {
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <div class="brand">Config Studio</div>
      <nav>
        <button class="nav-item active" data-view="config-view">Configuración</button>
        <button class="nav-item" data-view="db-view">Base de datos</button>
      </nav>
      <div class="actions side-actions">
        <button id="save">Guardar cambios</button>
        <button class="secondary" id="reload">Recargar</button>
        <button class="secondary" id="run-weekly">Run Weekly</button>
        <button class="secondary" id="run-dry">Run Dry-Run</button>
        <button class="secondary" id="open-latest">Abrir último HTML</button>
        <div class="banner" id="status"></div>
      </div>
    </aside>
    <div class="content">
      <header>
        <h1>Config Studio</h1>
        <p>Edita la configuración del proyecto y guarda cambios en <code>config.yaml</code>.</p>
      </header>
      <main>
        <section id="config-view" class="view active">
          <div class="grid">
      <div class="card">
        <h2>App</h2>
        <div class="row">
          <div>
            <label>Weekly target</label>
            <input id="weekly_target_count" type="number" min="1" />
          </div>
          <div>
            <label>Humor threshold</label>
            <input id="humor_threshold" type="number" min="0" max="100" />
          </div>
        </div>
        <div class="row">
          <div>
            <label>Max reviews/place</label>
            <input id="max_reviews_per_place" type="number" min="1" />
          </div>
          <div>
            <label>Max places/run</label>
            <input id="max_places_per_run" type="number" min="1" />
          </div>
        </div>
        <label>Locale</label>
        <input id="locale" type="text" />
        <div class="toggle">
          <input id="allow_repeat_suggestions" type="checkbox" />
          <label for="allow_repeat_suggestions">Allow repeat suggestions</label>
        </div>
        <div class="toggle">
          <input id="enable_screenshots" type="checkbox" />
          <label for="enable_screenshots">Enable screenshots</label>
        </div>
      </div>

      <div class="card">
        <h2>Discovery</h2>
        <label>Country</label>
        <input id="country" type="text" />
        <label>Regions (una por línea)</label>
        <textarea id="regions"></textarea>
        <label>Categories (una por línea)</label>
        <textarea id="categories"></textarea>
        <div class="row">
          <div>
            <label>Min total reviews</label>
            <input id="min_total_reviews" type="number" min="0" />
          </div>
          <div>
            <label>Require recent days</label>
            <input id="require_recent_days" type="number" min="1" />
          </div>
        </div>
      </div>

      <div class="card">
        <h2>Scoring (OpenAI)</h2>
        <label>Model</label>
        <input id="scoring_model" type="text" />
        <div class="row">
          <div>
            <label>Temperature</label>
            <input id="temperature" type="number" step="0.1" min="0" max="2" />
          </div>
          <div>
            <label>Max output tokens</label>
            <input id="max_output_tokens" type="number" min="1" />
          </div>
        </div>
        <label>Prompt</label>
        <textarea id="prompt"></textarea>
      </div>

      <div class="card">
        <h2>Screenshots</h2>
        <div class="row">
          <div>
            <label>Timeout (ms)</label>
            <input id="screenshot_timeout_ms" type="number" min="1000" step="1000" />
          </div>
          <div>
            <label>Max per run</label>
            <input id="screenshot_max_per_run" type="number" min="1" />
          </div>
        </div>
        <label>Mode</label>
        <select id="screenshot_mode">
          <option value="rendered">rendered</option>
          <option value="live">live</option>
        </select>
        <div class="toggle">
          <input id="screenshot_debug" type="checkbox" />
          <label for="screenshot_debug">Screenshot debug</label>
        </div>
      </div>

      <div class="card">
        <h2>Curation</h2>
        <label>Theme limits (formato: tag=valor por línea)</label>
        <textarea id="theme_limits"></textarea>
      </div>
          </div>

        </section>

        <section id="db-view" class="view">
          <div class="card db-grid">
            <div class="row" style="align-items: center;">
              <div>
                <h2>Base de datos</h2>
                <p class="hint">Vista rápida de las tablas en <code>data/humor_reviews.db</code>.</p>
              </div>
              <div class="actions" style="margin-top: 0;">
                <button class="secondary" id="db-refresh">Actualizar</button>
              </div>
            </div>
            <div class="db-summary" id="db-summary"></div>
            <div class="db-section">
              <div class="db-controls">
                <h3 style="margin-right: auto;">Reseñas</h3>
                <label class="hint" for="review-sort">Ordenar por</label>
                <select id="review-sort">
                  <option value="humor_score">Humor score</option>
                  <option value="updated_at">Última actualización</option>
                </select>
              </div>
              <div class="table-wrap" id="db-reviews"></div>
            </div>
          </div>
        </section>
      </main>
    </div>
  </div>
  <script>
    const byId = (id) => document.getElementById(id);
    const status = byId("status");
    const navItems = Array.from(document.querySelectorAll(".nav-item"));
    const views = Array.from(document.querySelectorAll(".view"));
    const reviewSort = byId("review-sort");

    function listToText(list) {
      return (list || []).join("\\n");
    }

    function textToList(text) {
      return text.split("\\n").map(s => s.trim()).filter(Boolean);
    }

    function themeLimitsToText(obj) {
      const lines = [];
      for (const [k, v] of Object.entries(obj || {})) {
        lines.push(`${k}=${v}`);
      }
      return lines.join("\\n");
    }

    function textToThemeLimits(text) {
      const out = {};
      text.split("\\n").forEach(line => {
        const trimmed = line.trim();
        if (!trimmed || !trimmed.includes("=")) return;
        const [k, v] = trimmed.split("=", 2);
        out[k.trim()] = Number(v.trim());
      });
      return out;
    }

    async function loadConfig() {
      const res = await fetch("/config");
      const cfg = await res.json();
      byId("weekly_target_count").value = cfg.app.weekly_target_count || 0;
      byId("humor_threshold").value = cfg.app.humor_threshold || 0;
      byId("max_reviews_per_place").value = cfg.app.max_reviews_per_place || 0;
      byId("max_places_per_run").value = cfg.app.max_places_per_run || 0;
      byId("locale").value = cfg.app.locale || "en";
      byId("allow_repeat_suggestions").checked = !!cfg.app.allow_repeat_suggestions;
      byId("enable_screenshots").checked = !!cfg.app.enable_screenshots;

      byId("country").value = cfg.discovery.country || "";
      byId("regions").value = listToText(cfg.discovery.regions);
      byId("categories").value = listToText(cfg.discovery.categories);
      byId("min_total_reviews").value = cfg.discovery.min_total_reviews || 0;
      byId("require_recent_days").value = cfg.discovery.require_recent_days || 0;

      byId("scoring_model").value = cfg.scoring.model || "";
      byId("temperature").value = cfg.scoring.temperature || 0;
      byId("max_output_tokens").value = cfg.scoring.max_output_tokens || 0;
      byId("prompt").value = cfg.scoring.prompt || "";

      byId("screenshot_timeout_ms").value = cfg.app.screenshot_timeout_ms || 0;
      byId("screenshot_max_per_run").value = cfg.app.screenshot_max_per_run || 0;
      byId("screenshot_mode").value = cfg.app.screenshot_mode || "rendered";
      byId("screenshot_debug").checked = !!cfg.app.screenshot_debug;

      byId("theme_limits").value = themeLimitsToText(cfg.curation.theme_limits);
      status.textContent = "";
    }

    async function saveConfig() {
      const payload = {
        app: {
          output_dir: "out",
          data_dir: "data",
          weekly_target_count: Number(byId("weekly_target_count").value),
          humor_threshold: Number(byId("humor_threshold").value),
          max_reviews_per_place: Number(byId("max_reviews_per_place").value),
          max_places_per_run: Number(byId("max_places_per_run").value),
          allow_repeat_suggestions: byId("allow_repeat_suggestions").checked,
          locale: byId("locale").value.trim(),
          enable_screenshots: byId("enable_screenshots").checked,
          screenshot_dir: "out/screenshots",
          screenshot_timeout_ms: Number(byId("screenshot_timeout_ms").value),
          screenshot_max_per_run: Number(byId("screenshot_max_per_run").value),
          screenshot_mode: byId("screenshot_mode").value,
          screenshot_debug: byId("screenshot_debug").checked,
        },
        discovery: {
          provider: "serpapi_maps",
          country: byId("country").value.trim(),
          regions: textToList(byId("regions").value),
          categories: textToList(byId("categories").value),
          min_total_reviews: Number(byId("min_total_reviews").value),
          require_recent_days: Number(byId("require_recent_days").value),
        },
        providers: {
          serpapi: {
            api_key_env: "SERPAPI_API_KEY",
            hl: "es",
            gl: "es",
          }
        },
        scoring: {
          provider: "openai",
          model: byId("scoring_model").value.trim(),
          api_key_env: "OPENAI_API_KEY",
          temperature: Number(byId("temperature").value),
          max_output_tokens: Number(byId("max_output_tokens").value),
          prompt: byId("prompt").value,
        },
        safety: {
          pii_patterns: [],
          sensitive_keywords: [],
          accusation_keywords: [],
        },
        curation: {
          theme_limits: textToThemeLimits(byId("theme_limits").value),
        }
      };
      const res = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        status.textContent = "Guardado correctamente.";
      } else {
        status.textContent = "Error al guardar.";
      }
    }

    async function runWeekly() {
      status.textContent = "Ejecutando pipeline...";
      const res = await fetch("/run-weekly", { method: "POST" });
      const text = await res.text();
      status.textContent = text;
    }

    async function runDryRun() {
      status.textContent = "Ejecutando dry-run...";
      const res = await fetch("/run-dry-run", { method: "POST" });
      const text = await res.text();
      status.textContent = text;
    }

    function openLatest() {
      window.open("/latest-html", "_blank");
    }

    function setView(viewId) {
      views.forEach((view) => {
        view.classList.toggle("active", view.id === viewId);
      });
      navItems.forEach((item) => {
        item.classList.toggle("active", item.dataset.view === viewId);
      });
      if (viewId === "db-view") {
        loadDb();
      }
    }

    function renderTable(container, rows) {
      if (!rows || rows.length === 0) {
        container.innerHTML = "<div class='hint' style='padding: 12px;'>Sin datos.</div>";
        return;
      }
      const headers = Object.keys(rows[0]);
      const thead = "<thead><tr>" + headers.map((h) => `<th>${h}</th>`).join("") + "</tr></thead>";
      const tbody = "<tbody>" + rows.map((row) => {
        return "<tr>" + headers.map((h) => `<td>${row[h] ?? ""}</td>`).join("") + "</tr>";
      }).join("") + "</tbody>";
      container.innerHTML = `<table>${thead}${tbody}</table>`;
    }

    function renderReviews(container, rows) {
      if (!rows || rows.length === 0) {
        container.innerHTML = "<div class='hint' style='padding: 12px;'>Sin datos.</div>";
        return;
      }
      const headers = ["Humor score", "Nombre", "Localidad", "Fecha"];
      const thead = "<thead><tr>" + headers.map((h) => `<th>${h}</th>`).join("") + "</tr></thead>";
      const tbody = "<tbody>" + rows.map((row) => {
        const name = row.place_name || "Sitio";
        const url = row.review_url || "";
        const site = url ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${name}</a>` : name;
        return "<tr>" + [
          `<td>${row.humor_score ?? ""}</td>`,
          `<td>${site}</td>`,
          `<td>${row.place_locality || ""}</td>`,
          `<td>${row.date || ""}</td>`,
        ].join("") + "</tr>";
      }).join("") + "</tbody>";
      container.innerHTML = `<table>${thead}${tbody}</table>`;
    }

    async function loadDb() {
      const res = await fetch(`/db-data?sort=${encodeURIComponent(reviewSort.value)}`);
      if (!res.ok) {
        byId("db-summary").innerHTML = "<div class='hint'>No se pudo cargar la base de datos.</div>";
        return;
      }
      const payload = await res.json();
      const summary = payload.summary || {};
      byId("db-summary").innerHTML = [
        `<div class="db-summary-card"><div class="label">Lugares</div><div class="value">${summary.places || 0}</div></div>`,
        `<div class="db-summary-card"><div class="label">Reseñas</div><div class="value">${summary.reviews || 0}</div></div>`,
        `<div class="db-summary-card"><div class="label">Reseñas vacías (última)</div><div class="value">${summary.empty_reviews_skipped_last || 0}</div></div>`,
        `<div class="db-summary-card"><div class="label">Reseñas vacías (total)</div><div class="value">${summary.empty_reviews_skipped_total || 0}</div></div>`,
      ].join("");
      renderReviews(byId("db-reviews"), payload.reviews || []);
    }

    byId("save").addEventListener("click", saveConfig);
    byId("reload").addEventListener("click", loadConfig);
    byId("run-weekly").addEventListener("click", runWeekly);
    byId("run-dry").addEventListener("click", runDryRun);
    byId("open-latest").addEventListener("click", openLatest);
    byId("db-refresh").addEventListener("click", loadDb);
    reviewSort.addEventListener("change", loadDb);
    navItems.forEach((item) => {
      item.addEventListener("click", () => setView(item.dataset.view));
    });
    loadConfig();
  </script>
</body>
</html>
"""


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
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
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
