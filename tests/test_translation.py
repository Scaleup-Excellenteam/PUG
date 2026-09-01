"""Unit tests for the translation and input adaptation module."""

from __future__ import annotations

from io import StringIO
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index
from main import run_cli
from translation import (
    AdaptationMode,
    InputAdaptationPipeline,
    KeyboardLayoutMapper,
    SigmaGuard,
    SigmaPolicy,
    TokenTranslator,
)
from translation.token_translator import is_foreign_token


class KeyboardLayoutMapperTests(unittest.TestCase):
    """Test physical keyboard layout remapping."""

    def setUp(self) -> None:
        self.mapper = KeyboardLayoutMapper.load_default()

    def test_hebrew_qwerty_word_remap(self) -> None:
        # "יקךךם" on Hebrew keyboard corresponds to "hello" on US QWERTY
        self.assertEqual(self.mapper.remap_text("יקךךם"), "hello")

    def test_hebrew_qwerty_single_chars(self) -> None:
        self.assertEqual(self.mapper.remap_char("ש"), "a")
        self.assertEqual(self.mapper.remap_char("ד"), "s")
        self.assertEqual(self.mapper.remap_char("ג"), "d")
        self.assertEqual(self.mapper.remap_char("כ"), "f")

    def test_english_text_left_unchanged(self) -> None:
        self.assertEqual(self.mapper.remap_text("hello world"), "hello world")

    def test_candidate_detection(self) -> None:
        self.assertTrue(self.mapper.is_candidate("יקךךם"))
        self.assertTrue(self.mapper.is_candidate("hello יקךךם"))
        self.assertFalse(self.mapper.is_candidate("hello world"))

    def test_custom_layout_from_file(self) -> None:
        custom_data = {
            "name": "rot1_sample",
            "mapping": {"a": "b", "b": "c", "c": "d"},
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(custom_data, tmp)
            tmp_path = Path(tmp.name)

        try:
            custom_mapper = KeyboardLayoutMapper.from_file(tmp_path)
            self.assertEqual(custom_mapper.name, "rot1_sample")
            self.assertEqual(custom_mapper.remap_text("abc"), "bcd")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_multiple_layouts_loaded_and_active_simultaneously(self) -> None:
        # Default mapper auto-loads both Hebrew and Arabic layouts from layouts/
        mapper = KeyboardLayoutMapper.load_default()
        self.assertGreaterEqual(len(mapper.loaded_layouts), 2)
        # Hebrew typing remaps to "hello"
        self.assertEqual(mapper.remap_text("יקךךם"), "hello")
        # Arabic typing remaps to "hello" simultaneously without mode switches!
        self.assertEqual(mapper.remap_text("اثممخ"), "hello")
        # Arabic typing remaps to "python"
        self.assertEqual(mapper.remap_text("حغفاخى"), "python")
        # Copy-pasted text with invisible RTL marks (\u200f, \u200e) remaps cleanly
        self.assertEqual(mapper.remap_text("\u200fحغفاخى\u200e"), "python")


class SigmaGuardTests(unittest.TestCase):
    """Test Sigma alphabet validation and policy enforcement."""

    def setUp(self) -> None:
        self.guard = SigmaGuard(default_policy=SigmaPolicy.WARN)

    def test_clean_ascii_query_passes(self) -> None:
        result = self.guard.validate("hello, world! 123 - search query")
        self.assertTrue(result.is_valid)
        self.assertEqual(result.violations, [])
        self.assertFalse(result.is_blocked)
        self.assertIsNone(result.warning)

    def test_hebrew_chars_detected_under_warn(self) -> None:
        result = self.guard.validate("hello שלום", policy=SigmaPolicy.WARN)
        self.assertFalse(result.is_valid)
        self.assertTrue(len(result.violations) > 0)
        self.assertFalse(result.is_blocked)
        self.assertIn("Warning: query contains symbols outside", result.warning or "")

    def test_hebrew_chars_blocked_under_block(self) -> None:
        result = self.guard.validate("hello שלום", policy=SigmaPolicy.BLOCK)
        self.assertFalse(result.is_valid)
        self.assertTrue(result.is_blocked)
        self.assertIn("Blocked query containing symbols", result.warning or "")

    def test_policy_off_never_warns_or_blocks(self) -> None:
        result = self.guard.validate("שלום עולם 😊", policy=SigmaPolicy.OFF)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.violations, [])
        self.assertFalse(result.is_blocked)
        self.assertIsNone(result.warning)


