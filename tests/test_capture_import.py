from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

from humor_reviews.humor import HumorResult
from humor_reviews.collect import RawReview
from humor_reviews.safety import SafetyResult
from humor_reviews.storage import Place
from scripts import config_ui


class CaptureImportTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            f"app:\n  data_dir: {self.root / 'data'}\n"
            "scoring:\n  model: gpt-6-sol\n  reasoning_effort: low\n",
            encoding="utf-8",
        )
        config = patch.object(config_ui, "CONFIG_PATH", self.config_path)
        config.start()
        self.addCleanup(config.stop)
        env = patch.dict(config_ui.os.environ, {"OPENAI_API_KEY": "test-key", "SERPAPI_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)
        client = patch.object(config_ui, "OpenAI")
        self.client = client.start().return_value
        self.addCleanup(client.stop)
        self.payload = {
            "place_name": "Restaurante de prueba", "reviewer_name": "Ana", "rating": 1,
            "date": "hoy", "review_text": "El arroz estaba congelado.",
            "owner_reply_text": "", "owner_reply_date": "", "place_address": "Alicante",
        }
        self.client.responses.create.return_value = Mock(
            status="completed", output_text=json.dumps(self.payload),
        )
        self.images = [{"bytes": b"first-image", "mime_type": "image/png"}]

    def prepare_site_lookup(self):
        env = patch.dict(config_ui.os.environ, {"SERPAPI_API_KEY": "test-serp-key"})
        env.start()
        self.addCleanup(env.stop)
        self.metadata = {
            "title": self.payload["place_name"], "address": "Alicante, Espa\u00f1a",
            "rating": 4.5, "reviews": 120, "type": "Restaurante",
            "data_id": "0xabc:0x123", "place_id": "google-place", "link": "https://google.com/maps?cid=291",
        }
        for name, result in (
            ("_serpapi_maps_search", ([self.metadata], False)),
            ("_resolve_place_data_id", ("https://google.com/maps/place/test", "0xabc:0x123")),
            ("_fetch_import_place_metadata", self.metadata),
            ("collect_reviews", []),
        ):
            patcher = patch.object(config_ui, name, return_value=result)
            setattr(self, name.lstrip("_"), patcher.start())
            self.addCleanup(patcher.stop)

    def extra_review(self, token="extra", rating=1, text="Rese\u00f1a adicional"):
        return RawReview(
            "api-review:" + token, "0xabc:0x123", rating, "hoy", "Autor " + token,
            "", text, "", "https://google.com/maps/reviews/data=!1s" + token + "!",
        )

    def test_configured_reasoning_model_uses_responses_without_temperature(self) -> None:
        result = config_ui._extract_review_from_images(self.images)

        self.assertEqual(result["place_name"], self.payload["place_name"])
        self.assertEqual(result["review_text"], self.payload["review_text"])
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-6-sol")
        self.assertEqual(request["reasoning"], {"effort": "low", "mode": "standard"})
        self.assertNotIn("temperature", request)
        self.assertNotIn("messages", request)
        self.assertNotIn("max_completion_tokens", request)
        self.assertGreaterEqual(request["max_output_tokens"], 4096)
        self.assertFalse(request["store"])
        self.assertTrue(request["text"]["format"]["strict"])
        self.client.chat.completions.create.assert_not_called()

    def test_model_compatibility_and_invalid_options_use_supported_defaults(self) -> None:
        for model, effort, expected_effort, temperature in (
            ("gpt-4o-mini", "low", None, True),
            ("gpt-5", "none", "low", False),
            ("gpt-5-mini", "none", "low", False),
            ("gpt-5.2", "none", "none", True),
            ("gpt-5.2", "high", "high", False),
            ("gpt-6-astra", "none", "low", False),
            ("gpt-6-sol", "none", "none", True),
            ("gpt-5.4-pro", "none", None, False),
        ):
            with self.subTest(model=model, effort=effort):
                settings = config_ui.load_settings(self.config_path)
                settings.scoring.model = model
                settings.scoring.reasoning_effort = effort
                with patch.object(config_ui, "load_settings", return_value=settings):
                    config_ui._extract_review_from_images(self.images)
                request = self.client.responses.create.call_args.kwargs
                self.assertEqual(request["model"], model)
                self.assertEqual(request.get("reasoning", {}).get("effort"), expected_effort)
                self.assertEqual("temperature" in request, temperature)
                if temperature:
                    self.assertEqual(request["temperature"], 0)

    def test_mode_verbosity_service_tier_and_larger_token_budget_are_respected(self) -> None:
        settings = config_ui.load_settings(self.config_path)
        settings.scoring.reasoning_mode = "pro"
        settings.scoring.verbosity = "medium"
        settings.scoring.service_tier = "flex"
        settings.scoring.max_output_tokens = 8000
        with patch.object(config_ui, "load_settings", return_value=settings):
            config_ui._extract_review_from_images(self.images)

        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["reasoning"]["mode"], "pro")
        self.assertEqual(request["text"]["verbosity"], "medium")
        self.assertEqual(request["service_tier"], "flex")
        self.assertEqual(request["max_output_tokens"], 8000)

    def test_all_images_are_sent_in_order_in_one_request(self) -> None:
        images = self.images + [{"bytes": b"second-image", "mime_type": "image/jpeg"}]
        config_ui._extract_review_from_images(images)

        content = self.client.responses.create.call_args.kwargs["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual([item["type"] for item in content[1:]], ["input_image", "input_image"])
        self.assertEqual(content[1]["image_url"], config_ui._data_uri_from_image_bytes(b"first-image", "image/png"))
        self.assertEqual(content[2]["image_url"], config_ui._data_uri_from_image_bytes(b"second-image", "image/jpeg"))
        self.client.responses.create.assert_called_once()

    def test_incomplete_response_has_clear_error_and_does_not_score_or_save(self) -> None:
        self.client.responses.create.return_value = Mock(status="incomplete", output_text='{"place_name":')
        with patch.object(config_ui, "score_review") as score:
            with self.assertRaisesRegex(RuntimeError, "incompleta"):
                config_ui._import_review_from_images(self.images)
            score.assert_not_called()
        self.assertFalse(config_ui._db_path().exists())

    def test_empty_or_invalid_json_has_clear_error(self) -> None:
        for content, message in (("", "no devolvi"), ("not JSON", "rese"), ("[]", "interpretar")):
            with self.subTest(content=content):
                self.client.responses.create.return_value = Mock(status="completed", output_text=content)
                with self.assertRaisesRegex(RuntimeError, message):
                    config_ui._extract_review_from_images(self.images)

    def test_extraction_still_requires_review_text(self) -> None:
        payload = {**self.payload, "review_text": ""}
        self.client.responses.create.return_value = Mock(status="completed", output_text=json.dumps(payload))
        with self.assertRaisesRegex(RuntimeError, "texto"):
            config_ui._extract_review_from_images(self.images)

    def test_missing_or_invalid_stars_are_kept_as_unknown(self) -> None:
        for value in (0, None, "", "no rating", -1, 6):
            with self.subTest(rating=value):
                payload = {**self.payload, "rating": value}
                self.client.responses.create.return_value = Mock(status="completed", output_text=json.dumps(payload))
                result = config_ui._extract_review_from_images(self.images)
                self.assertEqual(result["rating"], 0)
                self.assertEqual(result["review_text"], self.payload["review_text"])

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_review_without_stars_is_scored_and_saved_without_inventing_rating(self, score, safety) -> None:
        payload = {**self.payload, "rating": 0}
        self.client.responses.create.return_value = Mock(status="completed", output_text=json.dumps(payload))

        result = config_ui._import_review_from_images(self.images)

        self.assertTrue(result["ok"])
        self.assertEqual(result["rating"], 0)
        self.assertEqual(score.call_args.args[2], 0)
        with sqlite3.connect(config_ui._db_path()) as conn:
            rating, humor = conn.execute("SELECT rating, humor_score FROM reviews").fetchone()
        self.assertEqual((rating, humor), (0, 88))

    def test_unknown_rating_displays_label_instead_of_empty_stars(self) -> None:
        self.assertIn("no rating", config_ui._render_stars(0))
        self.assertNotIn("gm-star-empty", config_ui._render_stars(0))
        self.assertEqual(config_ui._render_stars(1).count("gm-star-filled"), 1)
        self.assertEqual(config_ui._render_stars(5).count("gm-star-filled"), 5)

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_successful_extraction_imports_the_site_and_review(self, score, safety) -> None:
        result = config_ui._import_review_from_images(self.images, submitted_by="Pedro")

        self.assertEqual(result["place_name"], self.payload["place_name"])
        self.assertEqual(result["humor_score"], 88)
        place_id = config_ui._manual_place_id(self.payload["place_name"], "")
        self.assertEqual(result["place_id"], place_id)
        self.assertEqual(result["detail_url"], f"/place?id={quote(place_id, safe='')}")
        with sqlite3.connect(config_ui._db_path()) as conn:
            place = conn.execute("SELECT name, address FROM places").fetchone()
            review = conn.execute("SELECT place_id, text, submitted_by, humor_score FROM reviews").fetchone()
        self.assertEqual(place, (self.payload["place_name"], self.payload["place_address"]))
        self.assertEqual(review[0], config_ui._manual_place_id(self.payload["place_name"], ""))
        self.assertEqual(review[1:], (self.payload["review_text"], "Pedro", 88))
        score.assert_called_once()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_reimport_also_opens_the_same_site_not_the_individual_review(self, score, safety) -> None:
        first = config_ui._import_review_from_images(self.images)
        repeated = config_ui._import_review_from_images(self.images)

        self.assertFalse(first["already_exists"])
        self.assertTrue(repeated["already_exists"])
        self.assertEqual(repeated["place_id"], first["place_id"])
        self.assertEqual(repeated["detail_url"], first["detail_url"])
        self.assertTrue(repeated["detail_url"].startswith("/place?id="))
        with sqlite3.connect(config_ui._db_path()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
        score.assert_called_once()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_different_captures_with_same_link_do_not_overwrite_processed_review(self, score, safety) -> None:
        review_url = "https://goo.gl/maps/shared-link"
        first = config_ui._import_review_from_images(self.images, review_url, "Pedro")
        legacy_id = "manual-image-review:" + hashlib.sha256(review_url.encode("utf-8")).hexdigest()[:24]
        with sqlite3.connect(config_ui._db_path()) as conn:
            conn.execute("UPDATE reviews SET review_id=? WHERE review_id=?", (legacy_id, first["review_id"]))
            first["review_id"] = legacy_id
            conn.execute("UPDATE reviews SET status='accepted' WHERE review_id=?", (first["review_id"],))
            conn.execute("UPDATE places SET processed_at='2026-10-05' WHERE place_id=?", (first["place_id"],))
            original = conn.execute("SELECT * FROM reviews WHERE review_id=?", (first["review_id"],)).fetchone()
        payload = {**self.payload, "place_name": "Baby Nails", "review_text": "La manicura fue inesperada."}
        self.client.responses.create.return_value.output_text = json.dumps(payload)

        second = config_ui._import_review_from_images(self.images, review_url, "Cristina")

        self.assertFalse(second["already_exists"])
        self.assertNotEqual(second["review_id"], first["review_id"])
        self.assertNotEqual(second["place_id"], first["place_id"])
        with sqlite3.connect(config_ui._db_path()) as conn:
            self.assertEqual(original, conn.execute("SELECT * FROM reviews WHERE review_id=?", (first["review_id"],)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 2)
            self.assertEqual(
                conn.execute("SELECT place_id FROM reviews WHERE review_id=?", (second["review_id"],)).fetchone()[0],
                second["place_id"],
            )

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_same_site_can_have_multiple_captured_reviews_with_the_same_link(self, score, safety) -> None:
        review_url = "https://goo.gl/maps/shared-place"
        first = config_ui._import_review_from_images(self.images, review_url)
        self.client.responses.create.return_value.output_text = json.dumps({
            **self.payload, "reviewer_name": "Otro autor", "review_text": "Nos sirvieron el postre antes de llegar.",
        })

        second = config_ui._import_review_from_images(self.images, review_url)

        self.assertEqual(second["place_id"], first["place_id"])
        self.assertNotEqual(second["review_id"], first["review_id"])
        with sqlite3.connect(config_ui._db_path()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews WHERE place_id=?", (first["place_id"],)).fetchone()[0], 2)

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_changed_ocr_place_name_opens_the_saved_site_without_creating_empty_place(self, score, safety) -> None:
        review_url = "https://goo.gl/maps/same-review"
        first = config_ui._import_review_from_images(self.images, review_url)
        self.client.responses.create.return_value.output_text = json.dumps({**self.payload, "place_name": "Otro nombre OCR"})

        repeated = config_ui._import_review_from_images(self.images, review_url)

        self.assertTrue(repeated["already_exists"])
        self.assertEqual(repeated["place_id"], first["place_id"])
        self.assertEqual(repeated["place_name"], first["place_name"])
        self.assertEqual(repeated["detail_url"], first["detail_url"])
        with sqlite3.connect(config_ui._db_path()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT place_id FROM reviews").fetchone()[0], repeated["place_id"])
        score.assert_called_once()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_legacy_capture_reimport_preserves_owner_moderation_translations_and_export(self, score, safety) -> None:
        review_url = "https://goo.gl/maps/legacy-review"
        first = config_ui._import_review_from_images(self.images, review_url, "Pedro")
        legacy_id = "manual-image-review:" + hashlib.sha256(review_url.encode("utf-8")).hexdigest()[:24]
        with sqlite3.connect(config_ui._db_path()) as conn:
            config_ui._ensure_review_columns(conn)
            conn.execute(
                "UPDATE reviews SET review_id=?, status='accepted', humor_score=91, "
                "translated_text='Texto traducido', original_text_language='en', notion_page_id='notion-page' "
                "WHERE review_id=?", (legacy_id, first["review_id"]),
            )
            original = conn.execute("SELECT * FROM reviews WHERE review_id=?", (legacy_id,)).fetchone()
        self.client.responses.create.return_value.output_text = json.dumps({**self.payload, "place_name": "Nombre OCR distinto"})

        repeated = config_ui._import_review_from_images(self.images, review_url)

        self.assertTrue(repeated["already_exists"])
        self.assertEqual(repeated["review_id"], legacy_id)
        self.assertEqual(repeated["place_id"], first["place_id"])
        self.assertEqual(repeated["humor_score"], 91)
        self.assertEqual(repeated["submitted_by"], "Pedro")
        with sqlite3.connect(config_ui._db_path()) as conn:
            self.assertEqual(original, conn.execute("SELECT * FROM reviews WHERE review_id=?", (legacy_id,)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0], 1)
        score.assert_called_once()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_changed_ocr_name_cannot_bypass_the_saved_sites_processed_flag(self, score, safety) -> None:
        review_url = "https://goo.gl/maps/processed-review"
        first = config_ui._import_review_from_images(self.images, review_url)
        legacy_id = "manual-image-review:" + hashlib.sha256(review_url.encode("utf-8")).hexdigest()[:24]
        with sqlite3.connect(config_ui._db_path()) as conn:
            conn.execute("UPDATE reviews SET review_id=? WHERE review_id=?", (legacy_id, first["review_id"]))
            conn.execute("UPDATE places SET processed_at='2026-10-05' WHERE place_id=?", (first["place_id"],))
        self.client.responses.create.return_value.output_text = json.dumps({**self.payload, "place_name": "Otra lectura OCR"})

        with self.assertRaisesRegex(ValueError, "ya está marcado como procesado"):
            config_ui._import_review_from_images(self.images, review_url)

        score.assert_called_once()
        with sqlite3.connect(config_ui._db_path()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0], 1)

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_identified_capture_collects_and_groups_extra_reviews_with_sender_only_on_capture(self, score, safety):
        self.prepare_site_lookup()
        self.collect_reviews.return_value = [
            self.extra_review("duplicate", text=self.payload["review_text"]),
            self.extra_review("funny"), self.extra_review("positive", rating=5),
        ]
        self.collect_reviews.return_value[0].reviewer_name = self.payload["reviewer_name"]
        score.side_effect = [HumorResult(88, "Captura", [], ""), HumorResult(94, "Adicional", [], "")]
        progress = []

        result = config_ui._import_review_from_images(self.images, submitted_by="Cristina", on_progress=progress.append)

        self.assertEqual(result["additional_reviews"], 1)
        self.assertEqual(result["new_reviews"], 2)
        self.assertEqual(result["review_count"], 2)
        self.assertEqual(result["inspected_reviews"], 3)
        self.assertEqual(score.call_count, 2)
        self.assertEqual(self.collect_reviews.call_args.args[0], ["0xabc:0x123"])
        self.assertEqual(self.collect_reviews.call_args.args[2], config_ui.load_settings(self.config_path).app.max_reviews_per_place)
        self.assertTrue(self.collect_reviews.call_args.kwargs["raise_on_error"])
        self.assertTrue(any(item.get("additional_reviews") == 1 for item in progress))
        with sqlite3.connect(config_ui._db_path()) as conn:
            rows = conn.execute("SELECT place_id, submitted_by, humor_score FROM reviews ORDER BY humor_score").fetchall()
            place = conn.execute("SELECT name, address, data_id, average_rating FROM places").fetchone()
        self.assertEqual(rows, [(result["place_id"], "Cristina", 88), (result["place_id"], "", 94)])
        self.assertEqual(place, (self.payload["place_name"], self.metadata["address"], "0xabc:0x123", 4.5))

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_capture_link_can_identify_site_when_its_name_is_not_visible(self, score, safety):
        self.prepare_site_lookup()
        self.client.responses.create.return_value.output_text = json.dumps({**self.payload, "place_name": "", "place_address": ""})
        self.collect_reviews.return_value = [self.extra_review()]

        result = config_ui._import_review_from_images(self.images, "https://goo.gl/maps/same-review")

        self.assertEqual(result["place_name"], self.metadata["title"])
        self.assertEqual(result["additional_reviews"], 1)
        self.serpapi_maps_search.assert_not_called()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_stale_link_with_other_place_name_does_not_override_the_capture(self, score, safety):
        self.prepare_site_lookup()
        self.fetch_import_place_metadata.return_value = {**self.metadata, "title": "Sitio anterior", "data_id": "0xdef:0x456"}

        result = config_ui._import_review_from_images(self.images, "https://goo.gl/maps/previous-review")

        self.assertEqual(result["place_name"], self.payload["place_name"])
        self.assertEqual(self.collect_reviews.call_args.args[0], ["0xabc:0x123"])
        self.serpapi_maps_search.assert_called_once()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_ambiguous_site_names_keep_only_the_capture(self, score, safety):
        self.prepare_site_lookup()
        self.serpapi_maps_search.return_value = ([self.metadata, {**self.metadata, "data_id": "0xdef:0x456"}], False)

        result = config_ui._import_review_from_images(self.images)

        self.assertEqual(result["additional_reviews"], 0)
        self.assertEqual(result["review_count"], 1)
        self.assertIn("nico sitio", result["warning"])
        self.collect_reviews.assert_not_called()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_visible_address_disambiguates_same_name_places(self, score, safety):
        self.prepare_site_lookup()
        self.serpapi_maps_search.return_value = ([self.metadata, {**self.metadata, "data_id": "0xdef:0x456", "address": "Madrid"}], False)

        result = config_ui._import_review_from_images(self.images)

        self.assertEqual(result["place_id"], "google-place")
        self.assertFalse(result["warning"])
        self.assertEqual(self.serpapi_maps_search.call_args.args[0], "Restaurante de prueba Alicante")
        self.assertNotIn("location", self.serpapi_maps_search.call_args.kwargs)

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_capture_without_visible_name_or_link_does_not_search_its_generated_label(self, score, safety):
        self.prepare_site_lookup()
        self.client.responses.create.return_value.output_text = json.dumps({**self.payload, "place_name": ""})

        result = config_ui._import_review_from_images(self.images)

        self.assertEqual(result["review_count"], 1)
        self.serpapi_maps_search.assert_not_called()
        self.collect_reviews.assert_not_called()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_existing_manual_site_is_enriched_without_changing_its_id(self, score, safety):
        first = config_ui._import_review_from_images(self.images)
        self.prepare_site_lookup()
        self.collect_reviews.return_value = [self.extra_review()]

        repeated = config_ui._import_review_from_images(self.images)

        self.assertEqual(repeated["place_id"], first["place_id"])
        self.assertTrue(repeated["already_exists"])
        self.assertEqual(repeated["review_count"], 2)
        with sqlite3.connect(config_ui._db_path()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT data_id FROM places").fetchone()[0], "0xabc:0x123")
        self.assertEqual(score.call_count, 2)

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_capture_joins_existing_real_site_and_does_not_rescore_existing_extras(self, score, safety):
        self.prepare_site_lookup()
        storage = config_ui._storage()
        storage.upsert_place(Place("canonical", "0xabc:0x123", self.metadata["title"], self.metadata["address"], "restaurant", 120, None, "serpapi", average_rating=4.5))
        self.collect_reviews.return_value = [self.extra_review()]

        first = config_ui._import_review_from_images(self.images)
        repeated = config_ui._import_review_from_images(self.images)

        self.assertEqual(first["place_id"], "canonical")
        self.assertEqual(first["detail_url"], "/place?id=canonical")
        self.assertEqual(repeated["additional_reviews"], 0)
        self.assertEqual(repeated["new_reviews"], 0)
        self.assertEqual(repeated["review_count"], 2)
        self.assertEqual(score.call_count, 2)

    @patch.object(config_ui, "score_review")
    def test_resolved_processed_site_is_blocked_before_scoring_capture(self, score):
        self.prepare_site_lookup()
        config_ui._storage().upsert_place(Place("canonical", "0xabc:0x123", self.metadata["title"], self.metadata["address"], "restaurant", 120, None, "serpapi"))
        config_ui._set_place_processed("canonical", True)

        with self.assertRaisesRegex(ValueError, "procesado"):
            config_ui._import_review_from_images(self.images)

        score.assert_not_called()
        self.collect_reviews.assert_not_called()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_partial_collection_failure_keeps_saved_capture_and_extra(self, score, safety):
        self.prepare_site_lookup()
        def collect(*args, **kwargs):
            yield self.extra_review()
            raise RuntimeError("Provider offline")
        self.collect_reviews.side_effect = collect

        result = config_ui._import_review_from_images(self.images)

        self.assertTrue(result["ok"])
        self.assertEqual(result["review_count"], 2)
        self.assertIn("Provider offline", result["warning"])

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_site_lookup_failure_keeps_the_capture(self, score, safety):
        self.prepare_site_lookup()
        self.serpapi_maps_search.side_effect = RuntimeError("Provider offline")

        result = config_ui._import_review_from_images(self.images)

        self.assertTrue(result["ok"])
        self.assertEqual(result["review_count"], 1)
        self.assertTrue(result["warning"])
        self.collect_reviews.assert_not_called()

    @patch.object(config_ui, "assess_safety", return_value=SafetyResult("safe", ""))
    @patch.object(config_ui, "score_review", return_value=HumorResult(88, "Buena", [], ""))
    def test_marking_identified_capture_site_processed_stops_additional_scoring(self, score, safety):
        self.prepare_site_lookup()
        def collect(*args, **kwargs):
            config_ui._set_place_processed("google-place", True)
            yield self.extra_review()
        self.collect_reviews.side_effect = collect

        result = config_ui._import_review_from_images(self.images)

        self.assertEqual(result["additional_reviews"], 0)
        self.assertEqual(result["review_count"], 1)
        self.assertIn("procesado", result["warning"])
        score.assert_called_once()


if __name__ == "__main__":
    unittest.main()
