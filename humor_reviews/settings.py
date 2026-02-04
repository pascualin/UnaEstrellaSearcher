from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass
class AppSettings:
    output_dir: Path
    data_dir: Path
    weekly_target_count: int
    humor_threshold: int
    max_reviews_per_place: int
    max_places_per_run: int
    allow_repeat_suggestions: bool
    locale: str
    enable_screenshots: bool
    screenshot_dir: Path
    screenshot_timeout_ms: int
    screenshot_max_per_run: int
    screenshot_debug: bool
    screenshot_mode: str


@dataclass
class DiscoverySettings:
    provider: str
    country: str
    regions: List[str]
    categories: List[str]
    min_total_reviews: int
    require_recent_days: int


@dataclass
class ProviderSettings:
    serpapi_api_key_env: str
    serpapi_hl: str
    serpapi_gl: str


@dataclass
class ScoringSettings:
    provider: str
    model: str
    api_key_env: str
    prompt: str
    temperature: float
    max_output_tokens: int


@dataclass
class SafetySettings:
    pii_patterns: List[str]
    sensitive_keywords: List[str]
    accusation_keywords: List[str]


@dataclass
class CurationSettings:
    theme_limits: Dict[str, int]


@dataclass
class Settings:
    app: AppSettings
    discovery: DiscoverySettings
    providers: ProviderSettings
    scoring: ScoringSettings
    safety: SafetySettings
    curation: CurationSettings


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_settings(path: str | Path = "config.yaml") -> Settings:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing config file at {path}. Copy config.example.yaml to config.yaml."
        )

    raw = _load_yaml(path)

    app_raw = raw.get("app", {})
    discovery_raw = raw.get("discovery", {})
    providers_raw = raw.get("providers", {}).get("serpapi", {})
    scoring_raw = raw.get("scoring", {})
    safety_raw = raw.get("safety", {})
    curation_raw = raw.get("curation", {})

    app = AppSettings(
        output_dir=Path(app_raw.get("output_dir", "out")),
        data_dir=Path(app_raw.get("data_dir", "data")),
        weekly_target_count=int(app_raw.get("weekly_target_count", 40)),
        humor_threshold=int(app_raw.get("humor_threshold", 55)),
        max_reviews_per_place=int(app_raw.get("max_reviews_per_place", 25)),
        max_places_per_run=int(app_raw.get("max_places_per_run", 40)),
        allow_repeat_suggestions=bool(app_raw.get("allow_repeat_suggestions", False)),
        locale=str(app_raw.get("locale", "en")),
        enable_screenshots=bool(app_raw.get("enable_screenshots", False)),
        screenshot_dir=Path(app_raw.get("screenshot_dir", "out/screenshots")),
        screenshot_timeout_ms=int(app_raw.get("screenshot_timeout_ms", 15000)),
        screenshot_max_per_run=int(app_raw.get("screenshot_max_per_run", 10)),
        screenshot_debug=bool(app_raw.get("screenshot_debug", False)),
        screenshot_mode=str(app_raw.get("screenshot_mode", "rendered")),
    )

    discovery = DiscoverySettings(
        provider=str(discovery_raw.get("provider", "google_places")),
        country=str(discovery_raw.get("country", "US")),
        regions=list(discovery_raw.get("regions", [])),
        categories=list(discovery_raw.get("categories", [])),
        min_total_reviews=int(discovery_raw.get("min_total_reviews", 100)),
        require_recent_days=int(discovery_raw.get("require_recent_days", 120)),
    )

    providers = ProviderSettings(
        serpapi_api_key_env=str(providers_raw.get("api_key_env", "SERPAPI_API_KEY")),
        serpapi_hl=str(providers_raw.get("hl", "es")),
        serpapi_gl=str(providers_raw.get("gl", "us")),
    )

    scoring = ScoringSettings(
        provider=str(scoring_raw.get("provider", "openai")),
        model=str(scoring_raw.get("model", "gpt-4o-mini")),
        api_key_env=str(scoring_raw.get("api_key_env", "OPENAI_API_KEY")),
        prompt=str(
            scoring_raw.get(
                "prompt",
                (
                    "Evalua UNA resena individual y devuelve SOLO una puntuacion de humor.\n"
                    "Devuelve un entero de 0 a 100 donde 0 es nada gracioso y 100 es muy gracioso.\n"
                    "Prioriza resenas de una estrella si son graciosas.\n"
                    "Nuestro humor es gamberro: insultos, situaciones dantescas y anecdotas graciosas.\n"
                    "Si hay respuesta del propietario graciosa y no es copia y pega, sube la puntuacion.\n"
                    "Devuelve JSON con score (0-100), notes (explicacion corta) y tags (lista).\n"
                    "Si no hay nada gracioso, pon una puntuacion baja.\n"
                    "No incluyas explicaciones ni texto extra.\n\n"
                    "ESTRELLAS:\n{rating}\n\n"
                    "RESENA:\n{review_text}\n\n"
                    "RESPUESTA DEL PROPIETARIO:\n{owner_reply}"
                ),
            )
        ),
        temperature=float(scoring_raw.get("temperature", 0.2)),
        max_output_tokens=int(scoring_raw.get("max_output_tokens", 20)),
    )

    safety = SafetySettings(
        pii_patterns=list(safety_raw.get("pii_patterns", [])),
        sensitive_keywords=list(safety_raw.get("sensitive_keywords", [])),
        accusation_keywords=list(safety_raw.get("accusation_keywords", [])),
    )

    curation = CurationSettings(
        theme_limits=dict(curation_raw.get("theme_limits", {})),
    )

    return Settings(
        app=app,
        discovery=discovery,
        providers=providers,
        scoring=scoring,
        safety=safety,
        curation=curation,
    )