class TokenTranslatorTests(unittest.TestCase):
    """Test token-level translation for bridging forgotten words."""

    def test_foreign_token_detection(self) -> None:
        self.assertTrue(is_foreign_token("שלום"))
        self.assertTrue(is_foreign_token("café"))
        self.assertTrue(is_foreign_token("привет"))
        self.assertFalse(is_foreign_token("hello"))
        self.assertFalse(is_foreign_token("bonjour"))  # standard ASCII characters
        self.assertFalse(is_foreign_token("123"))
        self.assertFalse(is_foreign_token(",!?-"))

    def test_mock_token_translation_preserves_structure(self) -> None:
        dictionary = {
            "שלום": ("hello", "he"),
            "מסמך": ("document", "he"),
            "bonjour": ("hello", "fr"),
        }

        def mock_translate_api(words: list[str], target_language: str) -> list[dict[str, str]]:
            return [
                {
                    "translatedText": dictionary.get(w, (w, "und"))[0],
                    "detectedSourceLanguage": dictionary.get(w, (w, "und"))[1],
                }
                for w in words
            ]

        translator = TokenTranslator(custom_translate_fn=mock_translate_api)
        query = "שלום, world! the מסמך is here."
        final_query, tokens, languages = translator.translate_tokens(query)

        self.assertEqual(final_query, "hello, world! the document is here.")
        self.assertEqual(languages, ["he"])

        # Verify individual tokens
        adapted_words = [t for t in tokens if t.was_adapted]
        self.assertEqual(len(adapted_words), 2)
        self.assertEqual(adapted_words[0].original, "שלום")
        self.assertEqual(adapted_words[0].adapted, "hello")
        self.assertEqual(adapted_words[1].original, "מסמך")
        self.assertEqual(adapted_words[1].adapted, "document")

    def test_caching_eliminates_redundant_calls(self) -> None:
        call_count = 0

        def counting_translate(words: list[str], target_language: str) -> list[dict[str, str]]:
            nonlocal call_count
            call_count += 1
            return [{"translatedText": "hello", "detectedSourceLanguage": "he"} for _ in words]

        translator = TokenTranslator(custom_translate_fn=counting_translate)
        translator.translate_tokens("שלום")
        self.assertEqual(call_count, 1)

        # Repeating same token should hit cache and not call translate
        translator.translate_tokens("שלום again")
        self.assertEqual(call_count, 1)

    def test_bypasses_gracefully_when_service_unavailable(self) -> None:
        # Default with no API key and no google credentials
        translator = TokenTranslator(api_key=None)
        final_query, tokens, languages = translator.translate_tokens("שלום world")
        # Should not crash, leaves token unchanged
        self.assertEqual(final_query, "שלום world")


