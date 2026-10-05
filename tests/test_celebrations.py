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
from humor_reviews.celebration_relevance import (
    RelevanceResult,
    score_celebration_relevance,
    score_celebration_relevance_local,
)
from humor_reviews.celebration_strategy import (
    CelebrationStrategy,
    SearchPlan,
    build_celebration_strategy_from_text,
    observance_exclusion_reason,
)
from humor_reviews.collect import RawReview
from humor_reviews.discover import DiscoveredPlace
from humor_reviews.humor import HumorResult
from humor_reviews.run import (
    _episode_new_target_met,
    _scope_episode_searches,
    run_episode_search,
)
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
                    "score": 99,
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

        self.assertEqual(result.score, 99)
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

    def test_local_relevance_recognizes_place_name_variants(self) -> None:
        result = score_celebration_relevance_local(
            "El servicio fue un desastre.",
            "",
            "La Pulpería de Victoria",
            "restaurant",
            ["Día Internacional del Pulpo"],
        )

        self.assertGreaterEqual(result.score, 60)
        self.assertEqual(result.observance, "Día Internacional del Pulpo")

    def test_local_relevance_does_not_treat_generic_space_as_outer_space(self) -> None:
        result = score_celebration_relevance_local(
            "El espacio entre las mesas era mínimo y no se podía pasar.",
            "",
            "Bar del Centro",
            "restaurant",
            ["Semana Mundial del Espacio"],
        )

        self.assertEqual(result.score, 0)

    def test_local_relevance_recognizes_specific_space_signals(self) -> None:
        result = score_celebration_relevance_local(
            "Fuimos a buscar aliens y no vimos ninguno.",
            "",
            "Area 51",
            "tourist attraction",
            ["Semana Mundial del Espacio"],
        )

        self.assertGreaterEqual(result.score, 60)

    def test_local_relevance_does_not_match_alien_with_allan_herndon_dudley(self) -> None:
        result = score_celebration_relevance_local(
            "Fuimos a buscar aliens y no vimos ninguno.",
            "",
            "Area 51",
            "tourist attraction",
            ["Día Mundial del Síndrome de Allan-Herndon-Dudley o Deficiencia de #MCT8"],
        )

        self.assertEqual(result.score, 0)
        self.assertEqual(result.observance, "")


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
        self.assertEqual(len(strategy.searches), 12)

    @patch("humor_reviews.celebration_strategy.OpenAI")
    def test_local_strategy_expands_conflict_searches_to_full_budget(self, openai: Mock) -> None:
        strategy = build_celebration_strategy_from_text(
            "Día de la Resolución de Conflictos",
            _settings("typesafe"),
        )

        openai.assert_not_called()
        queries = [item.query for item in strategy.searches]
        self.assertEqual(len(queries), 12)
        self.assertIn("centro de mediación", queries)
        self.assertIn("terapia de pareja", queries)
        self.assertNotIn("la Resolución de Conflictos", queries)

    @patch("humor_reviews.celebration_strategy.OpenAI")
    def test_openai_strategy_rejects_loose_museum_association(self, openai: Mock) -> None:
        openai.return_value.chat.completions.create.return_value = Mock(
            choices=[
                Mock(
                    finish_reason="stop",
                    message=Mock(
                        content=json.dumps(
                            {
                                "selected_observances": ["Día Internacional del Pulpo"],
                                "discarded_observances": [],
                                "notes": "Búsquedas del pulpo",
                                "searches": [
                                    {
                                        "query": "restaurantes de pulpo",
                                        "region": "España",
                                        "rationale": "Relación directa",
                                    },
                                    {
                                        "query": "museos del mar",
                                        "region": "España",
                                        "rationale": "Asociación lateral",
                                    },
                                ],
                            }
                        ),
                    ),
                )
            ]
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            strategy = build_celebration_strategy_from_text(
                "Día Internacional del Pulpo",
                _settings("openai"),
            )

        queries = [item.query for item in strategy.searches]
        self.assertEqual(len(queries), 12)
        self.assertIn("restaurantes de pulpo", queries)
        self.assertNotIn("museos del mar", queries)

    @patch("humor_reviews.celebration_strategy.OpenAI")
    def test_local_strategy_discards_unsuitable_medical_observances(self, openai: Mock) -> None:
        syndrome = (
            "Día Mundial del Síndrome de Allan-Herndon-Dudley o Deficiencia de #MCT8"
        )
        strategy = build_celebration_strategy_from_text(
            f"{syndrome}\nSemana Mundial del Espacio",
            _settings("typesafe"),
        )

        openai.assert_not_called()
        self.assertEqual(strategy.selected_observances, ["Semana Mundial del Espacio"])
        self.assertEqual(strategy.discarded_observances, [syndrome])
        self.assertFalse(
            any(
                "allan" in item.query.casefold() or "mct8" in item.query.casefold()
                for item in strategy.searches
            )
        )

    @patch("humor_reviews.celebration_strategy.OpenAI")
    def test_local_strategy_discards_sensitive_observances(self, openai: Mock) -> None:
        sensitive = "Día Internacional de las Víctimas de la Violencia"
        strategy = build_celebration_strategy_from_text(
            f"{sensitive}\nDía Internacional del Pulpo",
            _settings("typesafe"),
        )

        openai.assert_not_called()
        self.assertEqual(strategy.selected_observances, ["Día Internacional del Pulpo"])
        self.assertEqual(strategy.discarded_observances, [sensitive])
        self.assertEqual(observance_exclusion_reason(sensitive), "Tema sensible")

    @patch("humor_reviews.celebration_strategy.OpenAI")
    def test_local_strategy_gives_each_observance_a_query_before_repeating(self, openai: Mock) -> None:
        observances = [f"Día Internacional del Tema {index}" for index in range(1, 8)]
        strategy = build_celebration_strategy_from_text(
            "\n".join(observances),
            _settings("typesafe"),
        )

        openai.assert_not_called()
        first_round = strategy.searches[: len(observances)]
        self.assertEqual(
            {search.rationale for search in first_round},
            {f"Búsqueda relacionada con {observance}." for observance in observances},
        )

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
    @patch("humor_reviews.run.build_celebration_strategy_from_text")
    @patch("humor_reviews.run.fetch_observances")
    def test_episode_search_uses_only_explicitly_selected_observances(
        self,
        fetch_calendar: Mock,
        build_strategy: Mock,
        emit: Mock,
    ) -> None:
        pulpo = "Día Internacional del Pulpo"
        vision = "Día Mundial de la Visión"
        fetch_calendar.return_value = [
            Observance(pulpo, "2026-10-08", "https://example.com/pulpo"),
            Observance(vision, "2026-10-08", "https://example.com/vision"),
        ]
        build_strategy.return_value = CelebrationStrategy([vision], [], "", [])

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
                episode_date=date(2026, 10, 8),
                target_reviews=5,
                humor_threshold=60,
                relevance_threshold=60,
                max_searches=5,
                max_places=10,
                max_reviews_per_place=10,
                selected_observance_names=[vision],
            )

        build_strategy.assert_called_once_with(
            vision,
            settings.scoring,
            search_limit=5,
        )
        started_event = next(
            call.args[1]
            for call in emit.call_args_list
            if call.args[0] == "run_started"
        )
        self.assertEqual(started_event["mode"], "episode")
        self.assertEqual(started_event["humor_threshold"], 60)
        self.assertEqual(started_event["relevance_threshold"], 60)
        found_event = next(
            call.args[1]
            for call in emit.call_args_list
            if call.args[0] == "observances_found"
        )
        self.assertEqual(found_event["observances"], [vision])
        complete_event = next(
            call.args[1]
            for call in emit.call_args_list
            if call.args[0] == "run_complete"
        )
        self.assertFalse(complete_event["target_met"])
        self.assertEqual(complete_event["completion_reason"], "searches_exhausted")
        self.assertEqual(complete_event["searches_attempted"], 0)

    def test_multiple_observances_require_three_new_reviews_each(self) -> None:
        observances = ["Día del Pulpo", "Día de la Visión"]

        self.assertFalse(
            _episode_new_target_met(
                {f"pulpo-{index}" for index in range(5)},
                {"Día del Pulpo": 5, "Día de la Visión": 0},
                observances,
                5,
            )
        )
        self.assertTrue(
            _episode_new_target_met(
                {f"review-{index}" for index in range(6)},
                {"Día del Pulpo": 3, "Día de la Visión": 3},
                observances,
                5,
            )
        )

    def test_global_target_still_applies_with_multiple_observances(self) -> None:
        self.assertFalse(
            _episode_new_target_met(
                {f"review-{index}" for index in range(6)},
                {"Día del Pulpo": 3, "Día de la Visión": 3},
                ["Día del Pulpo", "Día de la Visión"],
                10,
            )
        )

    def test_episode_search_scope_uses_config_instead_of_planner_region(self) -> None:
        searches = [SearchPlan("pulpería", "Madrid", "Pulpo")]

        configured = _scope_episode_searches(searches, ["Barcelona"], "", 5)
        country_wide = _scope_episode_searches(searches, [], "ES", 5)
        unrestricted = _scope_episode_searches(searches, [], "", 5)

        self.assertEqual([item.region for item in configured], ["Barcelona"])
        self.assertEqual([item.region for item in country_wide], ["Spain"])
        self.assertEqual([item.region for item in unrestricted], [""])

    @patch("humor_reviews.run._emit_progress")
    @patch("humor_reviews.run.assess_safety")
    @patch("humor_reviews.run.score_review")
    @patch("humor_reviews.run.score_celebration_relevance")
    @patch("humor_reviews.run.collect_reviews")
    @patch("humor_reviews.run.discover_places_for_queries")
    @patch("humor_reviews.run.build_celebration_strategy_from_text")
    @patch("humor_reviews.run.fetch_observances")
    def test_episode_search_continues_past_place_limit_until_target(
        self,
        fetch_calendar: Mock,
        build_strategy: Mock,
        discover: Mock,
        collect: Mock,
        relevance: Mock,
        humor: Mock,
        safety: Mock,
        emit: Mock,
    ) -> None:
        observance = "Día del Chocolate"
        fetch_calendar.return_value = [
            Observance(observance, "2026-09-13", "https://example.com/chocolate")
        ]
        build_strategy.return_value = CelebrationStrategy(
            [observance],
            [],
            "",
            [
                SearchPlan("chocolatería", "Madrid", "Primera"),
                SearchPlan("tienda de chocolate", "Madrid", "Segunda"),
            ],
        )
        places = [
            Place("place-1", "data-1", "Primera", "Madrid", "shop", 10, None, "test"),
            Place("place-2", "data-2", "Segunda", "Madrid", "shop", 10, None, "test"),
        ]
        discover.side_effect = [[DiscoveredPlace(place)] for place in places]
        collect.side_effect = [
            [RawReview("review-1", "data-1", 1, "hoy", "A", "", "Normal", "", "u1")],
            [RawReview("review-2", "data-2", 1, "hoy", "B", "", "Graciosa", "", "u2")],
        ]
        humor.side_effect = [
            HumorResult(10, "floja", ["poco_gracioso"], ""),
            HumorResult(90, "buena", ["absurdo"], ""),
        ]
        relevance.return_value = RelevanceResult(90, observance, "Encaja")
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
                max_searches=2,
                max_places=1,
                max_reviews_per_place=10,
            )

        self.assertEqual(discover.call_count, 2)
        complete_event = next(
            call.args[1]
            for call in emit.call_args_list
            if call.args[0] == "run_complete"
        )
        self.assertTrue(complete_event["target_met"])
        self.assertEqual(complete_event["discovered"], 2)
        self.assertEqual(complete_event["completion_reason"], "target_met")

    @patch("humor_reviews.run._emit_progress")
    @patch("humor_reviews.run.assess_safety")
    @patch("humor_reviews.run.score_review")
    @patch("humor_reviews.run.score_celebration_relevance")
    @patch("humor_reviews.run.collect_reviews")
    @patch("humor_reviews.run.discover_places_for_queries")
    @patch("humor_reviews.run.build_celebration_strategy_from_text")
    @patch("humor_reviews.run.fetch_observances")
    def test_episode_search_reaches_each_observance_quota(
        self,
        fetch_calendar: Mock,
        build_strategy: Mock,
        discover: Mock,
        collect: Mock,
        relevance: Mock,
        humor: Mock,
        safety: Mock,
        emit: Mock,
    ) -> None:
        pulpo = "Día Internacional del Pulpo"
        vision = "Día Mundial de la Visión"
        fetch_calendar.return_value = [
            Observance(pulpo, "2026-10-08", "https://example.com/pulpo"),
            Observance(vision, "2026-10-08", "https://example.com/vision"),
        ]
        build_strategy.return_value = CelebrationStrategy(
            [pulpo, vision],
            [],
            "",
            [
                SearchPlan("restaurante de pulpo", "Spain", "Pulpo"),
                SearchPlan("óptica", "Spain", "Visión"),
            ],
        )
        places = [
            Place(
                "pulpo-place",
                "pulpo-data",
                "Pulpería",
                "Madrid",
                "restaurant",
                10,
                None,
                "test",
            ),
            Place(
                "vision-place",
                "vision-data",
                "Óptica",
                "Madrid",
                "optician",
                10,
                None,
                "test",
            ),
        ]
        discover.side_effect = [[DiscoveredPlace(place)] for place in places]
        collect.side_effect = [
            [
                RawReview(
                    f"pulpo-{index}",
                    "pulpo-data",
                    1,
                    "hoy",
                    "A",
                    "",
                    "Pulpo",
                    "",
                    f"u-p-{index}",
                )
                for index in range(5)
            ],
            [
                RawReview(
                    f"vision-{index}",
                    "vision-data",
                    1,
                    "hoy",
                    "B",
                    "",
                    "Visión",
                    "",
                    f"u-v-{index}",
                )
                for index in range(3)
            ],
        ]
        relevance.side_effect = [
            *[RelevanceResult(90, pulpo, "Encaja") for _ in range(5)],
            *[RelevanceResult(90, vision, "Encaja") for _ in range(3)],
        ]
        humor.return_value = HumorResult(90, "buena", ["absurdo"], "")
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
                episode_date=date(2026, 10, 8),
                target_reviews=5,
                humor_threshold=60,
                relevance_threshold=60,
                max_searches=2,
                max_places=10,
                max_reviews_per_place=10,
            )

        self.assertEqual(discover.call_count, 2)
        complete_event = next(
            call.args[1]
            for call in emit.call_args_list
            if call.args[0] == "run_complete"
        )
        self.assertEqual(complete_event["new_relevant"], 8)
        self.assertEqual(
            complete_event["new_relevant_by_observance"],
            {pulpo: 5, vision: 3},
        )
        self.assertEqual(complete_event["per_observance_target"], 3)
        self.assertTrue(complete_event["target_met"])
        self.assertEqual(complete_event["completion_reason"], "target_met")
        self.assertEqual(complete_event["searches_attempted"], 2)
        self.assertEqual(discover.call_args_list[0].args[1].min_total_reviews, 1)

    @patch("humor_reviews.run._emit_progress")
    @patch("humor_reviews.run.assess_safety")
    @patch("humor_reviews.run.score_review")
    @patch("humor_reviews.run.score_celebration_relevance")
    @patch("humor_reviews.run.collect_reviews")
    @patch("humor_reviews.run.discover_places_for_queries")
    @patch("humor_reviews.run.build_celebration_strategy_from_text")
    @patch("humor_reviews.run.fetch_observances")
    def test_existing_reviews_are_not_scanned_for_episode_search(
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
            Observance("Día del Chocolate", "2026-09-13", "https://example.com")
        ]
        build_strategy.return_value = CelebrationStrategy(
            ["Día del Chocolate"],
            [],
            "",
            [SearchPlan("chocolatería", "Madrid", "Relacionado")],
        )
        relevance.return_value = RelevanceResult(95, "Día del Chocolate", "Encaja.")
        humor.return_value = HumorResult(90, "buena", ["absurdo"], "")
        safety.return_value = SafetyResult("safe", "")
        new_place = Place(
            "place-new", "data-new", "Chocolatería Nueva", "Madrid", "shop", 10, None, "test"
        )
        discover.return_value = [DiscoveredPlace(new_place)]
        collect.return_value = [
            RawReview(
                "new-review",
                "data-new",
                1,
                "hoy",
                "Autor nuevo",
                "",
                "Chocolate nuevo muy gracioso",
                "",
                "url-new",
            )
        ]

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
            storage.upsert_review(
                Review(
                    "archived-extra",
                    "data-1",
                    1,
                    "ayer",
                    "Otro autor",
                    "",
                    "Más chocolate gracioso",
                    "",
                    "",
                    "url-extra",
                    89,
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
            )
            with sqlite3.connect(storage.db_path) as conn:
                matches = conn.execute(
                    """
                    SELECT review_id, is_episode_candidate, source
                    FROM celebration_review_matches ORDER BY source, review_id
                    """
                ).fetchall()
                relevant_count = conn.execute(
                    "SELECT relevant_count FROM celebration_runs"
                ).fetchone()[0]

        discover.assert_called_once()
        self.assertEqual(
            matches,
            [("new-review", 1, "search")],
        )
        self.assertEqual(relevant_count, 1)
        self.assertEqual(relevance.call_count, 1)
        theme_event = next(
            call.args[1]
            for call in _emit.call_args_list
            if call.args[0] == "theme_review_scored"
        )
        self.assertEqual(theme_event["place_id"], "data-new")
        self.assertEqual(theme_event["place_data_id"], "data-new")
        self.assertEqual(theme_event["google_place_id"], "place-new")
        archive_events = [
            call.args[0]
            for call in _emit.call_args_list
            if call.args[0].startswith("archive_scan")
        ]
        self.assertEqual(archive_events, [])
        complete_event = next(
            call.args[1]
            for call in _emit.call_args_list
            if call.args[0] == "run_complete"
        )
        self.assertEqual(complete_event["new_relevant"], 1)
        self.assertNotIn("archived_relevant", complete_event)

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

    @patch("humor_reviews.run._emit_progress")
    @patch("humor_reviews.run.assess_safety")
    @patch("humor_reviews.run.score_review")
    @patch("humor_reviews.run.score_celebration_relevance")
    @patch("humor_reviews.run.collect_reviews")
    @patch("humor_reviews.run.discover_places_for_queries")
    @patch("humor_reviews.run.build_celebration_strategy_from_text")
    @patch("humor_reviews.run.fetch_observances")
    def test_episode_search_skips_processed_places_before_collecting_reviews(
        self,
        fetch_calendar: Mock,
        build_strategy: Mock,
        discover: Mock,
        collect: Mock,
        relevance: Mock,
        humor: Mock,
        safety: Mock,
        emit: Mock,
    ) -> None:
        observance = "Día del Chocolate"
        fetch_calendar.return_value = [
            Observance(observance, "2026-09-13", "https://example.com/chocolate")
        ]
        build_strategy.return_value = CelebrationStrategy(
            [observance],
            [],
            "",
            [SearchPlan("chocolatería", "Madrid", "Relacionado")],
        )
        processed_place = Place(
            "processed-place", "processed-data", "Antigua", "Madrid", "shop", 10, None, "test"
        )
        fresh_place = Place(
            "fresh-place", "fresh-data", "Nueva", "Madrid", "shop", 10, None, "test"
        )
        discover.return_value = [
            DiscoveredPlace(processed_place),
            DiscoveredPlace(fresh_place),
        ]
        collect.return_value = [
            RawReview(
                "fresh-review",
                "fresh-data",
                1,
                "hoy",
                "Autor",
                "",
                "Chocolate muy gracioso",
                "",
                "https://example.com/review",
            )
        ]
        humor.return_value = HumorResult(90, "buena", ["absurdo"], "")
        relevance.return_value = RelevanceResult(90, observance, "Encaja")
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
            storage.upsert_place(processed_place)
            with sqlite3.connect(storage.db_path) as conn:
                conn.execute(
                    "UPDATE places SET processed_at='2026-09-30T12:00:00' WHERE place_id=?",
                    (processed_place.place_id,),
                )

            run_episode_search(
                storage,
                settings,
                episode_date=date(2026, 9, 13),
                target_reviews=1,
                humor_threshold=60,
                relevance_threshold=60,
                max_searches=1,
                max_places=10,
                max_reviews_per_place=10,
            )

        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[0], ["fresh-data"])
        humor.assert_called_once()
        skipped = [
            call.args[1]
            for call in emit.call_args_list
            if call.args[0] == "processed_place_skipped"
        ]
        self.assertEqual(
            skipped,
            [{"place_id": "processed-place", "place_name": "Antigua"}],
        )


if __name__ == "__main__":
    unittest.main()
