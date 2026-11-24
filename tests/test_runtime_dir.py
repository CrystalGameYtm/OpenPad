"""The runtime directory (socket, hidden.json) must be private and ours."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gamepad_remapd import config as C


@unittest.skipUnless(os.name == "posix", "POSIX permissions")
class EnsurePrivateDirTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def mode(self, p):
        return stat.S_IMODE(os.stat(p).st_mode)

    def test_creates_a_private_directory(self):
        d = C.ensure_private_dir(self.root / "a" / "run")
        self.assertTrue(d.is_dir())
        self.assertEqual(self.mode(d), 0o700)

    def test_tightens_an_open_directory_of_ours(self):
        d = self.root / "run"
        d.mkdir()
        os.chmod(d, 0o777)
        C.ensure_private_dir(d)
        self.assertEqual(self.mode(d), 0o700)

    def test_refuses_a_symlink(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "run"
        os.symlink(real, link)
        with self.assertRaises(PermissionError):
            C.ensure_private_dir(link)

    def test_refuses_a_file(self):
        f = self.root / "run"
        f.write_text("")
        with self.assertRaises(PermissionError):
            C.ensure_private_dir(f)

    def test_refuses_a_directory_of_another_user(self):
        d = self.root / "run"
        d.mkdir()
        with mock.patch.object(C.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(PermissionError):
                C.ensure_private_dir(d)


@unittest.skipUnless(os.name == "posix", "POSIX permissions")
class DefaultRuntimeDirTests(unittest.TestCase):
    def test_xdg_runtime_dir_wins(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1234"}):
            self.assertEqual(C.default_runtime_dir(), Path("/run/user/1234/gamepad-remapd"))

    def test_falls_back_to_the_private_run_user_directory(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        real_lstat = os.lstat
        st = os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 2, os.getuid(), 0, 0, 0, 0, 0))

        def fake(p, *a, **k):
            return st if str(p) == f"/run/user/{os.getuid()}" else real_lstat(p, *a, **k)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(C.os, "lstat", fake):
            self.assertEqual(C.default_runtime_dir(), Path(f"/run/user/{os.getuid()}/gamepad-remapd"))

    def test_tmp_fallback_when_run_user_is_not_usable(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(C.os, "lstat", side_effect=FileNotFoundError):
            self.assertEqual(C.default_runtime_dir(), Path(tempfile.gettempdir()) / f"gamepad-remapd-{os.getuid()}")


if __name__ == "__main__":
    unittest.main()
