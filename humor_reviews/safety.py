from __future__ import annotations

import re
from dataclasses import dataclass

from .settings import SafetySettings


@dataclass
class SafetyResult:
    label: str
    notes: str


def assess_safety(text: str, owner_reply: str, settings: SafetySettings) -> SafetyResult:
    combined = f"{text} {owner_reply}".lower()
    notes = []
    label = "safe"

    for pat in settings.pii_patterns:
        if re.search(pat, combined, re.IGNORECASE):
            notes.append("Possible personal data")
            label = "caution"
            break

    if any(word in combined for word in settings.sensitive_keywords):
        notes.append("Sensitive topic detected")
        label = "caution"

    if any(word in combined for word in settings.accusation_keywords):
        notes.append("Criminal accusation language")
        label = "caution"

    if any(word in combined for word in ["child", "minor"]):
        notes.append("Mentions minors")
        label = "not_recommended"

    return SafetyResult(label=label, notes="; ".join(notes) or "No obvious risks")
