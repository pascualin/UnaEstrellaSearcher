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
        self.heading_tag = ""
        self.heading_text: list[str] = []
        self.heading_href = ""
        self.in_target_section = False
        self.observances: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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
        for key, value in attrs:
            if key == "href" and value:
                self.heading_href = value

    def handle_data(self, data: str) -> None:
        if self.heading_tag:
            self.heading_text.append(data)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" or tag != self.heading_tag:
            return
        text = " ".join("".join(self.heading_text).split())
        normalized = _normalize(text)
        if tag in {"h1", "h2"}:
            if normalized == self.target_heading:
                self.in_target_section = True
            elif self.in_target_section and re.match(r"^\d{1,2} de ", normalized):
                self.in_target_section = False
        elif tag == "h3" and self.in_target_section and text:
            self.observances.append((text, self.heading_href))
        self.heading_tag = ""
        self.heading_text = []
        self.heading_href = ""


def fetch_observances(day: date, cache_dir: Path) -> list[Observance]:
    cache_payload = {"date": day.isoformat(), "source": "diainternacionalde.com"}
    cached = load_cached_json(cache_dir, "celebrations", cache_payload)
    if isinstance(cached, list):
        return [Observance(**item) for item in cached if isinstance(item, dict)]

    month_name = MONTH_NAMES[day.month - 1]
    url = f"{CALENDAR_BASE_URL}/calendario/{month_name}/{day.day}"
    response = requests.get(
        url,
        headers={"User-Agent": "UnaEstrellaSearcher/1.0 (+local editorial research)"},
        timeout=20,
    )
    response.raise_for_status()
    observances = parse_observances_html(response.text, day, url)
    save_cached_json(
        cache_dir,
        "celebrations",
        cache_payload,
        [asdict(observance) for observance in observances],
    )
    return observances


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
