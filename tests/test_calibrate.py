import unittest

from gamepad_remapd.logic.calibrate import Calibrator, default_plan

from . import calib_sim as sim


def _expected_buttons(spec):
    return {k: v for k, v in spec["buttons"].items()}


class WizardOnKnownFormats(unittest.TestCase):
    def check(self, name, absent=()):
        spec = sim.ALL[name]
        cal = sim.run_wizard(spec, absent=absent)
        self.assertTrue(cal.finished, (name, cal.status()))
        got = cal.build()
        for key, (byte, bit) in spec["buttons"].items():
            if key in absent:
                continue
            if key.startswith("PAD_") and spec.get("hat"):
                continue
            self.assertIn(key, got["buttons"], (name, key, cal.status()["done"]))
            self.assertEqual((got["buttons"][key]["byte"], got["buttons"][key]["bit"]), (byte, bit), (name, key))
        extra = set(got["buttons"]) - set(spec["buttons"])
        self.assertFalse(extra, (name, extra))
        for key, a in spec["axes"].items():
            f = got["fields"].get(key)
            self.assertIsNotNone(f, (name, key, cal.status()["done"]))
            self.assertEqual((f["offset"], f["fmt"].lstrip("us") if False else f["fmt"][1:]), (a["offset"], a["fmt"][1:]), (name, key))
            if not a.get("trigger"):
                self.assertEqual(bool(f.get("invert")), bool(a.get("inverted")), (name, key))
        if spec.get("hat"):
            self.assertEqual((got["hat"]["byte"], got["hat"]["shift"]), (spec["hat"]["byte"], spec["hat"]["shift"]))
        self.assertEqual(got["sticks"], {"left": ["LX", "LY"], "right": ["RX", "RY"]})
        self.assertEqual(got["triggers"], {"left": "L2", "right": "R2"})
        self.assertEqual(cal.verify(), [], name)
        cal.layout()                                   # завантажується як DeviceLayout
        return cal, got

    def test_xbox360(self):
        self.check("xbox360", absent=("M1", "M2", "M3", "M4", "C", "Z"))

    def test_ds4_usb_noisy_gyro_and_counter(self):
        self.check("ds4_usb", absent=("M1", "M2", "M3", "M4", "C", "Z"))

    def test_flydigi_with_extras(self):
        _, got = self.check("flydigi_v1")
        self.assertEqual(got["report_id"], 4)

    def test_sync_learned_from_alien_reports(self):
        """Відповіді handshake (той самий Report ID, інший другий байт) у спокої -> sync."""
        spec = sim.FLYDIGI_V1
        pad = sim.SimPad(spec)
        cal = Calibrator(0x04b4, 0x2412, "Flydigi", 2)
        t = 0.0
        for n in range(900):
            t += sim.DT
            f = bytearray(pad.frame())
            if n % 20 == 3:
                f[1] = 0x00                    # службовий звіт
            cal.feed(bytes(f), t)
        self.assertEqual(cal.sync, (1, 0xFE))
        self.assertEqual(cal.build()["sync"], {"byte": 1, "value": "fe"})

    def test_generic_16bit_be(self):
        _, got = self.check("generic_be16", absent=("M1", "M2", "M3", "M4", "C", "Z"))
        self.assertEqual(got["buttons"]["ML"], {"byte": 2, "bit": 5, "target": "ML"})   # бокові «весла»
        self.assertEqual(got["buttons"]["MR"], {"byte": 2, "bit": 6, "target": "MR"})


class WizardUnderTilt(unittest.TestCase):
    def test_buttons_survive_accelerometer_drift(self):
        """Реальний збій: натискання нахиляє геймпад, байти акселерометра плавно змінюються й «тримаються»
        кілька звітів — раніше це давало «змінилось 9 бітів» або кнопка, прив'язана до акселерометра."""
        cal = sim.run_wizard(sim.DS4_USB, absent=("M1", "M2", "M3", "M4", "C", "Z", "ML", "MR"))
        self.assertTrue(cal.finished)
        got = cal.build()["buttons"]
        for key, (byte, bit) in sim.DS4_USB["buttons"].items():
            self.assertEqual((got[key]["byte"], got[key]["bit"]), (byte, bit), key)
        self.assertNotIn("Змінилось", " ".join(d["result"] for d in cal.done_items))


class WizardBehaviour(unittest.TestCase):
    def test_two_buttons_at_once_asks_to_repeat(self):
        spec = sim.DS4_USB
        pad = sim.SimPad(spec)
        cal = Calibrator(1, 2, plan=default_plan()[:2])
        t = 0.0
        for _ in range(900):
            t += sim.DT
            cal.feed(pad.frame(), t)
        self.assertEqual(cal.current.key, "A")
        pad.pressed = {"A", "B"}
        for _ in range(sim.PRESS_FRAMES):
            t += sim.DT
            cal.feed(pad.frame(), t)
        self.assertIn("кілька", cal.message)
        self.assertNotIn("A", cal.buttons)
        pad.pressed = set()
        for _ in range(sim.PRESS_FRAMES):
            t += sim.DT
            cal.feed(pad.frame(), t)
        self.assertEqual(cal.current.key, "A")            # той самий крок, не пропущено

    def test_skip_everything_gives_empty_but_valid_layout(self):
        spec = sim.XBOX360_USB
        cal = sim.run_wizard(spec, absent={i.key for i in default_plan()})
        self.assertTrue(cal.finished)
        self.assertTrue(cal.warnings)
        cal.layout()

    def test_handshake_is_stored(self):
        hs = [{"op": "output", "data": "05 ec 00 00"}]
        cal = sim.run_wizard(sim.FLYDIGI_V1, cal=Calibrator(0x04b4, 0x2412, "Flydigi", 2, handshake=hs))
        self.assertEqual(cal.build()["handshake"], hs)
        self.assertEqual(cal.build()["match"], {"vid": "04b4", "pid": "2412", "interface": 2})


if __name__ == "__main__":
    unittest.main()
