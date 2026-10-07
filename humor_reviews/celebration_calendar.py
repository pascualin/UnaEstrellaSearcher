from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests

from .api_cache import load_cached_json, save_cached_json


CALENDAR_BASE_URL = "https://www.diainternacionalde.com"
CACHE_VERSION = 2
MODERN_SECTION_IDS = {"dias-internacionales", "semanas-internacionales"}
MONTH_NAMES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]


@dataclass(frozen=True)
class Observance:
    name: str
    date: str
    source_url: str
    source: str = "diainternacionalde.com"


class _CalendarDayParser(HTMLParser):
    def __init__(self, target_heading: str):
        super().__init__(convert_charrefs=True)
        self.target_heading = _normalize(target_heading)
        self.target_page_heading = _normalize(f"El {target_heading} se celebra")
        self.heading_tag = ""
        self.heading_text: list[str] = []
        self.heading_href = ""
        self.heading_id = ""
        self.in_target_section = False
        self.is_target_page = False
        self.in_modern_section = False
        self.article_depth = 0
        self.observances: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "article":
            self.article_depth += 1
            return
        if tag == "a" and self.heading_tag:
            for key, value in attrs:
                if key == "href" and value and not self.heading_href:
                    self.heading_href = value
            return
        if tag not in {"h1", "h2", "h3"}:
            return
        self.heading_tag = tag
        self.heading_text = []
        self.heading_href = ""
        self.heading_id = ""
        for key, value in attrs:
            if key == "href" and value:
                self.heading_href = value
            elif key == "id" and value:
                self.heading_id = value
        if tag == "h2" and self.is_target_page:
            self.in_modern_section = self.heading_id in MODERN_SECTION_IDS

    def handle_data(self, data: str) -> None:
        if self.heading_tag:
            self.heading_text.append(data)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
            return
        if tag == "a" or tag != self.heading_tag:
            return
        text = " ".join("".join(self.heading_text).split())
        normalized = _normalize(text)
        if tag == "h1" and normalized == self.target_page_heading:
            self.is_target_page = True
        if tag in {"h1", "h2"}:
            if normalized == self.target_heading:
                self.in_target_section = True
            elif self.in_target_section and re.match(r"^\d{1,2} de ", normalized):
                self.in_target_section = False
        elif tag == "h3" and text and (
            self.in_target_section
            or (self.is_target_page and self.in_modern_section and self.article_depth > 0)
        ):
            self.observances.append((text, self.heading_href))
        self.heading_tag = ""
        self.heading_text = []
        self.heading_href = ""
        self.heading_id = ""


def fetch_observances(day: date, cache_dir: Path) -> list[Observance]:
    cache_payload = {
        "date": day.isoformat(),
        "source": "diainternacionalde.com",
        "parser_version": CACHE_VERSION,
    }
    cached = load_cached_json(cache_dir, "celebrations", cache_payload)
    if isinstance(cached, list) and cached:
        return [Observance(**item) for item in cached if isinstance(item, dict)]

    month_name = MONTH_NAMES[day.month - 1]
    urls = [
        f"{CALENDAR_BASE_URL}/calendario/{month_name}/{day.day}",
        f"{CALENDAR_BASE_URL}/mes/{month_name}",
    ]
    errors: list[str] = []
    for url in urls:
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "UnaEstrellaSearcher/1.0 (+local editorial research)"},
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue

        observances = parse_observances_html(response.text, day, url)
        if not observances:
            errors.append(f"{url}: la respuesta no contenía la fecha solicitada")
            continue
        save_cached_json(
            cache_dir,
            "celebrations",
            cache_payload,
            [asdict(observance) for observance in observances],
        )
        return observances

    detail = "; ".join(errors)
    raise RuntimeError(
        f"No se pudieron obtener las celebraciones del {day.isoformat()}. {detail}"
    )


def parse_observances_html(html: str, day: date, page_url: str) -> list[Observance]:
    month_name = MONTH_NAMES[day.month - 1]
    parser = _CalendarDayParser(f"{day.day} de {month_name}")
    parser.feed(html)
    seen: set[str] = set()
    observances: list[Observance] = []
    for name, href in parser.observances:
        normalized = _normalize(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        observances.append(
            Observance(
                name=name,
                date=day.isoformat(),
                source_url=urljoin(page_url, href) if href else page_url,
            )
        )
    return observances


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(ascii_text.casefold().split())
