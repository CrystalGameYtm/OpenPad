"""Triggers: sensitivity/inversion, analog/digital/off modes, stages (hair-trigger), button_value."""
import os
import unittest
from pathlib import Path

from gamepad_remapd import constants as C
from gamepad_remapd.logic.layout import DeviceLayout, ReportParser
from gamepad_remapd.logic.pipeline import Pipeline
from gamepad_remapd.logic.profile import ProfileError, compile_profile, normalize_profile
from gamepad_remapd.output.uinput_linux import SPECS, UinputPad

from tests.fakes import drain, ds4_report

LAYOUTS = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"
KEY_F = 33


def rig(profile):
    lay = DeviceLayout.from_file(LAYOUTS / "sony_ds4_usb.json")
    r, w = os.pipe()
    kr, kw = os.pipe()
    for fd in (r, kr):
        os.set_blocking(fd, False)
    pad = UinputPad(SPECS["xbox360"], fd=w, kbd_fd=kw)
    cp = compile_profile(dict(profile, name="t"), lay, ReportParser(lay), pad)
    p = Pipeline(pad)
    p.cp = cp
    return p, cp, pad, r, kr


def feed(p, r, **kw):
    p.process(ds4_report(**kw))
    return drain(r)


def lt_of(ev):
    v = [x for t, c, x in ev if t == C.EV_ABS and c == C.ABS_Z]
    return v[-1] if v else None


class Schema(unittest.TestCase):
    def test_defaults(self):
        t = normalize_profile({"name": "a"})["triggers"]["left"]
        self.assertEqual((t["mode"], t["sensitivity"], t["invert"], t["button_value"], t["stages"]),
                         ("analog", 1.0, False, 1.0, {}))

    def test_bad(self):
        for bad in ({"mode": "x"}, {"sensitivity": 9}, {"stages": {"half": {}}},
                    {"stages": {"soft": {"at": 0.8, "targets": ["A"]}, "full": {"at": 0.5, "targets": ["B"]}}},
                    {"stages": {"soft": {"at": 0.5, "targets": ["NOPE"]}}}):
            with self.assertRaises(ProfileError, msg=bad):
                normalize_profile({"name": "a", "triggers": {"left": bad}})

    def test_empty_stage_dropped(self):
        t = normalize_profile({"name": "a", "triggers": {"left": {"stages": {"soft": {"targets": []}}}}})
        self.assertEqual(t["triggers"]["left"]["stages"], {})


class Analog(unittest.TestCase):
    def test_sensitivity_and_invert(self):
        p, _, _, r, _ = rig({"triggers": {"left": {"sensitivity": 2.0}}})
        self.assertAlmostEqual(lt_of(feed(p, r, l2=60)), 120, delta=2)
        p, _, _, r, _ = rig({"triggers": {"left": {"sensitivity": 2.0}}})
        self.assertEqual(lt_of(feed(p, r, l2=200)), 255)
        p, _, _, r, _ = rig({"triggers": {"left": {"invert": True}}})
        self.assertAlmostEqual(lt_of(feed(p, r, l2=55)), 200, delta=2)


class Modes(unittest.TestCase):
    def test_digital(self):
        p, _, _, r, _ = rig({"triggers": {"left": {"mode": "digital", "threshold": 0.4}}})
        self.assertIsNone(lt_of(feed(p, r, l2=50)))          # 0 -> 0: no event
        self.assertEqual(lt_of(feed(p, r, l2=120)), 255)
        self.assertEqual(lt_of(feed(p, r, l2=40)), 0)

    def test_off(self):
        p, _, _, r, _ = rig({"triggers": {"left": {"mode": "off"}}})
        self.assertIsNone(lt_of(feed(p, r, l2=255)))


class Stages(unittest.TestCase):
    def test_two_stage_keys(self):
        p, cp, _, r, kr = rig({"triggers": {"right": {"stages": {
            "soft": {"at": 0.3, "targets": ["KEY_F"]},
            "full": {"at": 0.9, "targets": ["KEY_SPACE"]}}}}})
        feed(p, r, r2=10)
        self.assertEqual(drain(kr), [])
        feed(p, r, r2=100)                                   # ~39% -> soft
        ev = [(c, v) for t, c, v in drain(kr) if t == C.EV_KEY]
        self.assertEqual(ev, [(KEY_F, 1)])
        feed(p, r, r2=250)                                   # full
        ev = [(c, v) for t, c, v in drain(kr) if t == C.EV_KEY]
        self.assertEqual(ev, [(57, 1)])
        feed(p, r, r2=0)
        ev = sorted((c, v) for t, c, v in drain(kr) if t == C.EV_KEY)
        self.assertEqual(ev, [(KEY_F, 0), (57, 0)])

    def test_hysteresis(self):
        p, _, _, r, kr = rig({"triggers": {"right": {"stages": {"soft": {"at": 0.5, "targets": ["KEY_F"]}}}}})
        feed(p, r, r2=130)
        drain(kr)
        feed(p, r, r2=124)                                   
        self.assertEqual([x for x in drain(kr) if x[0] == C.EV_KEY], [])
        feed(p, r, r2=100)
        self.assertEqual([(c, v) for t, c, v in drain(kr) if t == C.EV_KEY], [(KEY_F, 0)])

    def test_stage_button_only_mode_off(self):
        p, _, _, r, _ = rig({"triggers": {"left": {"mode": "off", "stages": {
            "full": {"at": 0.5, "targets": ["A"]}}}}})
        ev = feed(p, r, l2=200)
        self.assertIsNone(lt_of(ev))
        self.assertIn((C.EV_KEY, C.BTN_SOUTH, 1), ev)

    def test_swap_does_not_move_stage(self):
        p, _, _, r, kr = rig({"swap_triggers": True, "triggers": {"left": {"stages": {
            "soft": {"at": 0.3, "targets": ["KEY_F"]}}}}})
        feed(p, r, l2=200)
        self.assertEqual([(c, v) for t, c, v in drain(kr) if t == C.EV_KEY], [(KEY_F, 1)])

    def test_profile_swap_releases(self):
        p, cp, pad, r, kr = rig({"triggers": {"right": {"stages": {"soft": {"at": 0.3, "targets": ["KEY_F"]}}}}})
        feed(p, r, r2=200)
        drain(kr)
        p.cp = compile_profile({"name": "u"}, cp.layout, cp.parser, pad)
        feed(p, r, r2=200)
        self.assertIn((KEY_F, 0), [(c, v) for t, c, v in drain(kr) if t == C.EV_KEY])


class ButtonValue(unittest.TestCase):
    def test_button_to_analog_value(self):
        p, _, _, r, _ = rig({"button_map": {"L1": "LT"}, "triggers": {"left": {"button_value": 0.5}}})
        self.assertAlmostEqual(lt_of(feed(p, r, l1=True)), 128, delta=1)
        self.assertEqual(lt_of(feed(p, r, l1=False)), 0)

    def test_physical_above_value_wins(self):
        p, _, _, r, _ = rig({"button_map": {"L1": "LT"}, "triggers": {"left": {"button_value": 0.5}}})
        self.assertEqual(lt_of(feed(p, r, l1=True, l2=240)), 240)


if __name__ == "__main__":
    unittest.main()
