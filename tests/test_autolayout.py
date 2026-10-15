"""Тести етапу 1.5: розбір HID Report Descriptor та автогенерація розкладки.

Дескриптори тут — синтетичні, складені вручну за специфікацією HID 1.11. «DS4-подібний»
повторює СТРУКТУРУ звіту DualShock 4 (Report ID 1, осі X/Y/Z/Rz, hat, 14 кнопок, 6 біт
лічильника, Rx/Ry, 54 вендорські байти), але не є байт-у-байт копією справжнього
дескриптора: перевірте на своєму пристрої `gamepad-remapd autolayout /dev/hidrawN`.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from gamepad_remapd import constants as C
from gamepad_remapd.config import BUILTIN_LAYOUTS, DaemonConfig
from gamepad_remapd.core.manager import Manager
from gamepad_remapd.input.base import DeviceInfo, run_handshake
from gamepad_remapd.input.hidraw_linux import enumerate_hidraw
from gamepad_remapd.logic.autolayout import AutoLayoutError, build_layout_dict, layout_from_descriptor
from gamepad_remapd.logic.hid_descriptor import (DescriptorError, extract_field, format_fields,
                                                 parse_report_descriptor)
from gamepad_remapd.logic.layout import DeviceLayout, ReportParser
from gamepad_remapd.logic.pipeline import Pipeline
from gamepad_remapd.logic.profile import compile_profile
from gamepad_remapd.store import ProfileStore

from .fakes import FakeDevice, drain, ds4_info, ds4_report, frames, make_pad


def H(*parts: str) -> bytes:
    """hex-рядки -> байти (пробіли ігноруються)."""
    return bytes.fromhex("".join(parts).replace(" ", ""))


# DualShock 4-подібний: Report ID 1, 64 байти.
DS4_LIKE = H(
    "05 01 09 05 A1 01 85 01",
    "09 30 09 31 09 32 09 35 15 00 26 FF 00 75 08 95 04 81 02",          # X Y Z Rz -> байти 1..4
    "09 39 15 00 25 07 35 00 46 3B 01 65 14 75 04 95 01 81 42",          # hat (нібл) -> байт 5
    "65 00 05 09 19 01 29 0E 15 00 25 01 75 01 95 0E 81 02",            # 14 кнопок
    "06 00 FF 09 20 75 06 95 01 81 02",                                  # вендорський лічильник 6 біт
    "05 01 09 33 09 34 15 00 26 FF 00 75 08 95 02 81 02",               # Rx Ry -> байти 8,9
    "06 00 FF 09 21 95 36 81 02",                                        # 54 вендорські байти
    "C0")

# Без Report ID: X Y Z Rz, hat, 12 кнопок (6 байт).
PLAIN = H(
    "05 01 09 05 A1 01",
    "09 30 09 31 09 32 09 35 15 00 26 FF 00 75 08 95 04 81 02",
    "09 39 15 00 25 07 75 04 95 01 81 42",
    "05 09 19 01 29 0C 15 00 25 01 75 01 95 0C 81 02",
    "C0")

# Joystick зі знаковими 16-бітними осями та 4 кнопками + набивка.
SIGNED16 = H(
    "05 01 09 04 A1 01 09 30 09 31 16 00 80 26 FF 7F 75 10 95 02 81 02",
    "05 09 19 01 29 04 15 00 25 01 75 01 95 04 81 02 75 04 95 01 81 03 C0")

# Миша (не геймпад).
MOUSE = H("05 01 09 02 A1 01 09 01 A1 00 05 09 19 01 29 03 15 00 25 01 95 03 75 01 81 02",
          "95 01 75 05 81 03 05 01 09 30 09 31 15 81 25 7F 75 08 95 02 81 06 C0 C0")

# Лише вендорська collection.
VENDOR_ONLY = H("06 00 FF 09 01 A1 01 09 02 15 00 26 FF 00 75 08 95 20 81 02 C0")


class DescriptorParserTests(unittest.TestCase):
    def test_ds4_like_offsets(self):
        d = parse_report_descriptor(DS4_LIKE)
        self.assertTrue(d.uses_report_ids)
        self.assertEqual(d.applications, [(0x01, 0x05)])
        self.assertEqual(d.input_bits, {1: 504})
        self.assertEqual(d.input_report_len(1), 64)
        by_name = {}
        for f in d.inputs:
            by_name.setdefault(f.name, f)
        self.assertEqual((by_name["X"].bit_offset, by_name["Z"].bit_offset, by_name["Rz"].bit_offset), (0, 16, 24))
        self.assertEqual((by_name["Hat"].bit_offset, by_name["Hat"].bit_size), (32, 4))
        self.assertEqual(by_name["Button1"].bit_offset, 36)
        self.assertEqual(by_name["Rx"].bit_offset, 56)
        self.assertEqual(sum(1 for f in d.inputs if f.usage_page == 0x09), 14)

    def test_report_ids_have_independent_offsets(self):
        d = parse_report_descriptor(H(
            "05 01 09 05 A1 01 85 01 09 30 09 31 15 00 26 FF 00 75 08 95 02 81 02",
            "85 02 05 09 19 01 29 08 15 00 25 01 75 01 95 08 81 02 C0"))
        self.assertEqual(d.input_bits, {1: 16, 2: 8})
        self.assertEqual(d.input_report_len(1), 3)
        self.assertEqual([f.bit_offset for f in d.inputs if f.report_id == 2], list(range(8)))

    def test_push_pop_restores_globals(self):
        d = parse_report_descriptor(H(
            "05 01 09 05 A1 01 75 08 95 01 15 00 26 FF 00 A4 75 01 95 08 B4 09 30 81 02 C0"))
        self.assertEqual([(f.bit_size, f.lmax) for f in d.inputs], [(8, 255)])

    def test_usage_range_and_repeated_usage(self):
        d = parse_report_descriptor(H(
            "05 09 19 01 29 03 15 00 25 01 75 01 95 05 81 02",     # діапазон 1..3, а полів 5
            "05 01 09 30 75 08 95 02 81 02"))                       # один usage на 2 поля
        self.assertEqual([f.usage for f in d.inputs if f.usage_page == 0x09], [1, 2, 3, 3, 3])
        self.assertEqual([f.usage for f in d.inputs if f.usage_page == 0x01], [0x30, 0x30])

    def test_constant_padding_advances_offset(self):
        d = parse_report_descriptor(H("05 09 19 01 29 02 15 00 25 01 75 01 95 02 81 02",
                                      "75 03 95 01 81 03", "09 30 75 08 95 01 81 02"))
        self.assertEqual([f.bit_offset for f in d.inputs], [0, 1, 5])

    def test_logical_max_unsigned_when_min_nonnegative(self):
        d = parse_report_descriptor(H("05 01 09 30 15 00 25 FF 75 08 95 01 81 02"))
        self.assertEqual((d.inputs[0].lmin, d.inputs[0].lmax, d.inputs[0].signed), (0, 255, False))
        d = parse_report_descriptor(H("05 01 09 30 15 81 25 7F 75 08 95 01 81 02"))
        self.assertEqual((d.inputs[0].lmin, d.inputs[0].lmax, d.inputs[0].signed), (-127, 127, True))

    def test_long_item_skipped(self):
        d = parse_report_descriptor(H("FE 02 10 AA BB", "05 01 09 30 15 00 25 FF 75 08 95 01 81 02"))
        self.assertEqual(len(d.inputs), 1)

    def test_broken_descriptors_raise(self):
        for bad in (H("05"), H("B4"), H("C0"), H("05 01 09 30 15 00 25 FF 75 08 95 FF 81"),
                    H("85 00")):
            with self.assertRaises(DescriptorError, msg=bad.hex()):
                parse_report_descriptor(bad)

    def test_extract_field_signed16(self):
        d = parse_report_descriptor(SIGNED16)
        x = d.inputs[0]
        self.assertEqual(extract_field(b"\x00\x80\xff\x7f\x00", x, False), -32768)
        self.assertEqual(extract_field(b"\x00\x80\xff\x7f\x00", d.inputs[1], False), 32767)

    def test_format_fields_smoke(self):
        txt = format_fields(parse_report_descriptor(DS4_LIKE))
        self.assertIn("Hat", txt)
        self.assertIn("read() поверне 64 байт", txt)


class AutoLayoutTests(unittest.TestCase):
    def test_ds4_like_matches_hand_written_ds4_layout(self):
        """Авторозкладка з DS4-подібного дескриптора збігається з ручною sony_ds4_usb.json
        у тому, що дескриптор може сказати: осі, hat, тригери, кнопки, report_id."""
        auto = layout_from_descriptor(DS4_LIKE, 0x054C, 0x05C4, "Wireless Controller")
        hand = DeviceLayout.from_file(BUILTIN_LAYOUTS / "sony_ds4_usb.json")
        self.assertTrue(auto.generated)
        self.assertEqual(auto.report_id, hand.report_id)
        for n in ("LX", "LY", "RX", "RY", "L2", "R2"):
            self.assertEqual((auto.fields[n].offset, auto.fields[n].fmt),
                             (hand.fields[n].offset, hand.fields[n].fmt), n)
        self.assertEqual(auto.sticks, hand.sticks)
        self.assertEqual(auto.triggers, hand.triggers)
        self.assertEqual(auto.hat, hand.hat)
        # BTN2 = «хрестик» (байт 5, біт 5); BTN5 = L1 (байт 6, біт 0); BTN13 = PS (байт 7, біт 0)
        for n, (byte, bit) in {"BTN2": (5, 5), "BTN5": (6, 0), "BTN13": (7, 0)}.items():
            self.assertEqual((auto.buttons[n].byte, auto.buttons[n].bit), (byte, bit), n)

    def test_ds4_like_report_roundtrip_through_pipeline(self):
        """Звіт у форматі DS4 проходить авторозкладку -> ReportParser -> Pipeline -> uinput."""
        lay = layout_from_descriptor(DS4_LIKE, 0x054C, 0x05C4)
        pr = ReportParser(lay)
        word, vals = pr.parse(ds4_report(cross=True, hat=2, lx=10, l2=200))
        on = {n for n, b in pr.name_to_bit.items() if word >> b & 1}
        self.assertEqual(on, {"BTN2", "DPAD_RIGHT"})
        self.assertEqual((vals[pr.index["LX"]], vals[pr.index["L2"]]), (10, 200))
        out, r = make_pad("xbox360")
        p = Pipeline(out)
        p.cp = compile_profile({"name": "t"}, lay, pr, out)
        p.process(ds4_report(cross=True))
        ev = [e for fr in frames(drain(r)) for e in fr]
        # Позиційна евристика: BTN1=A, BTN2=B, тож «хрестик» DS4 (BTN2) -> B -> BTN_EAST (0x131).
        # Саме тому розкладку позначено needs_confirmation: на DS4 хрестик має бути A.
        self.assertIn((C.EV_KEY, C.BTN_EAST, 1), ev)

    def test_plain_descriptor_without_report_id(self):
        d = build_layout_dict(PLAIN, 0x1234, 0x5678, "Cheap Pad")
        self.assertNotIn("report_id", d)
        self.assertEqual(d["match"], {"vid": "1234", "pid": "5678"})
        # Без Rx/Ry правий стік — Z/Rz (Logitech F310 у DInput та подібні).
        self.assertEqual(d["sticks"], {"left": ["LX", "LY"], "right": ["RX", "RY"]})
        self.assertEqual((d["fields"]["RX"]["offset"], d["fields"]["RY"]["offset"]), (2, 3))
        self.assertNotIn("triggers", d)
        self.assertEqual(d["hat"], {"byte": 4, "shift": 0, "mask": 15, "first": 0})
        self.assertEqual(d["buttons"]["BTN1"], {"byte": 4, "bit": 4, "target": "A"})
        self.assertEqual(d["buttons"]["BTN12"]["target"], "M1")           # понад 11 -> M1..
        lay = DeviceLayout.from_dict(d, "x")                              # формат валідний
        self.assertEqual(lay.min_len, 6)

    def test_signed_16bit_axes(self):
        d = build_layout_dict(SIGNED16, 1, 2)
        self.assertEqual(d["fields"]["LX"], {"offset": 0, "fmt": "s16le", "raw_min": -32768, "raw_max": 32767})
        self.assertEqual(d["fields"]["LY"]["offset"], 2)
        self.assertIn("Правий стік не знайдено.", d["_auto"]["notes"])

    def test_non_gamepads_rejected(self):
        for desc in (MOUSE, VENDOR_ONLY):
            with self.assertRaises(AutoLayoutError):
                build_layout_dict(desc, 1, 2)
        with self.assertRaises(AutoLayoutError):
            build_layout_dict(b"\x05", 1, 2)                              # битий дескриптор

    def test_vendor_blocks_reported_for_sniffer(self):
        notes = build_layout_dict(DS4_LIKE, 0x054C, 0x05C4)["_auto"]["notes"]
        self.assertTrue(any("FF00:0021" in n and "байти 10..63" in n for n in notes), notes)
        self.assertTrue(any("M1–M4" in n for n in notes))

    def test_unusual_bit_widths_use_bit_fields(self):
        """12-бітні осі не по межі байта -> бітове поле, яке ReportParser вміє читати."""
        desc = H("05 01 09 05 A1 01 09 30 09 31 15 00 26 FF 0F 75 0C 95 02 81 02 C0")
        d = build_layout_dict(desc, 1, 2)
        self.assertEqual(d["fields"]["LX"], {"offset": 0, "bit_offset": 0, "bits": 12, "signed": False,
                                             "raw_min": 0, "raw_max": 4095})
        self.assertEqual(d["fields"]["LY"]["bit_offset"], 4)
        pr = ReportParser(DeviceLayout.from_dict(d, "x"))
        _, vals = pr.parse(bytes([0xBC, 0x9A, 0x78]))     # LX=0xABC, LY=0x789
        self.assertEqual((vals[pr.index["LX"]], vals[pr.index["LY"]]), (0xABC, 0x789))

    def test_hat_with_wrong_position_count_skipped(self):
        desc = H("05 01 09 05 A1 01 09 30 09 31 15 00 26 FF 00 75 08 95 02 81 02",
                 "09 39 15 00 25 03 75 04 95 01 81 42 75 04 95 01 81 03 C0")
        d = build_layout_dict(desc, 1, 2)
        self.assertNotIn("hat", d)
        self.assertTrue(any("Hat має 4 позицій" in n for n in d["_auto"]["notes"]))

    def test_xbox_one_bt_style_triggers_on_simulation_page(self):
        desc = H("05 01 09 05 A1 01 09 30 09 31 09 32 09 35 15 00 27 FF FF 00 00 75 10 95 04 81 02",
                 "05 02 09 C5 09 C4 15 00 26 FF 03 75 0A 95 02 81 02 C0")
        d = build_layout_dict(desc, 0x045E, 0x02FD)
        self.assertEqual(d["triggers"], {"left": "L2", "right": "R2"})
        self.assertEqual(d["sticks"]["right"], ["RX", "RY"])
        self.assertEqual(d["fields"]["RX"]["offset"], 4)                  # Z
        self.assertEqual(d["fields"]["L2"]["bits"], 10)
        self.assertEqual(d["fields"]["L2"]["raw_max"], 1023)

    def test_consumer_page_button_is_exposed_unassigned(self):
        """Flydigi Direwolf 3: Home/Menu лежить на сторінці Consumer (0x0C:0x69), біт 36."""
        desc = H("05 01 09 05 A1 01 09 30 09 31 15 00 26 FF 00 75 08 95 02 81 02",
                 "05 0C 09 69 15 00 25 01 75 01 95 01 81 02",
                 "05 09 19 01 29 04 95 04 81 02 75 03 95 01 81 03 C0")
        d = build_layout_dict(desc, 1, 2)
        self.assertEqual(d["buttons"]["CONSUMER_0069"], {"byte": 2, "bit": 0, "target": None})
        self.assertEqual(d["buttons"]["BTN1"]["bit"], 1)
        DeviceLayout.from_dict(d, "x")

    def test_interface_goes_into_match_and_id(self):
        lay = layout_from_descriptor(PLAIN, 1, 2, "n", interface=3)
        self.assertEqual((lay.interface, lay.id), (3, "auto_0001_0002_if3"))


class ManagerAutoLayoutTests(unittest.TestCase):
    def _mgr(self, layouts=None, **cfg_kw):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        t = Path(self._tmp.name)
        cfg = DaemonConfig(config_dir=t, **cfg_kw)
        store = ProfileStore(t / "profiles", t / "builtin", t / "state.json")
        return Manager(cfg, store, layouts or {})

    def info(self, desc, vid=0x1234, pid=0x5678, **kw):
        return DeviceInfo(path="/dev/hidraw0", vid=vid, pid=pid, descriptor=desc, backend="hidraw", **kw)

    def test_unknown_gamepad_gets_generated_layout_cached(self):
        m = self._mgr()
        lay = m.pick_layout(self.info(DS4_LIKE))
        self.assertIsNotNone(lay)
        self.assertTrue(lay.generated)
        self.assertIs(m.pick_layout(self.info(DS4_LIKE)), lay)           # кеш: без повторного розбору
        self.assertIn(lay.id, m.all_layouts())

    def test_known_layout_wins_over_generated(self):
        hand = DeviceLayout.from_file(BUILTIN_LAYOUTS / "sony_ds4_usb.json")
        m = self._mgr({hand.id: hand})
        self.assertIs(m.pick_layout(self.info(DS4_LIKE, vid=0x054C, pid=0x05C4)), hand)

    def test_disabled_and_non_gamepad_give_none(self):
        self.assertIsNone(self._mgr(auto_layout=False).pick_layout(self.info(DS4_LIKE)))
        m = self._mgr()
        self.assertIsNone(m.pick_layout(self.info(MOUSE)))
        self.assertIsNone(m.pick_layout(self.info(None)))
        self.assertNotIn(None, m.all_layouts().values())

    def test_enumerate_hidraw_reads_descriptor(self):
        with tempfile.TemporaryDirectory() as t:
            dev = Path(t) / "hidraw3" / "device"
            dev.mkdir(parents=True)
            (dev / "uevent").write_text("HID_ID=0003:00001234:00005678\nHID_NAME=Pad\n")
            (dev / "report_descriptor").write_bytes(PLAIN)
            nodesc = Path(t) / "hidraw4" / "device"
            nodesc.mkdir(parents=True)
            (nodesc / "uevent").write_text("HID_ID=0003:00001234:00005679\nHID_NAME=Pad2\n")
            infos = enumerate_hidraw(t)
        self.assertEqual([i.descriptor for i in infos], [PLAIN, None])
        self.assertNotIn("descriptor", infos[0].as_dict())


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        from gamepad_remapd.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_autolayout_from_binary_and_hex_file_and_save(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "d.bin").write_bytes(DS4_LIKE)
            (t / "d.txt").write_text(" ".join(f"{b:02x}" for b in DS4_LIKE))
            for f in ("d.bin", "d.txt"):
                rc, out, err = self.run_cli("--config-dir", str(t), "autolayout",
                                            "--descriptor-file", str(t / f), "--vid", "054c", "--pid", "05c4")
                self.assertEqual(rc, 0, err)
                self.assertEqual(json.loads(out)["report_id"], 1)
            rc, _, err = self.run_cli("--config-dir", str(t), "autolayout", "--descriptor-file",
                                      str(t / "d.bin"), "--vid", "054c", "--pid", "05c4", "--save")
            self.assertEqual(rc, 0, err)
            saved = t / "devices" / "auto_054c_05c4.json"
            lay = DeviceLayout.from_file(saved)           # збережена розкладка завантажується як звичайна
            self.assertFalse(lay.generated)
            self.assertTrue(lay.matches(0x054C, 0x05C4, None))

    def test_autolayout_fields_table_and_errors(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "m.bin"
            f.write_bytes(MOUSE)
            rc, out, err = self.run_cli("--config-dir", t, "autolayout", "--descriptor-file", str(f), "--fields")
            self.assertEqual(rc, 0)
            self.assertIn("applications: 0001:0002", out)
            rc, _, err = self.run_cli("--config-dir", t, "autolayout", "--descriptor-file", str(f))
            self.assertEqual(rc, 1)
            self.assertIn("не геймпад", err)
            rc, _, err = self.run_cli("--config-dir", t, "autolayout")
            self.assertEqual(rc, 1)


class MonitorTests(unittest.TestCase):
    def _run(self, script, layouts=None, **kw):
        """Запускає monitor у потоці з фейковим пристроєм; `script(dev)` подає звіти."""
        from gamepad_remapd.tools.monitor_cli import run
        import threading
        info, dev, out = ds4_info(), None, io.StringIO()
        dev = FakeDevice(info)
        hand = DeviceLayout.from_file(BUILTIN_LAYOUTS / "sony_ds4_usb.json")
        res = {}
        th = threading.Thread(target=lambda: res.update(rc=run(
            info.path, layouts if layouts is not None else {hand.id: hand}, None, kw.get("seconds", 0),
            enumerate_fn=lambda b: [info], open_fn=lambda i, s: dev, out=out)))
        th.start()
        script(dev)
        th.join(5)
        self.assertFalse(th.is_alive())
        return res["rc"], out.getvalue(), dev

    def test_prints_button_hat_and_axis_events(self):
        def script(dev):
            dev.inject(ds4_report())                       # точка відліку
            dev.inject(ds4_report(cross=True, hat=2))      # CROSS + D-pad вправо
            dev.inject(ds4_report(lx=255))                 # LX зсунувся на 127 з 255 -> друкується
            dev.inject(ds4_report(lx=255, ps=True))        # PS не змінює LX -> лише кнопка
            dev.unplug()
        rc, text, dev = self._run(script)
        self.assertEqual(rc, 0)
        self.assertIn("+ CROSS            → A", text)
        self.assertIn("+ DPAD_RIGHT       → RIGHT", text)
        self.assertIn("- CROSS", text)
        self.assertIn("LX             = 255", text)
        self.assertIn("+ PS               → GUIDE", text)
        self.assertEqual(text.count("LX  "), 1)            # вісь надруковано один раз
        self.assertTrue(dev.closed)

    def test_auto_layout_used_when_none_saved(self):
        """Без збереженої розкладки monitor бере авторозкладку з дескриптора."""
        info = DeviceInfo(path="/dev/hidraw7", vid=0x054C, pid=0x05C4, interface=0, backend="hidraw",
                          descriptor=DS4_LIKE)
        from gamepad_remapd.tools.monitor_cli import run
        dev, out = FakeDevice(info), io.StringIO()
        import threading
        th = threading.Thread(target=run, args=(info.path, {}, None, 0),
                              kwargs=dict(enumerate_fn=lambda b: [info], open_fn=lambda i, s: dev, out=out))
        th.start()
        dev.inject(ds4_report())
        dev.inject(ds4_report(cross=True))
        dev.unplug()
        th.join(5)
        self.assertIn("(авто, потребує перевірки)", out.getvalue())
        self.assertIn("+ BTN2", out.getvalue())

    def test_raw_dump_marks_filtered_reports(self):
        from gamepad_remapd.tools.monitor_cli import run
        import threading
        info, out = ds4_info(), io.StringIO()
        dev = FakeDevice(info)
        hand = DeviceLayout.from_file(BUILTIN_LAYOUTS / "sony_ds4_usb.json")
        th = threading.Thread(target=run, args=(info.path, {hand.id: hand}, None, 0, "auto", True),
                              kwargs=dict(enumerate_fn=lambda b: [info], open_fn=lambda i, s: dev, out=out))
        th.start()
        dev.inject(ds4_report())
        dev.inject(b"\x07" + bytes(63))                  # чужий Report ID
        dev.unplug()
        th.join(5)
        lines = [l for l in out.getvalue().splitlines() if l.endswith(("00", "01 80 80 80 80 08 00 00 00 00"))
                 or " 64B " in l]
        self.assertEqual([l[0] for l in lines], [" ", "x"])

    def test_no_reports_hint_and_missing_layout(self):
        rc, text, _ = self._run(lambda dev: None, seconds=0.3)
        self.assertEqual(rc, 0)
        self.assertIn("Звітів не надійшло", text)
        from gamepad_remapd.tools.monitor_cli import run
        info = ds4_info()
        self.assertEqual(run(info.path, {}, None, 0, enumerate_fn=lambda b: [info],
                             open_fn=lambda i, s: FakeDevice(i)), 1)      # немає розкладки й дескриптора
        self.assertEqual(run("/dev/nope", {}, None, 0, enumerate_fn=lambda b: [info]), 1)


def v1_report(b9=0, b10=0, b7=0, b8=0, lx=0x7F, ly=0x7F, rx=0x7F, ry=0x7F, l2=0, r2=0) -> bytes:
    """Звіт Flydigi V1 за offset-ами з SDL: 04 FE ..., довжина 32."""
    b = bytearray(32)
    b[0], b[1] = 0x04, 0xFE
    b[7], b[8], b[9], b[10] = b7, b8, b9, b10
    b[17], b[19], b[21], b[22], b[23], b[24] = lx, ly, rx, ry, l2, r2
    return bytes(b)


class FlydigiV1LayoutTests(unittest.TestCase):
    LAY = DeviceLayout.from_file(BUILTIN_LAYOUTS / "flydigi_v1_2g4.json")

    def test_layout_matches_only_vendor_interface_2(self):
        self.assertTrue(self.LAY.matches(0x04B4, 0x2412, 2))
        for iface in (0, 1, 3):
            self.assertFalse(self.LAY.matches(0x04B4, 0x2412, iface))
        self.assertEqual((self.LAY.report_id, self.LAY.sync, self.LAY.min_len), (4, (1, 0xFE), 25))

    def test_handshake_is_sdl_get_info_request(self):
        dev = FakeDevice(ds4_info())
        run_handshake(dev, self.LAY.handshake)
        self.assertEqual(dev.outputs, [bytes([0x05, 0xEC]) + bytes(10)])      # 12 байт: 05 EC + 10 нулів

    def test_parse_buttons_dpad_and_axes(self):
        pr = ReportParser(self.LAY)
        word, vals = pr.parse(v1_report(b9=0x10 | 0x03, b10=0x01 | 0x80, b7=0x04 | 0x20, b8=0x08,
                                        lx=0xFF, ry=0x00, l2=200))
        on = {n for n, b in pr.name_to_bit.items() if word >> b & 1}
        self.assertEqual(on, {"A", "PAD_UP", "PAD_RIGHT", "Y", "RS", "M1", "M4", "GUIDE"})
        g = lambda n: vals[pr.index[n]]
        self.assertEqual((g("LX"), g("RY"), g("L2"), g("LY")), (255, 0, 200, 127))

    def test_pipeline_drops_handshake_reply_and_emits_events(self):
        """Відповідь на handshake (звіт 04 без 0xFE) не повинна давати фантомних натискань."""
        out, r = make_pad("xbox360")
        p = Pipeline(out)
        p.cp = compile_profile({"name": "t"}, self.LAY, ReportParser(self.LAY), out)
        reply = bytearray(32)
        reply[0], reply[1], reply[9], reply[10], reply[15] = 0x04, 0x00, 0xFF, 0xFF, 0xEC
        p.process(bytes(reply))
        self.assertEqual((p.dropped, drain(r)), (1, []))
        p.process(v1_report())                      # стан спокою
        p.process(v1_report(b9=0x10))               # A
        ev = [e for fr in frames(drain(r)) for e in fr]
        self.assertIn((C.EV_KEY, C.BTN_SOUTH, 1), ev)

    def test_manager_opens_iface2_and_skips_autolayout_on_other_interfaces(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            m = Manager(DaemonConfig(config_dir=td), ProfileStore(td / "p", td / "b", td / "s.json"),
                        {self.LAY.id: self.LAY})
            self._check_manager(m)

    def _check_manager(self, m):
        base = dict(path="/dev/hidrawX", vid=0x04B4, pid=0x2412, backend="hidraw")
        self.assertIs(m.pick_layout(DeviceInfo(interface=2, descriptor=None, **base)), self.LAY)
        # інтерфейс 0 має геймпадний дескриптор, але розкладка для цього VID/PID вже є -> без дубліката
        self.assertIsNone(m.pick_layout(DeviceInfo(interface=0, descriptor=PLAIN, **base)))


class FlydigiV1EndToEnd(unittest.IsolatedAsyncioTestCase):
    """Повний демон (Manager+Session+Pipeline) з фейковим ресивером Direwolf 3: інтерфейс 0 (мовчить,
    є геймпадний дескриптор) і інтерфейс 2 (вендорський, ним керуємо)."""

    async def test_handshake_then_buttons_reach_virtual_pad(self):
        from .test_integration import Rig, kv
        from .fakes import wait_frames
        with tempfile.TemporaryDirectory() as td:
            rig = Rig(td)
            await rig.start()
            try:
                base = dict(vid=0x04B4, pid=0x2412, name="Flydigi Flydigi Direwolf 3", backend="hidraw", bus=3)
                rig.present += [DeviceInfo(path="/dev/hidraw8", interface=0, descriptor=PLAIN, **base),
                                DeviceInfo(path="/dev/hidraw10", interface=2, **base)]
                await rig.wait_for(lambda: "hidraw10" in rig.mgr.sessions)
                await rig.mgr.scan()
                self.assertEqual(sorted(rig.mgr.sessions), ["hidraw10"])      # інтерфейс 0 не відкрито
                dev = rig.devices["/dev/hidraw10"]
                self.assertEqual(dev.outputs[0], bytes([0x05, 0xEC]) + bytes(10))   # handshake виконано
                reply = bytearray(32)
                reply[0], reply[9], reply[15] = 0x04, 0xFF, 0xEC                  # відповідь на GET_INFO
                dev.inject(bytes(reply))
                dev.inject(v1_report())
                dev.inject(v1_report(b9=0x10))
                ev = kv(wait_frames(rig.r, 1)[0])
                self.assertEqual(ev[(C.EV_KEY, C.BTN_SOUTH)], 1)                  # саме A, без фантомів
                self.assertEqual(len(ev), 1)
            finally:
                await rig.stop()


if __name__ == "__main__":
    unittest.main()
