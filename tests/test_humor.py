from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from humor_reviews.humor import JEV_HUMOR_LEVELS, HumorResult, score_review
from humor_reviews.openai_models import openai_model_profile
from humor_reviews.settings import ScoringSettings, load_settings


def _settings(provider: str = "typesafe") -> ScoringSettings:
    return ScoringSettings(
        provider=provider,
        model="jev-latest" if provider == "typesafe" else "gpt-test",
        api_key_env="TYPESAFE_API_KEY" if provider == "typesafe" else "OPENAI_API_KEY",
        prompt=(
            "Valora esta reseña para el podcast.\n"
            "ESTRELLAS: {rating}\n"
            "RESEÑA: {review_text}\n"
            "RESPUESTA: {owner_reply}"
        ),
        reasoning_effort="none",
        reasoning_mode="standard",
        verbosity="low",
        service_tier="auto",
        temperature=0.2,
        max_output_tokens=320,
    )


class TypeSafeScoringTests(unittest.TestCase):
    @patch("humor_reviews.humor.requests.post")
    def test_typesafe_score_is_mapped_to_zero_to_one_hundred(self, post: Mock) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "model": "jev-1.13.0",
            "answers": {
                "humor_score": {
                    "type": "score",
                    "score": 7.35,
                    "confidence": 0.82,
                    "legend": {},
                    "probabilities": {},
                },
                "primary_humor_style": {
                    "type": "choice",
                    "choice": "absurdo",
                    "confidence": 0.91,
                    "probabilities": {"absurdo": 0.91},
                },
            },
            "usage": {"input_tokens": 100, "output_tokens": 2},
        }
        post.return_value = response

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            result = score_review("Todo ardió, pero el flan estaba bien.", "", 1, _settings())

        self.assertEqual(result.score, 74)
        self.assertEqual(result.tags, ["absurdo"])
        self.assertEqual(result.notes, "Jev score (confidence 82%)")
        self.assertEqual(result.summary, "")

        request = post.call_args.kwargs
        self.assertEqual(request["json"]["model"], "jev-latest")
        self.assertEqual(
            request["headers"]["Authorization"],
            "Bearer test-secret",
        )
        self.assertEqual(
            request["json"]["questions"]["humor_score"]["criteria"],
            JEV_HUMOR_LEVELS,
        )
        self.assertIn("Todo ardió", request["json"]["state"])

    @patch("humor_reviews.humor._score_review_openai")
    def test_openai_provider_keeps_existing_path(self, openai_score: Mock) -> None:
        expected = HumorResult(score=61, notes="ok", tags=["ironico"], summary="resumen")
        openai_score.return_value = expected

        result = score_review("texto", "", 1, _settings("openai"))

        self.assertIs(result, expected)
        openai_score.assert_called_once()

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported scoring provider"):
            score_review("texto", "", 1, _settings("otro"))


class ScoringSettingsTests(unittest.TestCase):
    def test_typesafe_provider_gets_jev_defaults(self) -> None:
        config = """
app: {}
discovery: {}
providers: {}
scoring:
  provider: typesafe
safety: {}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(config, encoding="utf-8")
            settings = load_settings(path)

        self.assertEqual(settings.scoring.provider, "typesafe")
        self.assertEqual(settings.scoring.model, "jev-latest")
        self.assertEqual(settings.scoring.api_key_env, "TYPESAFE_API_KEY")

    def test_openai_execution_defaults_are_loaded(self) -> None:
        config = """
app: {}
discovery: {}
providers: {}
scoring:
  provider: openai
safety: {}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(config, encoding="utf-8")
            settings = load_settings(path)

        self.assertEqual(settings.scoring.reasoning_effort, "none")
        self.assertEqual(settings.scoring.reasoning_mode, "standard")
        self.assertEqual(settings.scoring.verbosity, "low")
        self.assertEqual(settings.scoring.service_tier, "auto")


class OpenAIScoringTests(unittest.TestCase):
    @patch("humor_reviews.humor.OpenAI")
    def test_reasoning_request_uses_responses_api_without_temperature(self, openai: Mock) -> None:
        settings = _settings("openai")
        settings.model = "gpt-6-astra"
        settings.prompt += '\n{"score": 0, "summary": "resumen corto"}'
        settings.reasoning_effort = "high"
        settings.reasoning_mode = "pro"
        settings.service_tier = "fast"
        response = Mock(
            status="completed",
            output_text='{"score": 87, "notes": "Muy buena", "tags": ["absurdo"]}',
        )
        openai.return_value.responses.create.return_value = response

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}):
            result = score_review("Texto", "Respuesta", 1, settings)

        self.assertEqual(result.score, 87)
        request = openai.return_value.responses.create.call_args.kwargs
        self.assertEqual(request["reasoning"], {"effort": "high", "mode": "pro"})
        self.assertEqual(request["service_tier"], "fast")
        self.assertEqual(request["text"]["verbosity"], "low")
        self.assertNotIn("summary", request["text"]["format"]["schema"]["properties"])
        self.assertNotIn("summary", request["text"]["format"]["schema"]["required"])
        self.assertIn("No generes ni incluyas ningún resumen", request["instructions"])
        self.assertNotIn('"summary"', request["input"])
        self.assertNotIn("temperature", request)
        self.assertFalse(request["store"])
        self.assertEqual(result.summary, "")

    @patch("humor_reviews.humor.OpenAI")
    def test_none_reasoning_keeps_temperature(self, openai: Mock) -> None:
        settings = _settings("openai")
        settings.model = "gpt-5.4"
        response = Mock(
            status="completed",
            output_text='{"score": 42, "notes": "Ok", "tags": []}',
        )
        openai.return_value.responses.create.return_value = response

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}):
            score_review("Texto", "", 1, settings)

        request = openai.return_value.responses.create.call_args.kwargs
        self.assertEqual(request["reasoning"], {"effort": "none"})
        self.assertEqual(request["temperature"], 0.2)

    def test_model_profiles_expose_only_supported_reasoning_controls(self) -> None:
        astra = openai_model_profile("gpt-6-astra")
        sol = openai_model_profile("gpt-6-sol")
        classic = openai_model_profile("gpt-4o-mini")
        pro = openai_model_profile("gpt-5.4-pro")

        self.assertNotIn("none", astra["reasoning_efforts"])
        self.assertIn("none", sol["reasoning_efforts"])
        self.assertEqual(sol["reasoning_modes"], ["standard", "pro"])
        self.assertEqual(classic["reasoning_efforts"], [])
        self.assertTrue(classic["supports_temperature"])
        self.assertEqual(pro["reasoning_efforts"], [])
        self.assertFalse(pro["supports_temperature"])


if __name__ == "__main__":
    unittest.main()
