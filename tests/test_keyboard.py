"""Клавіатура як геймпад: keymap, пакувальник, пристрій (захоплення/перемикач), менеджер, наскрізно."""
import asyncio
import os
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

from gamepad_remapd import constants as C
from gamepad_remapd.config import (BUILTIN_KEYMAPS, BUILTIN_PROFILES, ConfigError, DaemonConfig, load_config,
                                   parse_keyboard_config)
from gamepad_remapd.core.manager import Manager
from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.input.keyboard_linux import EVIOCGRAB, KeyboardDevice, enumerate_keyboards, is_keyboard
from gamepad_remapd.logic.keyboard import KeyMap, KeyMapError, KeyPacker, build_layout, key_code, load_keymap
from gamepad_remapd.logic.layout import ReportParser
from gamepad_remapd.output.uinput_linux import SPECS, UinputPad
from gamepad_remapd.store import ProfileStore
from tests.fakes import wait_frames

EV = struct.Struct("llHHi")
KEYS = {n: key_code(n) for n in ("W", "A", "S", "D", "SPACE", "Z", "LEFTALT", "LEFTCTRL", "G", "LEFTSHIFT")}


def km(keys=None, **opts):
    return KeyMap.from_dict({"keys": keys or {"W": "LS_UP", "S": "LS_DOWN", "A": "LS_LEFT", "D": "LS_RIGHT",
                                              "SPACE": "A", "Z": "LT", "UP": "UP"},
                             "options": opts})


def decode(pk: KeyPacker):
    lay = build_layout(pk.km, 1, 2)
    return ReportParser(lay), lay


class KeyMapTests(unittest.TestCase):
    def test_key_names(self):
        self.assertEqual(key_code("KEY_W"), key_code("w"))
        self.assertEqual(key_code("left shift"), 42)
        with self.assertRaises(KeyMapError):
            key_code("NOPE")

    def test_validation(self):
        for bad in ({"keys": {}}, {"keys": {"W": "FLY"}}, {"keys": {"W": "A", "KEY_W": "B"}},
                    {"keys": {"W": "A"}, "options": {"walk_scale": 5}}, {"keys": {"W": "A"}, "options": {"circle": 1}}):
            with self.assertRaises(KeyMapError, msg=str(bad)):
                KeyMap.from_dict(bad)

    def test_builtin_default_is_valid_and_builds_layout(self):
        k = load_keymap("default", [BUILTIN_KEYMAPS])
        lay = build_layout(k, 0x1234, 0x5678)
        ReportParser(lay)                                               # без конфліктів бітів
        self.assertIn("PAD_UP", lay.buttons)                            # D-pad не конфліктує з hat-іменами
        self.assertEqual(lay.sticks and set(lay.sticks), {"left", "right"})

    def test_user_keymap_overrides_builtin(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "default.json").write_text('{"keys": {"H": "A"}}', encoding="utf-8")
            k = load_keymap("default", [BUILTIN_KEYMAPS, Path(d)])
        self.assertEqual(list(k.keys.values()), ["A"])

    def test_missing_keymap(self):
        with self.assertRaises(KeyMapError):
            load_keymap("nope", [BUILTIN_KEYMAPS])


