const byId = (id) => document.getElementById(id);
const has = (id) => Boolean(byId(id));
const textToList = (text) => String(text || "").split("\n").map((s) => s.trim()).filter(Boolean);
const listToText = (list) => (list || []).join("\n");

let appConfig = null;
let progressOffset = 0;
let progressTimer = null;
let runFinished = false;
let progressBootstrapped = false;
let importedReviewImages = [];
const reviewStatusCache = new Map();
let reviewStatusHydrationPromise = null;

const progressState = {
  collectedReviews: 0,
  aboveThreshold: 0,
  processedSites: 0,
  startedAtMs: 0,
  lastScoreText: "",
  scoredReviews: 0,
  totalScore: 0,
  topScore: null,
  currentQuery: "",
  currentRegion: "",
  searchCount: 0,
  noResultsCount: 0,
  failedSearchCount: 0,
  failedPlaceCount: 0,
  productivePlaces: 0,
  recentActivity: [],
  topReviews: [],
  placeSummaries: [],
};

function setText(id, value) {
  const el = byId(id);
  if (el) el.textContent = value;
}

function fieldValue(id, fallback = "") {
  const el = byId(id);
  if (!el) return fallback;
  return el.value ?? fallback;
}

function setFieldValue(id, value) {
  const el = byId(id);
  if (el) el.value = value;
}

function currentCategories() {
  if (has("categories")) return textToList(fieldValue("categories"));
  return appConfig?.discovery?.categories || [];
}

function configNumber(path, fallback = 0) {
  const [section, key] = path.split(".");
  return Number(appConfig?.[section]?.[key] ?? fallback);
}

function describeSearchCategory(event) {
  const categories = currentCategories();
  if (categories.length === 0) return "búsqueda general";
  const category = String(event?.category || "").trim();
  if (category) return category;
  const query = String(event?.query || "").trim();
  if (query && !query.toLowerCase().startsWith("places in ")) return query;
  if (categories.length === 1) return categories[0];
  return categories.join(", ");
}

function formatEta(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "Menos de 1 min";
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes <= 0) return `${seconds}s`;
  if (minutes < 60) return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return remMinutes ? `${hours}h ${remMinutes}m` : `${hours}h`;
}

function formatPercent(value) {
  if (!Number.isFinite(value)) return "0%";
  return `${Math.round(value)}%`;
}

function pushLimited(list, item, max = 8) {
  list.unshift(item);
  if (list.length > max) list.length = max;
}

function rememberTopReview(item) {
  progressState.topReviews.push(item);
  progressState.topReviews.sort((a, b) => b.score - a.score);
  progressState.topReviews = progressState.topReviews.slice(0, 8);
}

function reviewDetailHref(reviewId) {
  const value = String(reviewId || "").trim();
  if (!value) return "";
  return `/review?id=${encodeURIComponent(value)}`;
}

function reviewStatusClass(status) {
  const value = String(status || "").trim().toLowerCase();
  if (["accepted", "aceptada", "selected", "used"].includes(value)) return "run-live-item--accepted";
  if (["rejected", "rechazada", "discarded"].includes(value)) return "run-live-item--rejected";
  return "";
}

function visibleReviewItems() {
  return [...progressState.recentActivity, ...progressState.topReviews].filter((item) => item && item.reviewId);
}

async function hydrateVisibleReviewStatuses() {
  const items = visibleReviewItems();
  const reviewIds = [...new Set(items.map((item) => String(item.reviewId || "").trim()).filter(Boolean))];
  if (!reviewIds.length) return;
  if (reviewStatusHydrationPromise) {
    await reviewStatusHydrationPromise;
    return;
  }
  reviewStatusHydrationPromise = (async () => {
    try {
      const res = await fetch(`/api/review-statuses?ids=${encodeURIComponent(reviewIds.join(","))}`);
      if (!res.ok) return;
      const payload = await res.json();
      const statuses = payload?.statuses || {};
      for (const reviewId of reviewIds) {
        reviewStatusCache.set(reviewId, String(statuses[reviewId] || "").trim().toLowerCase());
      }
      items.forEach((item) => {
        const reviewId = String(item.reviewId || "").trim();
        if (!reviewId) return;
        if (reviewStatusCache.has(reviewId)) item.status = reviewStatusCache.get(reviewId) || "";
      });
      renderLiveDashboard();
    } catch (err) {
      return;
    } finally {
      reviewStatusHydrationPromise = null;
    }
  })();
  await reviewStatusHydrationPromise;
}

