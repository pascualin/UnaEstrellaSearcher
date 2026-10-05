from __future__ import annotations

import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from humor_reviews.notion_sync import NotionPage, _build_children, _build_place_children, _place_page_title
from humor_reviews.storage import Place, Review, Storage
from humor_reviews.translation import TranslationResult
from scripts import config_ui


PNG_BYTES = b"\x89PNG\r\n\x1a\nmock-png"


def _review(review_id: str, place_id: str, score: int) -> Review:
    return Review(
        review_id=review_id,
        place_id=place_id,
        rating=1,
        date="hace un día",
        reviewer_name=f"Autor {review_id}",
        reviewer_profile_url="",
        text=f"Texto {review_id}",
        summary="",
        owner_reply="",
        review_url=f"https://example.com/{review_id}",
        humor_score=score,
        humor_notes=f"Nota {review_id}",
        safety_label="safe",
        safety_notes="",
        tags="absurdo",
        translated_text=f"Texto {review_id}",
        original_text_language="es",
    )


class PlaceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.config_path.write_text(
            f"app:\n  data_dir: {self.data_dir}\n",
            encoding="utf-8",
        )
        self.storage = Storage(self.data_dir / "humor_reviews.db")
        self.storage.upsert_place(
            Place(
                place_id="place-1",
                data_id="data-1",
                name="Sitio Uno",
                address="Madrid",
                category="restaurant",
                total_reviews=3,
                last_review_date=None,
                provider="test",
                place_url="https://example.com/place-1",
                average_rating=4.6,
                country="España",
            )
        )
        self.storage.upsert_place(
            Place(
                place_id="place-2",
                data_id="data-2",
                name="Sitio Dos",
                address="Sevilla",
                category="cafe",
                total_reviews=1,
                last_review_date=None,
                provider="test",
                place_url="https://example.com/place-2",
                average_rating=4.2,
                country="España",
            )
        )
        self.storage.upsert_review(_review("low", "place-1", 22))
        self.storage.upsert_review(_review("top", "data-1", 91))
        self.storage.upsert_review(_review("middle", "place-1", 64))
        self.storage.upsert_review(_review("other", "place-2", 75))
        self.storage.update_status("top", "accepted")
        self.storage.update_status("low", "rejected")
        self.config_patch = patch.object(config_ui, "CONFIG_PATH", self.config_path)
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def test_snapshot_groups_reviews_and_uses_highest_score(self) -> None:
        snapshot = config_ui._fetch_db_snapshot("humor_score", "all")

        self.assertEqual([place["place_name"] for place in snapshot["places"]], ["Sitio Uno", "Sitio Dos"])
        first = snapshot["places"][0]
        self.assertEqual(first["top_humor_score"], 91)
        self.assertEqual(first["review_count"], 3)
        self.assertEqual(first["accepted_count"], 1)
        self.assertEqual(first["rejected_count"], 1)
        self.assertEqual(first["pending_count"], 1)

    def test_processed_filter_separates_finished_sites(self) -> None:
        self.assertTrue(config_ui._set_place_processed("place-1", True))

        unprocessed = config_ui._fetch_db_snapshot("humor_score", "unprocessed")
        processed = config_ui._fetch_db_snapshot("humor_score", "processed")

        self.assertEqual([place["place_name"] for place in unprocessed["places"]], ["Sitio Dos"])
        self.assertEqual([place["place_name"] for place in processed["places"]], ["Sitio Uno"])
        self.assertTrue(processed["places"][0]["processed_at"])
        self.assertEqual(processed["places"][0]["review_count"], 3)

    def test_places_can_be_reopened(self) -> None:
        self.assertTrue(config_ui._set_place_processed("data-1", True))
        self.assertTrue(config_ui._set_place_processed("place-1", False))

        detail = config_ui._fetch_place_detail("place-1")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertFalse(detail["place"]["processed"])

    def test_place_statuses_resolve_place_and_data_ids(self) -> None:
        self.assertTrue(config_ui._set_place_processed("data-1", True))

        statuses = config_ui._fetch_place_statuses(
            ["place-1", "data-1", "place-2", "missing", "data-1"]
        )

        self.assertEqual(
            statuses,
            {"place-1": True, "data-1": True, "place-2": False},
        )

    def test_marking_place_processed_keeps_detail_open(self) -> None:
        detail_html = (config_ui.ROOT / "scripts" / "place_detail.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('window.location.assign("/db")', detail_html)
        self.assertIn("Sitio marcado como procesado.", detail_html)

    def test_database_cards_show_processed_and_pending_badges(self) -> None:
        db_html = (config_ui.ROOT / "scripts" / "db_view.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('processed ? "PROCESADO" : "PENDIENTE"', db_html)
        self.assertIn('processed ? "is-processed" : "is-unprocessed"', db_html)

    def test_processed_places_are_excluded_from_automated_storage_queries(self) -> None:
        self.assertTrue(config_ui._set_place_processed("place-1", True))
        with sqlite3.connect(self.data_dir / "humor_reviews.db") as conn:
            conn.execute(
                "UPDATE reviews SET humor_notes='Parse failure' WHERE review_id='middle'"
            )

        candidates = self.storage.fetch_candidates(0, allow_repeat=True)
        rescore = self.storage.fetch_reviews_needing_rescore()

        self.assertEqual([review.review_id for review in candidates], ["other"])
        self.assertEqual(rescore, [])
        self.assertEqual(self.storage.get_place_ids(), ["data-2"])
        self.assertEqual(
            set(self.storage.get_place_ids(include_processed=True)),
            {"data-1", "data-2"},
        )
        self.assertEqual(
            self.storage.get_processed_place_ids(),
            {"place-1", "data-1"},
        )

    @patch("scripts.config_ui.score_review")
    @patch("scripts.config_ui._extract_review_from_images")
    def test_image_import_does_not_score_a_processed_place(self, extract, score) -> None:
        place_id = config_ui._manual_place_id("Sitio procesado", "")
        self.storage.upsert_place(
            Place(
                place_id,
                place_id,
                "Sitio procesado",
                "Madrid",
                "manual_image",
                1,
                None,
                "manual_image",
            )
        )
        self.assertTrue(config_ui._set_place_processed(place_id, True))
        extract.return_value = {
            "place_name": "Sitio procesado",
            "reviewer_name": "Autor",
            "rating": 1,
            "date": "hoy",
            "review_text": "Texto",
            "owner_reply_text": "",
            "owner_reply_date": "",
            "place_address": "Madrid",
            "review_url": "",
        }

        with self.assertRaisesRegex(ValueError, "ya está marcado como procesado"):
            config_ui._import_review_from_images([{"bytes": PNG_BYTES}])

        score.assert_not_called()

    @patch("scripts.config_ui.score_review")
    @patch("scripts.config_ui._find_review_in_serpapi")
    @patch("scripts.config_ui._resolve_place_data_id")
    def test_url_import_stops_before_fetching_a_processed_place(
        self,
        resolve_place,
        find_review,
        score,
    ) -> None:
        self.assertTrue(config_ui._set_place_processed("place-1", True))
        resolve_place.return_value = ("https://example.com/review", "data-1")

        with patch.dict(config_ui.os.environ, {"SERPAPI_API_KEY": "test-key"}):
            with self.assertRaisesRegex(ValueError, "ya está marcado como procesado"):
                config_ui._import_review_from_url("https://example.com/review")

        find_review.assert_not_called()
        score.assert_not_called()

    def test_date_sort_uses_latest_review_in_each_site(self) -> None:
        with sqlite3.connect(self.data_dir / "humor_reviews.db") as conn:
            conn.execute("UPDATE reviews SET updated_at='2024-01-01T00:00:00' WHERE place_id IN ('place-1', 'data-1')")
            conn.execute("UPDATE reviews SET updated_at='2025-01-01T00:00:00' WHERE review_id='other'")

        snapshot = config_ui._fetch_db_snapshot("updated_at", "all")

        self.assertEqual([place["place_name"] for place in snapshot["places"]], ["Sitio Dos", "Sitio Uno"])

    def test_place_detail_is_sorted_and_contains_all_review_states(self) -> None:
        detail = config_ui._fetch_place_detail("place-1")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual([review["review_id"] for review in detail["reviews"]], ["top", "middle", "low"])
        self.assertEqual(detail["place"]["top_humor_score"], 91)
        self.assertEqual(detail["place"]["accepted_count"], 1)
        self.assertEqual(detail["place"]["pending_count"], 1)

    @patch("scripts.config_ui.translate_reviews_to_spanish")
    def test_place_detail_translates_and_stores_missing_review_text(self, translate_batch) -> None:
        with sqlite3.connect(self.data_dir / "humor_reviews.db") as conn:
            conn.execute(
                """
                UPDATE reviews
                SET text='Very funny review', translated_text='', original_text_language=''
                WHERE review_id='other'
                """
            )
        translate_batch.return_value = {
            "other": TranslationResult(
                review_text_es="Reseña muy graciosa",
                owner_reply_es="",
                review_language="en",
                owner_reply_language="",
            )
        }

        detail = config_ui._fetch_place_detail("place-2")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["reviews"][0]["review_text"], "Reseña muy graciosa")
        self.assertEqual(detail["reviews"][0]["review_language"], "en")
        translate_batch.assert_called_once()
        with sqlite3.connect(self.data_dir / "humor_reviews.db") as conn:
            stored = conn.execute(
                "SELECT translated_text, original_text_language FROM reviews WHERE review_id='other'"
            ).fetchone()
        self.assertEqual(stored, ("Reseña muy graciosa", "en"))

    @patch("scripts.config_ui.sync_place_reviews_page")
    def test_export_sends_only_accepted_reviews_to_one_place_page(self, sync_page) -> None:
        sync_page.return_value = NotionPage(page_id="notion-page", url="https://notion.so/page")

        result = config_ui._export_place_to_notion("place-1", {"top": PNG_BYTES})

        self.assertEqual(result["exported_reviews"], 1)
        exported_reviews = sync_page.call_args.args[1]
        self.assertEqual([review["review_id"] for review in exported_reviews], ["top"])
        exported_place = sync_page.call_args.args[0]
        self.assertEqual(exported_place["average_rating"], 4.6)
        self.assertEqual(exported_place["place_country"], "España")
        self.assertEqual(sync_page.call_args.kwargs["review_images"], {"top": PNG_BYTES})
        with sqlite3.connect(self.data_dir / "humor_reviews.db") as conn:
            place_url = conn.execute("SELECT notion_page_url FROM places WHERE place_id='place-1'").fetchone()[0]
            image_uploaded_at = conn.execute(
                "SELECT notion_image_uploaded_at FROM reviews WHERE review_id='top'"
            ).fetchone()[0]
        self.assertEqual(place_url, "https://notion.so/page")
        self.assertTrue(image_uploaded_at)

    def test_export_requires_an_accepted_review(self) -> None:
        with self.assertRaisesRegex(ValueError, "Acepta al menos una"):
            config_ui._export_place_to_notion("place-2", {})

    @patch("scripts.config_ui.sync_place_reviews_page")
    def test_export_preserves_the_sender_for_each_review(self, sync_page) -> None:
        sync_page.return_value = NotionPage(page_id="notion-page", url="https://notion.so/page")
        with sqlite3.connect(self.storage.db_path) as conn:
            conn.execute("UPDATE reviews SET submitted_by='Ana' WHERE review_id='top'")
        self.storage.update_status("middle", "accepted")

        config_ui._export_place_to_notion("place-1", {"top": PNG_BYTES, "middle": PNG_BYTES})

        reviews = sync_page.call_args.args[1]
        self.assertEqual([review["review_id"] for review in reviews], ["top", "middle"])
        self.assertEqual([review["submitted_by"] for review in reviews], ["Ana", ""])
        children = _build_place_children(reviews)
        attributions = [
            block["paragraph"]["rich_text"][0]["text"]["content"]
            for block in children if block["type"] == "paragraph"
            and block["paragraph"]["rich_text"][0]["text"]["content"].startswith("Nos la env\u00eda:")
        ]
        self.assertEqual(attributions, ["Nos la env\u00eda: Ana"])

    def test_export_requires_a_capture_for_every_accepted_review(self) -> None:
        with self.assertRaisesRegex(ValueError, "captura de todas"):
            config_ui._export_place_to_notion("place-1", {})

    def test_notion_capture_payload_is_decoded(self) -> None:
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")

        images = config_ui._decode_notion_review_images(
            [{"review_id": "top", "image_data": f"data:image/png;base64,{encoded}"}]
        )

        self.assertEqual(images, {"top": PNG_BYTES})


class NotionPlaceDocumentTests(unittest.TestCase):
    def test_sender_is_distinguished_from_the_review_author(self) -> None:
        children = _build_children({
            "submitted_by": "  Ana  ", "reviewer_name": "Luis", "review_text": "Una queja divertida",
        })
        self.assertEqual([block["type"] for block in children], ["paragraph", "paragraph", "quote"])
        self.assertEqual(children[0]["paragraph"]["rich_text"][0]["text"]["content"], "Nos la env\u00eda: Ana")
        self.assertEqual(children[1]["paragraph"]["rich_text"][0]["text"]["content"], "Luis")
        self.assertEqual(children[2]["quote"]["rich_text"][0]["text"]["content"], "Una queja divertida")

    def test_reviews_without_a_sender_keep_the_existing_format(self) -> None:
        for submitted_by in (None, "", "   "):
            with self.subTest(submitted_by=submitted_by):
                children = _build_children({
                    "submitted_by": submitted_by, "reviewer_name": "Luis", "review_text": "Texto",
                })
                self.assertEqual([block["type"] for block in children], ["paragraph", "quote"])
                self.assertEqual(children[0]["paragraph"]["rich_text"][0]["text"]["content"], "Luis")

    def test_grouped_document_keeps_each_sender_with_the_correct_review(self) -> None:
        children = _build_place_children([
            {"submitted_by": "Ana", "reviewer_name": "Primera", "review_text": "Uno", "humor_score": 90},
            {"reviewer_name": "Segunda", "review_text": "Dos", "humor_score": 80},
            {"submitted_by": "Pedro", "reviewer_name": "Tercera", "review_text": "Tres", "humor_score": 70},
        ])
        sections = [[]]
        for block in children:
            if block["type"] == "divider":
                sections.append([])
            elif block["type"] == "paragraph":
                sections[-1].append(block["paragraph"]["rich_text"][0]["text"]["content"])
        self.assertEqual(sections, [
            ["Nos la env\u00eda: Ana", "Primera"], ["Segunda"], ["Nos la env\u00eda: Pedro", "Tercera"],
        ])

    def test_title_contains_place_location_rating_and_selected_count(self) -> None:
        title = _place_page_title(
            {
                "place_name": "Rosi La Loca",
                "place_address": "C. de Cádiz, 4, Centro, 28012 Madrid",
                "place_country": "España",
                "average_rating": 4.7,
            },
            [{"review_id": "first"}, {"review_id": "second"}],
        )

        self.assertEqual(
            title,
            "Rosi La Loca · Madrid, Madrid, España · 4,7/5 · 2 reseñas seleccionadas",
        )

    def test_title_uses_singular_for_one_selected_review(self) -> None:
        title = _place_page_title(
            {
                "place_name": "El Social",
                "place_address": "Madrid, Spain",
                "average_rating": 4.8,
            },
            [{"review_id": "only"}],
        )

        self.assertEqual(
            title,
            "El Social · Madrid, Madrid, España · 4,8/5 · 1 reseña seleccionada",
        )

    def test_reviews_are_rendered_in_order_with_dividers(self) -> None:
        reviews = [
            {"reviewer_name": "Primera", "review_text": "Uno", "humor_score": 90},
            {"reviewer_name": "Segunda", "review_text": "Dos", "humor_score": 70},
        ]

        children = _build_place_children(reviews)

        self.assertEqual(children[0]["heading_2"]["rich_text"][0]["text"]["content"], "Reseña 1 · Humor 90/100")
        self.assertTrue(any(block["type"] == "divider" for block in children))
        headings = [
            block["heading_2"]["rich_text"][0]["text"]["content"]
            for block in children
            if block["type"] == "heading_2"
        ]
        self.assertEqual(headings, ["Reseña 1 · Humor 90/100", "Reseña 2 · Humor 70/100"])

    def test_review_capture_is_inserted_before_the_next_review(self) -> None:
        reviews = [
            {"review_id": "first", "reviewer_name": "Primera", "review_text": "Uno", "humor_score": 90},
            {"review_id": "second", "reviewer_name": "Segunda", "review_text": "Dos", "humor_score": 70},
        ]

        children = _build_place_children(reviews, {"first": "upload-1", "second": "upload-2"})

        block_types = [block["type"] for block in children]
        self.assertEqual(block_types, ["heading_2", "paragraph", "quote", "image", "divider", "heading_2", "paragraph", "quote", "image"])
        self.assertEqual(children[3]["image"]["file_upload"]["id"], "upload-1")


if __name__ == "__main__":
    unittest.main()
