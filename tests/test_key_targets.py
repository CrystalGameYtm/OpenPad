"""Геймпад -> клавіатура/миша: цілі KEY_*/MOUSE_* у button_map віддаються окремою віртуальною клавіатурою."""
import os
import unittest

from gamepad_remapd import constants as C
from gamepad_remapd.logic.keytargets import key_target_code, key_target_names
from gamepad_remapd.logic.layout import DeviceLayout, ReportParser
from gamepad_remapd.logic.pipeline import Pipeline
from gamepad_remapd.logic.profile import ProfileError, compile_profile, normalize_profile
from gamepad_remapd.output.null import NullPad
from gamepad_remapd.output.uinput_linux import SPECS, UinputPad

from tests.fakes import drain, ds4_report, frames
from pathlib import Path

LAYOUTS = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"
KEY_F, KEY_SPACE, BTN_LEFT = 33, 57, 0x110


def rig(button_map):
    lay = DeviceLayout.from_file(LAYOUTS / "sony_ds4_usb.json")
    r, w = os.pipe()
    kr, kw = os.pipe()
    for fd in (r, kr):
        os.set_blocking(fd, False)
    pad = UinputPad(SPECS["xbox360"], fd=w, kbd_fd=kw)
    cp = compile_profile({"name": "t", "button_map": button_map}, lay, ReportParser(lay), pad)
    p = Pipeline(pad)
    p.cp = cp
    return p, cp, pad, r, kr


class Targets(unittest.TestCase):
    def test_table_matches_keyboard_module_when_present(self):
        try:
            from gamepad_remapd.logic import keyboard
        except ImportError:
            self.skipTest("модуль клавіатури не встановлено")
        from gamepad_remapd.logic import keytargets
        self.assertEqual(keytargets._KEYS, keyboard._KEYS)

    def test_names(self):
        self.assertEqual(key_target_code("KEY_F"), KEY_F)
        self.assertEqual(key_target_code("key_space"), KEY_SPACE)
        self.assertEqual(key_target_code("MOUSE_LEFT"), BTN_LEFT)
        self.assertEqual(key_target_code("KEY_VOLUMEUP"), 115)
        self.assertIsNone(key_target_code("A"))
        self.assertIsNone(key_target_code("KEY_NOPE"))
        self.assertIn("KEY_W", key_target_names())

    def test_profile_validation(self):
        d = normalize_profile({"button_map": {"A": ["key_f", "B"], "X": "MOUSE_LEFT"}})
        self.assertEqual(d["button_map"]["A"], ["KEY_F", "B"])
        with self.assertRaises(ProfileError):
            normalize_profile({"button_map": {"A": "KEY_NOPE"}})


class Pipe(unittest.TestCase):
    def test_press_and_release_key(self):
        p, cp, pad, r, kr = rig({"CROSS": "KEY_F"})
        self.assertEqual(cp.key_codes, (KEY_F,))
        p.process(ds4_report(cross=True))
        self.assertEqual(frames(drain(kr)), [[(C.EV_KEY, KEY_F, 1)]])
        self.assertNotIn((C.EV_KEY, C.BTN_SOUTH, 1), drain(r), "A більше не йде на віртуальний геймпад")
        p.process(ds4_report(cross=False))
        self.assertEqual(frames(drain(kr)), [[(C.EV_KEY, KEY_F, 0)]])

    def test_gamepad_and_key_together_and_mouse(self):
        p, cp, pad, r, kr = rig({"CROSS": ["A", "KEY_SPACE"], "CIRCLE": "MOUSE_LEFT"})
        p.process(ds4_report(cross=True, circle=True))
        pad_events = [e for f in frames(drain(r)) for e in f]
        self.assertIn((C.EV_KEY, C.BTN_SOUTH, 1), pad_events)
        kbd = frames(drain(kr))
        self.assertEqual(len(kbd), 1, "усі клавіші кадру — одним SYN")
        self.assertEqual(sorted(kbd[0]), sorted([(C.EV_KEY, KEY_SPACE, 1), (C.EV_KEY, BTN_LEFT, 1)]))

    def test_two_buttons_one_key_refcount(self):
        p, cp, pad, r, kr = rig({"CROSS": "KEY_F", "CIRCLE": "KEY_F"})
        self.assertEqual(len(cp.key_codes), 1)
        p.process(ds4_report(cross=True))
        p.process(ds4_report(cross=True, circle=True))
        p.process(ds4_report(circle=True))              # A відпущено, B ще тримається -> клавіша лишається
        ev = [e for f in frames(drain(kr)) for e in f]
        self.assertEqual(ev, [(C.EV_KEY, KEY_F, 1)])
        p.process(ds4_report())
        self.assertEqual([e for f in frames(drain(kr)) for e in f], [(C.EV_KEY, KEY_F, 0)])

    def test_release_all_frees_held_keys(self):
        p, cp, pad, r, kr = rig({"CROSS": "KEY_F"})
        p.process(ds4_report(cross=True))
        drain(kr)
        pad.release_all()                               # зміна профілю / відключення геймпада
        self.assertEqual([e for f in frames(drain(kr)) for e in f], [(C.EV_KEY, KEY_F, 0)])

    def test_unsupported_backend_warns_and_skips(self):
        lay = DeviceLayout.from_file(LAYOUTS / "sony_ds4_usb.json")
        cp = compile_profile({"name": "t", "button_map": {"CROSS": "KEY_F"}}, lay, ReportParser(lay), NullPad())
        self.assertEqual(cp.key_codes, ())
        self.assertTrue(any("клавіатуру" in w for w in cp.warnings))

    def test_no_keyboard_device_without_key_targets(self):
        p, cp, pad, r, kr = rig({"CROSS": "B"})
        self.assertIsNone(pad._kbd)


if __name__ == "__main__":
    unittest.main()


class DpadButtons(unittest.TestCase):
    def test_dpad_as_buttons(self):
        r, w = os.pipe()
        os.set_blocking(r, False)
        pad = UinputPad(SPECS["xbox360"], fd=w, dpad_buttons=True)
        pad.set_button(pad.button_id("UP"), True)
        pad.set_button(pad.button_id("RIGHT"), True)
        pad.flush()
        self.assertEqual(frames(drain(r)), [[(C.EV_KEY, C.BTN_DPAD_UP, 1), (C.EV_KEY, C.BTN_DPAD_RIGHT, 1)]])

    def test_default_is_hat(self):
        r, w = os.pipe()
        os.set_blocking(r, False)
        pad = UinputPad(SPECS["xbox360"], fd=w)
        pad.set_button(pad.button_id("UP"), True)
        pad.flush()
        self.assertEqual(frames(drain(r)), [[(C.EV_ABS, C.ABS_HAT0Y, -1)]])

    def test_factory_validates_mode(self):
        from gamepad_remapd.output import create_output
        with self.assertRaises(ValueError):
            create_output("xbox360", dpad_mode="nope")