function upsertPlaceSummary(item) {
  progressState.placeSummaries = progressState.placeSummaries.filter((entry) => entry.placeKey !== item.placeKey);
  progressState.placeSummaries.unshift(item);
  progressState.placeSummaries = progressState.placeSummaries.slice(0, 8);
}

function renderList(containerId, items, emptyText, renderItem) {
  const container = byId(containerId);
  if (!container) return;
  if (!items.length) {
    container.innerHTML = `<div class="run-empty-state">${emptyText}</div>`;
    return;
  }
  container.innerHTML = items.map(renderItem).join("");
}

function renderLiveDashboard() {
  if (!has("live-stage")) return;
  const elapsedMinutes = progressState.startedAtMs ? Math.max((Date.now() - progressState.startedAtMs) / 60000, 1 / 60) : 0;
  const throughput = elapsedMinutes > 0 ? progressState.scoredReviews / elapsedMinutes : 0;
  const hitRate = progressState.scoredReviews > 0 ? (progressState.aboveThreshold / progressState.scoredReviews) * 100 : 0;
  const averageScore = progressState.scoredReviews > 0 ? progressState.totalScore / progressState.scoredReviews : 0;
  const placeSuccessRate = progressState.processedSites > 0 ? (progressState.productivePlaces / progressState.processedSites) * 100 : 0;
  const statusText = byId("live-stage")?.textContent || "En espera";

  setText("run-status-chip", runFinished ? "Completado" : statusText);
  setText("run-current-query", progressState.currentQuery || "Sin búsqueda activa");
  setText("run-current-region", progressState.currentRegion || "Esperando arranque");
  setText("run-throughput", `${throughput.toFixed(1)} reseñas/min`);
  setText("run-hit-rate", formatPercent(hitRate));
  setText("run-avg-score", progressState.scoredReviews ? averageScore.toFixed(1) : "0");
  setText("run-top-score", progressState.topScore == null ? "-" : `#${progressState.topScore}`);
  setText("run-searches", String(progressState.searchCount));
  setText("run-no-results", String(progressState.noResultsCount));
  setText("run-place-failures", String(progressState.failedPlaceCount + progressState.failedSearchCount));
  setText("run-place-success-rate", formatPercent(placeSuccessRate));

  renderList(
    "run-activity-list",
    progressState.recentActivity,
    "Todavía no hay actividad registrada en esta ejecución.",
    (item) => `
      <div class="run-live-item ${reviewStatusClass(item.status)}">
        <div class="run-live-item-head">
          <div class="run-live-item-title">${item.href ? `<a href="${item.href}" target="_blank" rel="noopener">${item.title}</a>` : item.title}</div>
          <div class="run-live-item-score">${item.badge || ""}</div>
        </div>
        <div class="run-live-item-meta">${item.meta || ""}</div>
        <div class="run-live-item-copy">${item.copy || ""}</div>
        ${item.href ? `<div><a class="run-live-link" href="${item.href}" target="_blank" rel="noopener">Abrir reseña</a></div>` : ""}
      </div>
    `,
  );

  renderList(
    "run-top-list",
    progressState.topReviews,
    "Las mejores reseñas aparecerán aquí cuando el scorer encuentre material interesante.",
    (item) => `
      <div class="run-live-item ${reviewStatusClass(item.status)}">
        <div class="run-live-item-head">
          <div class="run-live-item-title">${item.href ? `<a href="${item.href}" target="_blank" rel="noopener">${item.reviewer || "Autor desconocido"}</a>` : (item.reviewer || "Autor desconocido")}</div>
          <div class="run-live-item-score">#${item.score}</div>
        </div>
        <div class="run-live-item-meta">${item.place || "Sitio desconocido"}</div>
        <div class="run-live-item-copy">${item.copy}</div>
        ${item.href ? `<div><a class="run-live-link" href="${item.href}" target="_blank" rel="noopener">Abrir reseña</a></div>` : ""}
      </div>
    `,
  );

  renderList(
    "run-place-list",
    progressState.placeSummaries,
    "Verás aquí un resumen de cada sitio en cuanto termine de procesarse.",
    (item) => `
      <div class="run-live-item">
        <div class="run-live-item-head">
          <div class="run-live-item-title">${item.place}</div>
          <div class="run-live-item-score">${item.badge}</div>
        </div>
        <div class="run-live-item-meta">${item.meta}</div>
        <div class="run-live-item-copy">${item.copy}</div>
      </div>
    `,
  );
}

