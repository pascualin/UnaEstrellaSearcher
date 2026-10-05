from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

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


if __name__ == "__main__":
    unittest.main()