class PackerTests(unittest.TestCase):
    def values(self, pk):
        p, lay = decode(pk)
        r = pk.report()
        self.assertEqual(len(r), pk.size)
        return p.parse(r), lay

    def test_idle_is_neutral(self):
        pk = KeyPacker(km())
        (_, vals), _ = self.values(pk)
        self.assertEqual(vals, (0, 0, 0))                              # LX, LY, L2

    def test_wasd_axes_and_y_up_is_negative(self):
        pk = KeyPacker(km())
        pk.press(KEYS["W"])
        (_, v), lay = self.values(pk)
        names = list(lay.fields)
        self.assertEqual((v[names.index("LX")], v[names.index("LY")]), (0, -32767))
        pk.release(KEYS["W"]); pk.press(KEYS["D"])
        (_, v), _ = self.values(pk)
        self.assertEqual((v[names.index("LX")], v[names.index("LY")]), (32767, 0))

    def test_opposite_keys_last_wins(self):
        pk = KeyPacker(km())
        pk.press(KEYS["A"]); pk.press(KEYS["D"])
        (_, v), lay = self.values(pk)
        self.assertEqual(v[list(lay.fields).index("LX")], 32767)
        pk.release(KEYS["D"])
        (_, v), _ = self.values(pk)
        self.assertEqual(v[list(lay.fields).index("LX")], -32767)      # лишилась A

    def test_diagonal_is_normalized_unless_disabled(self):
        for circle, expect in ((True, 23170), (False, 32767)):
            pk = KeyPacker(km(circle=circle))
            pk.press(KEYS["W"]); pk.press(KEYS["D"])
            (_, v), lay = self.values(pk)
            self.assertEqual(abs(v[list(lay.fields).index("LX")]), expect)

    def test_walk_scales_left_stick(self):
        pk = KeyPacker(km(walk_key="LEFTALT", walk_scale=0.5))
        pk.press(KEYS["D"]); pk.press(KEYS["LEFTALT"])
        (_, v), lay = self.values(pk)
        self.assertEqual(v[list(lay.fields).index("LX")], round(32767 * 0.5))
        pk.release(KEYS["LEFTALT"])
        (_, v), _ = self.values(pk)
        self.assertEqual(v[list(lay.fields).index("LX")], 32767)

    def test_buttons_and_trigger(self):
        pk = KeyPacker(km())
        p, lay = decode(pk)
        pk.press(KEYS["SPACE"]); pk.press(KEYS["Z"])
        mask, vals = p.parse(pk.report())
        self.assertTrue(mask & (1 << p.src_names.index("A")))
        self.assertEqual(vals[list(lay.fields).index("L2")], 255)
        self.assertFalse(mask & (1 << p.src_names.index("PAD_UP")))
        pk.release(KEYS["SPACE"]); pk.release(KEYS["Z"])
        mask, vals = p.parse(pk.report())
        self.assertEqual((mask, vals[-1]), (0, 0))

    def test_unmapped_key_changes_nothing(self):
        pk = KeyPacker(km())
        self.assertFalse(pk.press(key_code("F5")))
        self.assertFalse(pk.release(key_code("F5")))


class FakeKb:
    """Пристрій-клавіатура на socketpair: тест пише події «ядра», пристрій їх читає."""

    def __init__(self, n=1, **kw):
        self.pairs = [socket.socketpair() for _ in range(n)]
        for a, _ in self.pairs:
            a.setblocking(False)
        self.fds = iter(os.dup(a.fileno()) for a, _ in self.pairs)
        self.grabs = []
        info = DeviceInfo(path="/dev/input/event9", vid=1, pid=2, name="kb", backend="evdev",
                          extra={"kind": "keyboard", "paths": [f"/dev/input/event{9 + i}" for i in range(n)]})
        self.dev = KeyboardDevice(info, km(), 64, opener=lambda p: next(self.fds), ioctl_fn=self.ioctl, **kw)

    def ioctl(self, fd, req, arg=0, mutate=False):
        if req == EVIOCGRAB:
            self.grabs.append(arg)
        return 0

    def send(self, i, *keys):
        """keys: (код, значення)...; кожен виклик = один SYN_REPORT."""
        data = b"".join(EV.pack(0, 0, 1, c, v) for c, v in keys) + EV.pack(0, 0, 0, 0, 0)
        self.pairs[i][1].send(data)

    def read(self):
        return self.dev.read()

    def close(self):
        self.dev.close()
        for a, b in self.pairs:
            a.close(); b.close()


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.kb = FakeKb()
        self.kb.dev.open()
        self.addCleanup(self.kb.close)
        self.lay = build_layout(self.kb.dev.keymap, 1, 2)
        self.p = ReportParser(self.lay)
        self.read()                                                    # початковий нейтральний звіт

    def read(self):
        return self.kb.read()

    def test_press_release_make_reports(self):
        self.kb.send(0, (KEYS["SPACE"], 1))
        mask, _ = self.p.parse(self.read())
        self.assertTrue(mask & (1 << self.p.src_names.index("A")))
        self.kb.send(0, (KEYS["SPACE"], 0))
        mask, _ = self.p.parse(self.read())
        self.assertEqual(mask, 0)

    def test_autorepeat_ignored_and_unmapped_makes_no_report(self):
        self.kb.send(0, (KEYS["SPACE"], 1))
        self.read()
        self.kb.send(0, (KEYS["SPACE"], 2), (key_code("F5"), 1))      # повтор + невідома клавіша
        self.assertIsNone(self.read())

    def test_grab_only_when_idle_and_toggle_releases_keyboard(self):
        # старт: нічого не натиснуто -> захоплено
        self.assertEqual(self.kb.grabs, [1])
        ctrl, alt, g = KEYS["LEFTCTRL"], KEYS["LEFTALT"], KEYS["G"]
        self.kb.send(0, (ctrl, 1)); self.read()
        self.kb.send(0, (alt, 1)); self.read()
        self.kb.send(0, (g, 1))                                        # chord: режим вимикається
        self.read()
        self.assertFalse(self.kb.dev.active)
        self.assertEqual(self.kb.grabs, [1, 0], "клавіатуру повернуто системі")
        self.kb.send(0, (KEYS["SPACE"], 1))                            # пауза: гра нічого не бачить
        self.assertIsNone(self.read())
        # вмикаємо назад: захоплення відкладено, поки клавіші не відпущено
        for k in (ctrl, alt, g):
            self.kb.send(0, (k, 0)); self.read()
        self.kb.send(0, (KEYS["SPACE"], 0)); self.read()
        self.kb.send(0, (ctrl, 1)); self.read()
        self.kb.send(0, (alt, 1)); self.read()
        self.kb.send(0, (g, 1)); self.read()
        self.assertTrue(self.kb.dev.active)
        self.assertEqual(self.kb.grabs, [1, 0], "ще утримуються клавіші: не захоплюємо")
        for k in (g, alt):
            self.kb.send(0, (k, 0)); self.read()
        self.assertEqual(self.kb.grabs, [1, 0], "Ctrl ще утримується")
        self.kb.send(0, (ctrl, 0)); self.read()
        self.assertEqual(self.kb.grabs, [1, 0, 1])

    def test_toggle_releases_pressed_game_buttons(self):
        self.kb.send(0, (KEYS["D"], 1)); self.read()
        for k in (KEYS["LEFTCTRL"], KEYS["LEFTALT"], KEYS["G"]):
            self.kb.send(0, (k, 1))
            self.read()
        self.assertEqual(self.kb.dev.packer.report()[1:], bytes(self.kb.dev.packer.size - 1))

    def test_close_releases_grab(self):
        self.kb.dev.close()
        self.assertEqual(self.kb.grabs[-1], 0)


