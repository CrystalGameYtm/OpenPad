"""Switch Pro: розкладки 0x30/0x3F, вибір режиму протоколу, handshake за шиною, гіроскоп -> миша."""
import asyncio
import os
import struct
import tempfile
import unittest
from pathlib import Path

from gamepad_remapd import constants as C
from gamepad_remapd.config import BUILTIN_PROFILES, DaemonConfig
from gamepad_remapd.core.manager import Manager, ManagerError
from gamepad_remapd.input.base import DeviceInfo, run_handshake
from gamepad_remapd.logic.layout import DeviceLayout, ReportParser, load_layouts
from gamepad_remapd.logic.pipeline import Pipeline
from gamepad_remapd.logic.profile import compile_profile
from gamepad_remapd.output.uinput_linux import SPECS, UinputPad
from gamepad_remapd.store import ProfileStore

from tests.fakes import FakeDevice, drain

LAYOUTS = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"
REL = 0x02


def full_report(buttons=(0, 0, 0), lx=2048, ly=2048, gyro=(0, 0, 0)) -> bytes:
    b = bytearray(64)
    b[0] = 0x30
    b[3], b[4], b[5] = buttons
    b[6] = lx & 0xFF
    b[7] = ((lx >> 8) & 0x0F) | ((ly & 0x0F) << 4)
    b[8] = ly >> 4
    b[9], b[10], b[11] = 0x00, 0x08, 0x80            # правий стік у центрі (2048)
    struct.pack_into("<hhh", b, 19, *gyro)
    return bytes(b)


def info(bus=3):
    return DeviceInfo(path="/dev/hidraw9", vid=0x057E, pid=0x2009, name="Pro Controller", interface=0,
                      bus=bus, backend="hidraw")


class Layouts(unittest.TestCase):
    def setUp(self):
        self.lay = DeviceLayout.from_file(LAYOUTS / "nintendo_switch_pro.json")
        self.par = ReportParser(self.lay)

    def test_buttons_and_sticks_12bit(self):
        word, vals = self.par.parse(full_report(buttons=(0x08, 0x02, 0x02), lx=3000, ly=1000))
        names = {n for n, i in self.par.name_to_bit.items() if word >> i & 1}
        self.assertEqual(names, {"A", "PLUS", "PAD_UP"})
        self.assertEqual(vals[self.par.index["LX"]], 3000)
        self.assertEqual(vals[self.par.index["LY"]], 1000)
        self.assertEqual(vals[self.par.index["RX"]], 2048)

    def test_gyro_fields(self):
        _, vals = self.par.parse(full_report(gyro=(100, -200, 300)))
        self.assertEqual([vals[self.par.index[n]] for n in ("GYRO_X", "GYRO_Y", "GYRO_Z")], [100, -200, 300])
        self.assertTrue(self.lay.gyro)

    def test_simple_layout_hat(self):
        lay = DeviceLayout.from_file(LAYOUTS / "nintendo_switch_pro_simple.json")
        par = ReportParser(lay)
        raw = bytearray(12)
        raw[0], raw[1], raw[3] = 0x3F, 0x02, 2            # A + hat «вправо»
        word, _ = par.parse(bytes(raw))
        names = {n for n, i in par.name_to_bit.items() if word >> i & 1}
        self.assertEqual(names, {"A", "DPAD_RIGHT"})
        self.assertFalse(lay.gyro)

    def test_handshake_bus_filter(self):
        lay = self.lay
        for bus, usb_steps in ((3, True), (5, False)):
            dev = FakeDevice(info(bus))
            run_handshake(dev, lay.handshake)
            firsts = [o[:2] for o in dev.outputs]
            self.assertEqual(b"\x80\x02" in firsts, usb_steps)
            self.assertTrue(any(o[0] == 1 and o[10] == 0x03 and o[11] == 0x30 for o in dev.outputs))   # режим 0x30
            self.assertTrue(any(o[0] == 1 and o[10] == 0x40 and o[11] == 1 for o in dev.outputs))      # IMU вкл


