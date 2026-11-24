"""Web token authentication (pure logic: aiohttp is not needed) and the web_auth option."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from gamepad_remapd.config import ConfigError, DaemonConfig, load_config, read_or_create_token
from gamepad_remapd.web.security import SESSION_COOKIE, authorized, token_matches

TOKEN = "0123456789abcdef0123456789abcdef"


class AuthorizedTests(unittest.TestCase):
    def test_auth_off_lets_everything_in(self):
        self.assertTrue(authorized(None, None, None))

    def test_nothing_presented(self):
        self.assertFalse(authorized(TOKEN, None, None))
        self.assertFalse(authorized(TOKEN, "", ""))

    def test_cookie(self):
        self.assertTrue(authorized(TOKEN, None, TOKEN))
        self.assertFalse(authorized(TOKEN, None, TOKEN[:-1] + "0"))
        self.assertFalse(authorized(TOKEN, None, TOKEN + "x"))
        self.assertFalse(authorized(TOKEN, None, TOKEN[:-1]))

    def test_bearer(self):
        self.assertTrue(authorized(TOKEN, f"Bearer {TOKEN}", None))
        self.assertTrue(authorized(TOKEN, f"bearer  {TOKEN} ", None))
        self.assertFalse(authorized(TOKEN, f"Basic {TOKEN}", None))
        self.assertFalse(authorized(TOKEN, "Bearer wrong", None))
        self.assertFalse(authorized(TOKEN, "Bearer", None))
        self.assertFalse(authorized(TOKEN, TOKEN, None))

    def test_non_ascii_does_not_crash(self):
        self.assertFalse(token_matches("tоken\ud800", TOKEN))
        self.assertFalse(authorized(TOKEN, "Bearer é", "é"))

    def test_empty_never_matches(self):
        self.assertFalse(token_matches("", ""))
        self.assertFalse(token_matches(None, TOKEN))
        self.assertFalse(token_matches(TOKEN, ""))

    def test_cookie_name(self):
        self.assertEqual(SESSION_COOKIE, "gpr_session")


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_web_token_is_created_private_and_stable(self):
        cfg = DaemonConfig(config_dir=self.dir)
        t1 = read_or_create_token(cfg.web_token_path)
        self.assertGreaterEqual(len(t1), 32)
        self.assertEqual(read_or_create_token(cfg.web_token_path), t1)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(cfg.web_token_path).st_mode), 0o600)
        self.assertNotEqual(cfg.web_token_path, cfg.token_path)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_a_readable_token_file_is_tightened(self):
        cfg = DaemonConfig(config_dir=self.dir)
        t = read_or_create_token(cfg.web_token_path)
        os.chmod(cfg.web_token_path, 0o644)
        self.assertEqual(read_or_create_token(cfg.web_token_path), t)
        self.assertEqual(stat.S_IMODE(os.stat(cfg.web_token_path).st_mode), 0o600)


class ConfigTests(unittest.TestCase):
    def load(self, raw):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td, "daemon.json")
            p.write_text(json.dumps(raw))
            return load_config(p)

    def test_default_is_on(self):
        self.assertTrue(DaemonConfig().web_auth)

    def test_can_be_switched_off(self):
        self.assertFalse(self.load({"web_auth": False}).web_auth)

    def test_must_be_a_bool(self):
        for bad in ("no", 0, None, []):
            with self.assertRaises(ConfigError):
                self.load({"web_auth": bad})


if __name__ == "__main__":
    unittest.main()