class InputAdaptationPipelineTests(unittest.TestCase):
    """Test pipeline coordinating translation, remapping, and Sigma guard."""

    def test_pipeline_default_remaps_hebrew_layout_out_of_the_box(self) -> None:
        pipeline = InputAdaptationPipeline()
        # Out of the box with defaults:
        self.assertTrue(pipeline.enable_keymap)
        self.assertFalse(pipeline.enable_translate)
        self.assertEqual(pipeline.sigma_policy, SigmaPolicy.WARN)

        result = pipeline.process("יקךךם")
        self.assertEqual(result.final_query, "hello")
        self.assertTrue(result.was_adapted)
        self.assertTrue(result.keymap_applied)
        self.assertFalse(result.is_blocked)
        self.assertEqual(result.sigma_violations, [])
        self.assertIsNone(result.warning_message)

    def test_pipeline_off_mode_keeps_original(self) -> None:
        pipeline = InputAdaptationPipeline(
            enable_keymap=False,
            enable_translate=False,
            sigma_policy=SigmaPolicy.OFF,
        )
        result = pipeline.process("שלום world")
        self.assertEqual(result.final_query, "שלום world")
        self.assertFalse(result.was_adapted)
        self.assertFalse(result.is_blocked)

    def test_pipeline_keyboard_remap_mode(self) -> None:
        pipeline = InputAdaptationPipeline(
            enable_keymap=True,
            enable_translate=False,
            sigma_policy=SigmaPolicy.BLOCK,
        )
        result = pipeline.process("יקךךם")
        # Remapped to "hello", which is valid ASCII, so not blocked
        self.assertEqual(result.final_query, "hello")
        self.assertTrue(result.was_adapted)
        self.assertFalse(result.is_blocked)
        self.assertEqual(result.sigma_violations, [])

    def test_pipeline_translate_mode_with_sigma_guard(self) -> None:
        def mock_translate(words: list[str], target_lang: str) -> list[dict[str, str]]:
            return [{"translatedText": "hello", "detectedSourceLanguage": "he"} for _ in words]

        translator = TokenTranslator(custom_translate_fn=mock_translate)
        pipeline = InputAdaptationPipeline(
            enable_keymap=False,
            enable_translate=True,
            sigma_policy=SigmaPolicy.BLOCK,
            translator=translator,
        )
        result = pipeline.process("שלום world")
        # Translated to "hello world", which is now inside Sigma
        self.assertEqual(result.final_query, "hello world")
        self.assertTrue(result.was_adapted)
        self.assertFalse(result.is_blocked)
        self.assertEqual(result.detected_languages, ["he"])

    def test_pipeline_blocks_untranslated_foreign_when_guard_is_block(self) -> None:
        pipeline = InputAdaptationPipeline(
            enable_keymap=False,
            enable_translate=False,
            sigma_policy=SigmaPolicy.BLOCK,
        )
        result = pipeline.process("שלום world")
        self.assertTrue(result.is_blocked)
        self.assertIn("Blocked query containing symbols", result.warning_message or "")