async function loadConfig() {
  const res = await fetch("/api/config");
  const cfg = await res.json();
  appConfig = cfg;

  setFieldValue("humor_threshold", cfg.app?.humor_threshold || 0);
  setFieldValue("max_reviews_per_place", cfg.app?.max_reviews_per_place || 0);
  setFieldValue("max_places_per_run", cfg.app?.max_places_per_run || 0);
  setFieldValue("country", cfg.discovery?.country || "");
  setFieldValue("regions", listToText(cfg.discovery?.regions));
  setFieldValue("name_contains", cfg.discovery?.name_contains || "");
  setFieldValue("categories", listToText(cfg.discovery?.categories));
  setFieldValue("min_total_reviews", cfg.discovery?.min_total_reviews || 0);
  setFieldValue("scoring_model", cfg.scoring?.model || "");
  setFieldValue("prompt", cfg.scoring?.prompt || "");
  setText("status", "");
}

async function saveConfig() {
  const normalizedCountry = fieldValue("country").trim().toUpperCase();
  const payload = {
    app: {
      output_dir: "out",
      data_dir: "data",
      humor_threshold: Number(fieldValue("humor_threshold", 0)),
      max_reviews_per_place: Number(fieldValue("max_reviews_per_place", 0)),
      max_places_per_run: Number(fieldValue("max_places_per_run", 0)),
      allow_repeat_suggestions: false,
      locale: "es",
    },
    discovery: {
      provider: "serpapi_maps",
      country: normalizedCountry,
      regions: textToList(fieldValue("regions")),
      name_contains: fieldValue("name_contains").trim(),
      categories: textToList(fieldValue("categories")),
      min_total_reviews: Number(fieldValue("min_total_reviews", 0)),
      require_recent_days: 3650,
    },
    providers: {
      serpapi: {
        api_key_env: "SERPAPI_API_KEY",
        hl: "es",
        gl: (normalizedCountry || "ES").toLowerCase(),
      },
    },
    scoring: {
      provider: "openai",
      model: fieldValue("scoring_model").trim(),
      api_key_env: "OPENAI_API_KEY",
      temperature: 0.2,
      max_output_tokens: 320,
      prompt: fieldValue("prompt"),
    },
    safety: {
      pii_patterns: [],
      sensitive_keywords: [],
      accusation_keywords: [],
    },
  };
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  setText("status", res.ok ? "Guardado correctamente." : "Error al guardar.");
}

