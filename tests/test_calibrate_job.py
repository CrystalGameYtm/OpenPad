import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.logic.layout import DeviceLayout
from gamepad_remapd.tools.calibrate_job import CalibJob, run_cli

from . import calib_sim as sim
from .fakes import FakeDevice


class SimDevice(FakeDevice):
    """Фейковий пристрій, що «живе» за сценарієм калібрування. awake_on — префікс output-звіту, що його будить."""

    def __init__(self, info, spec, job_ref, awake_on=None, absent=()):
        super().__init__(info)
        self.awake = awake_on is None
        self.awake_on = awake_on
        self.gen = sim.script(spec, lambda: job_ref[0].cal, absent=absent)

    def write_output(self, data: bytes) -> None:
        super().write_output(data)
        if self.awake_on and bytes(data).startswith(self.awake_on):
            self.awake = True

    def read(self):
        if not self.awake:
            time.sleep(0.01)
            return None
        time.sleep(0.0003)
        try:
            return next(self.gen)
        except StopIteration:
            time.sleep(0.05)
            return None


def info(vid=0x1234, pid=0x5678, path="/dev/hidraw9", interface=0, bus=3, backend="hidraw"):
    return DeviceInfo(path=path, vid=vid, pid=pid, name="Sim", interface=interface, bus=bus, backend=backend)


def run_job(spec, inf, awake_on=None, absent=("M1", "M2", "M3", "M4", "C", "Z"), wait=40, **kw):
    ref = [None]
    dev = SimDevice(inf, spec, ref, awake_on, absent)
    job = CalibJob(inf, {}, open_fn=lambda i, n: dev, baseline_s=0.3, listen_s=0.3, wake_s=0.6, **kw)
    ref[0] = job
    job.start()
    deadline = time.monotonic() + wait
    while job.active and time.monotonic() < deadline:
        time.sleep(0.05)
    job.stop()
    job.join()
    return job, dev


class CalibJobTests(unittest.TestCase):
    def test_streaming_device_end_to_end_and_save(self):
        job, dev = run_job(sim.XBOX360_USB, info())
        self.assertEqual(job.state, "done", job.status())
        self.assertIsNone(job.handshake_id)
        self.assertEqual(dev.outputs, [])                 # живому пристрою нічого не шлемо
        with tempfile.TemporaryDirectory() as d:
            dest = job.save(Path(d))
            self.assertTrue(dest.name.startswith("calibrated_1234_5678"))
            data = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(data["match"]["bus"], "usb")
            self.assertEqual(data["priority"], 10)
            lay = DeviceLayout.from_file(dest)
            self.assertTrue(lay.matches(0x1234, 0x5678, 0, 3))
            self.assertFalse(lay.matches(0x1234, 0x5678, 0, 5))
            self.assertEqual(lay.sticks["left"], ("LX", "LY"))

    def test_silent_receiver_is_woken_by_library_handshake(self):
        inf = info(vid=0x04B4, pid=0x2412, interface=2)
        job, dev = run_job(sim.FLYDIGI_V1, inf, awake_on=bytes([0x05, 0xEC]))
        self.assertEqual(job.state, "done", job.status())
        self.assertEqual(job.handshake_id, "flydigi_v1")
        self.assertTrue(dev.outputs and dev.outputs[0].startswith(b"\x05\xec"))
        d = job.result_layout()
        self.assertEqual(d["handshake"][0]["op"], "output")
        self.assertTrue(d["handshake"][0]["data"].startswith("05 ec"))
        self.assertEqual(d["report_id"], 4)
        self.assertEqual(d["buttons"]["A"], {"byte": 9, "bit": 4, "target": "A"})

    def test_opt_in_vendor_protocols_only_with_broad(self):
        inf = info(vid=0x3537, pid=0x1010)                # «чужий» VID: vendor-кандидатів немає
        _, dev = run_job(sim.XBOX360_USB, inf, awake_on=b"\xa2\x01", wait=2.5)
        self.assertEqual(dev.outputs, [])                 # без broad пакети чужих виробників не шлемо
        _, dev = run_job(sim.XBOX360_USB, inf, awake_on=b"\xa2\x01", wait=2.5, broad=True)
        self.assertTrue(any(o.startswith(b"\xa2\x01\x00") for o in dev.outputs), dev.outputs)

    def test_evdev_rejected(self):
        with self.assertRaises(ValueError):
            CalibJob(info(backend="evdev"))

    def test_cli_runs_and_saves(self):
        inf = info()
        ref = [None]
        dev = SimDevice(inf, sim.GENERIC_BE16, ref, absent=("M1", "M2", "M3", "M4", "C", "Z"))
        out = io.StringIO()
        import gamepad_remapd.tools.calibrate_job as cj
        orig = cj.CalibJob

        class J(orig):
            def __init__(self, *a, **k):
                super().__init__(*a, baseline_s=0.3, listen_s=0.3, **k)
                ref[0] = self
        cj.CalibJob = J
        try:
            with tempfile.TemporaryDirectory() as d:
                rc = run_cli(inf.path, {}, Path(d), save=True, enumerate_fn=lambda b: [inf],
                             open_fn=lambda i, n: dev, out=out, lines=iter(()))
                saved = list(Path(d).glob("calibrated_*.json"))
        finally:
            cj.CalibJob = orig
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(len(saved), 1)
        self.assertIn("збережено", out.getvalue())
        self.assertIn("ЛІВИЙ стік", out.getvalue())


class PriorityTests(unittest.TestCase):
    def test_calibrated_layout_beats_builtin(self):
        from gamepad_remapd.logic.layout import load_layouts
        from gamepad_remapd.tools.monitor_cli import _find_layout
        base = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"
        lays = load_layouts(base)
        d = json.loads((base / "sony_ds4_usb.json").read_text(encoding="utf-8"))
        d["priority"] = 10
        d["name"] = "mine"
        lays["calibrated_054c_05c4"] = DeviceLayout.from_dict(d, "calibrated_054c_05c4")
        inf = DeviceInfo(path="/dev/hidraw1", vid=0x054C, pid=0x05C4, name="x", interface=0, bus=3, backend="hidraw")
        self.assertEqual(_find_layout(inf, lays).name, "mine")


if __name__ == "__main__":
    unittest.main()