class AutocompleteEngineIntegrationTests(unittest.TestCase):
    """Test pipeline feeding into AutocompleteSystem."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.corpus = Path(self.temporary_directory.name) / "corpus"
        self.corpus.mkdir(parents=True)
        (self.corpus / "data.txt").write_text(
            "Hello, world! This is a test sentence.\nWelcome to the PUG autocomplete project.\n",
            encoding="utf-8",
        )
        index, master = build_index(self.corpus)
        self.system = AutocompleteSystem(index, master)

    def tearDown(self) -> None:
        self.system.close()
        self.temporary_directory.cleanup()

    def test_keyboard_remap_enables_search(self) -> None:
        pipeline = InputAdaptationPipeline(
            enable_keymap=True,
            sigma_policy=SigmaPolicy.OFF,
        )
        # User meant "hello" but typed "יקךךם" on Hebrew keyboard
        adaptation = pipeline.process("יקךךם")
        self.assertEqual(adaptation.final_query, "hello")

        completions = self.system.get_best_k_completions(adaptation.final_query)
        self.assertTrue(len(completions) > 0)
        self.assertIn("Hello, world!", completions[0].completed_sentence)

    def test_token_translate_enables_search(self) -> None:
        def mock_translate(words: list[str], target_lang: str) -> list[dict[str, str]]:
            return [{"translatedText": "welcome", "detectedSourceLanguage": "he"} for _ in words]

        translator = TokenTranslator(custom_translate_fn=mock_translate)
        pipeline = InputAdaptationPipeline(
            enable_keymap=False,
            enable_translate=True,
            sigma_policy=SigmaPolicy.OFF,
            translator=translator,
        )
        # User types mixed query: "ברוך הבא to the PUG"
        adaptation = pipeline.process("ברוך to the PUG")
        self.assertIn("welcome to the PUG", adaptation.final_query)

        completions = self.system.get_best_k_completions(adaptation.final_query)
        self.assertTrue(len(completions) > 0)
        self.assertIn("Welcome to the PUG", completions[0].completed_sentence)


class CliAdaptationTests(unittest.TestCase):
    """Test CLI interactive commands and adaptation behavior."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.corpus = Path(self.temporary_directory.name) / "corpus"
        self.corpus.mkdir(parents=True)
        (self.corpus / "data.txt").write_text(
            "Hello, world! This is a demo sentence.\n",
            encoding="utf-8",
        )
        index, master = build_index(self.corpus)
        self.system = AutocompleteSystem(index, master)

    def tearDown(self) -> None:
        self.system.close()
        self.temporary_directory.cleanup()

    def test_cli_interactive_toggles(self) -> None:
        pipeline = InputAdaptationPipeline()
        output = StringIO()
        # Feed commands: :translate, :keymap, :sigma, :status, then EOFError
        with patch("builtins.input", side_effect=[":translate", ":keymap", ":sigma", ":status", EOFError]):
            with redirect_stdout(output):
                run_cli(self.system, pipeline)

        rendered = output.getvalue()
        self.assertIn("[Config] Token-level translation: ENABLED", rendered)
        self.assertIn("[Config] Keyboard remapping: DISABLED", rendered)
        self.assertIn("[Config] Sigma policy set to: block", rendered)
        self.assertIn("[Status]", rendered)

    def test_cli_blocks_foreign_when_policy_is_block(self) -> None:
        pipeline = InputAdaptationPipeline(
            enable_keymap=False,
            sigma_policy=SigmaPolicy.BLOCK,
        )
        output = StringIO()
        with patch("builtins.input", side_effect=["שלום", EOFError]):
            with redirect_stdout(output):
                run_cli(self.system, pipeline)

        rendered = output.getvalue()
        self.assertIn("[Blocked]", rendered)
        self.assertIn("Query was blocked by Sigma policy.", rendered)

    def test_cli_warns_and_proceeds_when_user_confirms(self) -> None:
        pipeline = InputAdaptationPipeline(
            enable_keymap=False,
            sigma_policy=SigmaPolicy.WARN,
        )
        output = StringIO()
        # User types "שלום", then answers "y" to "Proceed anyway?" prompt
        with patch("builtins.input", side_effect=["שלום", "y", EOFError]):
            with redirect_stdout(output):
                run_cli(self.system, pipeline)

        rendered = output.getvalue()
        self.assertIn("[Warning]", rendered)

    def test_cli_default_remaps_hebrew_keystrokes_without_prompt(self) -> None:
        output = StringIO()
        # By default out-of-the-box (no pipeline passed), typing "יקךךם" auto-remaps to "hello"
        with patch("builtins.input", side_effect=["יקךךם", EOFError]):
            with redirect_stdout(output):
                run_cli(self.system)

        rendered = output.getvalue()
        self.assertIn("[Adapted]: 'יקךךם' -> 'hello'", rendered)
        self.assertIn("1. Hello, world!", rendered)
        self.assertNotIn("[Warning]", rendered)


class PipelineLoggingTests(unittest.TestCase):
    """Test structured logging in InputAdaptationPipeline."""

    def test_pipeline_emits_structured_logs_on_remap_and_sigma(self) -> None:
        pipeline = InputAdaptationPipeline()
        with patch("translation.pipeline.log_event") as mock_log:
            pipeline.process("יקךךם")
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            self.assertEqual(args[1], "input_remapped")
            self.assertEqual(kwargs["original_query"], "יקךךם")
            self.assertEqual(kwargs["remapped_query"], "hello")

        pipeline_off = InputAdaptationPipeline(enable_keymap=False, sigma_policy=SigmaPolicy.BLOCK)
        with patch("translation.pipeline.log_event") as mock_log:
            pipeline_off.process("שלום")
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            self.assertEqual(args[1], "sigma_violations_detected")
            self.assertTrue(kwargs["is_blocked"])


if __name__ == "__main__":
    unittest.main()