function updateEta() {
  if (!has("live-stage")) return;
  const discoveredSites = Number(byId("live-sites")?.textContent || 0);
  const processedSites = progressState.processedSites;
  const remainingSites = Math.max(0, discoveredSites - processedSites);
  setText("live-processed-sites", String(processedSites));
  setText("live-remaining-sites", String(remainingSites));
  if (!progressState.startedAtMs || processedSites <= 0 || remainingSites <= 0) {
    setText("live-eta", remainingSites === 0 && processedSites > 0 ? "Completando" : "-");
    return;
  }
  const elapsedMs = Date.now() - progressState.startedAtMs;
  const avgPerSiteMs = elapsedMs / processedSites;
  setText("live-eta", formatEta(avgPerSiteMs * remainingSites));
  renderLiveDashboard();
}

function resetLiveProgress() {
  if (!has("live-stage")) return;
  reviewStatusCache.clear();
  progressState.collectedReviews = 0;
  progressState.aboveThreshold = 0;
  progressState.processedSites = 0;
  progressState.startedAtMs = Date.now();
  progressState.lastScoreText = "";
  progressState.scoredReviews = 0;
  progressState.totalScore = 0;
  progressState.topScore = null;
  progressState.currentQuery = "";
  progressState.currentRegion = "";
  progressState.searchCount = 0;
  progressState.noResultsCount = 0;
  progressState.failedSearchCount = 0;
  progressState.failedPlaceCount = 0;
  progressState.productivePlaces = 0;
  progressState.recentActivity = [];
  progressState.topReviews = [];
  progressState.placeSummaries = [];
  setText("live-stage", "Iniciando");
  setText("live-sites", "0");
  setText("live-place", "-");
  setText("live-processed-sites", "0");
  setText("live-remaining-sites", String(configNumber("app.max_places_per_run", 0)));
  setText("live-count", "0");
  setText("live-above-threshold", "0");
  setText("live-eta", "-");
  setText("live-scores", "Esperando primeras puntuaciones");
  renderLiveDashboard();
}

async function runWeekly() {
  setText("status", "Ejecutando pipeline...");
  runFinished = false;
  resetLiveProgress();
  progressOffset = 0;
  const res = await fetch("/api/run-weekly", { method: "POST" });
  if (!res.ok) {
    setText("status", "Error al iniciar el run.");
    return;
  }
  if (progressTimer) clearInterval(progressTimer);
  progressTimer = setInterval(pollProgress, 1200);
}

async function runDryRun() {
  setText("status", "Ejecutando dry-run...");
  const res = await fetch("/api/run-dry-run", { method: "POST" });
  const text = await res.text();
  setText("status", text);
}

function showNoResults(message) {
  const modal = byId("no-results-modal");
  if (!modal) return;
  setText("no-results-text", message);
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
}

function closeNoResults() {
  const modal = byId("no-results-modal");
  if (!modal) return;
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
}

