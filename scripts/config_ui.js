const byId = (id) => document.getElementById(id);
const has = (id) => Boolean(byId(id));
const textToList = (text) => String(text || "").split("\n").map((s) => s.trim()).filter(Boolean);
const listToText = (list) => (list || []).join("\n");

const scoringModels = {
  openai: [],
  typesafe: [{ id: "jev-latest" }],
};
const scoringApiKeyEnvs = {
  openai: "OPENAI_API_KEY",
  typesafe: "TYPESAFE_API_KEY",
};

let appConfig = null;
let progressOffset = 0;
let progressTimer = null;
let runFinished = false;
let progressBootstrapped = false;
let importedReviewImages = [];

const progressState = {
  collectedReviews: 0,
  aboveThreshold: 0,
  episodeCandidates: 0,
  newEpisodeCandidates: 0,
  archivedEpisodeCandidates: 0,
  reusableFinds: 0,
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
  topPlaces: [],
  failed: false,
  archivedChecked: 0,
  archivedTotal: 0,
  selectedObservances: [],
  perObservanceTarget: 0,
  newCandidatesByObservance: {},
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

function replaceSelectOptions(id, values, selectedValue, defaultValue = "") {
  const select = byId(id);
  if (!select) return "";
  const normalizedValues = values.map((value) => typeof value === "string" ? { value, label: value } : value);
  select.replaceChildren(...normalizedValues.map((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    return option;
  }));
  const allowed = normalizedValues.map((item) => item.value);
  const nextValue = allowed.includes(selectedValue) ? selectedValue : (allowed.includes(defaultValue) ? defaultValue : allowed[0] || "");
  select.value = nextValue;
  return nextValue;
}

function currentOpenAIProfile() {
  const model = fieldValue("scoring_model").trim();
  return scoringModels.openai.find((item) => item.id === model) || null;
}

function updateOpenAIExecutionControls({ resetDefaults = false } = {}) {
  const isOpenAI = fieldValue("scoring_provider", "openai") === "openai";
  const profile = isOpenAI ? currentOpenAIProfile() : null;
  const controls = [
    "reasoning-effort-field",
    "reasoning-mode-field",
    "verbosity-field",
    "service-tier-field",
    "temperature-field",
    "max-output-tokens-field",
  ];
  controls.forEach((id) => {
    const element = byId(id);
    if (element) element.hidden = !isOpenAI;
  });
  if (!isOpenAI || !profile) return;

  const effortOptions = profile.reasoning_efforts || [];
  const modeOptions = profile.reasoning_modes || [];
  const verbosityOptions = profile.verbosity_options || [];
  const tierOptions = profile.service_tiers || [];
  const selectedEffort = replaceSelectOptions(
    "scoring_reasoning_effort",
    effortOptions.map((value) => ({ value, label: value === "none" ? "none (sin razonamiento)" : value })),
    resetDefaults ? "" : fieldValue("scoring_reasoning_effort"),
    profile.default_reasoning_effort,
  );
  replaceSelectOptions(
    "scoring_reasoning_mode",
    modeOptions.map((value) => ({ value, label: value === "standard" ? "standard" : "pro" })),
    resetDefaults ? "" : fieldValue("scoring_reasoning_mode"),
    profile.default_reasoning_mode,
  );
  replaceSelectOptions(
    "scoring_verbosity",
    verbosityOptions,
    resetDefaults ? "" : fieldValue("scoring_verbosity"),
    profile.default_verbosity,
  );
  replaceSelectOptions(
    "scoring_service_tier",
    tierOptions,
    resetDefaults ? "" : fieldValue("scoring_service_tier"),
    profile.default_service_tier,
  );

  byId("reasoning-effort-field").hidden = effortOptions.length === 0;
  byId("reasoning-mode-field").hidden = modeOptions.length === 0;
  byId("verbosity-field").hidden = verbosityOptions.length === 0;
  byId("service-tier-field").hidden = tierOptions.length === 0;
  byId("temperature-field").hidden = !profile.supports_temperature || (effortOptions.length > 0 && selectedEffort !== "none");
}

async function refreshScoringModels({ resetDefaults = false, selectedModel = "" } = {}) {
  if (!has("scoring_provider")) return;
  const provider = fieldValue("scoring_provider", "openai") || "openai";
  if (provider === "openai") {
    setText("scoring-model-status", "Consultando modelos disponibles...");
    try {
      const response = await fetch("/api/scoring-models");
      if (!response.ok) throw new Error("model_list_failed");
      const payload = await response.json();
      scoringModels.openai = payload.models || [];
      setText(
        "scoring-model-status",
        payload.source === "account" ? "Disponibles para esta cuenta de OpenAI." : (payload.warning || "Catálogo general de OpenAI."),
      );
    } catch (error) {
      scoringModels.openai = selectedModel ? [{ id: selectedModel }] : [];
      setText("scoring-model-status", "No se pudo cargar el catálogo de OpenAI.");
    }
  } else {
    setText("scoring-model-status", "");
  }
  const models = scoringModels[provider] || [];
  const currentModel = selectedModel || fieldValue("scoring_model").trim();
  const modelOptions = [...models];
  if (currentModel && !modelOptions.some((item) => item.id === currentModel)) {
    modelOptions.unshift({ id: currentModel, unavailable: true });
  }
  const modelSelect = byId("scoring_model");
  if (modelSelect) {
    modelSelect.replaceChildren(...modelOptions.map((model) => {
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = model.unavailable ? `${model.id} (configurado; no disponible)` : model.id;
      return option;
    }));
    const defaultModel = models[0]?.id || "";
    modelSelect.value = resetDefaults ? defaultModel : (currentModel || defaultModel);
  }
  if (resetDefaults) {
    const currentApiKeyEnv = fieldValue("scoring_api_key_env").trim();
    const knownApiKeyEnvs = Object.values(scoringApiKeyEnvs);
    if (!currentApiKeyEnv || knownApiKeyEnvs.includes(currentApiKeyEnv)) {
      setFieldValue("scoring_api_key_env", scoringApiKeyEnvs[provider] || "");
    }
  }
  setText("scoring-heading", provider === "typesafe" ? "Puntuación (TypeSafe Jev)" : "Puntuación (OpenAI)");
  updateOpenAIExecutionControls({ resetDefaults });
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

function upsertRecentActivity(item) {
  if (item.placeKey) {
    progressState.recentActivity = progressState.recentActivity.filter(
      (entry) => entry.placeKey !== item.placeKey,
    );
  }
  pushLimited(progressState.recentActivity, item);
}

function upsertTopPlace(item) {
  const existing = progressState.topPlaces.find((entry) => entry.placeKey === item.placeKey);
  const incrementedReviewCount = Number(existing?.reviewCount || 0) + Number(item.reviewIncrement || 0);
  const merged = {
    ...(existing || {}),
    ...item,
    score: Math.max(Number(existing?.score || 0), Number(item.score || 0)),
    reviewCount: item.reviewIncrement
      ? incrementedReviewCount
      : Math.max(Number(existing?.reviewCount || 0), Number(item.reviewCount || 0)),
    qualifyingCount: Number(existing?.qualifyingCount || 0) + Number(item.qualifyingIncrement || 0),
  };
  delete merged.qualifyingIncrement;
  delete merged.reviewIncrement;
  progressState.topPlaces = progressState.topPlaces.filter((entry) => entry.placeKey !== item.placeKey);
  progressState.topPlaces.push(merged);
  progressState.topPlaces.sort((a, b) => b.score - a.score || b.reviewCount - a.reviewCount);
  progressState.topPlaces = progressState.topPlaces.slice(0, 8);
  return merged;
}

function placeDetailHref(placeId) {
  const value = String(placeId || "").trim();
  if (!value) return "";
  return `/place?id=${encodeURIComponent(value)}`;
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

  setText("run-status-chip", progressState.failed ? "Error" : (runFinished ? "Completado" : statusText));
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
      <div class="run-live-item">
        <div class="run-live-item-head">
          <div class="run-live-item-title">${item.href ? `<a href="${item.href}" target="_blank" rel="noopener">${item.title}</a>` : item.title}</div>
          <div class="run-live-item-score">${item.badge || ""}</div>
        </div>
        <div class="run-live-item-meta">${item.meta || ""}</div>
        <div class="run-live-item-copy">${item.copy || ""}</div>
        ${item.href ? `<div><a class="run-live-link" href="${item.href}">Abrir sitio</a></div>` : ""}
      </div>
    `,
  );

  renderList(
    "run-top-list",
    progressState.topPlaces,
    "Los sitios con mejores reseñas aparecerán aquí cuando el scorer encuentre material interesante.",
    (item) => `
      <div class="run-live-item run-live-place-item">
        <div class="run-live-item-head">
          <div class="run-live-item-title">${item.href ? `<a href="${item.href}">${item.place || "Sitio desconocido"}</a>` : (item.place || "Sitio desconocido")}</div>
          <div class="run-live-item-score">#${item.score}</div>
        </div>
        <div class="run-live-item-meta">${item.reviewCount || 0} reseñas puntuadas · ${item.qualifyingCount || 0} candidatas</div>
        <div class="run-live-item-copy">${item.copy || (item.latestReviewer ? `Última reseña: ${item.latestReviewer}.` : "Sitio procesado durante esta ejecución.")}</div>
        ${item.href ? `<div><a class="run-live-link" href="${item.href}">Abrir sitio</a></div>` : ""}
      </div>
    `,
  );

}

async function loadConfig() {
  const res = await fetch("/api/config");
  const cfg = await res.json();
  appConfig = cfg;

  setFieldValue("humor_threshold", cfg.app?.humor_threshold || 0);
  setFieldValue("episode-humor-threshold", cfg.app?.humor_threshold || 60);
  setFieldValue("max_reviews_per_place", cfg.app?.max_reviews_per_place || 0);
  setFieldValue("max_places_per_run", cfg.app?.max_places_per_run || 0);
  setFieldValue("country", cfg.discovery?.country || "");
  setFieldValue("regions", listToText(cfg.discovery?.regions));
  setFieldValue("name_contains", cfg.discovery?.name_contains || "");
  setFieldValue("categories", listToText(cfg.discovery?.categories));
  setFieldValue("min_total_reviews", cfg.discovery?.min_total_reviews || 0);
  setFieldValue("scoring_provider", cfg.scoring?.provider || "openai");
  setFieldValue(
    "scoring_api_key_env",
    cfg.scoring?.api_key_env || scoringApiKeyEnvs[cfg.scoring?.provider || "openai"],
  );
  setFieldValue("scoring_reasoning_effort", cfg.scoring?.reasoning_effort || "none");
  setFieldValue("scoring_reasoning_mode", cfg.scoring?.reasoning_mode || "standard");
  setFieldValue("scoring_verbosity", cfg.scoring?.verbosity || "low");
  setFieldValue("scoring_service_tier", cfg.scoring?.service_tier || "auto");
  setFieldValue("scoring_temperature", cfg.scoring?.temperature ?? 0.2);
  setFieldValue("scoring_max_output_tokens", cfg.scoring?.max_output_tokens ?? 320);
  setFieldValue("prompt", cfg.scoring?.prompt || "");
  await refreshScoringModels({ selectedModel: cfg.scoring?.model || "" });
  setFieldValue("scoring_reasoning_effort", cfg.scoring?.reasoning_effort || "none");
  setFieldValue("scoring_reasoning_mode", cfg.scoring?.reasoning_mode || "standard");
  setFieldValue("scoring_verbosity", cfg.scoring?.verbosity || "low");
  setFieldValue("scoring_service_tier", cfg.scoring?.service_tier || "auto");
  updateOpenAIExecutionControls();
  setText("status", "");
}

async function saveConfig() {
  const normalizedCountry = fieldValue("country").trim().toUpperCase();
  const provider = fieldValue("scoring_provider", "openai") || "openai";
  const payload = JSON.parse(JSON.stringify(appConfig || {}));
  payload.app = {
    ...(payload.app || {}),
    output_dir: "out",
    data_dir: "data",
    humor_threshold: Number(fieldValue("humor_threshold", 0)),
    max_reviews_per_place: Number(fieldValue("max_reviews_per_place", 0)),
    max_places_per_run: Number(fieldValue("max_places_per_run", 0)),
    allow_repeat_suggestions: false,
    locale: "es",
  };
  payload.discovery = {
    ...(payload.discovery || {}),
    provider: "serpapi_maps",
    country: normalizedCountry,
    regions: textToList(fieldValue("regions")),
    name_contains: fieldValue("name_contains").trim(),
    categories: textToList(fieldValue("categories")),
    min_total_reviews: Number(fieldValue("min_total_reviews", 0)),
    require_recent_days: 3650,
  };
  payload.providers = {
    ...(payload.providers || {}),
    serpapi: {
      ...(payload.providers?.serpapi || {}),
      api_key_env: "SERPAPI_API_KEY",
      hl: "es",
      gl: (normalizedCountry || "ES").toLowerCase(),
    },
  };
  payload.scoring = {
    ...(payload.scoring || {}),
    provider,
    model: fieldValue("scoring_model").trim(),
    api_key_env: fieldValue("scoring_api_key_env").trim() || scoringApiKeyEnvs[provider],
    reasoning_effort: fieldValue("scoring_reasoning_effort").trim(),
    reasoning_mode: fieldValue("scoring_reasoning_mode").trim(),
    verbosity: fieldValue("scoring_verbosity").trim(),
    service_tier: fieldValue("scoring_service_tier").trim(),
    temperature: Number(fieldValue("scoring_temperature", 0.2)),
    max_output_tokens: Number(fieldValue("scoring_max_output_tokens", 320)),
    prompt: fieldValue("prompt"),
  };
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) appConfig = payload;
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
  progressState.collectedReviews = 0;
  progressState.aboveThreshold = 0;
  progressState.episodeCandidates = 0;
  progressState.newEpisodeCandidates = 0;
  progressState.archivedEpisodeCandidates = 0;
  progressState.reusableFinds = 0;
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
  progressState.topPlaces = [];
  progressState.failed = false;
  progressState.archivedChecked = 0;
  progressState.archivedTotal = 0;
  progressState.selectedObservances = [];
  progressState.perObservanceTarget = 0;
  progressState.newCandidatesByObservance = {};
  setText("live-stage", "Iniciando");
  setText("live-sites", "0");
  setText("live-place", "-");
  setText("live-processed-sites", "0");
  setText("live-remaining-sites", String(configNumber("app.max_places_per_run", 0)));
  setText("live-count", "0");
  setText("live-above-threshold", "0");
  setText("live-episode-candidates", "0");
  setText("live-reusable", "0");
  setText("episode-observances", "");
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

async function runEpisodeSearch() {
  const episodeDate = fieldValue("episode-date").trim();
  if (!episodeDate) {
    setText("status", "Selecciona la fecha de emisión.");
    return;
  }
  const button = byId("run-episode");
  if (button) button.disabled = true;
  setText("status", "Preparando la búsqueda del episodio...");
  runFinished = false;
  resetLiveProgress();
  progressOffset = 0;
  try {
    const res = await fetch("/api/run-episode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        date: episodeDate,
        target: Number(fieldValue("episode-target", 5)),
        humor_threshold: Number(fieldValue("episode-humor-threshold", 60)),
        relevance_threshold: Number(fieldValue("episode-relevance-threshold", 60)),
      }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => ({}));
      setText("status", payload.message || "No se pudo iniciar la búsqueda del episodio.");
      runFinished = true;
      return;
    }
    setText("status", `Buscando celebraciones y reseñas para ${episodeDate}.`);
    if (progressTimer) clearInterval(progressTimer);
    progressTimer = setInterval(pollProgress, 1200);
  } catch (error) {
    setText("status", "Error de red al iniciar la búsqueda del episodio.");
    runFinished = true;
  } finally {
    if (button) button.disabled = false;
  }
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

function applyProgressPayload(payload, { showTransientAlerts = true } = {}) {
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
    if (event.event === "observances_found") {
      const observances = Array.isArray(event.observances) ? event.observances : [];
      setText("live-stage", "Preparando celebraciones");
      setText(
        "episode-observances",
        observances.length
          ? `Celebraciones: ${observances.join(" · ")}`
          : "No se encontraron celebraciones para esa fecha.",
      );
      pushLimited(progressState.recentActivity, {
        title: "Celebraciones encontradas",
        badge: String(observances.length),
        meta: event.episode_date || "",
        copy: observances.join(" · ") || "Sin celebraciones disponibles.",
      });
    }
    if (event.event === "celebration_strategy") {
      const selected = Array.isArray(event.selected_observances) ? event.selected_observances : [];
      progressState.selectedObservances = selected;
      progressState.perObservanceTarget = Number(event.per_observance_target || 0);
      progressState.newCandidatesByObservance = Object.fromEntries(
        selected.map((observance) => [observance, 0]),
      );
      setText("live-stage", "Planificando búsquedas");
      pushLimited(progressState.recentActivity, {
        title: "Estrategia temática",
        badge: `${event.search_count || 0} búsquedas`,
        meta: selected.join(" · "),
        copy: progressState.perObservanceTarget
          ? `La estrategia buscará al menos ${progressState.perObservanceTarget} reseñas nuevas por celebración seleccionada.`
          : "La estrategia está lista y comienza revisando el archivo existente.",
      });
    }
    if (event.event === "archive_scan_started") {
      progressState.archivedChecked = 0;
      progressState.archivedTotal = Number(event.total || 0);
      setText("live-stage", "Revisando reseñas guardadas");
      setText(
        "status",
        progressState.archivedTotal
          ? `Buscando hasta ${event.target || 5} candidatas antiguas entre ${progressState.archivedTotal} reseñas guardadas.`
          : "No hay reseñas guardadas que revisar. Preparando búsquedas nuevas.",
      );
    }
    if (event.event === "archive_scan_progress") {
      progressState.archivedChecked = Number(event.checked || 0);
      progressState.archivedTotal = Number(event.total || progressState.archivedTotal);
      setText("live-stage", "Revisando reseñas guardadas");
      setText(
        "status",
        `Archivo: ${progressState.archivedChecked}/${progressState.archivedTotal} revisadas.`,
      );
    }
    if (event.event === "discovered_place") {
      setText("live-stage", "Descubriendo sitios");
      setText("live-place", event.place_name || event.place_id || byId("live-place")?.textContent || "-");
      upsertRecentActivity({
        placeKey: event.place_id || event.place_name,
        title: event.place_name || "Sitio descubierto",
        badge: "Sitio",
        meta: event.category || "Sin categoría",
        copy: "Entró en la cola de análisis.",
        href: placeDetailHref(event.place_id),
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
      upsertRecentActivity({
        placeKey: event.place_id || event.place_name,
        title: event.place_name || event.place_id || "Sitio en proceso",
        badge: "Leyendo",
        meta: "Inicio de recogida",
        copy: "Comenzando a recopilar reseñas de este sitio.",
        href: placeDetailHref(event.place_id),
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
      if (numericScore >= threshold) {
        progressState.aboveThreshold += 1;
        setText("live-above-threshold", String(progressState.aboveThreshold));
      }
      const scoreLabel = `#${numericScore}`;
      progressState.lastScoreText = reviewerName ? `${reviewerName}: ${scoreLabel}` : scoreLabel;
      setText("live-scores", progressState.lastScoreText);
      const reviewCopy = reviewerName
        ? `Última reseña puntuada: ${reviewerName}.`
        : "Nueva reseña puntuada en este sitio.";
      const placeKey = event.place_id || event.place_name || `place-${progressState.processedSites}`;
      const topPlace = upsertTopPlace({
        placeKey,
        place: event.place_name || event.place_id || "Sitio",
        score: numericScore,
        reviewCount: Number(event.review_count || 1),
        qualifyingIncrement: numericScore >= threshold ? 1 : 0,
        latestReviewer: reviewerName,
        href: placeDetailHref(event.place_id),
      });
      upsertRecentActivity({
        placeKey,
        title: topPlace.place,
        badge: `#${topPlace.score}`,
        meta: `${topPlace.reviewCount} reseñas puntuadas · mejor #${topPlace.score}`,
        copy: reviewCopy,
        href: topPlace.href,
      });
    }
    if (event.event === "theme_review_scored") {
      const candidate = Boolean(event.episode_candidate);
      const fromArchive = event.source === "archive";
      const numericHumorScore = Number(event.humor_score || 0);
      const reviewerName = String(event.reviewer_name || "").trim();
      if (candidate) {
        if (fromArchive) {
          progressState.archivedEpisodeCandidates += 1;
        } else {
          progressState.newEpisodeCandidates += 1;
          const observance = String(event.observance || "");
          if (Object.hasOwn(progressState.newCandidatesByObservance, observance)) {
            progressState.newCandidatesByObservance[observance] += 1;
          }
        }
        progressState.episodeCandidates += 1;
        setText("live-episode-candidates", String(progressState.episodeCandidates));
        const observanceProgress = progressState.perObservanceTarget
          ? progressState.selectedObservances
            .map((observance) => `${observance}: ${progressState.newCandidatesByObservance[observance] || 0}/${progressState.perObservanceTarget}`)
            .join(" · ")
          : "";
        setText(
          "status",
          observanceProgress
            ? `Nuevas: ${progressState.newEpisodeCandidates}. ${observanceProgress}. Antiguas: ${progressState.archivedEpisodeCandidates}.`
            : `Candidatas: ${progressState.newEpisodeCandidates} nuevas · ${progressState.archivedEpisodeCandidates} antiguas.`,
        );
      } else if (!fromArchive) {
        progressState.reusableFinds += 1;
        setText("live-reusable", String(progressState.reusableFinds));
      }
      const placeKey = event.place_id || event.place_name || event.review_id;
      if (fromArchive) {
        progressState.collectedReviews += 1;
        progressState.scoredReviews += 1;
        progressState.totalScore += numericHumorScore;
        progressState.topScore = progressState.topScore == null
          ? numericHumorScore
          : Math.max(progressState.topScore, numericHumorScore);
        progressState.aboveThreshold += 1;
        setText("live-count", String(progressState.collectedReviews));
        setText("live-above-threshold", String(progressState.aboveThreshold));
        if (candidate) {
          upsertTopPlace({
            placeKey,
            place: event.place_name || "Sitio archivado",
            score: numericHumorScore,
            reviewIncrement: 1,
            qualifyingIncrement: 1,
            latestReviewer: reviewerName,
            copy: `${event.observance || "Celebración"} · Relevancia #${event.relevance_score || 0}`,
            href: placeDetailHref(event.place_id),
          });
          setText("live-sites", String(progressState.topPlaces.length));
          progressState.processedSites = progressState.topPlaces.length;
          setText("live-processed-sites", String(progressState.processedSites));
          progressState.productivePlaces = progressState.topPlaces.length;
        }
        progressState.lastScoreText = reviewerName
          ? `${reviewerName}: #${numericHumorScore}`
          : `#${numericHumorScore}`;
        setText("live-scores", progressState.lastScoreText);
      }
      upsertRecentActivity({
        placeKey,
        title: event.place_name || "Reseña temática",
        badge: candidate
          ? (fromArchive ? "Antigua" : "Nueva")
          : (fromArchive ? "Archivo" : "Guardada"),
        meta: `Humor #${event.humor_score || 0} · Relevancia #${event.relevance_score || 0}`,
        copy: candidate
          ? `${event.observance || "Celebración"}: ${event.relevance_notes || "candidata relevante"}`
          : (fromArchive
            ? "La reseña del archivo no encaja con este episodio."
            : "Es graciosa y queda disponible para otro episodio."),
        href: placeDetailHref(event.place_id),
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
      const placeKey = event.place_id || event.place_name || `place-${progressState.processedSites}`;
      if (topScore != null) {
        upsertTopPlace({
          placeKey,
          place: event.place_name || event.place_id || "Sitio",
          score: topScore,
          reviewCount: scores.length,
          href: placeDetailHref(event.place_id),
        });
      }
      upsertRecentActivity({
        placeKey,
        title: event.place_name || event.place_id || "Sitio completado",
        badge: topScore == null ? "0" : `#${topScore}`,
        meta: scores.length ? `Top #${topScore}` : "Sin señal nueva",
        copy: scores.length ? `Se cerró el sitio con ${scores.length} reseñas puntuadas.` : "El sitio terminó sin reseñas nuevas.",
        href: placeDetailHref(event.place_id),
      });
    }
    if (event.event === "place_failed") {
      progressState.processedSites += 1;
      progressState.failedPlaceCount += 1;
      updateEta();
      setText("live-stage", "Error en un sitio");
      setText("live-place", event.place_name || event.place_id || byId("live-place")?.textContent || "-");
      setText("status", `Falló la recogida en ${event.place_name || event.place_id || "un sitio"}.`);
      const placeKey = event.place_id || event.place_name || `failed-${progressState.processedSites}`;
      upsertRecentActivity({
        placeKey,
        title: event.place_name || event.place_id || "Error en sitio",
        badge: "Error",
        meta: "Recogida fallida",
        copy: String(event.error || "Error no especificado"),
        href: placeDetailHref(event.place_id),
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
      const perObservanceTarget = Number(event.per_observance_target || 0);
      const countsByObservance = event.new_relevant_by_observance || {};
      const observanceSummary = perObservanceTarget
        ? Object.entries(countsByObservance)
          .map(([observance, count]) => `${observance}: ${count}/${perObservanceTarget}`)
          .join(" · ")
        : "";
      setText(
        "status",
        event.mode === "episode"
          ? `Finalizado. Nuevas: ${event.new_relevant || 0}/${event.target || 0}. ${observanceSummary ? `Por celebración: ${observanceSummary}. ` : ""}Antiguas: ${event.archived_relevant || 0}/${event.archive_target || event.target || 0}. Total: ${event.relevant || 0}. Guardadas para otros: ${event.reusable || 0}.`
          : `Finalizado. Sitios: ${event.discovered}, reseñas nuevas: ${event.collected}`,
      );
      setText("live-count", String(progressState.collectedReviews));
      const completedSiteCount = event.mode === "episode"
        ? Math.max(Number(event.discovered || 0), progressState.topPlaces.length)
        : Number(event.discovered ?? progressState.processedSites);
      setText("live-sites", String(completedSiteCount));
      progressState.processedSites = completedSiteCount;
      updateEta();
      setText("live-eta", "Completado");
      if (progressTimer) clearInterval(progressTimer);
      pushLimited(progressState.recentActivity, {
        title: "Ejecución completada",
        badge: "Done",
        meta: event.mode === "episode"
          ? `${event.new_relevant || 0}/${event.target || 0} nuevas · ${event.archived_relevant || 0} antiguas · ${event.reusable || 0} reutilizables`
          : `${event.discovered || 0} sitios · ${event.collected || 0} reseñas`,
        copy: event.mode === "episode"
          ? (observanceSummary || "La búsqueda temática terminó y todos los hallazgos graciosos quedaron guardados.")
          : "La ejecución terminó y ya no quedan sitios en cola.",
      });
      if (showTransientAlerts && Number(event.discovered || 0) === 0 && progressState.noResultsCount > 0) {
        const nameFilter = String(appConfig?.discovery?.name_contains || "").trim();
        const searchLabel = progressState.searchCount === 1 ? "1 búsqueda" : `${progressState.searchCount} búsquedas`;
        const filterDetail = nameFilter ? ` con "${nameFilter}" en el nombre` : "";
        showNoResults(`La ejecución terminó sin sitios válidos${filterDetail}. Se completaron ${searchLabel}.`);
      }
    }
    if (event.event === "run_started") {
      runFinished = false;
      progressState.failed = false;
      resetLiveProgress();
      setText("live-stage", "Ejecutando");
      setText("status", "Ejecutando pipeline...");
      pushLimited(progressState.recentActivity, {
        title: "Ejecución iniciada",
        badge: "Run",
        meta: event.mode === "episode" ? `Episodio ${event.episode_date || ""}` : "Pipeline semanal",
        copy: event.mode === "episode"
          ? `Buscando ${event.target || 5} reseñas nuevas y hasta ${event.archive_target || event.target || 5} antiguas relevantes.`
          : "Se ha puesto en marcha una nueva ejecución.",
      });
    }
    if (event.event === "run_failed") {
      runFinished = true;
      progressState.failed = true;
      setText("live-stage", "Error");
      setText("status", event.message || "Falló la ejecución. Revisa el log.");
      if (progressTimer) clearInterval(progressTimer);
      pushLimited(progressState.recentActivity, {
        title: "Ejecución fallida",
        badge: "Error",
        meta: "Pipeline detenido",
        copy: event.message || "La ejecución se interrumpió antes de terminar.",
      });
      renderLiveDashboard();
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
      if (event.reason === "filtered_out") {
        const bits = [];
        const rawResults = Number(event.raw_results || 0);
        if (rawResults > 0) bits.push(`La API devolvió ${rawResults} sitios, pero todos se descartaron después.`);
        const skippedRegion = Number(event.skipped_region || 0);
        const skippedNameContains = Number(event.skipped_name_contains || 0);
        const skippedMinReviews = Number(event.skipped_min_reviews || 0);
        const skippedRecent = Number(event.skipped_recent || 0);
        const skippedNoIds = Number(event.skipped_no_ids || 0);
        if (skippedRegion > 0) bits.push(`${skippedRegion} fuera de la región indicada.`);
        if (skippedNameContains > 0) {
          const expectedName = String(event.name_contains || "").trim();
          bits.push(expectedName
            ? `${skippedNameContains} porque el nombre no contiene "${expectedName}".`
            : `${skippedNameContains} por no coincidir con el filtro de nombre.`);
        }
        if (skippedMinReviews > 0) bits.push(`${skippedMinReviews} por no llegar al mínimo de reseñas.`);
        if (skippedRecent > 0) bits.push(`${skippedRecent} por antigüedad de reseñas.`);
        if (skippedNoIds > 0) bits.push(`${skippedNoIds} por datos incompletos.`);
        pushLimited(progressState.recentActivity, {
          title: "Búsqueda sin sitios válidos",
          badge: "0",
          meta: `${category} · ${region}`,
          copy: bits.join(" ") || "La búsqueda devolvió resultados, pero todos se descartaron.",
        });
      } else {
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
  closeNoResults();
  applyProgressPayload(payload, { showTransientAlerts: false });
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
  byId("scoring_provider")?.addEventListener("change", () => refreshScoringModels({ resetDefaults: true }));
  byId("scoring_model")?.addEventListener("change", () => updateOpenAIExecutionControls({ resetDefaults: true }));
  byId("scoring_reasoning_effort")?.addEventListener("change", updateOpenAIExecutionControls);
  byId("run-weekly")?.addEventListener("click", runWeekly);
  byId("run-episode")?.addEventListener("click", runEpisodeSearch);
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
