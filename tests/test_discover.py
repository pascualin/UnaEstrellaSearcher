from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from humor_reviews.discover import _effective_categories, _name_matches, _serpapi_maps_search, discover_places
from humor_reviews.settings import DiscoverySettings, ProviderSettings


class NameContainsTests(unittest.TestCase):
    @patch("humor_reviews.discover.requests.get")
    def test_exact_place_lookup_can_return_a_direct_place_result_and_cache_it(self, get):
        metadata = {"title": "Baby Nails", "data_id": "0xabc:0x123"}
        get.return_value.json.return_value = {"place_results": metadata}
        get.return_value.request.url = "https://serpapi.com/search.json?api_key=test-key"
        get.return_value.status_code = 200
        get.return_value.text = "{}"
        with tempfile.TemporaryDirectory() as directory:
            arguments = ("Baby Nails", "test-key", "es", "", Path(directory))
            first, _ = _serpapi_maps_search(*arguments, include_place_result=True)
            cached, _ = _serpapi_maps_search(*arguments, include_place_result=True)
            default, _ = _serpapi_maps_search(*arguments)
        self.assertEqual(first, [metadata])
        self.assertEqual(cached, first)
        self.assertEqual(default, [])
        self.assertEqual(get.call_count, 2)

    def test_name_only_search_does_not_expand_default_categories(self) -> None:
        self.assertEqual(_effective_categories([], "dislexia"), [""])
        self.assertEqual(_effective_categories([], ""), [
            "restaurant",
            "bar",
            "cafe",
            "museum",
            "tourist attraction",
            "hotel",
        ])

    def test_name_match_ignores_case_accents_and_punctuation(self) -> None:
        place = {"title": "Café-NANDÚ | Centro"}

        self.assertTrue(_name_matches(place, "cafe nandu"))
        self.assertFalse(_name_matches(place, "patentes"))
        self.assertTrue(_name_matches({"title": "北京饭店"}, "北京"))

    @patch("humor_reviews.discover.time.sleep")
    @patch("humor_reviews.discover._serpapi_maps_search")
    def test_discovery_discards_places_without_required_name(
        self,
        search: Mock,
        _sleep: Mock,
    ) -> None:
        search.return_value = (
            [
                {
                    "place_id": "wrong",
                    "data_id": "wrong",
                    "title": "Dot Cafe Bar",
                    "reviews": 500,
                },
                {
                    "place_id": "right",
                    "data_id": "right",
                    "title": "Volartpons - Patentes y Marcas",
                    "reviews": 500,
                },
            ],
            True,
        )
        discovery = DiscoverySettings(
            provider="serpapi_maps",
            country="ES",
            regions=[],
            categories=["restaurant"],
            name_contains="PATÉNTES",
            min_total_reviews=100,
            require_recent_days=3650,
        )
        providers = ProviderSettings(
            serpapi_api_key_env="SERPAPI_API_KEY",
            serpapi_hl="es",
            serpapi_gl="es",
        )

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
                results = list(discover_places(discovery, providers, Path(directory)))

        self.assertEqual([result.place.name for result in results], ["Volartpons - Patentes y Marcas"])

    @patch("humor_reviews.discover.time.sleep")
    @patch("humor_reviews.discover._emit_discovery_progress")
    @patch("humor_reviews.discover._serpapi_maps_search")
    def test_discovery_reports_name_filter_rejections(
        self,
        search: Mock,
        emit_progress: Mock,
        _sleep: Mock,
    ) -> None:
        search.return_value = (
            [{"place_id": "wrong", "title": "Dot Cafe Bar", "reviews": 500}],
            True,
        )
        discovery = DiscoverySettings(
            provider="serpapi_maps",
            country="ES",
            regions=[],
            categories=["restaurant"],
            name_contains="Patentes",
            min_total_reviews=100,
            require_recent_days=3650,
        )
        providers = ProviderSettings(
            serpapi_api_key_env="SERPAPI_API_KEY",
            serpapi_hl="es",
            serpapi_gl="es",
        )

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
                results = list(discover_places(discovery, providers, Path(directory)))

        self.assertEqual(results, [])
        no_results = [
            call.args[1]
            for call in emit_progress.call_args_list
            if call.args and call.args[0] == "no_results"
        ]
        self.assertEqual(no_results[-1]["skipped_name_contains"], 1)
        self.assertEqual(no_results[-1]["name_contains"], "Patentes")

    @patch("humor_reviews.discover.time.sleep")
    @patch("humor_reviews.discover._serpapi_maps_search")
    def test_name_only_discovery_launches_one_query(
        self,
        search: Mock,
        _sleep: Mock,
    ) -> None:
        search.return_value = ([], True)
        discovery = DiscoverySettings(
            provider="serpapi_maps",
            country="ES",
            regions=[],
            categories=[],
            name_contains="dislexia",
            min_total_reviews=100,
            require_recent_days=3650,
        )
        providers = ProviderSettings(
            serpapi_api_key_env="SERPAPI_API_KEY",
            serpapi_hl="es",
            serpapi_gl="es",
        )

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
                self.assertEqual(list(discover_places(discovery, providers, Path(directory))), [])

        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["query"], "dislexia in Spain")


if __name__ == "__main__":
    unittest.main()
