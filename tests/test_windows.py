"""Windows-частина, яку можна перевірити на Linux: SendInput-події, ViGEm-обгортка з підробним vgamepad,
імпорти без fcntl/uinput, hidapi-шина, HidHide-команди."""
import importlib
import io
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.output import sendinput_windows as si
from gamepad_remapd.output.vigem_windows import VigemPad
from gamepad_remapd.tools import hidhide

ROOT = Path(__file__).resolve().parent.parent


class SendInput(unittest.TestCase):
    def test_basic_scancode(self):
        e = si.key_event(33, True)                       # KEY_F
        self.assertEqual((e.type, e.ki.wScan, e.ki.dwFlags), (si.INPUT_KEYBOARD, 33, si.KEYEVENTF_SCANCODE))
        e = si.key_event(33, False)
        self.assertEqual(e.ki.dwFlags, si.KEYEVENTF_SCANCODE | si.KEYEVENTF_KEYUP)

    def test_extended_arrows(self):
        e = si.key_event(103, True)                      # KEY_UP -> 0x48 extended
        self.assertEqual(e.ki.wScan, 0x48)
        self.assertTrue(e.ki.dwFlags & si.KEYEVENTF_EXTENDEDKEY)
        self.assertEqual(si.key_event(97, True).ki.wScan, 0x1D)      # правий Ctrl

    def test_vk_keys(self):
        e = si.key_event(115, True)                      # Volume Up
        self.assertEqual((e.ki.wVk, e.ki.wScan, e.ki.dwFlags), (0xAF, 0, 0))
        self.assertEqual(si.key_event(183, True).ki.wVk, 0x7C)       # F13

    def test_mouse(self):
        e = si.key_event(0x110, True)
        self.assertEqual((e.type, e.mi.dwFlags), (si.INPUT_MOUSE, 0x0002))
        self.assertEqual(si.key_event(0x110, False).mi.dwFlags, 0x0004)
        x = si.key_event(0x114, True)
        self.assertEqual((x.mi.dwFlags, x.mi.mouseData), (0x0080, 2))

    def test_unknown_code(self):
        self.assertIsNone(si.key_event(9999, True))

    def test_batch_and_release_all(self):
        sent = []
        kb = si.WinKeyboard(lambda ev: sent.append(list(ev)) or len(ev))
        kb.set_key(33, True)
        kb.set_key(57, True)
        kb.flush()
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]), 2, "усе в одному SendInput")
        kb.flush()
        self.assertEqual(len(sent), 1, "порожній кадр не шле нічого")
        kb.release_all()
        self.assertEqual([e.ki.dwFlags & si.KEYEVENTF_KEYUP for e in sent[1]], [si.KEYEVENTF_KEYUP] * 2)


class FakeReport:
    wButtons = 0
    sThumbLX = sThumbLY = sThumbRX = sThumbRY = 0
    bLeftTrigger = bRightTrigger = 0


class FakeX360:
    def __init__(self):
        self.report = FakeReport()
        self.updates = 0
        self.callback = None

    def update(self): self.updates += 1
    def reset(self): self.report = FakeReport()
    def register_notification(self, callback_function): self.callback = callback_function


class Vigem(unittest.TestCase):
    def setUp(self):
        self.fake = FakeX360()
        mod = types.ModuleType("vgamepad")
        mod.VX360Gamepad = lambda: self.fake
        self.patch = mock.patch.dict(sys.modules, {"vgamepad": mod})
        self.patch.start()
        self.sent = []
        self.pad = VigemPad(send_fn=lambda ev: self.sent.append(list(ev)) or len(ev))
        self.pad.open()

    def tearDown(self):
        self.patch.stop()

    def test_buttons_axes_and_flush(self):
        self.pad.set_button(self.pad.button_id("A"), True)
        self.pad.set_axis(self.pad.axis_id("LY"), -1000)       # вгору (мінус) -> XInput плюс
        self.pad.flush()
        self.assertEqual(self.fake.report.wButtons, 0x1000)
        self.assertEqual(self.fake.report.sThumbLY, 1000)
        self.assertEqual(self.fake.updates, 1)
        self.pad.flush()
        self.assertEqual(self.fake.updates, 1, "без змін — без IOCTL")

    def test_rumble_callback(self):
        got = []
        self.pad.on_rumble = lambda s, w: got.append((s, w))
        self.fake.callback(None, None, 255, 0, 0, None)
        self.assertEqual(got, [(65535, 0)])

    def test_keys(self):
        self.assertTrue(self.pad.supports_keys)
        self.pad.ensure_keys()
        self.pad.set_key(33, True)
        self.pad.flush()
        self.assertEqual(len(self.sent), 1)
        self.pad.release_all()
        self.assertEqual(len(self.sent), 2)                     # клавішу відпущено
        self.assertIsNone(self.pad.button_id("M1"), "XInput не має M-кнопок")


