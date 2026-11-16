"""The vendor handshake is skipped when the device already streams state reports."""
import tempfile
import unittest

from gamepad_remapd.input.base import DeviceInfo, is_streaming
from tests.fakes import FakeDevice
from tests.test_autolayout import v1_report
from tests.test_integration import Rig


def info(path="/dev/hidraw10"):
    return DeviceInfo(path=path, vid=0x04B4, pid=0x2412, name="Flydigi Flydigi Direwolf 3",
                      interface=2, backend="hidraw", bus=3)


class IsStreaming(unittest.TestCase):
    def test_true_on_matching_report(self):
        d = FakeDevice(info())
        d.inject(b"\x01junk")
        d.inject(v1_report())
        self.assertTrue(is_streaming(d, lambda raw: raw[0] == 4 and raw[1] == 0xFE, window=1.0))

    def test_false_on_silence_and_foreign_reports(self):
        d = FakeDevice(info())
        d.inject(b"\x04\x00" + bytes(30))
        self.assertFalse(is_streaming(d, lambda raw: raw[1] == 0xFE, window=0.3))


class SessionSkipsHandshake(unittest.IsolatedAsyncioTestCase):
    async def _run(self, preload):
        with tempfile.TemporaryDirectory() as td:
            rig = Rig(td)
            orig = rig._open

            def open_(i, size):
                d = orig(i, size)
                if preload:
                    d.inject(v1_report())
                return d
            rig._open = open_
            await rig.start()
            try:
                rig.present.append(info())
                await rig.wait_for(lambda: "hidraw10" in rig.mgr.sessions)
                return list(rig.devices["/dev/hidraw10"].outputs)
            finally:
                await rig.stop()

    async def test_streaming_device_gets_no_handshake(self):
        self.assertEqual(await self._run(True), [])

    async def test_silent_device_still_gets_handshake(self):
        outs = await self._run(False)
        self.assertEqual(outs[0], bytes([0x05, 0xEC]) + bytes(10))


if __name__ == "__main__":
    unittest.main()
