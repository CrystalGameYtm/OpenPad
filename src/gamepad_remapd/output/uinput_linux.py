"""Linux: віртуальний геймпад через /dev/uinput.

Реалізація НЕ залежить від python-evdev: потрібні лише `os`, `fcntl`, `struct`
та `ctypes` зі стандартної бібліотеки. Це те саме ядерне evdev/uinput API, але:
  * усі події одного вхідного звіту (+ завершальний SYN_REPORT) збираються у
    попередньо виділений буфер та віддаються ядру ОДНИМ `write()`; python-evdev
    робить окремий системний виклик на кожну подію;
  * жодних алокацій на гарячому шляху (struct.pack_into у готовий буфер);
  * FF (вібрація) обробляється прямо тут: читаємо EV_UINPUT/EV_FF із того ж fd.

Номери ioctl кодуються через `util.IOC` (коректно для x86/ARM/RISC-V; на
PowerPC/MIPS/SPARC біти напрямку інші — там потрібен окремий порт констант).
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

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Структури ядра (linux/uinput.h, linux/input.h) через ctypes: ctypes сам
# дотримується C-вирівнювання, у т.ч. покажчика всередині ff_periodic_effect.
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
# struct uinput_abs_setup { __u16 code; /*2 байти паддингу*/ struct input_absinfo absinfo; }
_ABS_SETUP = struct.Struct("=H2xiiiiii")
# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# Нативне вирівнювання збігається з розкладкою ядра на поточній архітектурі.
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
EVENT_BUF_EVENTS = 256   # місткість буфера подій (з запасом: ≈40 подій на звіт)


# --------------------------------------------------------------------------
# Специфікації віртуальних пристроїв
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
    buttons: Dict[str, int]       # логічне ім'я -> evdev-код
    axes: Dict[str, AxisSpec]     # LX LY RX RY LT RT
    # fuzz/flat навмисно 0: deadzone ми вже застосували в Pipeline; ненульові
    # значення призвели б до повторної фільтрації ядром та joydev.


def _extras(first_code: int = C.BTN_TRIGGER_HAPPY1) -> Dict[str, int]:
    return {n: first_code + i for i, n in enumerate(C.EXTRA_BUTTONS)}


_STICK = (C.STICK_MIN, C.STICK_MAX)
_TRIG = (C.TRIGGER_MIN, C.TRIGGER_MAX)

# Xbox 360 (як драйвер xpad): X -> BTN_NORTH, Y -> BTN_WEST — так робить ядро
# (BTN_X == BTN_NORTH), і саме на це розраховані бази SDL/Steam.
XBOX360 = PadSpec(
    kind="xbox360", name="Gamepad Remapper Virtual X360 Pad",
    vendor=0x045E, product=0x028E, version=0x0110,
    buttons={"A": C.BTN_SOUTH, "B": C.BTN_EAST, "X": C.BTN_NORTH, "Y": C.BTN_WEST,
             "LB": C.BTN_TL, "RB": C.BTN_TR, "BACK": C.BTN_SELECT, "START": C.BTN_START,
             "GUIDE": C.BTN_MODE, "LS": C.BTN_THUMBL, "RS": C.BTN_THUMBR, **_extras()},
    axes={"LX": AxisSpec(C.ABS_X, *_STICK), "LY": AxisSpec(C.ABS_Y, *_STICK),
          "RX": AxisSpec(C.ABS_RX, *_STICK), "RY": AxisSpec(C.ABS_RY, *_STICK),
          "LT": AxisSpec(C.ABS_Z, *_TRIG), "RT": AxisSpec(C.ABS_RZ, *_TRIG)})

# DualShock 4 (як драйвер hid-sony/hid-playstation): X(квадрат) -> BTN_WEST,
# Y(трикутник) -> BTN_NORTH; правий стік на ABS_Z/ABS_RZ, тригери на ABS_RX/RY;
# усі осі 0..255 з центром 128.
DS4 = PadSpec(
    kind="ds4", name="Gamepad Remapper Virtual DS4 Pad",
    vendor=0x054C, product=0x09CC, version=0x8111,
    buttons={"A": C.BTN_SOUTH, "B": C.BTN_EAST, "X": C.BTN_WEST, "Y": C.BTN_NORTH,
             "LB": C.BTN_TL, "RB": C.BTN_TR, "BACK": C.BTN_SELECT, "START": C.BTN_START,
             "GUIDE": C.BTN_MODE, "LS": C.BTN_THUMBL, "RS": C.BTN_THUMBR, **_extras()},
    axes={"LX": AxisSpec(C.ABS_X, 0, 255), "LY": AxisSpec(C.ABS_Y, 0, 255),
          "RX": AxisSpec(C.ABS_Z, 0, 255), "RY": AxisSpec(C.ABS_RZ, 0, 255),
          "LT": AxisSpec(C.ABS_RX, 0, 255), "RT": AxisSpec(C.ABS_RY, 0, 255)})

SPECS: Dict[str, PadSpec] = {"xbox360": XBOX360, "ds4": DS4}
AXIS_ORDER = ("LX", "LY", "RX", "RY", "LT", "RT")   # id осі = індекс у цьому кортежі
_DPAD = C.DPAD_BUTTONS                               # id кнопок D-pad йдуть після звичайних


class UinputPad(OutputBackend):
    kind = "uinput"

    def __init__(self, spec: PadSpec = XBOX360, path: str = "/dev/uinput",
                 name: Optional[str] = None, fd: Optional[int] = None):
        """`fd` дозволяє підставити готовий файловий дескриптор (pipe у тестах/
        бенчмарку): тоді віртуальний пристрій НЕ створюється, події просто
        пишуться у цей fd у бінарному форматі struct input_event."""
        self.spec = spec
        self.path = path
        self.dev_name = name or spec.name
        self._fd = -1 if fd is None else fd
        self._external_fd = fd is not None
        self._created = False

        # id кнопок: спочатку кнопки з evdev-кодами, потім чотири напрямки D-pad.
        self._names: List[str] = list(spec.buttons) + list(_DPAD)
        self._ids = {n: i for i, n in enumerate(self._names)}
        self.n_buttons = len(self._names)
        self._codes = [spec.buttons.get(n, -1) for n in self._names]   # -1 => hat-напрямок
        self._btn_state = [False] * self.n_buttons
        self._dpad = [0, 0, 0, 0]          # UP DOWN LEFT RIGHT
        self._hat = [0, 0]                 # поточні ABS_HAT0X / ABS_HAT0Y

        # Таблиці осей за id: код, діапазон пристрою та параметри перемасштабування.
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

        # Буфер подій: фіксований, щоб memoryview не блокував зміну розміру.
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

    # ---- життєвий цикл --------------------------------------------------------
    def open(self) -> None:
        if self._external_fd:
            return
        # O_NONBLOCK: той самий fd і пишемо в нього, і читаємо FF-запити через epoll.
        fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            ioc = fcntl.ioctl
            ioc(fd, UI_SET_EVBIT, C.EV_KEY)    # оголошуємо типи подій, які пристрій вміє
            ioc(fd, UI_SET_EVBIT, C.EV_ABS)
            ioc(fd, UI_SET_EVBIT, C.EV_FF)
            for code in sorted({c for c in self._codes if c >= 0}):
                ioc(fd, UI_SET_KEYBIT, code)   # кожна кнопка
            abs_codes = [self.spec.axes[n].code for n in AXIS_ORDER] + [C.ABS_HAT0X, C.ABS_HAT0Y]
            for code in abs_codes:
                ioc(fd, UI_SET_ABSBIT, code)   # кожна вісь
            ioc(fd, UI_SET_FFBIT, C.FF_RUMBLE)  # вібромотори
            ioc(fd, UI_SET_FFBIT, C.FF_GAIN)    # гучність вібрації
            # Діапазони осей. fuzz=flat=0 — deadzone вже застосовано в Pipeline.
            # Початкове значення — НЕЙТРАЛЬ у одиницях пристрою (для DS4 центр стіка = 128,
            # а не 0): інакше гра, що читає стан осей до першої події, побачить стік
            # «вдавленим» у кут, доки користувач його не ворухне.
            for aid, n in enumerate(AXIS_ORDER):
                a = self.spec.axes[n]
                ioc(fd, UI_ABS_SETUP, _ABS_SETUP.pack(a.code, self._neutral_device_value(aid),
                                                      a.lo, a.hi, 0, 0, 0))
            for code in (C.ABS_HAT0X, C.ABS_HAT0Y):
                ioc(fd, UI_ABS_SETUP, _ABS_SETUP.pack(code, 0, -1, 1, 0, 0, 0))
            # Ідентичність пристрою (від неї залежить, як SDL/Steam його класифікують).
            s = self.spec
            ioc(fd, UI_DEV_SETUP, _SETUP.pack(BUS_USB, s.vendor, s.product, s.version,
                                              self.dev_name.encode()[:79], FF_EFFECTS_MAX))
            ioc(fd, UI_DEV_CREATE)             # пристрій з'являється в /dev/input/eventN
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._created = True
        log.info("uinput: створено віртуальний пристрій %r (%04x:%04x)",
                 self.dev_name, s.vendor, s.product)

    def close(self) -> None:
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
                fcntl.ioctl(self._fd, UI_DEV_DESTROY)   # зникає з /dev/input
            except OSError:
                pass
            os.close(self._fd)
        self._fd = -1
        self._created = False

    def fileno(self) -> int:
        return self._fd

    # ---- Output API (гарячий шлях) -----------------------------------------------
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
            # Записуємо подію прямо у готовий буфер: time=0 (ядро проставить само).
            _EV.pack_into(self._buf, n, 0, 0, C.EV_KEY, code, 1 if down else 0)
            self._n = n + EVSZ
            return
        # D-pad: чотири «кнопки» складаються у дві hat-осі (-1/0/1).
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
        """Нейтраль осі в одиницях ВІРТУАЛЬНОГО пристрою (та сама формула, що в set_axis)."""
        v = self._a_neutral[aid]
        if self._a_ident[aid]:
            return v
        cs = self._a_cspan[aid]
        return self._a_lo[aid] + ((v - self._a_clo[aid]) * self._a_span[aid] + cs // 2) // cs

    def set_axis(self, aid: int, value: int) -> None:
        self._a_state[aid] = value
        if not self._a_ident[aid]:
            # Канонічний діапазон -> діапазон пристрою (з округленням), напр. int16 -> 0..255.
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
        n = self._n
        if not n:
            return
        # Завершальний SYN_REPORT: ядро віддає споживачам усі події кадру разом.
        _EV.pack_into(self._buf, n, 0, 0, C.EV_SYN, C.SYN_REPORT, 0)
        self._write(n + EVSZ)
        self._n = 0

    def _spill(self) -> None:
        """Аварійно: буфер заповнено без SYN — віддаємо накопичене (надзвичайно рідко)."""
        self._write(self._n)
        self._n = 0

    def _write(self, nbytes: int) -> None:
        try:
            os.write(self._fd, self._mv[:nbytes])    # ОДИН системний виклик, без копіювання
        except BlockingIOError:                      # черга uinput переповнена: відкидаємо кадр
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
        self.flush()

    # ---- Force Feedback / Rumble passthrough -------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Підписуємось на читання fd: ядро шле сюди запити гри (upload/play/erase)."""
        self._loop = loop
        if self._fd >= 0 and not self._external_fd:
            loop.add_reader(self._fd, self.service_ff)

    def service_ff(self) -> None:
        while True:
            try:
                data = os.read(self._fd, EVSZ * 16)
            except BlockingIOError:
                return                               # подій більше немає
            except OSError:
                return
            if not data:
                return
            for off in range(0, len(data) - EVSZ + 1, EVSZ):
                _, _, etype, code, value = _EV.unpack_from(data, off)
                self.handle_ff_event(etype, code, value)

    def handle_ff_event(self, etype: int, code: int, value: int) -> None:
        if etype == C.EV_UINPUT:                    # ядро просить прийняти/стерти ефект
            if code == C.UI_FF_UPLOAD:
                self._ff_upload(value)
            elif code == C.UI_FF_ERASE:
                self._ff_erase(value)
        elif etype == C.EV_FF:                      # гра керує вже завантаженим ефектом
            if code == C.FF_GAIN:
                self._gain = value & 0xFFFF
                self._recompute_rumble()
            else:
                self._ff_play(code, value)

    def _ff_upload(self, request_id: int) -> None:
        buf = bytearray(ctypes.sizeof(_UinputFFUpload))
        up = _UinputFFUpload.from_buffer(buf)        # ctypes-«вікно» на той самий буфер
        up.request_id = request_id
        fcntl.ioctl(self._fd, UI_BEGIN_FF_UPLOAD, buf, True)  # ядро заповнює up.effect
        eff = up.effect
        if eff.type == C.FF_RUMBLE:
            r = eff.u.rumble
            self._effects[eff.id] = (r.strong_magnitude, r.weak_magnitude,
                                     eff.replay.length, eff.replay.delay)
            up.retval = 0
            if self._playing.get(eff.id):
                self._recompute_rumble()             # ефект змінили «на льоту»
        else:
            up.retval = -errno.EINVAL                # інші типи ми не оголошували
        fcntl.ioctl(self._fd, UI_END_FF_UPLOAD, buf, True)   # підтверджуємо ядру

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
        for h in self._timers.pop(eid, []):          # скасовуємо попередні таймери ефекту
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
        if length and loop is not None:              # length==0 => грає, доки не зупинять
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
                cb(strong, weak)                     # -> фізичний пристрій
