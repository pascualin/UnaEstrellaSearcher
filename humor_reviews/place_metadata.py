from __future__ import annotations

import re


COUNTRY_NAMES = {
    "AR": "Argentina",
    "DE": "Alemania",
    "ES": "España",
    "FR": "Francia",
    "GB": "Reino Unido",
    "IT": "Italia",
    "MX": "México",
    "PT": "Portugal",
    "UK": "Reino Unido",
    "US": "Estados Unidos",
}

KNOWN_COUNTRIES = {
    "alemania": "Alemania",
    "argentina": "Argentina",
    "españa": "España",
    "espana": "España",
    "france": "Francia",
    "francia": "Francia",
    "germany": "Alemania",
    "italia": "Italia",
    "italy": "Italia",
    "méxico": "México",
    "mexico": "México",
    "portugal": "Portugal",
    "reino unido": "Reino Unido",
    "spain": "España",
    "united kingdom": "Reino Unido",
    "united states": "Estados Unidos",
    "united states of america": "Estados Unidos",
    "usa": "Estados Unidos",
}


def country_name(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    return COUNTRY_NAMES.get(cleaned.upper(), KNOWN_COUNTRIES.get(cleaned.casefold(), cleaned))


def place_location(address: str, fallback_country: str = "") -> tuple[str, str, str]:
    parts = [part.strip() for part in str(address or "").split(",") if part.strip()]
    country = country_name(fallback_country)
    if parts:
        explicit_country = KNOWN_COUNTRIES.get(parts[-1].casefold())
        if explicit_country:
            country = explicit_country
            parts.pop()

    locality = ""
    province = ""
    for index in range(len(parts) - 1, -1, -1):
        part = parts[index]
        postal_prefix = re.match(r"^(?:[A-Z]\s*)?\d{4,5}[A-Z]{0,3}\s+(.+)$", part)
        if postal_prefix:
            locality = postal_prefix.group(1).strip()
            province = parts[index + 1] if index + 1 < len(parts) else locality
            break

        postal_suffix = re.match(r"^(.+?)\s+\d{5}(?:-\d{4})?$", part)
        if postal_suffix:
            province = postal_suffix.group(1).strip()
            locality = parts[index - 1] if index > 0 else province
            break

    if not locality and parts:
        locality = parts[-1]
        province = locality
    if not province:
        province = locality
    return locality, province, country


def format_place_location(address: str, fallback_country: str = "") -> str:
    locality, province, country = place_location(address, fallback_country)
    values = [locality, province, country]
    return ", ".join(value for value in values if value) or "Ubicación no disponible"
