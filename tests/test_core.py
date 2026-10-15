"""Юніт-тести ядра: парсер розкладок, пайплайн, криві, профілі, sniffer, uinput, hidraw."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from gamepad_remapd import constants as C
from gamepad_remapd.config import BUILTIN_LAYOUTS, BUILTIN_PROFILES
from gamepad_remapd.input.base import DeviceGone, DeviceInfo
from gamepad_remapd.input.hidraw_linux import HidrawDevice, enumerate_hidraw
from gamepad_remapd.logic.curves import CurveError, build_curve_lut, lut_eval, validate_curve
from gamepad_remapd.logic.layout import DeviceLayout, LayoutError, ReportParser
from gamepad_remapd.logic.pipeline import Pipeline
from gamepad_remapd.logic.profile import (ProfileError, compile_profile, merge_patch,
                                          normalize_profile)
from gamepad_remapd.logic.sniffer import HexSniffer
from gamepad_remapd.util import parse_hex, parse_id

from .fakes import ds4_report, drain, frames, make_pad

DS4 = DeviceLayout.from_file(BUILTIN_LAYOUTS / "sony_ds4_usb.json")


def new_pipe(profile=None, layout=DS4, kind="xbox360"):
    out, r = make_pad(kind)
    p = Pipeline(out)
    p.cp = compile_profile(profile or {"name": "t"}, layout, ReportParser(layout), out)
    return p, out, r


def codes(frame):
    return {(t, c): v for t, c, v in frame}


class ParserTests(unittest.TestCase):
    def test_ds4_buttons_hat_sticks(self):
        pr = ReportParser(DS4)
        word, vals = pr.parse(ds4_report(cross=True, l1=True, hat=2, lx=10, l2=200))
        on = {n for n, b in pr.name_to_bit.items() if word >> b & 1}
        self.assertEqual(on, {"CROSS", "L1", "DPAD_RIGHT"})
        self.assertEqual(vals[pr.index["LX"]], 10)
        self.assertEqual(vals[pr.index["L2"]], 200)

    def test_hat_diagonal_and_neutral(self):
        pr = ReportParser(DS4)
        w, _ = pr.parse(ds4_report(hat=1))                       # вгору+вправо
        names = {n for n, b in pr.name_to_bit.items() if w >> b & 1}
        self.assertEqual(names, {"DPAD_UP", "DPAD_RIGHT"})
        w, _ = pr.parse(ds4_report(hat=8))
        self.assertFalse(any(w >> b & 1 for n, b in pr.name_to_bit.items() if n.startswith("DPAD")))

    def test_layout_validation(self):
        with self.assertRaises(LayoutError):
            DeviceLayout.from_dict({"buttons": {"A": {"byte": 0}}}, "x")
        with self.assertRaises(LayoutError):
            DeviceLayout.from_dict({"fields": {"LX": {"offset": 1}}, "sticks": {"left": ["LX", "NOPE"]}}, "x")

    def test_unlimited_rollover_64_buttons(self):
        """64 кнопок у 8 байтах: усі одночасно -> 64 одиниці, всі доходять до виходу."""
        lay = DeviceLayout.from_dict({
            "buttons": {f"K{i}": {"byte": i // 8, "bit": i % 8, "target": None} for i in range(64)},
        }, "many")
        pr = ReportParser(lay)
        word, _ = pr.parse(bytes([0xFF] * 8))
        self.assertEqual(bin(word).count("1"), 64)

    def test_all_extras_rollover_through_pipeline(self):
        """Усі 11 стандартних + 4 D-pad + 8 додаткових кнопок одночасно натиснуті."""
        spec = {"buttons": {}}
        targets = list(C.STANDARD_BUTTONS + C.EXTRA_BUTTONS)
        for i, t in enumerate(targets):
            spec["buttons"][f"IN{i}"] = {"byte": 1 + i // 8, "bit": i % 8, "target": t}
        lay = DeviceLayout.from_dict(spec, "all")
        p, out, r = new_pipe(layout=lay)
        raw = bytes([0] + [0xFF] * 3)
        p.process(raw)
        pressed = {c for t, c, v in drain(r) if t == C.EV_KEY and v == 1}
        self.assertEqual(len(pressed), len(targets))


class PipelineTests(unittest.TestCase):
    def test_button_press_release_single_syn_frame(self):
        p, out, r = new_pipe()
        p.process(ds4_report(cross=True))
        fr = frames(drain(r))
        self.assertEqual(len(fr), 1, "один звіт = один кадр = один SYN_REPORT")
        self.assertEqual(codes(fr[0])[(C.EV_KEY, C.BTN_SOUTH)], 1)
        p.process(ds4_report(cross=False))
        self.assertEqual(codes(frames(drain(r))[0])[(C.EV_KEY, C.BTN_SOUTH)], 0)

    def test_no_events_when_nothing_changes(self):
        p, out, r = new_pipe()
        p.process(ds4_report())
        drain(r)
        p.process(ds4_report())
        self.assertEqual(drain(r), [])

    def test_dpad_to_hat(self):
        p, out, r = new_pipe()
        p.process(ds4_report(hat=1))      # вгору+вправо
        ev = codes(frames(drain(r))[0])
        self.assertEqual(ev[(C.EV_ABS, C.ABS_HAT0X)], 1)
        self.assertEqual(ev[(C.EV_ABS, C.ABS_HAT0Y)], -1)

    def test_remap_and_refcount_two_sources_one_target(self):
        prof = {"name": "t", "button_map": {"CROSS": "B", "CIRCLE": "B"}}
        p, out, r = new_pipe(prof)
        p.process(ds4_report(cross=True, circle=True))
        self.assertEqual(codes(frames(drain(r))[0])[(C.EV_KEY, C.BTN_EAST)], 1)
        p.process(ds4_report(cross=True, circle=False))     # одна ще тримається
        self.assertEqual(drain(r), [], "ціль має лишатись натиснутою, доки є хоча б одне джерело")
        p.process(ds4_report())
        self.assertEqual(codes(frames(drain(r))[0])[(C.EV_KEY, C.BTN_EAST)], 0)

    def test_one_source_many_targets_and_disable(self):
        p, out, r = new_pipe({"name": "t", "button_map": {"CROSS": ["A", "M1"], "CIRCLE": "NONE"}})
        p.process(ds4_report(cross=True, circle=True))
        ev = codes(frames(drain(r))[0])
        self.assertEqual(ev[(C.EV_KEY, C.BTN_SOUTH)], 1)
        self.assertEqual(ev[(C.EV_KEY, C.BTN_TRIGGER_HAPPY1)], 1)
        self.assertNotIn((C.EV_KEY, C.BTN_EAST), ev)

    def test_digital_target_lt(self):
        p, out, r = new_pipe({"name": "t", "button_map": {"L1": "LT"}})
        p.process(ds4_report(l1=True))
        self.assertEqual(codes(frames(drain(r))[0])[(C.EV_ABS, C.ABS_Z)], 255)
        p.process(ds4_report(l1=False))
        self.assertEqual(codes(frames(drain(r))[0])[(C.EV_ABS, C.ABS_Z)], 0)

    def test_inner_deadzone_and_full_deflection(self):
        prof = {"name": "t", "sticks": {"left": {"inner_deadzone": 0.2, "outer_deadzone": 1.0}}}
        p, out, r = new_pipe(prof)
        p.process(ds4_report(lx=128 + 12))              # ≈ 9% — у мертвій зоні
        self.assertEqual(drain(r), [])
        p.process(ds4_report(lx=255))
        v = codes(frames(drain(r))[0])[(C.EV_ABS, C.ABS_X)]
        self.assertGreater(v, 31000)
        p.process(ds4_report(lx=0))
        self.assertLess(codes(frames(drain(r))[0])[(C.EV_ABS, C.ABS_X)], -31000)

    def test_radial_deadzone_clamps_to_unit_circle(self):
        p, out, r = new_pipe({"name": "t", "sticks": {"left": {"inner_deadzone": 0.0, "outer_deadzone": 1.0}}})
        p.process(ds4_report(lx=255, ly=255))           # «квадратний» кут -> на коло радіуса 1
        ev = codes(frames(drain(r))[0])
        x, y = ev[(C.EV_ABS, C.ABS_X)], ev[(C.EV_ABS, C.ABS_Y)]
        self.assertAlmostEqual((x * x + y * y) ** 0.5, 32767, delta=40)

    def test_trigger_curve_and_deadzone(self):
        prof = {"name": "t", "triggers": {"right": {"inner_deadzone": 0.1, "outer_deadzone": 0.9}}}
        p, out, r = new_pipe(prof)
        p.process(ds4_report(r2=10))
        self.assertEqual(drain(r), [])
        p.process(ds4_report(r2=240))
        self.assertEqual(codes(frames(drain(r))[0])[(C.EV_ABS, C.ABS_RZ)], 255)

    def test_hot_swap_profile_between_reports(self):
        p, out, r = new_pipe()
        p.process(ds4_report(cross=True))
        drain(r)
        new = compile_profile({"name": "n", "button_map": {"CROSS": "Y"}}, DS4, p.cp.parser, out)
        p.cp = new                                       # атомарна підміна, як у демоні
        p.process(ds4_report(cross=True))
        ev = codes(frames(drain(r))[-1])
        self.assertEqual(ev[(C.EV_KEY, C.BTN_WEST)], 1, "після підміни утримувана кнопка йде в нову ціль")

    def test_short_or_foreign_report_ignored(self):
        p, out, r = new_pipe()
        p.process(b"\x01\x00")
        p.process(b"\x05" + bytes(63))                   # інший Report ID
        self.assertEqual(p.dropped, 2)
        self.assertEqual(drain(r), [])

    def test_gyro_activation(self):
        prof = {"name": "g", "gyro": {"enabled": True, "activation": "L2_BTN", "deadzone_dps": 1,
                                      "full_dps": 100}}
        p, out, r = new_pipe(prof)
        p.process(ds4_report(gyro_y=2000))               # без активації — нічого
        self.assertEqual(drain(r), [])
        p.process(ds4_report(gyro_y=2000, l2_btn=True))  # 2000*0.061 = 122 °/с > full_dps
        ev = codes(frames(drain(r))[0])
        self.assertEqual(abs(ev[(C.EV_ABS, C.ABS_RX)]), 32767)

    def test_ds4_output_rescales_to_0_255(self):
        p, out, r = new_pipe(kind="ds4", profile={"name": "t", "sticks": {"left": {"inner_deadzone": 0.0}}})
        p.process(ds4_report(lx=255))
        v = codes(frames(drain(r))[0])[(C.EV_ABS, C.ABS_X)]
        self.assertGreaterEqual(v, 254)

    def test_stats_recorded(self):
        p, out, r = new_pipe()
        for _ in range(100):
            p.process(ds4_report(lx=200))
        self.assertEqual(p.stats()["count"], 100)

    def test_all_builtin_profiles_compile(self):
        for f in BUILTIN_PROFILES.glob("*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            out, r = make_pad()
            cp = compile_profile(data, DS4, ReportParser(DS4), out)
            self.assertEqual(cp.name, f.stem)


class ProfileTests(unittest.TestCase):
    def test_curve_validation(self):
        for bad in ({"type": "nope"}, {"type": "power", "exponent": 99}, {"type": "expo", "k": 0},
                    {"type": "points", "points": [[0, 0]]}, {"type": "points", "points": [[0, 0], [0, 1]]},
                    {"type": "smooth", "strength": 2}):
            with self.assertRaises(CurveError, msg=str(bad)):
                validate_curve(bad)

    def test_curve_lut_monotonic_endpoints(self):
        for spec in ({"type": "power", "exponent": 2}, {"type": "smooth", "strength": 1},
                     {"type": "expo", "k": 4}, {"type": "points", "points": [[0, 0], [0.5, 0.2], [1, 1]]}):
            lut = build_curve_lut(spec)
            self.assertAlmostEqual(lut_eval(lut, 0.0), 0.0, places=6)
            self.assertAlmostEqual(lut_eval(lut, 1.0), 1.0, places=6)
            self.assertTrue(all(b >= a - 1e-9 for a, b in zip(lut, lut[1:])))

    def test_normalize_errors_name_the_field(self):
        for bad, frag in (({"sticks": {"left": {"inner_deadzone": 2}}}, "inner_deadzone"),
                          ({"sticks": {"left": {"inner_deadzone": .6, "outer_deadzone": .6}}}, "outer"),
                          ({"button_map": {"A": "BOGUS"}}, "BOGUS"), ({"zzz": 1}, "zzz"),
                          ({"name": "../evil"}, "name"), ({"gyro": {"bias": [1]}}, "bias")):
            with self.assertRaises(ProfileError) as cm:
                normalize_profile(bad)
            self.assertIn(frag, str(cm.exception))

    def test_unknown_source_button_rejected_at_compile(self):
        out, _ = make_pad()
        with self.assertRaises(ProfileError):
            compile_profile({"button_map": {"NOPE": "A"}}, DS4, ReportParser(DS4), out)

    def test_profile_device_mismatch(self):
        out, _ = make_pad()
        with self.assertRaises(ProfileError):
            compile_profile({"device": "other"}, DS4, ReportParser(DS4), out)

    def test_merge_patch_rfc7386(self):
        t = {"a": {"b": 1, "c": 2}, "d": 3}
        self.assertEqual(merge_patch(t, {"a": {"b": None, "x": 5}, "d": 4}), {"a": {"c": 2, "x": 5}, "d": 4})
        self.assertEqual(t["a"]["b"], 1, "оригінал не мутується")


class SnifferTests(unittest.TestCase):
    def _stream(self, sn, n, mutate):
        for i in range(n):
            b = bytearray(64)
            b[0] = 1
            b[10] = i & 0xFF                      # лічильник — шум
            mutate(b, i)
            sn.feed(bytes(b))

    def test_finds_hidden_button_and_ignores_noise(self):
        sn = HexSniffer(baseline_s=0.0, numbered=True)
        self._stream(sn, 30, lambda b, i: None)               # baseline (+ перехід у live)
        self._stream(sn, 30, lambda b, i: None)
        self._stream(sn, 10, lambda b, i: b.__setitem__(12, 0x08))   # «M1»: байт 12, біт 3
        self._stream(sn, 10, lambda b, i: None)
        s = sn.summary()
        cands = [(c["byte"], c["bit"]) for c in s["candidates"]]
        self.assertIn((12, 3), cands)
        self.assertFalse(any(c[0] == 10 for c in cands), "байт-лічильник не має бути кнопкою")

    def test_learn(self):
        sn = HexSniffer(baseline_s=0.0, numbered=True)
        self._stream(sn, 40, lambda b, i: None)
        sn.learn("M2")
        self._stream(sn, 6, lambda b, i: b.__setitem__(13, 0x40))
        self.assertEqual(sn.summary()["learned"]["M2"]["byte"], 13)
        self.assertEqual(sn.summary()["layout_snippet"]["M2"], {"byte": 13, "bit": 6, "target": None})


class UinputTests(unittest.TestCase):
    def test_flush_is_one_write_with_trailing_syn(self):
        out, r = make_pad()
        out.set_button(out.button_id("A"), True)
        out.set_axis(out.axis_id("LX"), 1234)
        out.flush()
        ev = drain(r)
        self.assertEqual(ev[-1][:2], (C.EV_SYN, C.SYN_REPORT))
        self.assertEqual(len(frames(ev)), 1)

    def test_release_all_neutralizes(self):
        out, r = make_pad()
        out.set_button(out.button_id("A"), True)
        out.set_button(out.button_id("UP"), True)
        out.set_axis(out.axis_id("RT"), 200)
        out.flush()
        drain(r)
        out.release_all()
        ev = codes(frames(drain(r))[0])
        self.assertEqual(ev[(C.EV_KEY, C.BTN_SOUTH)], 0)
        self.assertEqual(ev[(C.EV_ABS, C.ABS_HAT0Y)], 0)
        self.assertEqual(ev[(C.EV_ABS, C.ABS_RZ)], 0)

    def test_rumble_gain_and_effect_mixing(self):
        out, r = make_pad()
        got = []
        out.on_rumble = lambda s, w: got.append((s, w))
        out._effects[1] = (0xFFFF, 0x8000, 0, 0)
        out.handle_ff_event(C.EV_FF, 1, 1)                    # play effect 1
        self.assertEqual(got[-1], (0xFFFF, 0x8000))
        out.handle_ff_event(C.EV_FF, C.FF_GAIN, 0x8000)       # gain 50%
        # gain 0x8000 з 0xFFFF ≈ 50%: strong*gain//0xFFFF == 0x8000 (повний 0xFFFF * 0x8000 // 0xFFFF)
        self.assertEqual(got[-1], (0x8000, 0x4000))
        out.handle_ff_event(C.EV_FF, 1, 0)                    # stop
        self.assertEqual(got[-1], (0, 0))


class HidrawTests(unittest.TestCase):
    def _dev(self):
        r, w = os.pipe()
        d = HidrawDevice(DeviceInfo("/dev/null", 1, 2, backend="hidraw"))
        import select
        d._fd = r
        d._wake_r, d._wake_w = os.pipe()
        d._poll = select.poll()
        d._poll.register(r, select.POLLIN | select.POLLERR | select.POLLHUP)
        d._poll.register(d._wake_r, select.POLLIN)
        return d, w

    def test_read_returns_report_and_wake_interrupts(self):
        d, w = self._dev()
        os.write(w, b"\x01\x02\x03")
        self.assertEqual(d.read(), b"\x01\x02\x03")
        d.wake()
        self.assertIsNone(d.read())

    def test_hangup_raises_device_gone(self):
        d, w = self._dev()
        os.close(w)
        with self.assertRaises(DeviceGone):
            d.read()

    def test_enumerate_from_fake_sysfs(self):
        with tempfile.TemporaryDirectory() as t:
            dev = Path(t) / "hidraw3" / "device"
            dev.mkdir(parents=True)
            (dev / "uevent").write_text("DRIVER=sony\nHID_ID=0003:0000054C:000005C4\nHID_NAME=Wireless Controller\n")
            infos = enumerate_hidraw(t)
        self.assertEqual(len(infos), 1)
        self.assertEqual((infos[0].vid, infos[0].pid, infos[0].path, infos[0].bus),
                         (0x054C, 0x05C4, "/dev/hidraw3", 3))


class UtilTests(unittest.TestCase):
    def test_hex(self):
        self.assertEqual(parse_hex("05 07 0x00,ff"), b"\x05\x07\x00\xff")
        with self.assertRaises(ValueError):
            parse_hex("zz")
        self.assertEqual(parse_id("045e"), 0x045E)
        self.assertEqual(parse_id(0x45E), 0x45E)


if __name__ == "__main__":
    unittest.main()