class Dry(unittest.TestCase):
    def test_pipeline_runs_on_dry_pad(self):
        """check/bench на Windows: профіль із KEY_-ціллю компілюється й проходить Pipeline без uinput."""
        from gamepad_remapd.logic.layout import DeviceLayout, ReportParser
        from gamepad_remapd.logic.pipeline import Pipeline
        from gamepad_remapd.logic.profile import compile_profile
        from gamepad_remapd.output.dry import DryPad
        from tests.fakes import ds4_report
        lay = DeviceLayout.from_file(ROOT / "src" / "gamepad_remapd" / "layouts" / "sony_ds4_usb.json")
        pad = DryPad()
        p = Pipeline(pad)
        p.cp = compile_profile({"name": "t", "button_map": {"CROSS": ["A", "KEY_F"]}}, lay, ReportParser(lay), pad)
        p.process(ds4_report(cross=True))
        self.assertGreaterEqual(pad.events, 2)                          # принаймні A і KEY_F


class NoLinuxModules(unittest.TestCase):
    def test_core_imports_without_fcntl_and_uinput(self):
        """Ядро має імпортуватись там, де нема fcntl/termios (Windows): uinput/evdev/hidraw — лише ліниво."""
        code = (
            "import sys\n"
            "for m in ('fcntl','termios'): sys.modules[m] = None\n"
            "import gamepad_remapd.cli, gamepad_remapd.daemon, gamepad_remapd.core.manager, gamepad_remapd.core.session\n"
            "import gamepad_remapd.logic.profile, gamepad_remapd.output.vigem_windows, gamepad_remapd.output.dry\n"
            "import gamepad_remapd.tools.bench, gamepad_remapd.input.hidapi_backend\n"
            "print('ok')\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                           env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""})
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)


class Hidapi(unittest.TestCase):
    def test_bus_and_interface(self):
        fake = types.ModuleType("hid")
        fake.enumerate = lambda a, b: [
            {"path": b"\\\\?\\HID#VID_054C&PID_09CC#7&2a#{x}", "vendor_id": 0x054C, "product_id": 0x09CC,
             "product_string": "Wireless Controller", "interface_number": -1, "bus_type": 2,
             "usage_page": 1, "usage": 5}]
        with mock.patch.dict(sys.modules, {"hid": fake}):
            from gamepad_remapd.input.hidapi_backend import enumerate_hidapi
            (d,) = enumerate_hidapi()
        self.assertEqual((d.bus, d.interface, d.backend), (5, None, "hidapi"))
        self.assertEqual(d.extra["usage"], 5)


class HidHide(unittest.TestCase):
    def test_instance_path(self):
        p = "\\\\?\\HID#VID_054C&PID_09CC#7&2a1b3c4d&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
        self.assertEqual(hidhide.instance_path(p), "HID\\VID_054C&PID_09CC\\7&2a1b3c4d&0&0000")

    def test_commands_and_run(self):
        calls, out = [], io.StringIO()
        rc = hidhide.run(0x054C, 0x09CC, ["\\\\?\\HID#VID_054C&PID_09CC#7&aa&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"],
                         execute=True, python_exe="C:\\Python\\python.exe", runner=lambda c: calls.append(c) or 0, out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["HidHideCLI", "--app-reg", "C:\\Python\\python.exe"])
        self.assertEqual(calls[1], ["HidHideCLI", "--dev-hide", "HID\\VID_054C&PID_09CC\\7&aa&0&0000"])
        self.assertEqual(calls[-1], ["HidHideCLI", "--cloak-on"])

    def test_dry_run_prints_only(self):
        calls, out = [], io.StringIO()
        hidhide.run(1, 2, ["\\\\?\\HID#VID_0001&PID_0002#x#{4d1e55b2-f16f-11cf-88cb-001111000030}"],
                    runner=lambda c: calls.append(c) or 0, out=out)
        self.assertEqual(calls, [])
        self.assertIn("--cloak-on", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class MissingHidapi(unittest.TestCase):
    def test_friendly_error_and_daemon_survives(self):
        from gamepad_remapd.input import BackendUnavailable
        from gamepad_remapd.input.hidapi_backend import enumerate_hidapi
        with mock.patch.dict(sys.modules, {"hid": None}):          # `import hid` -> ImportError
            with self.assertRaises(BackendUnavailable) as cm:
                enumerate_hidapi()
        self.assertIn("pip install hidapi", str(cm.exception))

    def test_manager_logs_once_and_returns_empty(self):
        from gamepad_remapd.config import BUILTIN_PROFILES, DaemonConfig
        from gamepad_remapd.core.manager import Manager
        from gamepad_remapd.input import BackendUnavailable
        from gamepad_remapd.store import ProfileStore
        import tempfile
        calls = []

        def boom(backend):
            calls.append(1)
            raise BackendUnavailable("нема hid")
        with tempfile.TemporaryDirectory() as d:
            cfg = DaemonConfig(config_dir=Path(d))
            m = Manager(cfg, ProfileStore(Path(d) / "p", BUILTIN_PROFILES, Path(d) / "s.json"), {}, enumerate_fn=boom)
            with self.assertLogs("gamepad_remapd.core.manager", level="ERROR") as lg:
                self.assertEqual(m.visible_devices(), [])
                self.assertEqual(m.visible_devices(), [])
        self.assertEqual(len(lg.records), 1, "причину логуємо один раз")