function applyProgressPayload(payload) {
  if (!has("live-stage")) return;
  (payload.lines || []).forEach((line) => {
    let event;
    try {
      event = JSON.parse(line);
    } catch (err) {
      return;
    }
    if (event.event === "search_query") {
      progressState.searchCount += 1;
      progressState.currentQuery = describeSearchCategory(event);
      progressState.currentRegion = event.region || "Sin región concreta";
      const bits = [event.category || "", event.region || ""].filter(Boolean);
      setText("live-stage", bits.length ? `Buscando: ${bits.join(" · ")}` : "Buscando sitios");
      pushLimited(progressState.recentActivity, {
        title: "Nueva búsqueda",
        badge: event.region || "",
        meta: progressState.currentQuery,
        copy: `Consulta lanzada para ${progressState.currentQuery}.`,
      });
    }
    if (event.event === "discovered_place") {
      setText("live-stage", "Descubriendo sitios");
      setText("live-place", event.place_name || event.place_id || byId("live-place")?.textContent || "-");
      pushLimited(progressState.recentActivity, {
        title: event.place_name || "Sitio descubierto",
        badge: "Sitio",
        meta: event.category || "Sin categoría",
        copy: "Entró en la cola de análisis.",
      });
    }
    if (event.event === "sites_found") {
      setText("live-sites", event.count ?? byId("live-sites")?.textContent ?? "0");
      updateEta();
    }
    if (event.event === "place_start") {
      setText("live-stage", "Recopilando reseñas");
      setText("live-place", event.place_name || event.place_id || "-");
      if (!progressState.lastScoreText) setText("live-scores", "Buscando reseñas nuevas para puntuar");
      pushLimited(progressState.recentActivity, {
        title: event.place_name || event.place_id || "Sitio en proceso",
        badge: "Leyendo",
        meta: "Inicio de recogida",
        copy: "Comenzando a recopilar reseñas de este sitio.",
      });
    }
    if ((event.event === "api_cache_hit" || event.event === "api_response") && event.api === "google_maps_reviews") {
      setText("live-stage", "Leyendo reseñas");
      progressState.collectedReviews += Number(event.review_count || 0);
      setText("live-count", String(progressState.collectedReviews));
      if (!progressState.lastScoreText) setText("live-scores", "Sin puntuaciones nuevas todavía");
    }
    if (event.event === "review_scored") {
      setText("live-stage", "Puntuando reseñas");
      setText("live-place", event.place_name || event.place_id || byId("live-place")?.textContent || "-");
      const reviewerName = String(event.reviewer_name || "").trim();
      const numericScore = Number(event.score || 0);
      progressState.scoredReviews += 1;
      progressState.totalScore += numericScore;
      progressState.topScore = progressState.topScore == null ? numericScore : Math.max(progressState.topScore, numericScore);
      const threshold = has("humor_threshold") ? Number(fieldValue("humor_threshold", 0)) : configNumber("app.humor_threshold", 0);
      const statusValue = numericScore >= threshold ? "new" : "rejected";
      if (numericScore >= threshold) {
        progressState.aboveThreshold += 1;
        setText("live-above-threshold", String(progressState.aboveThreshold));
      }
      const scoreLabel = `#${numericScore}`;
      progressState.lastScoreText = reviewerName ? `${reviewerName}: ${scoreLabel}` : scoreLabel;
      setText("live-scores", progressState.lastScoreText);
      const reviewCopy = reviewerName
        ? `${reviewerName} en ${event.place_name || "sitio actual"}`
        : `Nueva reseña puntuada en ${event.place_name || "sitio actual"}`;
      pushLimited(progressState.recentActivity, {
        title: "Reseña puntuada",
        badge: scoreLabel,
        meta: event.place_name || event.place_id || "Sitio",
        copy: reviewCopy,
        reviewId: event.review_id || "",
        href: reviewDetailHref(event.review_id),
        status: statusValue,
      });
      rememberTopReview({
        reviewer: reviewerName,
        place: event.place_name || event.place_id || "Sitio",
        score: numericScore,
        copy: `Reseña ${event.review_count || progressState.scoredReviews} del sitio actual.`,
        reviewId: event.review_id || "",
        href: reviewDetailHref(event.review_id),
        status: statusValue,
      });
    }
    if (event.event === "place_done") {
      progressState.processedSites += 1;
      const scores = Array.isArray(event.scores) ? event.scores : [];
      if (scores.length > 0) progressState.productivePlaces += 1;
      updateEta();
      if (scores.length) {
        progressState.lastScoreText = scores.map((score) => `#${score}`).join(", ");
        setText("live-scores", progressState.lastScoreText);
      } else if (!progressState.lastScoreText) {
        setText("live-scores", "Sin reseñas nuevas puntuadas en este sitio");
      }
      const topScore = scores.length ? Math.max(...scores) : null;
      upsertPlaceSummary({
        placeKey: event.place_id || event.place_name || `place-${progressState.processedSites}`,
        place: event.place_name || event.place_id || "Sitio",
        badge: scores.length ? `${scores.length} reseñas` : "Vacío",
        meta: topScore == null ? "Sin nuevas reseñas útiles" : `Mejor puntuación #${topScore}`,
        copy: scores.length ? `Se puntuaron ${scores.length} reseña(s) en este sitio.` : "No entraron reseñas nuevas en esta pasada.",
      });
      pushLimited(progressState.recentActivity, {
        title: event.place_name || event.place_id || "Sitio completado",
        badge: scores.length ? `${scores.length}` : "0",
        meta: scores.length ? `Top #${topScore}` : "Sin señal nueva",
        copy: scores.length ? `Se cerró el sitio con ${scores.length} reseñas puntuadas.` : "El sitio terminó sin reseñas nuevas.",
      });
    }
    if (event.event === "place_failed") {
      progressState.processedSites += 1;
      progressState.failedPlaceCount += 1;
      updateEta();
      setText("live-stage", "Error en un sitio");
      setText("live-place", event.place_name || event.place_id || byId("live-place")?.textContent || "-");
      setText("status", `Falló la recogida en ${event.place_name || event.place_id || "un sitio"}.`);
      upsertPlaceSummary({
        placeKey: event.place_id || event.place_name || `failed-${progressState.processedSites}`,
        place: event.place_name || event.place_id || "Sitio",
        badge: "Error",
        meta: "Fallo de recogida",
        copy: String(event.error || "Error no especificado"),
      });
      pushLimited(progressState.recentActivity, {
        title: event.place_name || event.place_id || "Error en sitio",
        badge: "Error",
        meta: "Recogida fallida",
        copy: String(event.error || "Error no especificado"),
      });
    }
    if (event.event === "search_failed") {
      progressState.failedSearchCount += 1;
      setText("live-stage", "Error en búsqueda");
      setText("status", `Falló una búsqueda para ${describeSearchCategory(event)} en ${event.region || "la región"}.`);
      pushLimited(progressState.recentActivity, {
        title: "Búsqueda fallida",
        badge: "Error",
        meta: `${describeSearchCategory(event)} · ${event.region || "sin región"}`,
        copy: "La consulta no pudo completarse.",
      });
    }
    if (event.event === "run_complete") {
      runFinished = true;
      setText("live-stage", "Completado");
      setText("status", `Finalizado. Sitios: ${event.discovered}, reseñas nuevas: ${event.collected}`);
      setText("live-count", String(progressState.collectedReviews));
      setText("live-sites", String(event.discovered ?? byId("live-sites")?.textContent ?? "0"));
      progressState.processedSites = Number(event.discovered ?? progressState.processedSites);
      updateEta();
      setText("live-eta", "Completado");
      if (progressTimer) clearInterval(progressTimer);
      pushLimited(progressState.recentActivity, {
        title: "Ejecución completada",
        badge: "Done",
        meta: `${event.discovered || 0} sitios · ${event.collected || 0} reseñas`,
        copy: "La ejecución terminó y ya no quedan sitios en cola.",
      });
    }
    if (event.event === "run_started") {
      runFinished = false;
      resetLiveProgress();
      setText("live-stage", "Ejecutando");
      setText("status", "Ejecutando pipeline...");
      pushLimited(progressState.recentActivity, {
        title: "Ejecución iniciada",
        badge: "Run",
        meta: "Pipeline semanal",
        copy: "Se ha puesto en marcha una nueva ejecución.",
      });
    }
    if (event.event === "run_failed") {
      runFinished = true;
      setText("live-stage", "Error");
      setText("status", "Falló la ejecución. Revisa el log.");
      if (progressTimer) clearInterval(progressTimer);
      pushLimited(progressState.recentActivity, {
        title: "Ejecución fallida",
        badge: "Error",
        meta: "Pipeline detenido",
        copy: "La ejecución se interrumpió antes de terminar.",
      });
    }
    if (event.event === "process_output" && event.stream === "stderr") {
      const text = String(event.text || "");
      if (!runFinished && !text.includes("NotOpenSSLWarning")) {
        setText("live-stage", "Con avisos");
        setText("status", "Aviso durante la ejecución. Revisa el log.");
        pushLimited(progressState.recentActivity, {
          title: "Aviso del proceso",
          badge: "Warn",
          meta: "stderr",
          copy: text.slice(0, 140),
        });
      }
    }
    if (event.event === "no_results") {
      progressState.noResultsCount += 1;
      const region = event.region || "la región";
      const category = describeSearchCategory(event);
      const isGeneralSearch = category === "búsqueda general";
      if (event.reason === "filtered_out") {
        const bits = [];
        const rawResults = Number(event.raw_results || 0);
        if (rawResults > 0) bits.push(`La API devolvió ${rawResults} sitios, pero todos se descartaron después.`);
        const skippedRegion = Number(event.skipped_region || 0);
        const skippedMinReviews = Number(event.skipped_min_reviews || 0);
        const skippedRecent = Number(event.skipped_recent || 0);
        const skippedNoIds = Number(event.skipped_no_ids || 0);
        if (skippedRegion > 0) bits.push(`${skippedRegion} fuera de la región indicada.`);
        if (skippedMinReviews > 0) bits.push(`${skippedMinReviews} por no llegar al mínimo de reseñas.`);
        if (skippedRecent > 0) bits.push(`${skippedRecent} por antigüedad de reseñas.`);
            if (skippedNoIds > 0) bits.push(`${skippedNoIds} por datos incompletos.`);
            const detail = bits.length ? ` ${bits.join(" ")}` : "";
            showNoResults(`No quedaron resultados válidos para "${category}" en ${region}.${detail}`);
            pushLimited(progressState.recentActivity, {
              title: "Búsqueda sin sitios válidos",
              badge: "0",
              meta: `${category} · ${region}`,
              copy: bits.join(" ") || "La búsqueda devolvió resultados, pero todos se descartaron.",
            });
          } else {
            const advice = isGeneralSearch
              ? "Prueba con otra región o añade una categoría concreta para orientar mejor la búsqueda."
              : "Revisa la categoría o prueba con otra región.";
            showNoResults(`La API no encontró resultados para "${category}" en ${region}. ${advice}`);
            pushLimited(progressState.recentActivity, {
              title: "Búsqueda vacía",
              badge: "0",
              meta: `${category} · ${region}`,
              copy: "La API no devolvió resultados para esta consulta.",
            });
          }
        }
    renderLiveDashboard();
  });
  hydrateVisibleReviewStatuses();
}

