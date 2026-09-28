from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from humor_reviews.notion_sync import NotionPage, _build_place_children
from humor_reviews.storage import Place, Review, Storage
from scripts import config_ui


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

    def test_status_filter_selects_places_without_hiding_their_other_reviews(self) -> None:
        accepted = config_ui._fetch_db_snapshot("humor_score", "accepted")

        self.assertEqual(len(accepted["places"]), 1)
        self.assertEqual(accepted["places"][0]["place_name"], "Sitio Uno")
        self.assertEqual(accepted["places"][0]["review_count"], 3)

    def test_place_detail_is_sorted_and_contains_all_review_states(self) -> None:
        detail = config_ui._fetch_place_detail("place-1")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual([review["review_id"] for review in detail["reviews"]], ["top", "middle", "low"])
        self.assertEqual(detail["place"]["top_humor_score"], 91)
        self.assertEqual(detail["place"]["accepted_count"], 1)
        self.assertEqual(detail["place"]["pending_count"], 1)

    @patch("scripts.config_ui.sync_place_reviews_page")
    def test_export_sends_only_accepted_reviews_to_one_place_page(self, sync_page) -> None:
        sync_page.return_value = NotionPage(page_id="notion-page", url="https://notion.so/page")

        result = config_ui._export_place_to_notion("place-1")

        self.assertEqual(result["exported_reviews"], 1)
        exported_reviews = sync_page.call_args.args[1]
        self.assertEqual([review["review_id"] for review in exported_reviews], ["top"])
        with sqlite3.connect(self.data_dir / "humor_reviews.db") as conn:
            place_url = conn.execute(
                "SELECT notion_page_url FROM places WHERE place_id='place-1'"
            ).fetchone()[0]
        self.assertEqual(place_url, "https://notion.so/page")

    def test_export_requires_an_accepted_review(self) -> None:
        with self.assertRaisesRegex(ValueError, "Acepta al menos una"):
            config_ui._export_place_to_notion("place-2")


class NotionPlaceDocumentTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
