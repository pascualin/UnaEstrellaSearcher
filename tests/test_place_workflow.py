from __future__ import annotations

import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from humor_reviews.notion_sync import NotionPage, _build_place_children, _place_page_title
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
