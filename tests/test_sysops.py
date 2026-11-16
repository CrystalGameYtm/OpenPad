"""Service operations for the "System" tab: profile check, auto-layout, udev, rumble, unhide."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gamepad_remapd.config import BUILTIN_LAYOUTS, DaemonConfig
from gamepad_remapd.core import sysops
from gamepad_remapd.daemon import load_layouts_safe
from gamepad_remapd.logic.profile import ProfileError
from tests.test_autolayout import DS4_LIKE


class Sys(unittest.TestCase):
    def setUp(self):
        self.layouts = load_layouts_safe([BUILTIN_LAYOUTS])

    def test_check_profile_ok_and_warns(self):
        r = sysops.check_profile({"name": "a"}, None, self.layouts)
        self.assertTrue(r["ok"])
        lid = "sony_ds4_usb"
        r = sysops.check_profile({"name": "a", "button_map": {"L1": "A"}}, lid, self.layouts)
        self.assertEqual(r["warnings"], [])

    def test_check_profile_errors(self):
        with self.assertRaises(ProfileError):
            sysops.check_profile({"name": "a", "triggers": {"left": {"mode": "x"}}}, None, self.layouts)
        with self.assertRaises(ValueError):
            sysops.check_profile({"name": "a"}, "nope", self.layouts)

    def test_udev(self):
        t = sysops.udev_rules(self.layouts, ["054c:09cc"], [], None)["text"]
        self.assertIn("054c", t)
        with self.assertRaises(ValueError):
            sysops.udev_rules(self.layouts, [], ["bad"], None)
        self.assertTrue(sysops.udev_rules(self.layouts, [], ["054c:09cc"], "input")["text"])

    def test_autolayout_save(self):
        info = SimpleNamespace(descriptor=DS4_LIKE, vid=0x054C, pid=0x05C4, name="Wireless Controller", interface=0)
        with tempfile.TemporaryDirectory() as d:
            r = sysops.autolayout(info, True, Path(d))
            self.assertTrue(Path(r["saved"]).exists())
            self.assertEqual(r["id"], "auto_054c_05c4_if0")
            self.assertIn("layout", r)
            r2 = sysops.autolayout(info, False, Path(d))
            self.assertNotIn("saved", r2)
        with self.assertRaises(ValueError):
            sysops.autolayout(SimpleNamespace(descriptor=b"", vid=1, pid=1, name="", interface=None), False, Path("."))

    def test_rumble(self):
        calls = []
        s = SimpleNamespace(layout=SimpleNamespace(rumble=object()), dev=object(),
                            _on_rumble=lambda a, b: calls.append((a, b)))
        r = sysops.rumble(s, 1.0, 0.5, 50)
        self.assertEqual(calls, [(65535, 32767), (0, 0)])
        self.assertEqual(r["ms"], 50)
        with self.assertRaises(ValueError):
            sysops.rumble(SimpleNamespace(layout=None, dev=object(), _on_rumble=None), 1, 1, 50)

    def test_system_info_and_keyboards(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = DaemonConfig(config_dir=Path(d))
            i = sysops.system_info(cfg)
            self.assertIn("hidden_nodes", i)
            self.assertIn("candidates", sysops.keyboards(cfg))


if __name__ == "__main__":
    unittest.main()
