from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from humor_reviews.translation import _config_scoring_model, _translate_review_batch


class BatchTranslationTests(unittest.TestCase):
    def test_batch_translation_maps_results_by_review_id(self) -> None:
        client = Mock()
        message = Mock()
        message.content = (
            '{"translations":[{"review_id":"review-1","review_text_es":"Muy malo",'
            '"owner_reply_es":"Gracias","review_language":"en",'
            '"owner_reply_language":"en"}]}'
        )
        response = Mock()
        response.choices = [Mock(message=message)]
        client.chat.completions.create.return_value = response

        results = _translate_review_batch(
            client,
            "gpt-test",
            [
                {
                    "review_id": "review-1",
                    "review_text": "Very bad",
                    "owner_reply_text": "Thank you",
                }
            ],
        )

        self.assertEqual(results["review-1"].review_text_es, "Muy malo")
        self.assertEqual(results["review-1"].review_language, "en")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["response_format"]["type"], "json_schema")

    @patch("humor_reviews.translation.Path")
    def test_typesafe_scoring_model_is_not_used_for_openai_translation(self, path_class) -> None:
        config_path = path_class.return_value
        config_path.exists.return_value = True
        config_path.read_text.return_value = "scoring:\n  provider: typesafe\n  model: jev-latest\n"

        self.assertEqual(_config_scoring_model(), "")


if __name__ == "__main__":
    unittest.main()