class Modes(unittest.TestCase):
    def mgr(self, td):
        cfg = DaemonConfig()
        cfg.config_dir = Path(td)
        layouts = load_layouts(LAYOUTS)
        return Manager(cfg, ProfileStore(Path(td) / "p", BUILTIN_PROFILES, Path(td) / "s.json"), layouts,
                       enumerate_fn=lambda b: [])

    def test_default_and_choice(self):
        with tempfile.TemporaryDirectory() as td:
            m = self.mgr(td)
            i = info()
            self.assertEqual(m.pick_layout(i).id, "nintendo_switch_pro")
            modes = m.modes_for(i, m.pick_layout(i))
            self.assertEqual([x["mode"] for x in modes], ["full", "simple"])
            self.assertTrue(modes[0]["current"] and modes[0]["gyro"] and not modes[1]["gyro"])
            m.store.set_mode("057e:2009", "nintendo_switch_pro_simple")
            self.assertEqual(m.pick_layout(i).id, "nintendo_switch_pro_simple")
            self.assertTrue(m.modes_for(i, m.pick_layout(i))[1]["current"])

    def test_no_modes_for_ordinary_pad(self):
        with tempfile.TemporaryDirectory() as td:
            m = self.mgr(td)
            ds4 = DeviceInfo(path="/dev/hidraw1", vid=0x054C, pid=0x05C4, interface=0, bus=3, backend="hidraw")
            self.assertEqual(m.modes_for(ds4, m.pick_layout(ds4)), [])

    def test_set_mode_validates(self):
        with tempfile.TemporaryDirectory() as td:
            m = self.mgr(td)
            i = info()
            m.known = {"hidraw9": i}
            m.loop = asyncio.new_event_loop()
            try:
                with self.assertRaises(ManagerError):
                    m.loop.run_until_complete(m.set_mode("hidraw9", "sony_ds4_usb"))
                m.loop.run_until_complete(m.set_mode("hidraw9", "nintendo_switch_pro_simple"))
                self.assertEqual(m.store.get_mode("057e:2009"), "nintendo_switch_pro_simple")
            finally:
                m.loop.close()


class GyroMouse(unittest.TestCase):
    def rig(self, **gyro):
        lay = DeviceLayout.from_file(LAYOUTS / "nintendo_switch_pro.json")
        r, w = os.pipe()
        kr, kw = os.pipe()
        for fd in (r, kr):
            os.set_blocking(fd, False)
        pad = UinputPad(SPECS["xbox360"], fd=w, kbd_fd=kw)
        g = {"enabled": True, "target": "mouse", "mouse_sens": 10.0, "deadzone_dps": 1.0}
        g.update(gyro)
        cp = compile_profile({"name": "t", "gyro": g}, lay, ReportParser(lay), pad)
        p = Pipeline(pad)
        p.cp = cp
        return p, cp, kr

    def test_mouse_moves_and_accumulates(self):
        p, cp, kr = self.rig()
        gm = cp.gyro
        self.assertTrue(gm.to_mouse)
        t = 1_000_000_000
        self.assertEqual(gm.mouse(cp.parser.parse(full_report())[1], 0, t), (0, 0))       # перший кадр: dt = 0
        yaw = round(100 / 0.07) + 1                                                           # ~100 °/с
        v = cp.parser.parse(full_report(gyro=(0, 0, yaw)))[1]
        dx, dy = gm.mouse(v, 0, t + 16_000_000)                                           # 16 мс
        self.assertEqual((dx, dy), (16, 0))                                               # 100*10*0.016
        # дрібні рухи накопичуються
        v2 = cp.parser.parse(full_report(gyro=(0, 0, int(10 / 0.07))))[1]
        total = sum(gm.mouse(v2, 0, t + 16_000_000 + (k + 1) * 10_000_000)[0] for k in range(10))
        self.assertAlmostEqual(total, 10, delta=1)

    def test_deadzone_and_invert_and_pause(self):
        p, cp, kr = self.rig(invert_yaw=True)
        gm = cp.gyro
        v = cp.parser.parse(full_report(gyro=(0, 0, round(50 / 0.07) + 1)))[1]
        gm.mouse(v, 0, 1_000_000_000)
        self.assertEqual(gm.mouse(v, 0, 1_020_000_000)[0], -10)                           # інверсія
        self.assertEqual(gm.mouse(v, 0, 5_000_000_000), (0, 0))                           # довга пауза — без стрибка
        still = cp.parser.parse(full_report(gyro=(0, 0, 5)))[1]                           # < мертвої зони
        self.assertEqual(gm.mouse(still, 0, 5_020_000_000), (0, 0))

    def test_pipeline_emits_rel_events(self):
        p, cp, kr = self.rig()
        yaw = int(200 / 0.07)
        for _ in range(3):
            p.process(full_report(gyro=(0, 0, yaw)))
            import time; time.sleep(0.01)
        ev = drain(kr)
        rel = [e for e in ev if e[0] == REL]
        self.assertTrue(rel and all(e[1] == 0 for e in rel), ev)                          # REL_X

    def test_profile_validates_target(self):
        from gamepad_remapd.logic.profile import ProfileError, normalize_profile
        self.assertEqual(normalize_profile({"gyro": {"target": "mouse"}})["gyro"]["target"], "mouse")
        with self.assertRaises(ProfileError):
            normalize_profile({"gyro": {"target": "foo"}})


if __name__ == "__main__":
    unittest.main()
