"""evdev-бекенд (XInput-режим): перелік із sysfs, синтетичні звіти, розкладка, дедуплікація, автовиявлення."""
from __future__ import annotations

import os
import struct
import tempfile
import time
import unittest
from pathlib import Path

from gamepad_remapd.input import DeviceGone, drop_shadowed_evdev
from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.input.evdev_linux import EvdevDevice, enumerate_evdev, parse_bits
from gamepad_remapd.logic.autolayout import try_layout
from gamepad_remapd.logic.layout import DeviceLayout, ReportParser
from gamepad_remapd.tools import testpage as tpm
from gamepad_remapd.tools.testpage import TestPage

from .fakes import FakeDevice
from .test_autolayout import v1_report

LAYOUTS = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"
EV = struct.Struct("llHHi")
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3

XPAD_KEYS = [0x130, 0x131, 0x133, 0x134, 0x136, 0x137, 0x13A, 0x13B, 0x13C, 0x13D, 0x13E]
XPAD_ABS = {0: (-32768, 32767), 1: (-32768, 32767), 2: (0, 255), 3: (-32768, 32767),
            4: (-32768, 32767), 5: (0, 255), 0x10: (-1, 1), 0x11: (-1, 1)}


def bitmap(bits) -> str:
    """Як у sysfs: слова по 64 біти hex, старше слово першим."""
    top = max(bits, default=0) // 64
    words = [0] * (top + 1)
    for b in bits:
        words[b // 64] |= 1 << (b % 64)
    return " ".join(f"{w:x}" for w in reversed(words))


def make_sysfs(root: Path, name: str, *, vid=0x045E, pid=0x028E, dev_name="Microsoft X-Box 360 pad",
               ev=(0, 1, 3), keys=XPAD_KEYS, abs_codes=tuple(XPAD_ABS), virtual=False) -> None:
    real = root / ("devices/virtual/input" if virtual else "devices/pci/input") / name
    (real / "device/id").mkdir(parents=True)
    (real / "device/capabilities").mkdir(parents=True)
    (real / "device/name").write_text(dev_name + "\n")
    for k, v in (("vendor", vid), ("product", pid), ("bustype", 3)):
        (real / "device/id" / k).write_text(f"{v:04x}\n")
    (real / "device/capabilities/ev").write_text(bitmap(ev) + "\n")
    (real / "device/capabilities/key").write_text(bitmap(keys) + "\n")
    (real / "device/capabilities/abs").write_text(bitmap(abs_codes) + "\n")
    cls = root / "class/input"
    cls.mkdir(parents=True, exist_ok=True)
    os.symlink(real, cls / name)


def fake_abs(node: str, code: int):
    return XPAD_ABS.get(code)


def ev(t, c, v) -> bytes:
    return EV.pack(0, 0, t, c, v)


def xpad_info(path="/dev/input/event5") -> DeviceInfo:
    return DeviceInfo(path=path, vid=0x045E, pid=0x028E, name="Microsoft X-Box 360 pad", backend="evdev",
                      extra={"keys": XPAD_KEYS, "abs": dict(XPAD_ABS), "range_known": True})


class SysfsTests(unittest.TestCase):
    def test_parse_bits_multiword(self):
        self.assertEqual(parse_bits("1 8000000000000001"), {0, 63, 64})
        self.assertEqual(parse_bits(""), set())

    def test_enumerates_gamepad_and_skips_others(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_sysfs(root, "event5")                                                    # xpad
            make_sysfs(root, "event6", dev_name="Mouse", keys=[0x110, 0x111], abs_codes=[])    # миша
            make_sysfs(root, "event7", dev_name="Touchpad", keys=[0x14A, 0x145], abs_codes=[0, 1])
            make_sysfs(root, "event8", dev_name="uinput pad", virtual=True)                # наш власний вихід
            make_sysfs(root, "event9", dev_name="Kbd", ev=(0, 1), keys=[30, 31], abs_codes=[])
            infos = enumerate_evdev(str(root / "class/input"), "/dev/input", read_abs=fake_abs)
        self.assertEqual([i.path for i in infos], ["/dev/input/event5"])
        i = infos[0]
        self.assertEqual((i.vid, i.pid, i.backend, i.interface), (0x045E, 0x028E, "evdev", None))
        self.assertEqual(i.extra["keys"], sorted(XPAD_KEYS))
        self.assertEqual(i.extra["abs"][2], (0, 255))
        self.assertTrue(i.extra["range_known"])

    def test_missing_permissions_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            make_sysfs(Path(td), "event5", dev_name="noperm-unique")
            infos = enumerate_evdev(str(Path(td) / "class/input"), "/dev/input", read_abs=lambda n, c: None)
        self.assertFalse(infos[0].extra["range_known"])


class LayoutAndDeviceTests(unittest.TestCase):
    def test_layout_uses_kernel_codes_not_guesses(self):
        lay = try_layout(xpad_info())
        self.assertTrue(lay.generated)
        t = {n: b.target for n, b in lay.buttons.items()}
        self.assertEqual((t["SOUTH"], t["EAST"], t["NORTH"], t["WEST"]), ("A", "B", "X", "Y"))
        self.assertEqual((t["TL"], t["TR"], t["SELECT"], t["START"], t["MODE"], t["THUMBL"], t["THUMBR"]),
                         ("LB", "RB", "BACK", "START", "GUIDE", "LS", "RS"))
        self.assertEqual(lay.sticks, {"left": ("LX", "LY"), "right": ("RX", "RY")})
        self.assertEqual(lay.triggers, {"left": "L2", "right": "R2"})
        self.assertIsNotNone(lay.hat)
        self.assertEqual(lay.fields["LX"].center, 0)

    def test_digital_trigger_buttons_have_no_target_when_analog_present(self):
        info = xpad_info()
        info.extra["keys"] = XPAD_KEYS + [0x138, 0x139]
        lay = try_layout(info)
        self.assertIsNone(lay.buttons["TL2"].target)

    def test_paddles_map_to_extra_buttons_and_dpad_buttons(self):
        info = xpad_info()
        info.extra["keys"] = XPAD_KEYS + [0x2C0, 0x2C1, 0x220, 0x221, 0x222, 0x223]
        lay = try_layout(info)
        self.assertEqual((lay.buttons["HAPPY1"].target, lay.buttons["HAPPY2"].target), ("M1", "M2"))
        self.assertEqual(lay.buttons["PAD_UP"].target, "UP")

    def _device(self):
        r, w = os.pipe()
        dev = EvdevDevice(xpad_info(), 64, opener=lambda p: r)
        dev.open()
        self.w_open = True

        def cleanup():
            dev.close()
            if self.w_open:
                os.close(w)
        self.addCleanup(cleanup)
        return dev, w

    def test_events_become_synthetic_reports(self):
        dev, w = self._device()
        pr = ReportParser(try_layout(xpad_info()))
        word0, _ = pr.parse(dev.read())                      # початковий стан (нічого не натиснуто)
        self.assertEqual(word0, 0)
        os.write(w, ev(EV_KEY, 0x130, 1) + ev(EV_ABS, 0, 20000) + ev(EV_ABS, 5, 200)
                 + ev(EV_ABS, 0x10, 1) + ev(EV_ABS, 0x11, -1) + ev(EV_SYN, 0, 0))
        raw = dev.read()
        word, vals = pr.parse(raw)
        pressed = {n for i, n in enumerate(pr.src_names) if n and word >> i & 1}
        self.assertEqual(pressed, {"SOUTH", "DPAD_UP", "DPAD_RIGHT"})
        self.assertEqual(vals[pr.index["LX"]], 20000)
        self.assertEqual(vals[pr.index["R2"]], 200)
        os.write(w, ev(EV_KEY, 0x130, 0) + ev(EV_ABS, 0x10, 0) + ev(EV_ABS, 0x11, 0) + ev(EV_SYN, 0, 0))
        word, _ = pr.parse(dev.read())
        self.assertEqual(word, 0)

    def test_several_syn_in_one_read_queue_up(self):
        dev, w = self._device()
        pr = ReportParser(try_layout(xpad_info()))
        dev.read()
        os.write(w, ev(EV_KEY, 0x131, 1) + ev(EV_SYN, 0, 0) + ev(EV_KEY, 0x131, 0) + ev(EV_SYN, 0, 0))
        a, b = pr.parse(dev.read())[0], pr.parse(dev.read())[0]
        self.assertNotEqual(a, 0)
        self.assertEqual(b, 0)

    def test_out_of_range_axis_is_clamped_and_key_repeat_ignored(self):
        dev, w = self._device()
        pr = ReportParser(try_layout(xpad_info()))
        dev.read()
        os.write(w, ev(EV_ABS, 2, 9999) + ev(EV_KEY, 0x130, 2) + ev(EV_SYN, 0, 0))
        word, vals = pr.parse(dev.read())
        self.assertEqual(vals[pr.index["L2"]], 255)
        self.assertEqual(word, 0)

    def test_wake_and_unplug(self):
        dev, w = self._device()
        dev.read()
        dev.wake()
        self.assertIsNone(dev.read())
        os.close(w)                                  # «пристрій зник»: кінець запису закрито
        self.w_open = False
        with self.assertRaises(DeviceGone):
            for _ in range(3):
                dev.read()

    def test_write_output_not_supported(self):
        dev, _ = self._device()
        with self.assertRaises(OSError):
            dev.write_output(b"\x00")


class ShadowAndManagerTests(unittest.TestCase):
    def test_evdev_hidden_when_hidraw_has_layout_else_kept(self):
        hid = DeviceInfo(path="/dev/hidraw10", vid=0x04B4, pid=0x2412, interface=2, backend="hidraw")
        ev_same = DeviceInfo(path="/dev/input/event3", vid=0x04B4, pid=0x2412, backend="evdev", extra={})
        ev_other = xpad_info()
        got = drop_shadowed_evdev([hid, ev_same, ev_other], lambda i: i.backend == "hidraw")
        self.assertEqual([i.path for i in got], ["/dev/hidraw10", "/dev/input/event5"])
        got = drop_shadowed_evdev([hid, ev_same], lambda i: False)          # hidraw без розкладки -> evdev лишається
        self.assertEqual(len(got), 2)

    def test_manager_picks_evdev_layout_and_ignores_hidraw_layouts(self):
        from gamepad_remapd.config import DaemonConfig
        from gamepad_remapd.core.manager import Manager
        from gamepad_remapd.store import ProfileStore
        from gamepad_remapd.config import BUILTIN_PROFILES
        with tempfile.TemporaryDirectory() as td:
            cfg = DaemonConfig()
            cfg.config_dir = Path(td)
            lay = DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")
            m = Manager(cfg, ProfileStore(Path(td) / "p", BUILTIN_PROFILES, Path(td) / "s.json"),
                        {lay.id: lay}, enumerate_fn=lambda b: [])
            # evdev-пристрій з VID/PID Flydigi: збережена hidraw-розкладка (interface=None-збіг) не має підходити
            info = DeviceInfo(path="/dev/input/event9", vid=0x04B4, pid=0x2412, backend="evdev",
                              extra={"keys": XPAD_KEYS, "abs": dict(XPAD_ABS), "range_known": True})
            picked = m.pick_layout(info)
            self.assertTrue(picked.id.startswith("auto_evdev_04b4_2412"))
            self.assertIs(m.pick_layout(info), picked)                      # кеш
            self.assertIn(picked.id, m.all_layouts())
            hid = DeviceInfo(path="/dev/hidraw10", vid=0x04B4, pid=0x2412, interface=2, backend="hidraw")
            m._enumerate = lambda b: [hid, info, xpad_info()]
            self.assertEqual([i.path for i in m.visible_devices()], ["/dev/hidraw10", "/dev/input/event5"])


# ---------------------------------------------------------------------------
# Автовиявлення тестової сторінки
# ---------------------------------------------------------------------------
FLY = DeviceInfo(path="/dev/hidraw10", vid=0x04B4, pid=0x2412, name="Flydigi", interface=2, backend="hidraw")


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class AutoDetectTests(unittest.TestCase):
    def setUp(self):
        self.lay = DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")
        self.present = []
        self.devs = {}
        self.tp = TestPage({self.lay.id: self.lay}, enumerate_fn=lambda b: list(self.present),
                           open_fn=lambda i, s: self.devs.setdefault(i.path, FakeDevice(i)))
        self.addCleanup(self.tp.shutdown)

    def test_finds_opens_and_follows_hotplug(self):
        self.tp.auto_tick()
        self.assertIsNone(self.tp.probe)                         # пристроїв немає
        self.present.append(FLY)
        self.tp.auto_tick()
        self.assertEqual(self.tp.probe.info.path, "/dev/hidraw10")
        self.assertEqual(self.tp.probe.layout.id, "flydigi_v1_2g4")
        self.assertTrue(wait_for(lambda: self.devs["/dev/hidraw10"].outputs))         # handshake виконано
        gen = self.tp.gen
        self.present.clear()                                     # висмикнули
        self.assertTrue(wait_for(lambda: self.tp.probe.status["state"] == "running"))
        self.tp.auto_tick()
        self.assertIsNone(self.tp.probe)
        self.assertGreater(self.tp.gen, gen)
        self.present.append(FLY)                                 # встромили знову -> знайдено сам
        self.devs.clear()
        self.tp.auto_tick()
        self.assertIsNotNone(self.tp.probe)

    def test_manual_open_disables_auto_and_is_not_replaced(self):
        self.present.append(FLY)
        self.tp.open("/dev/hidraw10", None)
        self.assertFalse(self.tp.auto)
        gen = self.tp.gen
        self.tp.auto_tick()
        self.assertEqual(self.tp.gen, gen)
        self.tp.set_auto(True)
        self.assertTrue(self.tp.auto)

    def test_silent_interface_rotates_to_next_candidate(self):
        old = tpm.SILENT_S
        tpm.SILENT_S = 0.15
        self.addCleanup(lambda: setattr(tpm, "SILENT_S", old))
        other = DeviceInfo(path="/dev/hidraw11", vid=0x04B4, pid=0x2412, name="Flydigi", interface=2, backend="hidraw")
        self.present[:] = [FLY, other]
        self.tp.auto_tick()
        self.assertEqual(self.tp.probe.info.path, "/dev/hidraw10")
        self.assertTrue(wait_for(lambda: self.tp.probe.status["state"] == "running"))
        time.sleep(0.25)                                          # перший мовчить
        self.tp.auto_tick()
        self.assertEqual(self.tp.probe.info.path, "/dev/hidraw11")
        time.sleep(0.25)                                          # і другий мовчить
        self.assertTrue(wait_for(lambda: self.tp.probe.status["state"] == "running"))
        self.tp.auto_tick()                                       # усі мовчали: лишаємось на найкращому
        self.assertEqual(self.tp.probe.info.path, "/dev/hidraw10")
        gen = self.tp.gen
        time.sleep(0.25)
        self.tp.auto_tick()
        self.assertEqual(self.tp.gen, gen)                        # більше не стрибає

    def test_open_error_is_kept_then_retried(self):
        def boom(i, s):
            raise PermissionError(13, "Permission denied")
        self.tp.open_fn = boom
        self.present.append(FLY)
        self.tp.auto_tick(now=100.0)
        self.assertTrue(wait_for(lambda: self.tp.probe.status["state"] == "error"))
        gen = self.tp.gen
        self.tp.auto_tick(now=101.0)                              # повідомлення лишається на екрані
        self.assertEqual(self.tp.gen, gen)
        self.assertIn("немає прав", self.tp.probe.status["message"])
        self.tp.open_fn = lambda i, s: FakeDevice(i)              # права з'явились
        self.tp.auto_tick(now=106.0)
        self.assertGreater(self.tp.gen, gen)
        self.assertTrue(wait_for(lambda: self.tp.probe.status["state"] == "running"))

    def test_xinput_mode_pad_via_evdev_is_autodetected_end_to_end(self):
        """Геймпад у XInput-режимі: hidraw немає, лишається evdev -> знайдено й прочитано без налаштувань."""
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        info = xpad_info()
        self.present.append(info)
        def open_evdev(i, size):                       # як input.open_device(): створити й відкрити
            d = EvdevDevice(i, size, opener=lambda p: r)
            d.open()
            return d
        self.tp.open_fn = open_evdev
        self.tp.auto_tick()
        self.assertEqual(self.tp.probe.layout.id, "auto_evdev_045e_028e")
        self.assertTrue(wait_for(lambda: self.tp.probe.snap["n"] >= 1))          # початковий стан
        os.write(w, ev(EV_KEY, 0x133, 1) + ev(EV_ABS, 1, -30000) + ev(EV_SYN, 0, 0))
        self.assertTrue(wait_for(lambda: "NORTH" in self.tp.probe.snap["buttons"]))
        self.assertEqual(self.tp.probe.snap["axes"]["LY"], -30000)
        d = self.tp.probe.describe()
        self.assertEqual({b["name"]: b["target"] for b in d["buttons"]}["NORTH"], "X")


class XInputModeEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Повний демон: геймпад у XInput-режимі видно лише як evdev -> сам відкривається й мапиться
    на віртуальний пад; а Flydigi (hidraw із розкладкою + evdev того ж VID/PID) — одним сеансом."""

    async def test_xinput_pad_reaches_virtual_pad_and_no_duplicates(self):
        from gamepad_remapd import constants as C
        from .fakes import wait_frames
        from .test_integration import Rig, kv

        pipes = {}

        class EvRig(Rig):
            def _open(self, info, read_size):
                if info.backend != "evdev":
                    return super()._open(info, read_size)
                r, w = os.pipe()
                pipes[info.path] = w
                d = EvdevDevice(info, read_size, opener=lambda p: r)
                d.open()
                self.devices[info.path] = d
                return d

        with tempfile.TemporaryDirectory() as td:
            rig = EvRig(td)
            await rig.start()
            try:
                fly_hid = DeviceInfo(path="/dev/hidraw10", vid=0x04B4, pid=0x2412, interface=2, backend="hidraw")
                fly_ev = DeviceInfo(path="/dev/input/event3", vid=0x04B4, pid=0x2412, backend="evdev",
                                    extra={"keys": XPAD_KEYS, "abs": dict(XPAD_ABS), "range_known": True})
                rig.present += [fly_hid, fly_ev]
                await rig.wait_for(lambda: "hidraw10" in rig.mgr.sessions)
                await rig.mgr.scan()
                self.assertEqual(sorted(rig.mgr.sessions), ["hidraw10"])          # evdev-«дубль» прихований
                rig.present.clear()
                rig.devices["/dev/hidraw10"].unplug()                             # як при реальному відключенні
                await rig.wait_for(lambda: not rig.mgr.sessions)
                rig.present.append(xpad_info())                                   # XInput-режим: лише evdev
                await rig.wait_for(lambda: "event5" in rig.mgr.sessions)
                self.assertEqual(rig.mgr.sessions["event5"].layout.id, "auto_evdev_045e_028e")
                os.write(pipes["/dev/input/event5"], ev(EV_KEY, 0x130, 1) + ev(EV_SYN, 0, 0))
                out = kv(wait_frames(rig.r, 1)[0])
                self.assertEqual(out[(C.EV_KEY, C.BTN_SOUTH)], 1)
            finally:
                for w in pipes.values():
                    os.close(w)
                await rig.stop()


if __name__ == "__main__":
    unittest.main()
