from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from humor_reviews.celebration_calendar import fetch_observances, parse_observances_html
from humor_reviews.celebration_calendar import Observance
from humor_reviews.celebration_relevance import RelevanceResult, score_celebration_relevance
from humor_reviews.celebration_strategy import (
    CelebrationStrategy,
    SearchPlan,
    build_celebration_strategy_from_text,
)
from humor_reviews.collect import RawReview
from humor_reviews.discover import DiscoveredPlace
from humor_reviews.humor import HumorResult
from humor_reviews.run import run_episode_search
from humor_reviews.safety import SafetyResult
from humor_reviews.settings import ScoringSettings, load_settings
from humor_reviews.storage import Place, Review, Storage


HTML = """
<html><body>
  <h2>12 de septiembre</h2>
  <h3><a href="/dias/dia-mundial-de-la-arepa">Día Mundial de la Arepa</a></h3>
  <h2>13 de septiembre</h2>
  <h3><a href="/dias/dia-internacional-del-chocolate">Día Internacional del Chocolate</a></h3>
  <h3><a href="/dias/dia-del-programador">Día del Programador</a></h3>
  <h2>14 de septiembre</h2>
  <h3>Día Mundial de la Dermatitis Atópica</h3>
</body></html>
"""

MODERN_HTML = """
<html><body>
  <div style="display:none">
    <h2>8 de septiembre</h2>
    <h3><a href="/ficha/dia-internacional-alfabetizacion">Día Internacional de la Alfabetización</a></h3>
  </div>
  <h1>El 8 de octubre se celebra</h1>
  <section>
    <h2 id="dias-internacionales">Días Internacionales y Mundiales</h2>
    <article><h3><a href="/ficha/dia-internacional-pulpo">Día Internacional del Pulpo</a></h3></article>
    <article><h3><a href="/ficha/dia-internacional-dislexia">Día Internacional de la Dislexia</a></h3></article>
  </section>
  <section>
    <h2 id="semanas-internacionales">Semanas Internacionales y Mundiales</h2>
    <article><h3><a href="/semanas-internacionales/semana-mundial-espacio">Semana Mundial del Espacio</a></h3></article>
    <h3><a href="/semanas-internacionales/calendario/octubre">Semanas de octubre</a></h3>
  </section>
  <section>
    <h2 id="efemerides">Efemérides</h2>
    <article><h3>Un acontecimiento que no es una celebración</h3></article>
  </section>
</body></html>
"""


def _settings(provider: str = "typesafe") -> ScoringSettings:
    uses_typesafe = provider == "typesafe"
    return ScoringSettings(
        provider=provider,
        model="jev-latest" if uses_typesafe else "gpt-test",
        api_key_env="TYPESAFE_API_KEY" if uses_typesafe else "OPENAI_API_KEY",
        prompt="",
        reasoning_effort="none",
        reasoning_mode="standard",
        verbosity="low",
        service_tier="auto",
        temperature=0.2,
        max_output_tokens=320,
    )


class CelebrationCalendarTests(unittest.TestCase):
    def test_parser_returns_only_observances_for_requested_day(self) -> None:
        observances = parse_observances_html(
            HTML,
            date(2026, 9, 13),
            "https://www.diainternacionalde.com/calendario/septiembre/13",
        )

        self.assertEqual(
            [item.name for item in observances],
            ["Día Internacional del Chocolate", "Día del Programador"],
        )
        self.assertEqual(
            observances[0].source_url,
            "https://www.diainternacionalde.com/dias/dia-internacional-del-chocolate",
        )

    def test_parser_supports_current_daily_page_without_collecting_other_sections(self) -> None:
        observances = parse_observances_html(
            MODERN_HTML,
            date(2026, 10, 8),
            "https://www.diainternacionalde.com/calendario/octubre/8",
        )

        self.assertEqual(
            [item.name for item in observances],
            [
                "Día Internacional del Pulpo",
                "Día Internacional de la Dislexia",
                "Semana Mundial del Espacio",
            ],
        )

    @patch("humor_reviews.celebration_calendar.requests.get")
    def test_calendar_response_is_cached_by_episode_date(self, get: Mock) -> None:
        response = Mock(text=HTML)
        response.raise_for_status.return_value = None
        get.return_value = response

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            first = fetch_observances(date(2026, 9, 13), cache_dir)
            second = fetch_observances(date(2026, 9, 13), cache_dir)

        self.assertEqual(first, second)
        get.assert_called_once()

    @patch("humor_reviews.celebration_calendar.requests.get")
    def test_calendar_falls_back_to_month_page(self, get: Mock) -> None:
        missing_day = Mock(text="<html><h1>Contenido incompleto</h1></html>")
        missing_day.raise_for_status.return_value = None
        month_page = Mock(text=HTML)
        month_page.raise_for_status.return_value = None
        get.side_effect = [missing_day, month_page]

        with tempfile.TemporaryDirectory() as directory:
            observances = fetch_observances(date(2026, 9, 13), Path(directory))

        self.assertEqual(len(observances), 2)
        self.assertEqual(get.call_count, 2)


