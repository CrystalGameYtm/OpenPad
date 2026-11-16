"""Linux: virtual gamepad through /dev/uinput.

The implementation does NOT depend on python-evdev: only `os`, `fcntl`, `struct`
and `ctypes` from the standard library are needed. It is the same kernel evdev/uinput API, but:
  * all events of one input report (+ the closing SYN_REPORT) are collected into a
    preallocated buffer and delivered to the kernel in ONE `write()`; python-evdev
    makes a separate system call for every event;
  * no allocations on the hot path (struct.pack_into into a ready buffer);
  * FF (rumble) is handled right here: we read EV_UINPUT/EV_FF from the same fd.

ioctl numbers are encoded via `util.IOC` (correct for x86/ARM/RISC-V; on
PowerPC/MIPS/SPARC the direction bits differ — that would need a separate port of the constants).
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import fcntl
import logging
import os
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .. import constants as C
from ..util import IO as _IO, IOW as _IOW, IOWR as _IOWR
from .base import OutputBackend
from ..i18n import tr

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
class _FFTrigger(ctypes.Structure):
    _fields_ = [("button", ctypes.c_uint16), ("interval", ctypes.c_uint16)]


class _FFReplay(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint16), ("delay", ctypes.c_uint16)]


class _FFEnvelope(ctypes.Structure):
    _fields_ = [("attack_length", ctypes.c_uint16), ("attack_level", ctypes.c_uint16),
                ("fade_length", ctypes.c_uint16), ("fade_level", ctypes.c_uint16)]


class _FFConstant(ctypes.Structure):
    _fields_ = [("level", ctypes.c_int16), ("envelope", _FFEnvelope)]


class _FFRamp(ctypes.Structure):
    _fields_ = [("start_level", ctypes.c_int16), ("end_level", ctypes.c_int16),
                ("envelope", _FFEnvelope)]


class _FFPeriodic(ctypes.Structure):
    _fields_ = [("waveform", ctypes.c_uint16), ("period", ctypes.c_uint16),
                ("magnitude", ctypes.c_int16), ("offset", ctypes.c_int16),
                ("phase", ctypes.c_uint16), ("envelope", _FFEnvelope),
                ("custom_len", ctypes.c_uint32), ("custom_data", ctypes.POINTER(ctypes.c_int16))]


class _FFCondition(ctypes.Structure):
    _fields_ = [("right_saturation", ctypes.c_uint16), ("left_saturation", ctypes.c_uint16),
                ("right_coeff", ctypes.c_int16), ("left_coeff", ctypes.c_int16),
                ("deadband", ctypes.c_uint16), ("center", ctypes.c_int16)]


class _FFRumble(ctypes.Structure):
    _fields_ = [("strong_magnitude", ctypes.c_uint16), ("weak_magnitude", ctypes.c_uint16)]


class _FFUnion(ctypes.Union):
    _fields_ = [("constant", _FFConstant), ("ramp", _FFRamp), ("periodic", _FFPeriodic),
                ("condition", _FFCondition * 2), ("rumble", _FFRumble)]


class _FFEffect(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint16), ("id", ctypes.c_int16),
                ("direction", ctypes.c_uint16), ("trigger", _FFTrigger),
                ("replay", _FFReplay), ("u", _FFUnion)]


class _UinputFFUpload(ctypes.Structure):
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32),
                ("effect", _FFEffect), ("old", _FFEffect)]


class _UinputFFErase(ctypes.Structure):
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32),
                ("effect_id", ctypes.c_uint32)]


# struct uinput_setup { struct input_id id; char name[80]; __u32 ff_effects_max; }
_SETUP = struct.Struct("=HHHH80sI")
# struct uinput_abs_setup { __u16 code; /*2 bytes of padding*/ struct input_absinfo absinfo; }
_ABS_SETUP = struct.Struct("=H2xiiiiii")
# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }

_EV = struct.Struct("llHHi")
EVSZ = _EV.size

UI_DEV_CREATE = _IO("U", 1)
UI_DEV_DESTROY = _IO("U", 2)
UI_DEV_SETUP = _IOW("U", 3, _SETUP.size)
UI_ABS_SETUP = _IOW("U", 4, _ABS_SETUP.size)
UI_SET_EVBIT = _IOW("U", 100, 4)
UI_SET_KEYBIT = _IOW("U", 101, 4)
UI_SET_ABSBIT = _IOW("U", 103, 4)
UI_SET_FFBIT = _IOW("U", 107, 4)
UI_BEGIN_FF_UPLOAD = _IOWR("U", 200, ctypes.sizeof(_UinputFFUpload))
UI_END_FF_UPLOAD = _IOW("U", 201, ctypes.sizeof(_UinputFFUpload))
UI_BEGIN_FF_ERASE = _IOWR("U", 202, ctypes.sizeof(_UinputFFErase))
UI_END_FF_ERASE = _IOW("U", 203, ctypes.sizeof(_UinputFFErase))

BUS_USB = 0x03
FF_EFFECTS_MAX = 16
EVENT_BUF_EVENTS = 256   


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AxisSpec:
    code: int
    lo: int
    hi: int


@dataclass(frozen=True)
class PadSpec:
    kind: str
    name: str
    vendor: int
    product: int
    version: int
    buttons: Dict[str, int]       
    axes: Dict[str, AxisSpec]     # LX LY RX RY LT RT
    # fuzz/flat are intentionally 0: the deadzone is already applied in the Pipeline; non-zero values
    


def _extras(first_code: int = C.BTN_TRIGGER_HAPPY1) -> Dict[str, int]:
    return {n: first_code + i for i, n in enumerate(C.EXTRA_BUTTONS)}


_STICK = (C.STICK_MIN, C.STICK_MAX)
_TRIG = (C.TRIGGER_MIN, C.TRIGGER_MAX)

# Xbox 360 (like the xpad driver): X -> BTN_NORTH, Y -> BTN_WEST — the kernel does this
# (BTN_X == BTN_NORTH), and SDL/Steam mappings rely on exactly this.
XBOX360 = PadSpec(
    kind="xbox360", name="Gamepad Remapper Virtual X360 Pad",
    vendor=0x045E, product=0x028E, version=0x0110,
    buttons={"A": C.BTN_SOUTH, "B": C.BTN_EAST, "X": C.BTN_NORTH, "Y": C.BTN_WEST,
             "LB": C.BTN_TL, "RB": C.BTN_TR, "BACK": C.BTN_SELECT, "START": C.BTN_START,
             "GUIDE": C.BTN_MODE, "LS": C.BTN_THUMBL, "RS": C.BTN_THUMBR, **_extras()},
    axes={"LX": AxisSpec(C.ABS_X, *_STICK), "LY": AxisSpec(C.ABS_Y, *_STICK),
          "RX": AxisSpec(C.ABS_RX, *_STICK), "RY": AxisSpec(C.ABS_RY, *_STICK),
          "LT": AxisSpec(C.ABS_Z, *_TRIG), "RT": AxisSpec(C.ABS_RZ, *_TRIG)})

# DualShock 4 (like the hid-sony/hid-playstation drivers): X (square) -> BTN_WEST,
# Y (triangle) -> BTN_NORTH; right stick on ABS_Z/ABS_RZ, triggers on ABS_RX/RY;

DS4 = PadSpec(
    kind="ds4", name="Gamepad Remapper Virtual DS4 Pad",
    vendor=0x054C, product=0x09CC, version=0x8111,
    buttons={"A": C.BTN_SOUTH, "B": C.BTN_EAST, "X": C.BTN_WEST, "Y": C.BTN_NORTH,
             "LB": C.BTN_TL, "RB": C.BTN_TR, "BACK": C.BTN_SELECT, "START": C.BTN_START,
             "GUIDE": C.BTN_MODE, "LS": C.BTN_THUMBL, "RS": C.BTN_THUMBR, **_extras()},
    axes={"LX": AxisSpec(C.ABS_X, 0, 255), "LY": AxisSpec(C.ABS_Y, 0, 255),
          "RX": AxisSpec(C.ABS_Z, 0, 255), "RY": AxisSpec(C.ABS_RZ, 0, 255),
          "LT": AxisSpec(C.ABS_RX, 0, 255), "RT": AxisSpec(C.ABS_RY, 0, 255)})



_JOY = ("A", "B", "X", "Y", "LB", "RB", "BACK", "START", "LS", "RS", "GUIDE")
DINPUT = PadSpec(
    kind="dinput", name="Gamepad Remapper Virtual DInput Pad",
    vendor=0x1209, product=0x0001, version=0x0100,
    buttons={**{n: C.BTN_TRIGGER + i for i, n in enumerate(_JOY)}, **_extras()},
    axes={"LX": AxisSpec(C.ABS_X, 0, 255), "LY": AxisSpec(C.ABS_Y, 0, 255),
          "RX": AxisSpec(C.ABS_Z, 0, 255), "RY": AxisSpec(C.ABS_RZ, 0, 255),
          "LT": AxisSpec(C.ABS_RX, 0, 255), "RT": AxisSpec(C.ABS_RY, 0, 255)})

SPECS: Dict[str, PadSpec] = {"xbox360": XBOX360, "ds4": DS4, "dinput": DINPUT}
AXIS_ORDER = ("LX", "LY", "RX", "RY", "LT", "RT")   # axis id = index in this tuple
_DPAD = C.DPAD_BUTTONS                               # D-pad button ids come after the regular ones


class UinputPad(OutputBackend):
    kind = "uinput"
    supports_keys = True

    def __init__(self, spec: PadSpec = XBOX360, path: str = "/dev/uinput",
                 name: Optional[str] = None, fd: Optional[int] = None, kbd_fd: Optional[int] = None,
                 dpad_buttons: bool = False):
        """`fd` lets you substitute a ready file descriptor (a pipe in tests/benchmarks): then the virtual device is NOT
        created, and events are simply written into this fd in the binary struct input_event format.
        """
        self.spec = spec
        self.path = path
        self.dev_name = name or spec.name
        self._fd = -1 if fd is None else fd
        self._external_fd = fd is not None
        self._created = False
        self._kbd = None                   # VirtualKeyboard | None: created by ensure_keys()
        self._kbd_fd = kbd_fd              # a pipe for tests

        # button ids: first the buttons with evdev codes, then the four D-pad directions.
        self._names: List[str] = list(spec.buttons) + list(_DPAD)
        self._ids = {n: i for i, n in enumerate(self._names)}
        self.n_buttons = len(self._names)
        self._codes = [spec.buttons.get(n, -1) for n in self._names]   # -1 => hat direction
        self.dpad_buttons = dpad_buttons
        if dpad_buttons:                   # D-pad as four BTN_DPAD_* buttons, without ABS_HAT0X/Y
            base = len(spec.buttons)
            for k, code in enumerate((C.BTN_DPAD_UP, C.BTN_DPAD_DOWN, C.BTN_DPAD_LEFT, C.BTN_DPAD_RIGHT)):
                self._codes[base + k] = code
        self._btn_state = [False] * self.n_buttons
        self._dpad = [0, 0, 0, 0]          # UP DOWN LEFT RIGHT
        self._hat = [0, 0]                 

        
        self._a_code: List[int] = []
        self._a_lo: List[int] = []
        self._a_span: List[int] = []
        self._a_clo: List[int] = []
        self._a_cspan: List[int] = []
        self._a_ident: List[bool] = []
        self._a_neutral: List[int] = []
        for n in AXIS_ORDER:
            a = spec.axes[n]
            clo, chi = _STICK if n in ("LX", "LY", "RX", "RY") else _TRIG
            self._a_code.append(a.code)
            self._a_lo.append(a.lo)
            self._a_span.append(a.hi - a.lo)
            self._a_clo.append(clo)
            self._a_cspan.append(chi - clo)
            self._a_ident.append((a.lo, a.hi) == (clo, chi))
            self._a_neutral.append(0)
        self._a_state = [0] * len(AXIS_ORDER)

        
        self._buf = bytearray(EVSZ * EVENT_BUF_EVENTS)
        self._mv = memoryview(self._buf)
        self._n = 0
        self.dropped_writes = 0

        # Force Feedback
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._effects: Dict[int, Tuple[int, int, int, int]] = {}  # id -> (strong, weak, length_ms, delay_ms)
        self._playing: Dict[int, bool] = {}
        self._timers: Dict[int, list] = {}
        self._gain = 0xFFFF
        self._last_rumble = (0, 0)
        self.on_rumble = None

    # ---- lifecycle ----
    def open(self) -> None:
        if self._external_fd:
            return
        # O_NONBLOCK: the same fd is used for writing and for reading FF requests via epoll.
        fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            ioc = fcntl.ioctl
            ioc(fd, UI_SET_EVBIT, C.EV_KEY)    
            ioc(fd, UI_SET_EVBIT, C.EV_ABS)
            ioc(fd, UI_SET_EVBIT, C.EV_FF)
            for code in sorted({c for c in self._codes if c >= 0}):
                ioc(fd, UI_SET_KEYBIT, code)   
            hat = [] if self.dpad_buttons else [C.ABS_HAT0X, C.ABS_HAT0Y]
            abs_codes = [self.spec.axes[n].code for n in AXIS_ORDER] + hat
            for code in abs_codes:
                ioc(fd, UI_SET_ABSBIT, code)   
            ioc(fd, UI_SET_FFBIT, C.FF_RUMBLE)  
            ioc(fd, UI_SET_FFBIT, C.FF_GAIN)    
            
            
            
            # "pressed" into the corner until the user moves it.
            for aid, n in enumerate(AXIS_ORDER):
                a = self.spec.axes[n]
                ioc(fd, UI_ABS_SETUP, _ABS_SETUP.pack(a.code, self._neutral_device_value(aid),
                                                      a.lo, a.hi, 0, 0, 0))
            for code in hat:
                ioc(fd, UI_ABS_SETUP, _ABS_SETUP.pack(code, 0, -1, 1, 0, 0, 0))
            
            s = self.spec
            ioc(fd, UI_DEV_SETUP, _SETUP.pack(BUS_USB, s.vendor, s.product, s.version,
                                              self.dev_name.encode()[:79], FF_EFFECTS_MAX))
            ioc(fd, UI_DEV_CREATE)             
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._created = True
        log.info(tr('uinput: virtual device %r created (%04x:%04x)'),
                 self.dev_name, s.vendor, s.product)

    def ensure_keys(self) -> None:
        if self._kbd is None:
            from .uinput_keyboard import VirtualKeyboard
            kbd = VirtualKeyboard(path=self.path, fd=self._kbd_fd)
            kbd.open()
            self._kbd = kbd

    def set_key(self, code: int, down: bool) -> None:
        if self._kbd is not None:
            self._kbd.set_key(code, down)

    def move_mouse(self, dx: int, dy: int) -> None:
        if self._kbd is not None:
            self._kbd.move(dx, dy)

    def close(self) -> None:
        if self._kbd is not None:
            self._kbd.close()
            self._kbd = None
        if self._loop is not None and self._fd >= 0:
            try:
                self._loop.remove_reader(self._fd)
            except Exception:  # noqa: BLE001
                pass
        for handles in self._timers.values():
            for h in handles:
                h.cancel()
        self._timers.clear()
        if self._created and self._fd >= 0:
            try:
                fcntl.ioctl(self._fd, UI_DEV_DESTROY)   
            except OSError:
                pass
            os.close(self._fd)
        self._fd = -1
        self._created = False

    def fileno(self) -> int:
        return self._fd

    # ---- Output API (hot path) ----
    def button_id(self, name: str) -> Optional[int]:
        return self._ids.get(name)

    def axis_id(self, name: str) -> int:
        return AXIS_ORDER.index(name)

    def set_button(self, bid: int, down: bool) -> None:
        self._btn_state[bid] = down
        code = self._codes[bid]
        if code >= 0:
            n = self._n
            if n >= len(self._buf) - 2 * EVSZ:
                self._spill()
                n = 0
            
            _EV.pack_into(self._buf, n, 0, 0, C.EV_KEY, code, 1 if down else 0)
            self._n = n + EVSZ
            return
        # D-pad: the four "buttons" combine into two hat axes (-1/0/1).
        k = bid - len(self.spec.buttons)
        d = self._dpad
        d[k] = 1 if down else 0
        x = d[3] - d[2]
        y = d[1] - d[0]
        if x != self._hat[0]:
            self._hat[0] = x
            self._put(C.EV_ABS, C.ABS_HAT0X, x)
        if y != self._hat[1]:
            self._hat[1] = y
            self._put(C.EV_ABS, C.ABS_HAT0Y, y)

    def _neutral_device_value(self, aid: int) -> int:
        """The neutral value of an axis in the units of the VIRTUAL device (the same formula as in set_axis)."""
        v = self._a_neutral[aid]
        if self._a_ident[aid]:
            return v
        cs = self._a_cspan[aid]
        return self._a_lo[aid] + ((v - self._a_clo[aid]) * self._a_span[aid] + cs // 2) // cs

    def set_axis(self, aid: int, value: int) -> None:
        self._a_state[aid] = value
        if not self._a_ident[aid]:
            
            cs = self._a_cspan[aid]
            value = self._a_lo[aid] + ((value - self._a_clo[aid]) * self._a_span[aid] + cs // 2) // cs
        self._put(C.EV_ABS, self._a_code[aid], value)

    def _put(self, etype: int, code: int, value: int) -> None:
        n = self._n
        if n >= len(self._buf) - 2 * EVSZ:
            self._spill()
            n = 0
        _EV.pack_into(self._buf, n, 0, 0, etype, code, value)
        self._n = n + EVSZ

    def flush(self) -> None:
        if self._kbd is not None:
            self._kbd.flush()                    
        n = self._n
        if not n:
            return
        
        _EV.pack_into(self._buf, n, 0, 0, C.EV_SYN, C.SYN_REPORT, 0)
        self._write(n + EVSZ)
        self._n = 0

    def _spill(self) -> None:
        """Emergency: the buffer is full without a SYN — we deliver what has accumulated (extremely rare)."""
        self._write(self._n)
        self._n = 0

    def _write(self, nbytes: int) -> None:
        try:
            os.write(self._fd, self._mv[:nbytes])    
        except BlockingIOError:                      
            self.dropped_writes += 1
        except OSError as e:
            if e.errno in (errno.EBADF, errno.ENODEV):
                self.dropped_writes += 1
            else:
                raise

    def release_all(self) -> None:
        for bid, down in enumerate(self._btn_state):
            if down:
                self.set_button(bid, False)
        for aid, v in enumerate(self._a_state):
            if v != self._a_neutral[aid]:
                self.set_axis(aid, self._a_neutral[aid])
        if self._kbd is not None:
            self._kbd.release_all()              
        self.flush()

    # ---- Force Feedback / Rumble passthrough -------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Subscribes to reads of the fd: the kernel sends the game's requests (upload/play/erase) here."""
        self._loop = loop
        if self._fd >= 0 and not self._external_fd:
            loop.add_reader(self._fd, self.service_ff)

    def service_ff(self) -> None:
        while True:
            try:
                data = os.read(self._fd, EVSZ * 16)
            except BlockingIOError:
                return                               
            except OSError:
                return
            if not data:
                return
            for off in range(0, len(data) - EVSZ + 1, EVSZ):
                _, _, etype, code, value = _EV.unpack_from(data, off)
                self.handle_ff_event(etype, code, value)

    def handle_ff_event(self, etype: int, code: int, value: int) -> None:
        if etype == C.EV_UINPUT:                    
            if code == C.UI_FF_UPLOAD:
                self._ff_upload(value)
            elif code == C.UI_FF_ERASE:
                self._ff_erase(value)
        elif etype == C.EV_FF:                      
            if code == C.FF_GAIN:
                self._gain = value & 0xFFFF
                self._recompute_rumble()
            else:
                self._ff_play(code, value)

    def _ff_upload(self, request_id: int) -> None:
        buf = bytearray(ctypes.sizeof(_UinputFFUpload))
        up = _UinputFFUpload.from_buffer(buf)        # a ctypes "view" onto the same buffer
        up.request_id = request_id
        fcntl.ioctl(self._fd, UI_BEGIN_FF_UPLOAD, buf, True)  
        eff = up.effect
        if eff.type == C.FF_RUMBLE:
            r = eff.u.rumble
            self._effects[eff.id] = (r.strong_magnitude, r.weak_magnitude,
                                     eff.replay.length, eff.replay.delay)
            up.retval = 0
            if self._playing.get(eff.id):
                self._recompute_rumble()             
        else:
            up.retval = -errno.EINVAL                
        fcntl.ioctl(self._fd, UI_END_FF_UPLOAD, buf, True)   

    def _ff_erase(self, request_id: int) -> None:
        buf = bytearray(ctypes.sizeof(_UinputFFErase))
        er = _UinputFFErase.from_buffer(buf)
        er.request_id = request_id
        fcntl.ioctl(self._fd, UI_BEGIN_FF_ERASE, buf, True)
        self._effects.pop(er.effect_id, None)
        self._set_playing(er.effect_id, False)
        er.retval = 0
        fcntl.ioctl(self._fd, UI_END_FF_ERASE, buf, True)

    def _ff_play(self, eid: int, count: int) -> None:
        for h in self._timers.pop(eid, []):          
            h.cancel()
        eff = self._effects.get(eid)
        if count <= 0 or eff is None:
            self._set_playing(eid, False)
            return
        _, _, length, delay = eff
        loop = self._loop
        if delay and loop is not None:
            handles = [loop.call_later(delay / 1000.0, self._set_playing, eid, True)]
        else:
            self._set_playing(eid, True)
            handles = []
        if length and loop is not None:              # length==0 => plays until stopped
            handles.append(loop.call_later((delay + length * count) / 1000.0,
                                           self._set_playing, eid, False))
        if handles:
            self._timers[eid] = handles

    def _set_playing(self, eid: int, on: bool) -> None:
        self._playing[eid] = on
        self._recompute_rumble()

    def _recompute_rumble(self) -> None:
        strong = weak = 0
        for eid, on in self._playing.items():
            if on and eid in self._effects:
                s, w, _, _ = self._effects[eid]
                strong, weak = max(strong, s), max(weak, w)
        g = self._gain
        strong, weak = strong * g // 0xFFFF, weak * g // 0xFFFF
        if (strong, weak) != self._last_rumble:
            self._last_rumble = (strong, weak)
            cb = self.on_rumble
            if cb is not None:
                cb(strong, weak)                     # -> the physical device