class MultiNodeTests(unittest.TestCase):
    def test_two_nodes_one_device(self):
        kb = FakeKb(n=2, grab=False)
        kb.dev.open()
        self.addCleanup(kb.close)
        kb.read()
        kb.send(1, (KEYS["SPACE"], 1))                                  # друга клавіатурна «інтерфейс»
        p = ReportParser(build_layout(kb.dev.keymap, 1, 2))
        mask, _ = p.parse(kb.read())
        self.assertTrue(mask & (1 << p.src_names.index("A")))
        self.assertEqual(kb.grabs, [])


def make_sys(root: Path, name: str, devname: str, vid, pid, keys: set, abs_=set(), phys_ok=True):
    d = root / name / "device"
    (d / "capabilities").mkdir(parents=True)
    (d / "id").mkdir()

    def bits(s):
        words, n = [], (max(s) // 64 + 1) if s else 1
        for w in range(n - 1, -1, -1):
            v = sum(1 << (b - 64 * w) for b in s if 64 * w <= b < 64 * (w + 1))
            words.append("%x" % v)
        return " ".join(words)
    (d / "capabilities" / "ev").write_text("%x" % ((1 << 1) | ((1 << 3) if abs_ else 0)))
    (d / "capabilities" / "key").write_text(bits(keys))
    (d / "capabilities" / "abs").write_text(bits(abs_) if abs_ else "0")
    (d / "id" / "vendor").write_text("%04x" % vid)
    (d / "id" / "product").write_text("%04x" % pid)
    (d / "id" / "bustype").write_text("0003")
    (d / "name").write_text(devname)


class EnumerateTests(unittest.TestCase):
    def test_finds_keyboards_not_gamepads_and_groups_nodes(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            full = {30, 17, 57, 28}
            make_sys(root, "event3", "HATOR kb", 0x379A, 0xA006, full)
            make_sys(root, "event4", "HATOR kb", 0x379A, 0xA006, full)           # другий вузол тієї ж клавіатури
            make_sys(root, "event5", "HATOR mouse", 0x379A, 0xA006, {0x110})      # без літер: не клавіатура
            make_sys(root, "event6", "Pad", 0x045E, 0x028E, full | {0x130, 0x131}, abs_={0, 1})
            make_sys(root, "event7", "Other kb", 0x1111, 0x2222, full)
            allk = enumerate_keyboards([], str(root), "/dev/input")
            self.assertEqual(sorted(i.name for i in allk), ["HATOR kb", "Other kb"])
            hator = next(i for i in allk if i.name == "HATOR kb")
            self.assertEqual(hator.extra["paths"], ["/dev/input/event3", "/dev/input/event4"])
            self.assertTrue(is_keyboard(hator))
            self.assertEqual([i.name for i in enumerate_keyboards(["hator"], str(root), "/dev/input")], ["HATOR kb"])
            self.assertEqual([i.name for i in enumerate_keyboards(["1111:2222"], str(root), "/dev/input")], ["Other kb"])
            self.assertEqual([i.name for i in enumerate_keyboards(["/dev/input/event7"], str(root), "/dev/input")], ["Other kb"])
            self.assertEqual(enumerate_keyboards(["nothing"], str(root), "/dev/input"), [])


class ConfigTests(unittest.TestCase):
    def test_defaults_disabled(self):
        self.assertFalse(DaemonConfig().keyboard.enabled)

    def test_enabled_requires_device(self):
        with self.assertRaises(ConfigError):
            parse_keyboard_config({"enabled": True})
        kb = parse_keyboard_config({"enabled": True, "device": "hator", "toggle": ["LEFTCTRL", "F12"]})
        self.assertEqual(kb.device, ["hator"])

    def test_unknown_key_and_bad_types(self):
        for bad in ({"foo": 1}, {"enabled": "yes"}, {"device": 3}, {"keymap": 5}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                parse_keyboard_config(bad)

    def test_load_config_reads_keyboard_section(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, "daemon.json").write_text('{"keyboard": {"enabled": true, "device": "x", "grab": false}}')
            cfg = load_config(None, Path(t))
        self.assertTrue(cfg.keyboard.enabled)
        self.assertFalse(cfg.keyboard.grab)


class ManagerEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.cfg = DaemonConfig(config_dir=td, poll_interval_s=0.05, realtime=False, grab_siblings=False)
        self.cfg.keyboard.enabled = True
        self.cfg.keyboard.device = ["kb"]
        self.cfg.keyboard.grab = False
        self.info = DeviceInfo(path="/dev/input/event9", vid=0x1234, pid=0x5678, name="kb", backend="evdev",
                               extra={"kind": "keyboard", "paths": ["/dev/input/event9"]})
        self.kb = FakeKb(grab=False)
        self.r = None

        def output(kind, name=None, uinput_path=None):
            r, w = os.pipe()
            os.set_blocking(r, False)
            self.r = r
            self.out_name = name
            return UinputPad(SPECS[kind], fd=w, name=name)
        store = ProfileStore(self.cfg.profiles_dir, BUILTIN_PROFILES, self.cfg.state_path)
        self.mgr = Manager(self.cfg, store, {}, enumerate_fn=lambda b: [],
                           output_fn=output, keyboard_enumerate_fn=lambda sel: [self.info],
                           keyboard_open_fn=lambda info, km_, size, kcfg: self._open(info, km_, size))

    def _open(self, info, km_, size):
        self.kb.dev.info = info
        self.kb.dev.keymap = km_                                       # менеджер дає keymap із конфігурації
        self.kb.dev.packer = KeyPacker(km_)
        self.kb.dev.open()
        return self.kb.dev

    async def asyncTearDown(self):
        await self.mgr.stop()
        self.kb.close()
        self._td.cleanup()

    async def test_keyboard_session_drives_virtual_pad(self):
        await self.mgr.start()
        self.assertIn("event9", self.mgr.sessions)
        d = [x for x in self.mgr.list_devices() if x["id"] == "event9"][0]
        self.assertEqual(d["layout"], "keyboard_1234_5678")
        wait_frames(self.r, 1)                                          # початковий кадр
        self.kb.send(0, (key_code("SPACE"), 1), (key_code("D"), 1))
        evs = {}
        for fr in wait_frames(self.r, 1):
            evs.update({(t, c): v for t, c, v in fr})
        self.assertEqual(evs[(C.EV_KEY, C.BTN_SOUTH)], 1)
        self.assertGreater(evs[(C.EV_ABS, C.ABS_X)], 20000)             # D -> ліве стікове вправо

    async def test_disabled_keyboard_is_never_opened(self):
        self.cfg.keyboard.enabled = False
        await self.mgr.start()
        self.assertEqual(self.mgr.sessions, {})

    async def test_bad_keymap_does_not_crash_daemon(self):
        self.cfg.keyboard.keymap = "missing"
        await self.mgr.start()
        self.assertEqual(self.mgr.sessions, {})


if __name__ == "__main__":
    unittest.main()
