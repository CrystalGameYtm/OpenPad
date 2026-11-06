import os
import tempfile
import unittest

from gamepad_remapd.ipc.client import ControlError
from gamepad_remapd.logic.outputs import native_kind, output_choices, pick_kind
from gamepad_remapd.output.uinput_linux import SPECS, UinputPad

from .test_integration import Rig


class Choices(unittest.TestCase):
    def test_native_kind(self):
        self.assertEqual(native_kind(0x054C, None), "ds4")
        self.assertEqual(native_kind(0x1234, "gps13_ds4_bt"), "ds4")
        self.assertEqual(native_kind(0xD7D7, "flydigi_v1_2g4"), "dinput")

    def test_choices(self):
        self.assertEqual([c["id"] for c in output_choices(0xD7D7, "x", windows=False)], ["xbox360", "dinput"])
        self.assertEqual([c["id"] for c in output_choices(0x054C, "sony_ds4_usb", windows=False)], ["xbox360", "ds4"])
        self.assertEqual([c["id"] for c in output_choices(0x054C, "sony_ds4_usb", windows=True)], ["xbox360"])

    def test_pick(self):
        ch = output_choices(0xD7D7, "x", windows=False)
        self.assertEqual(pick_kind("dinput", ch, "xbox360"), "dinput")
        self.assertEqual(pick_kind("ds4", ch, "xbox360"), "xbox360")      # недоступний -> запасний


class DinputSpec(unittest.TestCase):
    def test_pad_builds_and_emits(self):
        r, w = os.pipe()
        os.set_blocking(r, False)
        pad = UinputPad(SPECS["dinput"], fd=w)
        pad.set_button(0, True)
        pad.set_axis(0, 1000)
        pad.flush()
        self.assertGreater(len(os.read(r, 4096)), 0)
        os.close(r)
        os.close(w)


class OptionsIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.rig = Rig(self._td.name)
        self.kinds = []
        orig = self.rig._output

        def spy(kind, name=None, uinput_path=None, **kw):
            self.kinds.append(kind)
            return orig(kind, name=name, uinput_path=uinput_path)
        self.rig._output = spy
        await self.rig.start()
        self.rig.mgr._output_fn = spy

    async def asyncTearDown(self):
        await self.rig.stop()
        self._td.cleanup()

    async def test_switch_output_reopens_with_ds4(self):
        await self.rig.plug()
        devs = await self.rig.client.call("devices")
        self.assertEqual(devs[0]["options"]["output_kind"], "xbox360")
        self.assertFalse(devs[0]["hide_state"]["requested"])         # у Rig grab_siblings=False
        await self.rig.client.call("set_options", device="hidraw7", output_kind="ds4", hide=True)
        devs = await self.rig.client.call("devices")
        self.assertEqual(devs[0]["output_kind"], "ds4")
        self.assertTrue(devs[0]["options"]["hide"])
        self.assertTrue(devs[0]["hide_state"]["requested"])
        self.assertEqual(self.kinds[-1], "ds4")

    async def test_invalid_kind_rejected(self):
        await self.rig.plug()
        with self.assertRaises(ControlError):
            await self.rig.client.call("set_options", device="hidraw7", output_kind="dinput")


if __name__ == "__main__":
    unittest.main()


class Warnings(unittest.TestCase):
    def test_native_kinds_carry_warning_xinput_does_not(self):
        from gamepad_remapd.logic.outputs import warn_for
        ch = output_choices(0xD7D7, "flydigi", windows=False)
        self.assertIsNone(warn_for(ch, "xbox360"))
        self.assertIn("мапінгу", warn_for(ch, "dinput"))
        ch = output_choices(0x054C, "sony_ds4_usb", windows=False)
        self.assertIn("XInput", warn_for(ch, "ds4"))