async function pollProgress() {
  if (!has("live-stage")) return;
  const res = await fetch(`/api/progress?offset=${progressOffset}`);
  if (!res.ok) return;
  const payload = await res.json();
  progressOffset = payload.next_offset || progressOffset;
  applyProgressPayload(payload);
}

async function bootstrapProgress() {
  if (!has("live-stage")) return;
  const res = await fetch("/api/progress?offset=0");
  if (!res.ok) return;
  const payload = await res.json();
  progressOffset = payload.next_offset || 0;
  runFinished = true;
  applyProgressPayload(payload);
  progressBootstrapped = true;
  if (!runFinished && !progressTimer) progressTimer = setInterval(pollProgress, 1200);
}

function renderImportedImagesState() {
  if (!has("import-review-image-name")) return;
  if (!importedReviewImages.length) {
    setText("import-review-image-name", "");
    return;
  }
  const labels = importedReviewImages.map((file, index) => file.name || `captura-${index + 1}`);
  setText("import-review-image-name", `${importedReviewImages.length} captura(s): ${labels.join(", ")}`);
}

function appendImportedImageFiles(files) {
  const nextFiles = [...importedReviewImages];
  for (const file of files || []) {
    if (file) nextFiles.push(file);
  }
  importedReviewImages = nextFiles;
  renderImportedImagesState();
  if (importedReviewImages.length) {
    setText("import-review-result", `${importedReviewImages.length} captura(s) listas para importar.`);
  }
}

