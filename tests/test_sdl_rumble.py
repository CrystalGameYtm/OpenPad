import io
import unittest
from unittest import mock

from gamepad_remapd.tools import sdl_rumble as sr

TRACE = r'''1234 openat(AT_FDCWD, "/dev/hidraw13", O_RDWR|O_CLOEXEC) = 9
1234 write(1, "hello\n", 6) = 6
1235 write(9, "\21\300\0\7\0\0\377\0\0\0\0", 11) = 11
1235 write(9, "\21\300\0\7\0\0\0\377\"\\\x7f", 11) = 11
1235 close(9) = 0
1235 write(9, "\1\2", 2) = 2
'''


class T(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(sr.decode_strace_string(r'\21\300\0\x7f\"\\A\n'), b'\x11\xc0\x00\x7f"\\A\n')

    def test_hidraw_writes_only_hidraw_fds(self):
        w = sr.hidraw_writes(TRACE.splitlines())
        self.assertEqual([x[0] for x in w], ["/dev/hidraw13"] * 2)
        self.assertEqual(w[0][1], bytes([0x11, 0xC0, 0, 7, 0, 0, 0xFF, 0, 0, 0, 0]))
        self.assertEqual(w[1][1][-3:], b'"\\\x7f')

    def test_run_with_fake_sdl(self):
        lib = mock.MagicMock()
        lib.SDL_Init.return_value = True
        lib.SDL_GetError.return_value = b""
        lib.SDL_GetGamepadName.return_value = b"Pad"
        lib.SDL_RumbleGamepad.return_value = True

        def getpads(ref):
            ref._obj.value = 1
            return [7]
        lib.SDL_GetGamepads.side_effect = getpads
        out = io.StringIO()
        self.assertEqual(sr.run(lib, out, hold=0.2, sleep=lambda s: None), 0)
        self.assertEqual(lib.SDL_RumbleGamepad.call_count, 3)
        self.assertIn("геймпадів: 1", out.getvalue())

    def test_no_sdl(self):
        with mock.patch.object(sr, "load_sdl", return_value=None):
            self.assertEqual(sr.run(None), 1)


if __name__ == "__main__":
    unittest.main()
