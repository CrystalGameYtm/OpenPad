"""Web UI translations: locale files are consistent, user locales override, the config key is validated."""
from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

from gamepad_remapd.config import ConfigError, load_config
from gamepad_remapd.web import locales

PH = re.compile(r"\{(\w+)\}")
STATIC = Path(locales.BUILTIN_DIR).parent


class BuiltinLocales(unittest.TestCase):
    def setUp(self):
        self.en = locales.load_locale("en")
        self.uk = locales.load_locale("uk")

    def test_english_and_ukrainian_exist(self):
        self.assertTrue(self.en and self.uk)
        self.assertEqual(self.en["_name"], "English")

    def test_same_keys_and_placeholders(self):
        self.assertEqual(set(self.en), set(self.uk))
        for k in self.en:
            self.assertEqual(set(PH.findall(self.en[k])), set(PH.findall(self.uk[k])), k)

    def test_every_key_used_by_the_clients_exists(self):
        used = set()
        for js in ("app.js", "testpage.js"):
            used |= set(re.findall(r"tx\(['\"]([\w.]+)['\"]", (STATIC / js).read_text(encoding="utf-8")))
        for html in ("index.html", "testpage.html"):
            used |= set(re.findall(r'data-i18n[\w-]*="([\w.]+)"', (STATIC / html).read_text(encoding="utf-8")))
        used |= {f"tab.{t}" for t in ("device", "buttons", "sticks", "triggers", "gyro", "profiles", "diag", "system")}
        self.assertEqual(used - set(self.en), set())

    def test_no_cyrillic_outside_locales_in_client_code(self):
        cyr = re.compile("[Ѐ-ӿ]")
        for name in ("app.js", "testpage.js", "i18n.js", "index.html", "testpage.html"):
            self.assertIsNone(cyr.search((STATIC / name).read_text(encoding="utf-8")), name)


class UserLocales(unittest.TestCase):
    def test_user_file_overrides_and_adds(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "uk.json").write_text(json.dumps({"btn.open": "Розкрити"}), encoding="utf-8")
            (d / "pl.json").write_text(json.dumps({"_name": "Polski", "btn.open": "Otwórz"}), encoding="utf-8")
            self.assertEqual(locales.load_locale("uk", d)["btn.open"], "Розкрити")
            self.assertEqual(locales.load_locale("uk", d)["btn.close"], "Закрити")      # built-in key kept
            self.assertEqual(locales.load_locale("pl", d)["btn.open"], "Otwórz")
            codes = [x["code"] for x in locales.list_locales(d)]
            self.assertEqual(codes[0], "en")
            self.assertIn("pl", codes)
            self.assertEqual({x["code"]: x["name"] for x in locales.list_locales(d)}["pl"], "Polski")

    def test_bad_codes_and_files_are_ignored(self):
        self.assertIsNone(locales.load_locale("../etc/passwd"))
        self.assertIsNone(locales.load_locale("zz"))
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "xx.json").write_text("not json", encoding="utf-8")
            (d / "yy.json").write_text(json.dumps({"a": 1, "b": "ok"}), encoding="utf-8")
            self.assertEqual(locales.load_locale("xx", d), {})
            self.assertEqual(locales.load_locale("yy", d), {"b": "ok"})


class ConfigLanguage(unittest.TestCase):
    def cfg(self, value):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "daemon.json"
            p.write_text(json.dumps({"language": value}), encoding="utf-8")
            return load_config(p, config_dir=Path(d))

    def test_default_is_auto(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_config(config_dir=Path(d)).language, "auto")

    def test_valid_codes(self):
        for v in ("auto", "uk", "en", "pt-BR"):
            self.assertEqual(self.cfg(v).language, v)

    def test_invalid_code(self):
        for v in ("", "українська", "../x", 5):
            with self.assertRaises(ConfigError):
                self.cfg(v)


class TestPageEndpoints(unittest.TestCase):
    def test_locale_api(self):
        from gamepad_remapd.tools.testpage import TestPage, make_server
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("GAMEPAD_REMAPD_HOME")
            os.environ["GAMEPAD_REMAPD_HOME"] = home
            (Path(home) / "daemon.json").write_text(json.dumps({"language": "uk"}), encoding="utf-8")
            tp = TestPage({}, enumerate_fn=lambda b: [], open_fn=lambda i, s: None)
            srv = make_server(tp, "127.0.0.1", 0)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def get(path):
                    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                    c.request("GET", path)
                    r = c.getresponse()
                    data = r.read()
                    c.close()
                    return r.status, data
                st, body = get("/api/locales")
                meta = json.loads(body)
                self.assertEqual((st, meta["configured"]), (200, "uk"))
                self.assertEqual({x["code"] for x in meta["available"]} >= {"en", "uk"}, True)
                st, body = get("/api/locale/uk")
                self.assertEqual(st, 200)
                self.assertEqual(json.loads(body)["tab.buttons"], "Кнопки")
                self.assertEqual(get("/api/locale/zz")[0], 404)
                self.assertEqual(get("/i18n.js")[0], 200)
            finally:
                tp.close()
                srv.shutdown()
                srv.server_close()
                if old is None:
                    os.environ.pop("GAMEPAD_REMAPD_HOME", None)
                else:
                    os.environ["GAMEPAD_REMAPD_HOME"] = old


if __name__ == "__main__":
    unittest.main()
