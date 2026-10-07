from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

from humor_reviews.api_cache import save_cached_json
from humor_reviews.collect import RawReview, collect_reviews
from humor_reviews.humor import HumorResult
from humor_reviews.safety import SafetyResult
from humor_reviews.storage import Place, Review, Storage
from scripts import config_ui


def review_url(token: str, language: str = "es") -> str:
    return f"https://www.google.com/maps/reviews/data=!4m8!1s{token}!2m1!1s0x0:0x123!3m1!1s2@1:{token}?hl={language}"


def raw_review(token: str, rating: int = 1, text: str = "Texto") -> RawReview:
    link = review_url(token)
    return RawReview(
        review_id=f"data-1:{link}:Autor {token}",
        place_id="data-1",
        rating=rating,
        date="hoy",
        reviewer_name=f"Autor {token}",
        reviewer_profile_url="",
        text=text,
        owner_reply="",
        review_url=link,
    )


class GoogleReviewLinkTests(unittest.TestCase):
    short_url = "https://goo.gl/maps/TfZbDZE3rcniiQ2q7?g_st=atm"
    full_url = (
        "https://www.google.com/maps/reviews/data=!4m8!14m7!1m6!2m5"
        "!1sChdDSUhNMG9nS0VJQ0FnSUNIczREOW53RRAB!2m1!1s0x0:0x4398c57938932d33"
        "!3m1!1s2@1:CIHM0ogKEICAgICHs4D9nwE%7C%7C?hl=es"
    )

    def response(self, url: str, text: str = "", history=None) -> Mock:
        response = Mock()
        response.url = url
        response.text = text
        response.history = history or []
        return response

    def test_accepts_google_maps_short_links_but_not_other_short_links(self) -> None:
        for url in (self.short_url, "https://maps.app.goo.gl/review", self.full_url):
            with self.subTest(url=url):
                self.assertTrue(config_ui._is_google_review_link(url))
        for url in (
            "https://goo.gl/unrelated",
            "https://goo.gl/maps/",
            "https://goo.gl/maps/abc/other",
            "https://goo.gl.example.com/maps/abc",
            "https://example.com/review",
        ):
            with self.subTest(url=url):
                self.assertFalse(config_ui._is_google_review_link(url))

    @patch("scripts.config_ui.requests.get")
    def test_short_link_resolves_when_google_finishes_on_consent(self, get) -> None:
        get.return_value = self.response(
            "https://consent.google.com/ml?continue=" + quote(self.full_url, safe=""),
            history=[self.response(self.short_url), self.response(self.full_url)],
        )
        with patch.object(config_ui, "_resolve_place_data_id_from_db", return_value=""):
            resolved, data_id = config_ui._resolve_place_data_id(self.short_url)

        self.assertEqual(resolved, config_ui._normalize_review_url(self.full_url))
        self.assertEqual(data_id, "0x0:0x4398c57938932d33")
        self.assertEqual(
            config_ui._extract_review_id_from_url(resolved),
            "ChdDSUhNMG9nS0VJQ0FnSUNIczREOW53RRAB",
        )
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://goo.gl/maps/TfZbDZE3rcniiQ2q7")
        self.assertTrue(get.call_args.kwargs["allow_redirects"])

    @patch("scripts.config_ui.requests.get")
    def test_consent_continue_preserves_the_review_without_redirect_history(self, get) -> None:
        get.return_value = self.response(
            "https://consent.google.com/ml?continue=" + quote(self.full_url, safe=""),
            text="unrelated 0x123:0x456",
        )

        resolved, text = config_ui._resolve_review_url(self.short_url)

        self.assertEqual(resolved, config_ui._normalize_review_url(self.full_url))
        self.assertEqual(text, "")

    @patch("scripts.config_ui.requests.get")
    def test_short_link_rejects_a_non_google_maps_destination(self, get) -> None:
        get.return_value = self.response(
            "https://consent.google.com/ml?continue=" + quote("https://example.com/review", safe=""),
            history=[self.response(self.short_url)],
        )

        with self.assertRaisesRegex(RuntimeError, "no redirige"):
            config_ui._resolve_review_url(self.short_url)

    def test_cid_hint_does_not_match_identifiers_of_other_places(self) -> None:
        self.assertEqual(
            config_ui._extract_place_data_id_from_text("0x123:0x456", "0x4398c57938932d33"),
            "",
        )
        self.assertEqual(
            config_ui._extract_place_data_id_from_text(
                "0x0:0x4398c57938932d33 0x123:0x4398c57938932d33", "0x4398c57938932d33"
            ),
            "0x123:0x4398c57938932d33",
        )


class ManualUrlImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.config_path = root / "config.yaml"
        self.config_path.write_text(
            f"app:\n  data_dir: {root / 'data'}\n  humor_threshold: 60\n  max_reviews_per_place: 4\n"
            "discovery:\n  country: ''\nscoring: {}\nsafety: {}\n",
            encoding="utf-8",
        )
        self.config_patch = patch.object(config_ui, "CONFIG_PATH", self.config_path)
        self.config_patch.start()
        self.storage = Storage(root / "data" / "humor_reviews.db")
        self.storage.upsert_place(
            Place("place-1", "data-1", "Sitio", "Madrid", "restaurant", 100, None, "serpapi")
        )
        self.resolve = self.enter_patch("_resolve_place_data_id")
        self.resolve.return_value = (review_url("target"), "data-1")
        self.find = self.enter_patch("_find_review_in_serpapi")
        self.find.return_value = (
            {"place_info": {"title": "Sitio", "rating": 4.5}},
            {"link": review_url("target"), "user": {"name": "Autor target"}, "rating": 1, "snippet": "Texto"},
        )
        self.collect = self.enter_patch("collect_reviews")
        self.collect.return_value = []
        self.score = self.enter_patch("score_review")
        self.score.return_value = HumorResult(80, "buena", ["absurdo"], "")
        self.safety = self.enter_patch("assess_safety")
        self.safety.return_value = SafetyResult("safe", "")
        self.env_patch = patch.dict(config_ui.os.environ, {"SERPAPI_API_KEY": "test-key"})
        self.env_patch.start()

    def enter_patch(self, name: str) -> Mock:
        patcher = patch.object(config_ui, name)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.config_patch.stop()
        self.directory.cleanup()

    def store_review(self, raw: RawReview, score: int, status: str) -> None:
        self.storage.upsert_review(
            Review(
                raw.review_id, raw.place_id, raw.rating, raw.date,
                raw.reviewer_name, raw.reviewer_profile_url, raw.text, "",
                raw.owner_reply, raw.review_url, score, "Nota guardada", "safe", "", "absurdo",
                translated_text=raw.text, original_text_language="es",
            )
        )
        self.storage.update_status(raw.review_id, status)

    def test_url_import_groups_new_reviews_with_existing_site(self) -> None:
        self.collect.return_value = [
            raw_review("target"),
            raw_review("funny"),
            raw_review("weak"),
            raw_review("positive", rating=5),
            raw_review("empty", text=""),
        ]
        self.score.side_effect = [
            HumorResult(80, "target", [], ""),
            HumorResult(92, "funny", [], ""),
            HumorResult(15, "weak", [], ""),
        ]
        progress = []

        result = config_ui._import_review_from_url(
            review_url("target"), "Ana", on_progress=progress.append
        )

        self.assertEqual(result["detail_url"], "/place?id=place-1")
        self.assertEqual(result["new_reviews"], 3)
        self.assertEqual(result["additional_reviews"], 2)
        self.assertEqual(result["review_count"], 3)
        self.assertEqual(self.score.call_count, 3)
        self.assertEqual(self.collect.call_args.args[2], 4)
        self.assertTrue(self.collect.call_args.kwargs["raise_on_error"])
        self.assertTrue(any(item.get("detail_url") for item in progress))
        with sqlite3.connect(self.storage.db_path) as conn:
            places = conn.execute("SELECT place_id, data_id, address FROM places").fetchall()
            rows = conn.execute(
                "SELECT reviewer_name, humor_score, status, submitted_by FROM reviews ORDER BY humor_score DESC"
            ).fetchall()
        self.assertEqual(places, [("place-1", "data-1", "Madrid")])
        self.assertEqual(rows, [
            ("Autor funny", 92, "new", ""),
            ("Autor target", 80, "new", "Ana"),
            ("Autor weak", 15, "rejected", ""),
        ])

    def test_existing_reviews_keep_scores_and_moderation_across_url_variants(self) -> None:
        target = raw_review("target")
        saved = raw_review("saved")
        self.store_review(target, 95, "accepted")
        self.store_review(saved, 85, "rejected")
        variant = raw_review("saved")
        variant.review_url = review_url("saved", "en")
        variant.review_id = f"data-1:{variant.review_url}:Autor saved"
        self.collect.return_value = [target, variant, raw_review("fresh")]

        result = config_ui._import_review_from_url(review_url("target"))

        self.assertTrue(result["already_exists"])
        self.assertEqual(result["new_reviews"], 1)
        self.assertEqual(result["review_count"], 3)
        self.score.assert_called_once()
        with sqlite3.connect(self.storage.db_path) as conn:
            rows = conn.execute(
                "SELECT reviewer_name, humor_score, status FROM reviews ORDER BY reviewer_name"
            ).fetchall()
        self.assertEqual(rows, [
            ("Autor fresh", 80, "new"),
            ("Autor saved", 85, "rejected"),
            ("Autor target", 95, "accepted"),
        ])

    def test_additional_failure_keeps_the_linked_review_and_site_link(self) -> None:
        def fail_after_one(*args, **kwargs):
            yield raw_review("extra")
            raise RuntimeError("SerpAPI unavailable")

        self.collect.side_effect = fail_after_one

        result = config_ui._import_review_from_url(review_url("target"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["review_count"], 2)
        self.assertEqual(result["detail_url"], "/place?id=place-1")
        self.assertIn("SerpAPI unavailable", result["warning"])
        self.assertTrue(self.storage.review_exists(raw_review("target").review_id))

    def test_processed_site_stops_before_any_collection_or_scoring(self) -> None:
        config_ui._set_place_processed("place-1", True)

        with self.assertRaisesRegex(ValueError, "ya está marcado como procesado"):
            config_ui._import_review_from_url(review_url("target"))

        self.find.assert_not_called()
        self.collect.assert_not_called()
        self.score.assert_not_called()

    def test_marking_site_processed_during_import_stops_additional_scoring(self) -> None:
        def mark_processed(*args, **kwargs):
            config_ui._set_place_processed("place-1", True)
            yield raw_review("extra")

        self.collect.side_effect = mark_processed

        result = config_ui._import_review_from_url(review_url("target"))

        self.assertEqual(result["new_reviews"], 1)
        self.assertIn("procesado", result["warning"])
        self.score.assert_called_once()

    def test_collection_reports_fetch_errors_to_manual_import(self) -> None:
        settings = config_ui.load_settings(self.config_path)
        with patch("humor_reviews.collect._serpapi_reviews", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                list(collect_reviews(
                    ["data-1"], settings.providers, 4,
                    settings.app.data_dir / "api_cache", raise_on_error=True,
                ))


    def test_cid_alias_uses_the_existing_site_and_canonical_review_owner(self) -> None:
        self.storage.upsert_place(Place(
            "canonical", "0xabc:0x123", "Restaurante Peter", "Alicante, Espa\u00f1a",
            "restaurant", 100, None, "serpapi", average_rating=4.5,
        ))
        self.resolve.return_value = (review_url("target"), "0x0:0x123")
        self.find.return_value = ({}, self.find.return_value[1])
        self.collect.return_value = [raw_review("extra")]

        result = config_ui._import_review_from_url(review_url("target"))

        self.assertEqual(result["place_name"], "Restaurante Peter")
        self.assertEqual(result["detail_url"], "/place?id=canonical")
        with sqlite3.connect(self.storage.db_path) as conn:
            owners = conn.execute("SELECT DISTINCT place_id FROM reviews").fetchall()
            count = conn.execute("SELECT COUNT(*) FROM places WHERE name='Restaurante Peter'").fetchone()[0]
        self.assertEqual(owners, [("canonical",)])
        self.assertEqual(count, 1)
        self.assertEqual(config_ui._resolve_place_data_id_from_db(review_url("target")), "0xabc:0x123")

    def test_processed_cid_alias_stops_before_fetching_reviews(self) -> None:
        self.storage.upsert_place(Place(
            "canonical", "0xabc:0x123", "Peter", "", "restaurant", 0, None, "serpapi",
        ))
        config_ui._set_place_processed("canonical", True)
        self.resolve.return_value = (review_url("target"), "0x0:0x123")

        with self.assertRaisesRegex(ValueError, "procesado"):
            config_ui._import_review_from_url(review_url("target"))

        self.find.assert_not_called()
        self.score.assert_not_called()


class ManualPlaceMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config_path = root / "config.yaml"
        self.config_path.write_text(
            f"app:\n  data_dir: {root / 'data'}\ndiscovery:\n  country: ''\n",
            encoding="utf-8",
        )
        config_patch = patch.object(config_ui, "CONFIG_PATH", self.config_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        env_patch = patch.dict(config_ui.os.environ, {"SERPAPI_API_KEY": "test-key"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        config_ui._place_metadata_lookups.clear()
        self.addCleanup(config_ui._place_metadata_lookups.clear)
        self.storage = Storage(root / "data" / "humor_reviews.db")
        self.cache_dir = root / "data" / "api_cache"
        self.metadata = {
            "title": "Restaurante Peter", "address": "03509 Cala De, Alicante, Espa\u00f1a",
            "type": ["Restaurante mediterr\u00e1neo"], "rating": 4.5, "reviews": 2427,
            "data_id": "0xabc:0x123", "data_cid": "291", "place_id": "canonical",
        }

    @patch.object(config_ui, "_fetch_import_place_metadata")
    def test_missing_place_info_fetches_real_site_and_preserves_review_data(self, fetch) -> None:
        self.storage.upsert_place(Place(
            "0x0:0x123", "0x0:0x123", "Importado manualmente", "", "manual", 0, None, "serpapi",
        ))
        for status in ("accepted", "rejected"):
            raw = raw_review(status)
            self.storage.upsert_review(Review(
                raw.review_id, "0x0:0x123", 1, raw.date, raw.reviewer_name, "", raw.text,
                "", "", raw.review_url, 90, "Nota", "safe", "", "",
                translated_text="Traducci\u00f3n", original_text_language="en",
            ))
            self.storage.update_status(raw.review_id, status)
        with sqlite3.connect(self.storage.db_path) as conn:
            conn.execute("UPDATE places SET notion_page_url='https://notion.so/saved'")
            before = conn.execute("SELECT * FROM reviews ORDER BY review_id").fetchall()
        fetch.return_value = self.metadata

        place = config_ui._upsert_place_from_reviews_payload(self.storage, "0x0:0x123", {}, raw_review("target"))

        fetch.assert_called_once_with("0x0:0x123")
        self.assertEqual(place.place_id, "0x0:0x123")
        self.assertEqual(place.name, "Restaurante Peter")
        self.assertEqual(place.address, self.metadata["address"])
        self.assertEqual(place.category, "Restaurante mediterr\u00e1neo")
        self.assertEqual(place.average_rating, 4.5)
        self.assertEqual(place.country, "Espa\u00f1a")
        self.assertEqual(place.total_reviews, 2427)
        self.assertEqual(place.place_url, "https://www.google.com/maps?cid=291")
        with sqlite3.connect(self.storage.db_path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM reviews ORDER BY review_id").fetchall(), before)
            self.assertEqual(conn.execute("SELECT notion_page_url FROM places").fetchone()[0], "https://notion.so/saved")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0], 1)

    @patch.object(config_ui, "_fetch_import_place_metadata", return_value={})
    def test_unknown_sites_get_distinct_content_names_and_reimport_keeps_name(self, fetch) -> None:
        raw = raw_review("cold", text="Nos quejamos del fr\u00edo, no tienen calefacci\u00f3n.")
        first = config_ui._upsert_place_from_reviews_payload(self.storage, "0x0:0x123", {}, raw)
        second = config_ui._upsert_place_from_reviews_payload(self.storage, "0x0:0x456", {}, raw)
        repeated = config_ui._upsert_place_from_reviews_payload(
            self.storage, "0x0:0x123", {}, raw_review("other", text="El arroz estaba fatal."),
        )

        self.assertIn("el fr\u00edo", first.name)
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.name, repeated.name)
        self.assertNotIn("Importado manualmente", first.name)

    def test_unknown_topic_uses_review_excerpt(self) -> None:
        name = config_ui._review_based_place_name("El camarero parec\u00eda un astronauta. Incre\u00edble.", "id")
        self.assertIn("El camarero parec\u00eda un astronauta", name)
        self.assertNotIn("Incre\u00edble", name)

    @patch.object(config_ui, "_fetch_import_place_metadata", side_effect=RuntimeError("offline"))
    def test_metadata_failure_does_not_prevent_usable_content_name(self, fetch) -> None:
        place = config_ui._upsert_place_from_reviews_payload(
            self.storage, "0x0:0x123", {}, raw_review("target", text="El arroz estaba fatal."),
        )
        self.assertIn("el arroz", place.name)
        self.assertEqual(place.place_url, "https://www.google.com/maps?cid=291")

    @patch.object(config_ui, "_fetch_import_place_metadata")
    def test_complete_payload_needs_no_extra_request(self, fetch) -> None:
        place = config_ui._upsert_place_from_reviews_payload(
            self.storage, "0x0:0x123", {"place_info": self.metadata}, raw_review("target"),
        )
        fetch.assert_not_called()
        self.assertEqual(place.place_id, "canonical")
        self.assertEqual(place.data_id, "0xabc:0x123")

    @patch.object(config_ui, "_fetch_import_place_metadata", return_value={})
    def test_empty_metadata_preserves_known_site_information(self, fetch) -> None:
        known = Place(
            "canonical", "0xabc:0x123", "Peter", "Alicante, Espa\u00f1a", "restaurant", 50,
            None, "serpapi", "https://www.google.com/maps?cid=291", 4.5, "Espa\u00f1a",
        )
        self.storage.upsert_place(known)

        place = config_ui._upsert_place_from_reviews_payload(self.storage, "0x0:0x123", {}, raw_review("target"))

        fetch.assert_not_called()
        for field in ("place_id", "data_id", "name", "address", "category", "total_reviews", "place_url", "average_rating", "country"):
            self.assertEqual(getattr(place, field), getattr(known, field))

    @patch.object(config_ui, "_fetch_import_place_metadata")
    def test_fetched_google_place_id_reuses_existing_site(self, fetch) -> None:
        self.storage.upsert_place(Place(
            "canonical", "", "Peter", "", "restaurant", 0, None, "serpapi",
        ))
        fetch.return_value = self.metadata

        place = config_ui._upsert_place_from_reviews_payload(self.storage, "0x0:0x123", {}, raw_review("target"))

        self.assertEqual(place.place_id, "canonical")
        self.assertEqual(place.data_id, "0xabc:0x123")
        with sqlite3.connect(self.storage.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0], 1)

    @patch.object(config_ui, "OpenAI")
    def test_capture_without_place_name_gets_content_name_without_an_extra_ai_call(self, client) -> None:
        payload = {
            "place_name": "", "reviewer_name": "Ana", "rating": 1, "date": "hoy",
            "review_text": "No tienen calefacci\u00f3n y pasamos fr\u00edo.", "owner_reply_text": "",
            "owner_reply_date": "", "place_address": "",
        }
        response = Mock(status="completed", output_text=json.dumps(payload))
        client.return_value.responses.create.return_value = response
        with patch.dict(config_ui.os.environ, {"OPENAI_API_KEY": "test-key"}):
            result = config_ui._extract_review_from_images([{"bytes": b"image", "mime_type": "image/png"}])

        self.assertIn("el fr\u00edo", result["place_name"])
        self.assertNotEqual(result["place_name"], "Sitio importado desde captura")
        client.return_value.responses.create.assert_called_once()

    @patch("scripts.config_ui.requests.get")
    def test_place_lookup_uses_exact_decimal_cid_and_reuses_cache(self, get) -> None:
        get.return_value.json.return_value = {"place_results": self.metadata}

        first = config_ui._fetch_import_place_metadata("0x0:0x123")
        second = config_ui._fetch_import_place_metadata("0xabc:0x123")

        self.assertEqual(first, self.metadata)
        self.assertEqual(second, self.metadata)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["params"]["data_cid"], "291")
        self.assertEqual(get.call_args.kwargs["params"]["engine"], "google_maps")
        self.assertEqual(config_ui._cached_place_metadata(["0x0:0x123"]), self.metadata)

    @patch("scripts.config_ui.requests.get")
    def test_place_lookup_rejects_wrong_or_unidentified_site(self, get) -> None:
        for result in (
            {**self.metadata, "data_id": "0xabc:0x456"},
            {**self.metadata, "data_cid": "999"},
            {"title": "Wrong restaurant"},
        ):
            with self.subTest(result=result):
                get.return_value.json.return_value = {"place_results": result}
                self.assertEqual(config_ui._fetch_import_place_metadata("0x0:0x123"), {})

    def test_empty_cached_review_metadata_does_not_hide_other_page_metadata(self) -> None:
        save_cached_json(self.cache_dir, "reviews", {"page": 1}, {
            "search_parameters": {"data_id": "0x0:0x123"}, "place_info": {},
        })
        save_cached_json(self.cache_dir, "reviews", {"page": 2}, {
            "search_parameters": {"data_id": "0xabc:0x123"}, "place_info": self.metadata,
        })
        self.assertEqual(config_ui._cached_place_metadata(["0x0:0x123"]), self.metadata)


class ManualImportJobTests(unittest.TestCase):
    def setUp(self) -> None:
        with config_ui._review_import_lock:
            config_ui._review_import_job.clear()

    def tearDown(self) -> None:
        with config_ui._review_import_lock:
            config_ui._review_import_job.clear()

    def test_background_job_returns_immediately_and_rejects_duplicate_starts(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def import_site(url, submitted_by, on_progress):
            on_progress({"detail_url": "/place?id=site", "message": "Analizando"})
            started.set()
            release.wait(3)
            return {"ok": True, "place_name": "Sitio", "new_reviews": 2, "review_count": 2}

        original_runner = config_ui._run_review_import

        def runner(*args):
            try:
                original_runner(*args)
            finally:
                finished.set()

        with patch.object(config_ui, "_import_review_from_url", side_effect=import_site):
            with patch.object(config_ui, "_run_review_import", side_effect=runner):
                job = config_ui._start_review_import(review_url("target"), "Ana")
                try:
                    self.assertIsNotNone(job)
                    self.assertTrue(started.wait(1))
                    self.assertEqual(config_ui._review_import_status()["status"], "running")
                    self.assertEqual(config_ui._review_import_status()["detail_url"], "/place?id=site")
                    self.assertIsNone(config_ui._start_review_import(review_url("target"), "Ana"))
                finally:
                    release.set()
                    self.assertTrue(finished.wait(2))

        self.assertEqual(config_ui._review_import_status()["status"], "completed")

    def test_job_surfaces_an_error_and_allows_retry(self) -> None:
        with config_ui._review_import_lock:
            config_ui._review_import_job.update({"id": "job-1", "status": "running"})
        with patch.object(config_ui, "_import_review_from_url", side_effect=ValueError("Sitio procesado")):
            config_ui._run_review_import("job-1", review_url("target"), "")

        status = config_ui._review_import_status()
        self.assertEqual(status["status"], "failed")
        self.assertFalse(status["ok"])
        self.assertEqual(status["message"], "Sitio procesado")

    def test_image_job_runs_in_background_and_shares_the_import_lock(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        images = [{"bytes": b"test-image", "mime_type": "image/png"}]
        def import_capture(received_images, url, sender, on_progress):
            self.assertEqual(received_images, images)
            self.assertEqual(sender, "Cristina")
            on_progress({"message": "Analizando otras reseñas", "detail_url": "/place?id=baby-nails", "additional_reviews": 1})
            started.set()
            release.wait(3)
            return {"ok": True, "source": "image", "place_name": "Baby Nails", "new_reviews": 2, "review_count": 2}
        original_runner = config_ui._run_review_import
        def runner(*args):
            try:
                original_runner(*args)
            finally:
                finished.set()
        with patch.object(config_ui, "_import_review_from_images", side_effect=import_capture):
            with patch.object(config_ui, "_run_review_import", side_effect=runner):
                job = config_ui._start_review_import("", "Cristina", images=images)
                try:
                    self.assertEqual(job["source"], "image")
                    self.assertTrue(started.wait(1))
                    status = config_ui._review_import_status()
                    self.assertEqual(status["source"], "image")
                    self.assertEqual(status["detail_url"], "/place?id=baby-nails")
                    self.assertEqual(status["additional_reviews"], 1)
                    self.assertIsNone(config_ui._start_review_import(review_url("target"), "Ana"))
                    self.assertIsNone(config_ui._start_review_import("", "Cristina", images=images))
                finally:
                    release.set()
                    self.assertTrue(finished.wait(2))
        self.assertEqual(config_ui._review_import_status()["status"], "completed")

    @patch.object(config_ui, "_start_review_import")
    def test_capture_endpoint_returns_background_job_and_conflict(self, start):
        body = json.dumps({"images": [{"image_data": "data:image/png;base64,dGVzdA==", "mime_type": "image/png"}], "submitted_by": "Cristina"}).encode()
        for job, status in (({"id": "image-job", "ok": True, "source": "image", "status": "running"}, 202), (None, 409)):
            with self.subTest(status=status):
                handler = object.__new__(config_ui.Handler)
                handler.path = "/api/import-review-image"
                handler.headers = {"Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                handler._require_auth = Mock(return_value=True)
                handler._send = Mock()
                start.return_value = job

                handler.do_POST()

                self.assertEqual(handler._send.call_args.args[0], status)
                self.assertEqual(start.call_args.args, ("", "Cristina"))
                self.assertEqual(start.call_args.kwargs["images"], [{"bytes": b"test", "mime_type": "image/png"}])

    def test_empty_decoded_capture_is_rejected_without_starting_a_job(self):
        with patch.object(config_ui.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "captura"):
                config_ui._start_review_import("", "", images=[])
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