class CelebrationRelevanceTests(unittest.TestCase):
    @patch("humor_reviews.celebration_relevance.OpenAI")
    def test_relevance_is_contextual_and_uses_openai_planning_model(self, openai: Mock) -> None:
        openai.return_value.responses.create.return_value = Mock(
            output_text=json.dumps(
                {
                    "score": 84,
                    "observance": "Día Internacional del Chocolate",
                    "notes": "Ocurre en una chocolatería.",
                }
            )
        )

        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-key", "OPENAI_PLANNING_MODEL": "planning-test"},
        ):
            result = score_celebration_relevance(
                "El chocolate llegó derretido.",
                "",
                "Chocolatería Central",
                "chocolate_shop",
                ["Día Internacional del Chocolate"],
                _settings("openai"),
            )

        self.assertEqual(result.score, 84)
        self.assertEqual(result.observance, "Día Internacional del Chocolate")
        request = openai.return_value.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "planning-test")
        self.assertIn("No valores si es graciosa", request["instructions"])

    @patch("humor_reviews.celebration_relevance.OpenAI")
    def test_typesafe_configuration_uses_local_relevance(self, openai: Mock) -> None:
        result = score_celebration_relevance(
            "El pulpo estaba duro como una piedra.",
            "",
            "Pulpería Central",
            "restaurant",
            ["Día Internacional del Pulpo"],
            _settings("typesafe"),
        )

        openai.assert_not_called()
        self.assertGreaterEqual(result.score, 60)
        self.assertEqual(result.observance, "Día Internacional del Pulpo")