function clearImportedImages() {
  importedReviewImages = [];
  renderImportedImagesState();
  setText("import-review-result", "");
}

function normalizePastedImage(file) {
  if (!file) return null;
  return new File([file], file.name || `captura-${Date.now()}.png`, {
    type: file.type || "image/png",
  });
}

function handlePastedImages(files) {
  const normalized = (files || []).map(normalizePastedImage).filter(Boolean);
  if (!normalized.length) return false;
  appendImportedImageFiles(normalized);
  setText("import-review-result", "Capturas pegadas correctamente. Ya puedes importarlas.");
  return true;
}

async function importReview() {
  const button = byId("import-review-button");
  const files = importedReviewImages;
  if (!files.length) {
    setText("import-review-result", "Selecciona al menos una captura antes de importar.");
    return;
  }
  button.disabled = true;
  setText("import-review-result", `Leyendo ${files.length} captura(s) e importando reseña...`);
  try {
    const imagesPayload = await Promise.all(
      files.map((file) => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve({ image_data: reader.result, mime_type: file.type || "" });
        reader.onerror = () => reject(new Error("file_read_error"));
        reader.readAsDataURL(file);
      }))
    );
    const res = await fetch("/api/import-review-image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        review_url: fieldValue("import-review-url").trim(),
        submitted_by: fieldValue("import-review-submitted-by").trim(),
        images: imagesPayload,
      }),
    });
    const payload = await res.json();
    if (!res.ok || !payload.ok) {
      setText("import-review-result", payload.message || "No se pudo importar la reseña.");
      return;
    }
    const verb = payload.already_exists ? "actualizada" : "importada";
    const bits = [
      `Reseña ${verb}`,
      payload.place_name ? `Lugar: ${payload.place_name}` : "",
      payload.reviewer_name ? `Autor: ${payload.reviewer_name}` : "",
      Number.isFinite(Number(payload.rating)) ? `Estrellas: ${payload.rating}` : "",
      Number.isFinite(Number(payload.humor_score)) ? `Humor: ${payload.humor_score}` : "",
    ].filter(Boolean);
    setText("import-review-result", bits.join(" · "));
    if (payload.detail_url) window.open(payload.detail_url, "_blank", "noopener");
  } catch (err) {
    setText("import-review-result", "Error de red al importar la reseña.");
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  byId("save")?.addEventListener("click", saveConfig);
  byId("run-weekly")?.addEventListener("click", runWeekly);
  byId("run-dry")?.addEventListener("click", runDryRun);
  byId("import-review-button")?.addEventListener("click", importReview);
  byId("clear-import-images")?.addEventListener("click", clearImportedImages);
  byId("import-review-url")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      importReview();
    }
  });
  byId("import-paste-zone")?.addEventListener("paste", (event) => {
    const items = Array.from(event.clipboardData?.items || []);
    const files = items.filter((item) => item.type?.startsWith("image/")).map((item) => item.getAsFile()).filter(Boolean);
    if (handlePastedImages(files)) {
      event.preventDefault();
      event.stopPropagation();
    }
  });
  if (has("import-paste-zone")) {
    window.addEventListener("paste", (event) => {
      if (event.defaultPrevented) return;
      const active = document.activeElement;
      const isTypingField = active && ["INPUT", "TEXTAREA"].includes(active.tagName);
      if (isTypingField && active !== byId("import-paste-zone")) return;
      const items = Array.from(event.clipboardData?.items || []);
      const files = items.filter((item) => item.type?.startsWith("image/")).map((item) => item.getAsFile()).filter(Boolean);
      if (handlePastedImages(files)) event.preventDefault();
    });
  }
  byId("no-results-close")?.addEventListener("click", closeNoResults);
  byId("no-results-modal")?.addEventListener("click", (event) => {
    if (event.target === byId("no-results-modal")) closeNoResults();
  });
}

bindEvents();
loadConfig().then(() => {
  if (has("live-stage")) {
    resetLiveProgress();
  }
  if (!progressBootstrapped) bootstrapProgress();
});
