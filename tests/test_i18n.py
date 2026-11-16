"""Backend message translation (gamepad_remapd.i18n)."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from gamepad_remapd import i18n
from gamepad_remapd.i18n import set_language, tr


class I18n(unittest.TestCase):
    def tearDown(self):
        set_language("en")

    def test_english_is_the_source(self):
        set_language("en")
        self.assertEqual(tr("device {0!r} not found", "x"), "device 'x' not found")
        self.assertEqual(tr("no such message {0}", 5), "no such message 5")

    def test_builtin_ukrainian_catalog(self):
        self.assertEqual(set_language("uk"), "uk")
        self.assertIn("не знайдено", tr("device {0!r} not found", "x"))
        self.assertEqual(set_language("uk-UA"), "uk")             # region falls back to the base language

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(set_language("zz"), "en")
        self.assertEqual(set_language("../x"), "en")
        self.assertEqual(tr("stopped"), "stopped")

    def test_user_catalog_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "uk.json").write_text(json.dumps({"stopped": "стоп"}), encoding="utf-8")
            set_language("uk", Path(d))
            self.assertEqual(tr("stopped"), "стоп")
            self.assertIn("не знайдено", tr("device {0!r} not found", "x"))   # built-in messages stay
            (Path(d) / "de.json").write_text(json.dumps({"stopped": "gestoppt"}), encoding="utf-8")
            self.assertEqual(set_language("de", Path(d)), "de")
            self.assertEqual(tr("stopped"), "gestoppt")

    def test_bad_placeholders_do_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "de.json").write_text(json.dumps({"a {0}": "b {1} {x}"}), encoding="utf-8")
            set_language("de", Path(d))
            self.assertEqual(tr("a {0}", 1), "a 1")

    def test_auto_means_english(self):
        import os
        old = os.environ.get("LANG")
        try:
            os.environ["LANG"] = "uk_UA.UTF-8"
            self.assertEqual(set_language("auto"), "en")      # the system locale is deliberately ignored
        finally:
            if old is None:
                os.environ.pop("LANG", None)
            else:
                os.environ["LANG"] = old

    def test_catalog_placeholders_match(self):
        """The translation must keep exactly the placeholders of the English text."""
        cat = json.loads((i18n.BUILTIN_DIR / "uk.json").read_text(encoding="utf-8"))
        pat = re.compile(r"\{\d+(?:![rsa])?(?::[^}]*)?\}|%[-#0 +]*\d*(?:\.\d+)?[sdrxXfc]")
        for en, uk in cat.items():
            if en.startswith("_"):
                continue
            self.assertEqual(sorted(pat.findall(en)), sorted(pat.findall(uk)), en)
            self.assertFalse(re.search("[Ѐ-ӿ]", en), en)


if __name__ == "__main__":
    unittest.main()


class PerContextLanguage(unittest.TestCase):
    def tearDown(self):
        set_language("en")

    def test_use_language_overrides_only_inside_context(self):
        set_language("en")
        token = i18n.use_language("uk")
        try:
            self.assertIn("не знайдено", tr("device {0!r} not found", "x"))
        finally:
            i18n.reset_language(token)
        self.assertEqual(tr("device {0!r} not found", "x"), "device 'x' not found")
