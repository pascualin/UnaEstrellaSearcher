from __future__ import annotations

import re
from typing import Any, Iterable


# Current text-capable families from OpenAI's model catalog. The Models API is
# still the source of truth for what a particular project can actually use.
OPENAI_SCORING_MODELS = [
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-pro",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.2",
    "gpt-5.2-pro",
    "gpt-5.1",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-pro",
    "o3",
    "o3-pro",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-mini",
]

_EXCLUDED_MODEL_MARKERS = (
    "audio",
    "codex",
    "deep-research",
    "image",
    "instruct",
    "moderation",
    "realtime",
    "search",
    "transcribe",
    "tts",
)


def is_openai_scoring_model(model_id: str) -> bool:
    normalized = str(model_id or "").strip().lower()
    if not normalized or any(marker in normalized for marker in _EXCLUDED_MODEL_MARKERS):
        return False
    return normalized.startswith(("gpt-6", "gpt-5", "gpt-4.1", "gpt-4o")) or bool(
        re.match(r"^o\d", normalized)
    )


def openai_model_profile(model_id: str) -> dict[str, Any]:
    model = str(model_id or "").strip()
    normalized = model.lower()
    efforts: list[str] = []
    default_effort = ""
    modes: list[str] = []
    verbosity: list[str] = []

    is_pro_slug = "-pro" in normalized
    if normalized.startswith("gpt-6-astra"):
        efforts = ["low", "medium", "high", "xhigh", "max"]
        default_effort = "low"
    elif normalized.startswith(("gpt-6-sol", "gpt-6-luna", "gpt-5.6")):
        efforts = ["none", "low", "medium", "high", "xhigh", "max"]
        default_effort = "low"
    elif normalized.startswith("gpt-5.5") and not is_pro_slug:
        efforts = ["none", "low", "medium", "high", "xhigh"]
        default_effort = "none"
    elif normalized.startswith(("gpt-5.4", "gpt-5.2")) and not is_pro_slug:
        efforts = ["none", "low", "medium", "high", "xhigh"]
        default_effort = "none"
    elif normalized.startswith("gpt-5.1") and not is_pro_slug:
        efforts = ["none", "low", "medium", "high"]
        default_effort = "none"
    elif normalized.startswith("gpt-5") and not is_pro_slug:
        efforts = ["minimal", "low", "medium", "high"]
        default_effort = "low"
    elif re.match(r"^o\d", normalized) and not is_pro_slug:
        efforts = ["low", "medium", "high"]
        default_effort = "low"

    if normalized.startswith(("gpt-6", "gpt-5.6")):
        modes = ["standard", "pro"]
    if normalized.startswith(("gpt-6", "gpt-5")):
        verbosity = ["low", "medium", "high"]

    service_tiers = ["auto", "default", "fast"]
    if normalized.startswith(("gpt-6", "gpt-5")) or re.match(r"^o\d", normalized):
        service_tiers.insert(2, "flex")
    if normalized == "gpt-5.6" or normalized.startswith("gpt-5.6-sol"):
        service_tiers.append("ultrafast")

    supports_temperature = (not efforts and not is_pro_slug) or "none" in efforts
    return {
        "id": model,
        "reasoning_efforts": efforts,
        "default_reasoning_effort": default_effort,
        "reasoning_modes": modes,
        "default_reasoning_mode": "standard" if modes else "",
        "verbosity_options": verbosity,
        "default_verbosity": "low" if verbosity else "",
        "service_tiers": service_tiers,
        "default_service_tier": "auto",
        "supports_temperature": supports_temperature,
    }


def openai_model_catalog(available_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    if available_ids is None:
        model_ids = list(OPENAI_SCORING_MODELS)
    else:
        model_ids = sorted(
            {str(model_id).strip() for model_id in available_ids if is_openai_scoring_model(model_id)}
        )
        priority = {model_id: index for index, model_id in enumerate(OPENAI_SCORING_MODELS)}
        model_ids.sort(key=lambda model_id: (priority.get(model_id, len(priority)), model_id))
    return [openai_model_profile(model_id) for model_id in model_ids]
