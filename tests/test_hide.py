import os
import stat
import tempfile
import unittest
from pathlib import Path

from gamepad_remapd.input import hide_linux as H


class FakeSys:
    """Мінімальний sysfs + /dev у tmp: USB-геймпад (hidraw+event+js), клавіатурний інтерфейс, BT-пристрої."""

    def __init__(self, td: str):
        self.sys, self.dev = Path(td, "sys"), Path(td, "dev")
        (self.dev / "input").mkdir(parents=True)
        self.usb = self.sys / "devices/pci0/usb1/1-2"
        self.usb.mkdir(parents=True)
        (self.usb / "idVendor").write_text("04b4")
        # інтерфейс 0: геймпадний event + js ; інтерфейс 2: hidraw ; інтерфейс 1: «клавіатура»
        self.event(self.usb / "1-2:1.0/0003:04B4:2412.0003", 5, key="0", rel="0", js=0)
        self.event(self.usb / "1-2:1.1/0003:04B4:2412.0004", 6, key=hex(1 << 30)[2:], rel="0")
        self.hidraw(self.usb / "1-2:1.2/0003:04B4:2412.0005", 8)
        # Bluetooth: адаптер на 1-10, під ним два HID-пристрої
        self.bt = self.sys / "devices/pci0/usb1/1-10"
        self.bt.mkdir(parents=True)
        (self.bt / "idVendor").write_text("8087")
        self.hidraw(self.bt / "bluetooth/hci0/hci0:11/0005:054C:09CC.0007", 7)
        self.hidraw(self.bt / "bluetooth/hci0/hci0:12/0005:057E:2009.0008", 9)

    def _node(self, p: Path):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        os.chmod(p, 0o660)

    def hidraw(self, hid: Path, n: int):
        d = hid / "hidraw" / f"hidraw{n}"
        d.mkdir(parents=True)
        (self.sys / "class/hidraw").mkdir(parents=True, exist_ok=True)
        os.symlink(d, self.sys / f"class/hidraw/hidraw{n}")
        os.symlink(hid, d / "device")
        self._node(self.dev / f"hidraw{n}")

    def event(self, hid: Path, n: int, key: str, rel: str, js=None):
        inp = hid / "input" / "input1"
        ev = inp / f"event{n}"
        ev.mkdir(parents=True)
        (inp / "capabilities").mkdir(exist_ok=True)
        (inp / "capabilities/key").write_text(key)
        (inp / "capabilities/rel").write_text(rel)
        os.symlink(inp, ev / "device")
        (self.sys / "class/input").mkdir(parents=True, exist_ok=True)
        os.symlink(ev, self.sys / f"class/input/event{n}")
        self._node(self.dev / f"input/event{n}")
        if js is not None:
            (inp / f"js{js}").mkdir()
            self._node(self.dev / f"input/js{js}")


class CollectTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.f = FakeSys(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def nodes(self, path, backend):
        return [os.path.relpath(n, self.f.dev) for n in H.collect_nodes(path, backend, str(self.f.sys), str(self.f.dev))]

    def test_usb_all_gamepad_nodes_but_not_keyboard(self):
        self.assertEqual(self.nodes("/dev/hidraw8", "hidraw"), ["hidraw8", "input/event5", "input/js0"])

    def test_evdev_start_finds_same_device(self):
        self.assertEqual(self.nodes("/dev/input/event5", "evdev"), ["hidraw8", "input/event5", "input/js0"])

    def test_bluetooth_does_not_touch_other_bt_devices(self):
        self.assertEqual(self.nodes("/dev/hidraw7", "hidraw"), ["hidraw7"])
        self.assertEqual(self.nodes("/dev/hidraw9", "hidraw"), ["hidraw9"])

    def test_unknown_device(self):
        self.assertEqual(self.nodes("/dev/hidraw99", "hidraw"), [])


class HiderTests(unittest.TestCase):
    def setUp(self):
        H._REG.clear()
        self._td = tempfile.TemporaryDirectory()
        self.f = FakeSys(self._td.name)
        self.state = Path(self._td.name, "run/hidden.json")

    def tearDown(self):
        H._REG.clear()
        self._td.cleanup()

    def hider(self, **kw):
        return H.Hider(str(self.f.sys), str(self.f.dev), self.state, **kw)

    def mode(self, rel):
        return stat.S_IMODE(os.stat(self.f.dev / rel).st_mode)

    def test_hide_and_restore(self):
        h = self.hider()
        r = h.apply("/dev/hidraw8", "hidraw")
        self.assertEqual(r.mode, "hidden")
        self.assertEqual({self.mode(n) for n in ("hidraw8", "input/event5", "input/js0")}, {0o600})
        self.assertEqual(self.mode("hidraw7"), 0o660)                 # чужий пристрій не чіпаємо
        self.assertEqual(self.mode("input/event6"), 0o660)            # клавіатурний інтерфейс не чіпаємо
        self.assertTrue(self.state.exists())
        h.release()
        self.assertEqual({self.mode(n) for n in ("hidraw8", "input/event5", "input/js0")}, {0o660})
        self.assertFalse(self.state.exists())

    def test_two_sessions_refcount(self):
        a, b = self.hider(), self.hider()
        a.apply("/dev/hidraw8", "hidraw")
        b.apply("/dev/input/event5", "evdev")
        a.release()
        self.assertEqual(self.mode("hidraw8"), 0o600)                 # друга сесія ще тримає
        b.release()
        self.assertEqual(self.mode("hidraw8"), 0o660)

    def test_denied_changes_nothing(self):
        def deny(path, mode):
            raise PermissionError(1, "Operation not permitted")
        r = self.hider(chmod=deny).apply("/dev/hidraw8", "hidraw")
        self.assertEqual(r.mode, "denied")
        self.assertEqual(len(r.failed), 3)
        self.assertEqual(self.mode("hidraw8"), 0o660)
        self.assertFalse(H._REG)

    def test_partial(self):
        real = os.chmod

        def flaky(path, mode):
            if path.endswith("js0"):
                raise PermissionError(1, "Operation not permitted")
            real(path, mode)
        r = self.hider(chmod=flaky).apply("/dev/hidraw8", "hidraw")
        self.assertEqual(r.mode, "partial")

    def test_none(self):
        self.assertEqual(self.hider().apply("/dev/hidraw99", "hidraw").mode, "none")

    def test_restore_stale_after_crash(self):
        self.hider().apply("/dev/hidraw8", "hidraw")
        saved = dict(H._REG)
        H._REG.clear()                                                # «процес помер»: реєстру нема, файл лишився
        self.assertTrue(self.state.exists())
        done, bad = H.restore_stale(self.state)
        self.assertEqual(len(done), 3)
        self.assertEqual(bad, [])
        self.assertEqual(self.mode("hidraw8"), 0o660)
        self.assertFalse(self.state.exists())
        self.assertEqual(len(saved), 3)

    def test_restore_stale_skips_changed_nodes(self):
        self.hider().apply("/dev/hidraw8", "hidraw")
        H._REG.clear()
        os.chmod(self.f.dev / "hidraw8", 0o664)                       # хтось (udev) уже поставив нові права
        done, _ = H.restore_stale(self.state)
        self.assertNotIn(str(self.f.dev / "hidraw8"), done)
        self.assertEqual(self.mode("hidraw8"), 0o664)


class SdlListTests(unittest.TestCase):
    def test_output(self):
        import io
        from gamepad_remapd.tools import sdl_list

        class Lib:
            def __getattr__(self, n):
                return lambda *a, **k: None
        lib = Lib()
        lib.SDL_Init = lambda f: True
        lib.SDL_PumpEvents = lambda: None
        lib.SDL_Quit = lambda: None
        import ctypes
        arr = (ctypes.c_uint32 * 1)(7)

        def get_joysticks(ref):
            ref._obj.value = 1
            return arr
        lib.SDL_GetJoysticks = get_joysticks
        lib.SDL_GetJoystickNameForID = lambda i: b"Pad"
        lib.SDL_GetJoystickPathForID = lambda i: b"/dev/input/event9"
        lib.SDL_GetJoystickVendorForID = lambda i: 0x45e
        lib.SDL_GetJoystickProductForID = lambda i: 0x28e
        lib.SDL_IsGamepad = lambda i: True
        out = io.StringIO()
        self.assertEqual(sdl_list.run(lib, out, sleep=lambda s: None), 0)
        self.assertIn("045e:028e", out.getvalue())
        self.assertIn("геймпад", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class ReleaseGone(unittest.TestCase):
    def test_missing_node_on_release_is_silent(self):
        H._REG.clear()
        with tempfile.TemporaryDirectory() as td:
            f = FakeSys(td)
            h = H.Hider(str(f.sys), str(f.dev), None)
            h.apply("/dev/hidraw8", "hidraw")
            os.unlink(f.dev / "hidraw8")                       # пристрій від'єднали
            with self.assertNoLogs(H.log, level="WARNING"):
                h.release()
        H._REG.clear()