class CelebrationStrategyTests(unittest.TestCase):
    @patch("humor_reviews.celebration_strategy.OpenAI")
    def test_typesafe_configuration_builds_strategy_without_openai(self, openai: Mock) -> None:
        strategy = build_celebration_strategy_from_text(
            "Día Internacional del Pulpo\nDía Internacional de la Dislexia\nSemana Mundial del Espacio",
            _settings("typesafe"),
        )

        openai.assert_not_called()
        self.assertEqual(len(strategy.selected_observances), 3)
        self.assertIn("restaurante de pulpo", [item.query for item in strategy.searches])
        self.assertIn("planetario", [item.query for item in strategy.searches])

    @patch("humor_reviews.celebration_strategy.OpenAI")
    def test_openai_quota_error_has_actionable_message(self, openai: Mock) -> None:
        openai.return_value.chat.completions.create.side_effect = RuntimeError(
            "credit_balance_exhausted"
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with self.assertRaisesRegex(RuntimeError, "OpenAI no tiene saldo"):
                build_celebration_strategy_from_text(
                    "Día Internacional del Pulpo",
                    _settings("openai"),
                )


class EpisodeSearchTests(unittest.TestCase):
    @patch("humor_reviews.run._emit_progress")
    @patch("humor_reviews.run.score_celebration_relevance")
    @patch("humor_reviews.run.discover_places_for_queries")
    @patch("humor_reviews.run.build_celebration_strategy_from_text")
    @patch("humor_reviews.run.fetch_observances")
    def test_archive_is_checked_before_external_searches(
        self,
        fetch_calendar: Mock,
        build_strategy: Mock,
        discover: Mock,
        relevance: Mock,
        _emit: Mock,
    ) -> None:
        fetch_calendar.return_value = [
            Observance("Día del Chocolate", "2026-09-13", "https://example.com")
        ]
        build_strategy.return_value = CelebrationStrategy(
            ["Día del Chocolate"],
            [],
            "",
            [SearchPlan("chocolatería", "Madrid", "Relacionado")],
        )
        relevance.return_value = RelevanceResult(95, "Día del Chocolate", "Encaja.")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                f"app:\n  data_dir: {root / 'data'}\ndiscovery:\n  country: ES\nscoring: {{}}\nsafety: {{}}\n",
                encoding="utf-8",
            )
            settings = load_settings(config_path)
            storage = Storage(settings.app.data_dir / "humor_reviews.db")
            storage.upsert_place(
                Place("place-1", "data-1", "Chocolatería", "Madrid", "shop", 10, None, "test")
            )
            storage.upsert_review(
                Review(
                    "archived",
                    "data-1",
                    1,
                    "hoy",
                    "Autor",
                    "",
                    "Chocolate muy gracioso",
                    "",
                    "",
                    "url",
                    90,
                    "buena",
                    "safe",
                    "",
                    "absurdo",
                )
            )
            run_episode_search(
                storage,
                settings,
                date(2026, 9, 13),
                1,
                60,
                60,
                5,
                10,
                10,
                40,
            )
            with sqlite3.connect(storage.db_path) as conn:
                match = conn.execute(
                    "SELECT review_id, is_episode_candidate, source FROM celebration_review_matches"
                ).fetchone()

        discover.assert_not_called()
        self.assertEqual(match, ("archived", 1, "archive"))

    @patch("humor_reviews.run._emit_progress")
    @patch("humor_reviews.run.assess_safety")
    @patch("humor_reviews.run.score_review")
    @patch("humor_reviews.run.score_celebration_relevance")
    @patch("humor_reviews.run.collect_reviews")
    @patch("humor_reviews.run.discover_places_for_queries")
    @patch("humor_reviews.run.build_celebration_strategy_from_text")
    @patch("humor_reviews.run.fetch_observances")
    def test_funny_irrelevant_reviews_are_kept_for_future_episodes(
        self,
        fetch_calendar: Mock,
        build_strategy: Mock,
        discover: Mock,
        collect: Mock,
        relevance: Mock,
        humor: Mock,
        safety: Mock,
        _emit: Mock,
    ) -> None:
        fetch_calendar.return_value = [
            Observance(
                name="Día Internacional del Chocolate",
                date="2026-09-13",
                source_url="https://example.com/chocolate",
            )
        ]
        build_strategy.return_value = CelebrationStrategy(
            selected_observances=["Día Internacional del Chocolate"],
            discarded_observances=[],
            notes="",
            searches=[SearchPlan("chocolatería", "Madrid", "Relacionado")],
        )
        place = Place(
            place_id="place-1",
            data_id="data-1",
            name="Chocolatería",
            address="Madrid",
            category="chocolate_shop",
            total_reviews=100,
            last_review_date=None,
            provider="test",
        )
        discover.return_value = [DiscoveredPlace(place)]
        collect.return_value = [
            RawReview("funny-later", "data-1", 1, "hoy", "A", "", "Muy graciosa", "", "u1"),
            RawReview("episode", "data-1", 1, "hoy", "B", "", "Chocolate absurdo", "", "u2"),
        ]
        humor.side_effect = [
            HumorResult(88, "buena", ["absurdo"], ""),
            HumorResult(82, "buena", ["anecdota"], ""),
        ]
        relevance.side_effect = [
            RelevanceResult(25, "", "No está relacionada."),
            RelevanceResult(91, "Día Internacional del Chocolate", "Relación directa."),
        ]
        safety.return_value = SafetyResult("safe", "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                f"app:\n  data_dir: {root / 'data'}\ndiscovery:\n  country: ES\nscoring: {{}}\nsafety: {{}}\n",
                encoding="utf-8",
            )
            settings = load_settings(config_path)
            storage = Storage(settings.app.data_dir / "humor_reviews.db")
            run_episode_search(
                storage,
                settings,
                episode_date=date(2026, 9, 13),
                target_reviews=1,
                humor_threshold=60,
                relevance_threshold=60,
                max_searches=5,
                max_places=10,
                max_reviews_per_place=10,
                max_archived_candidates=0,
            )
            with sqlite3.connect(storage.db_path) as conn:
                reviews = conn.execute(
                    "SELECT review_id, status FROM reviews ORDER BY review_id"
                ).fetchall()
                run = conn.execute(
                    "SELECT relevant_count, reusable_count FROM celebration_runs"
                ).fetchone()
                matches = conn.execute(
                    """
                    SELECT review_id, is_episode_candidate
                    FROM celebration_review_matches ORDER BY review_id
                    """
                ).fetchall()

        self.assertEqual(reviews, [("episode", "new"), ("funny-later", "new")])
        self.assertEqual(run, (1, 1))
        self.assertEqual(matches, [("episode", 1), ("funny-later", 0)])


if __name__ == "__main__":
    unittest.main()
