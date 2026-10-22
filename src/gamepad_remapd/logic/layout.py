"""Опис формату сирого HID-звіту пристрою (JSON-«розкладка») та його парсер.

Розкладка — це ДАНІ, а не код: щоб додати новий геймпад/ресивер, достатньо
описати, в якому байті/біті лежить кожна кнопка та в яких зміщеннях — осі.
Усі зміщення ``byte`` / ``offset`` — індекси в масиві байтів, який повертає
``read()`` (тобто ВКЛЮЧНО з байтом Report ID, якщо пристрій його використовує).

Парсер (`ReportParser`) компілюється один раз і на гарячому шляху робить:
  * ОДИН виклик ``int.from_bytes`` для всієї області кнопок -> бітова маска;
  * ОДИН виклик ``struct.Struct.unpack_from`` для всіх 8/16/32-бітних полів;
  * ОДНЕ індексування LUT для hat-перемикача (D-pad).
Жодних проміжних об'єктів, словників чи фреймворків.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..util import parse_hex, parse_id


class LayoutError(ValueError):
    """Помилка в описі пристрою."""


# fmt -> (розмір, код struct (little-endian), signed, порядок байтів)
_NUMERIC_FMT: Dict[str, Tuple[int, str, bool, str]] = {
    "u8": (1, "B", False, "le"), "s8": (1, "b", True, "le"),
    "u16le": (2, "H", False, "le"), "s16le": (2, "h", True, "le"),
    "u16be": (2, "H", False, "be"), "s16be": (2, "h", True, "be"),
    "u32le": (4, "I", False, "le"), "s32le": (4, "i", True, "le"),
}

# Hat8: значення 0..7 по колу, починаючи з «вгору» за годинниковою стрілкою.
_HAT8 = (("UP",), ("UP", "RIGHT"), ("RIGHT",), ("DOWN", "RIGHT"),
         ("DOWN",), ("DOWN", "LEFT"), ("LEFT",), ("UP", "LEFT"))
_HAT_BIT = {"UP": 1, "DOWN": 2, "LEFT": 4, "RIGHT": 8}
# Імена «віртуальних» вхідних кнопок, які породжує hat; порядок = порядок бітів.
HAT_SOURCE_NAMES = ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")
HAT_DEFAULT_TARGETS = {"DPAD_UP": "UP", "DPAD_DOWN": "DOWN",
                       "DPAD_LEFT": "LEFT", "DPAD_RIGHT": "RIGHT"}


# --------------------------------------------------------------------------
# Дані розкладки
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ButtonDef:
    name: str
    byte: int
    bit: int
    target: Optional[str]  # цільова кнопка за замовчуванням (None = не мапити)

    @property
    def abs_bit(self) -> int:
        """Абсолютна позиція біта в звіті (little-endian нумерація)."""
        return self.byte * 8 + self.bit


@dataclass(frozen=True)
class FieldDef:
    """Числове поле звіту: вісь, тригер, гіроскоп, акселерометр тощо."""
    name: str
    offset: int
    fmt: Optional[str]          # None для бітового поля
    bit_offset: int = 0         # лише для бітових полів
    bits: int = 0               # лише для бітових полів
    signed: bool = False
    raw_min: int = 0
    raw_max: int = 255
    center: float = 127.5
    invert: bool = False

    @property
    def half_range(self) -> float:
        """Півдіапазон для нормалізації стіка в [-1, 1] відносно центру."""
        return max(self.center - self.raw_min, self.raw_max - self.center) or 1.0

    @property
    def end_byte(self) -> int:
        """Індекс байта ПІСЛЯ останнього байта поля (для min_len)."""
        if self.fmt is not None:
            return self.offset + _NUMERIC_FMT[self.fmt][0]
        return self.offset + (self.bit_offset + self.bits + 7) // 8


@dataclass(frozen=True)
class HatDef:
    byte: int
    shift: int = 0
    mask: int = 0x0F
    first: int = 0  # яке значення відповідає «вгору» (деякі пристрої нумерують з 1)


@dataclass(frozen=True)
class HandshakeStep:
    op: str                 # feature_set | feature_get | output | sleep_ms
    data: bytes = b""
    report_id: int = 0
    length: int = 64
    ms: int = 0


_BUS_NAMES = {"usb": 3, "bluetooth": 5, "bt": 5}


def _parse_bus(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str) and value.lower() in _BUS_NAMES:
        return _BUS_NAMES[value.lower()]
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LayoutError(f"match.bus: usb | bluetooth | число, отримано {value!r}") from None


@dataclass(frozen=True)
class RumbleSpec:
    """Шаблон вихідного звіту для вібромоторів конкретного пристрою."""
    op: str                             # "output" | "feature"
    tokens: Tuple[Tuple[str, object], ...]
    pad_to: int = 0

    def build(self, strong16: int, weak16: int) -> bytes:
        """Збирає звіт. Плейсхолдери: {strong}/{weak} (u8), {strong16}/{weak16} (u16 LE)."""
        buf = bytearray()
        for kind, val in self.tokens:
            if kind == "byte":
                buf.append(val)                                     # type: ignore[arg-type]
            elif val == "strong":
                buf.append(strong16 >> 8) if kind == "u8" else buf.extend(strong16.to_bytes(2, "little"))
            else:  # weak
                buf.append(weak16 >> 8) if kind == "u8" else buf.extend(weak16.to_bytes(2, "little"))
        if self.pad_to and len(buf) < self.pad_to:
            buf.extend(bytes(self.pad_to - len(buf)))
        return bytes(buf)


def _parse_rumble(d: dict) -> RumbleSpec:
    op = d.get("op", "output")
    if op not in ("output", "feature"):
        raise LayoutError("rumble.op має бути 'output' або 'feature'")
    tokens: List[Tuple[str, object]] = []
    for tok in str(d.get("template", "")).split():
        if tok in ("{strong}", "{weak}"):
            tokens.append(("u8", tok[1:-1]))
        elif tok in ("{strong16}", "{weak16}"):
            tokens.append(("u16", tok[1:-1]))
        else:
            try:
                tokens.extend(("byte", b) for b in parse_hex(tok))
            except ValueError as e:
                raise LayoutError(f"rumble.template: {e}") from None
    if not tokens:
        raise LayoutError("rumble.template порожній")
    return RumbleSpec(op, tuple(tokens), int(d.get("pad_to", 0)))


def _parse_field(name: str, d: dict) -> FieldDef:
    if not isinstance(d, dict) or "offset" not in d:
        raise LayoutError(f"fields.{name}: потрібен об'єкт з 'offset'")
    offset = int(d["offset"])
    invert = bool(d.get("invert", False))
    if "bits" in d:  # бітове поле довільної ширини (напр. 12-бітні осі)
        bits = int(d["bits"])
        bit_offset = int(d.get("bit_offset", 0))
        signed = bool(d.get("signed", False))
        if not 1 <= bits <= 32:
            raise LayoutError(f"fields.{name}.bits: 1..32")
        lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
        fmt = None
    else:
        fmt = d.get("fmt", "u8")
        if fmt not in _NUMERIC_FMT:
            raise LayoutError(f"fields.{name}.fmt: {fmt!r} не з {sorted(_NUMERIC_FMT)}")
        size, code, signed, _ = _NUMERIC_FMT[fmt]
        bits, bit_offset = 0, 0
        lo = -(1 << (8 * size - 1)) if signed else 0
        hi = (1 << (8 * size - 1)) - 1 if signed else (1 << (8 * size)) - 1
    raw_min = int(d.get("raw_min", lo))
    raw_max = int(d.get("raw_max", hi))
    if raw_max <= raw_min:
        raise LayoutError(f"fields.{name}: raw_max має бути > raw_min")
    center = float(d.get("center", (raw_min + raw_max) / 2))
    return FieldDef(name, offset, fmt, bit_offset, bits, signed, raw_min, raw_max, center, invert)


@dataclass
class DeviceLayout:
    id: str
    name: str
    vid: Optional[int] = None
    pid: Optional[int] = None            # перший (основний) PID — для повідомлень і сумісності
    pids: Tuple[int, ...] = ()           # усі PID, що підходять (match.pid може бути списком)
    interface: Optional[int] = None
    bus: Optional[int] = None            # 3 = USB, 5 = Bluetooth (match.bus: "usb"/"bluetooth"/число); None = будь-яка
    report_id: Optional[int] = None
    min_len: int = 0
    read_size: int = 64
    buttons: Dict[str, ButtonDef] = field(default_factory=dict)
    hat: Optional[HatDef] = None
    fields: Dict[str, FieldDef] = field(default_factory=dict)
    sticks: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    triggers: Dict[str, str] = field(default_factory=dict)
    gyro: Dict[str, object] = field(default_factory=dict)
    handshake: List[HandshakeStep] = field(default_factory=list)
    rumble: Optional[RumbleSpec] = None
    source_path: Optional[str] = None
    # Розкладка складена автоматично з HID-дескриптора (див. logic/autolayout.py): ролі осей і
    # імена кнопок — евристика, `notes` пояснюють, що перевірити. Збережена на диск розкладка
    # стає звичайною (generated=False).
    generated: bool = False
    notes: List[str] = field(default_factory=list)
    # «Сигнатура» корисних звітів: (індекс байта, значення). Деякі пристрої шлють на одному
    # Report ID і службові відповіді (напр. на handshake), які не можна парсити як стан.
    sync: Optional[Tuple[int, int]] = None

    # ---- завантаження ----------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict, layout_id: str) -> "DeviceLayout":
        if not isinstance(d, dict):
            raise LayoutError("корінь розкладки має бути об'єктом")
        match = d.get("match") or {}
        # "pid" — рядок ("09cc") або список (["05c4", "09cc"]): одна розкладка на кілька ревізій.
        raw_pid = match.get("pid")
        try:
            pids = tuple(parse_id(p) for p in raw_pid) if isinstance(raw_pid, list) \
                else (() if raw_pid is None else (parse_id(raw_pid),))
        except ValueError as e:
            raise LayoutError(f"match.pid: {e}") from None
        lay = cls(
            id=layout_id, name=str(d.get("name", layout_id)),
            vid=parse_id(match.get("vid")), pid=(pids[0] if pids else None), pids=pids,
            interface=(None if match.get("interface") is None else int(match["interface"])),
            bus=_parse_bus(match.get("bus")),
            report_id=(None if d.get("report_id") is None else int(d["report_id"])),
            min_len=int(d.get("min_len", 0)), read_size=int(d.get("read_size", 64)),
        )
        for bname, bd in (d.get("buttons") or {}).items():
            if bname in HAT_SOURCE_NAMES:
                raise LayoutError(f"buttons.{bname}: ім'я зарезервоване для hat")
            if not isinstance(bd, dict) or "byte" not in bd or "bit" not in bd:
                raise LayoutError(f"buttons.{bname}: потрібні 'byte' та 'bit'")
            byte, bit = int(bd["byte"]), int(bd["bit"])
            if byte < 0 or not 0 <= bit <= 7:
                raise LayoutError(f"buttons.{bname}: byte>=0, bit 0..7")
            lay.buttons[bname] = ButtonDef(bname, byte, bit, bd.get("target"))
        if d.get("hat"):
            h = d["hat"]
            lay.hat = HatDef(int(h["byte"]), int(h.get("shift", 0)),
                             int(h.get("mask", 0x0F)), int(h.get("first", 0)))
        for fname, fd in (d.get("fields") or {}).items():
            lay.fields[fname] = _parse_field(fname, fd)
        for side, pair in (d.get("sticks") or {}).items():
            if side not in ("left", "right") or len(pair) != 2:
                raise LayoutError("sticks: ключі left/right, значення [xField, yField]")
            for n in pair:
                if n not in lay.fields:
                    raise LayoutError(f"sticks.{side}: поле {n!r} не описане у fields")
            lay.sticks[side] = (pair[0], pair[1])
        for side, fname in (d.get("triggers") or {}).items():
            if side not in ("left", "right") or fname not in lay.fields:
                raise LayoutError(f"triggers.{side}: невідоме поле {fname!r}")
            lay.triggers[side] = fname
        g = d.get("gyro")
        if g:
            for axis in ("pitch", "yaw"):
                if g.get(axis) not in lay.fields:
                    raise LayoutError(f"gyro.{axis}: невідоме поле {g.get(axis)!r}")
            lay.gyro = dict(g)
            lay.gyro.setdefault("dps_per_lsb", 1.0)
        sy = d.get("sync")
        if sy is not None:
            try:
                lay.sync = (int(sy["byte"]), parse_hex(str(sy["value"]))[0])
                if lay.sync[0] < 0:
                    raise ValueError("byte < 0")
            except (KeyError, ValueError, TypeError, IndexError):
                raise LayoutError('sync: потрібно {"byte": N, "value": "FE"}') from None
        for i, st in enumerate(d.get("handshake") or []):
            op = st.get("op")
            if op not in ("feature_set", "feature_get", "output", "sleep_ms"):
                raise LayoutError(f"handshake[{i}].op: {op!r}")
            try:
                lay.handshake.append(HandshakeStep(
                    op, parse_hex(st.get("data", "")), int(st.get("report_id", 0)),
                    int(st.get("length", 64)), int(st.get("ms", 0))))
            except ValueError as e:
                raise LayoutError(f"handshake[{i}].data: {e}") from None
        if d.get("rumble"):
            lay.rumble = _parse_rumble(d["rumble"])
        # Мінімальна довжина звіту: все, до чого ми звертаємось, має в нього влізти.
        need = max([b.byte + 1 for b in lay.buttons.values()]
                   + ([lay.hat.byte + 1] if lay.hat else [])
                   + ([lay.sync[0] + 1] if lay.sync else [])
                   + [f.end_byte for f in lay.fields.values()] + [1])
        lay.min_len = max(lay.min_len, need)
        return lay

    @classmethod
    def from_file(cls, path: Path) -> "DeviceLayout":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise LayoutError(f"{path}: некоректний JSON: {e}") from None
        lay = cls.from_dict(data, Path(path).stem)
        lay.source_path = str(path)
        return lay

    def matches(self, vid: int, pid: int, interface: Optional[int], bus: Optional[int] = None) -> bool:
        if self.vid is None or not self.pids:
            return False  # розкладка без VID/PID обирається лише явно (daemon.json)
        if self.vid != vid or pid not in self.pids:
            return False
        if self.bus is not None and bus is not None and self.bus != bus:
            return False  # напр. USB-розкладка DS4 не підходить Bluetooth-підключенню (інший Report ID)
        return self.interface is None or interface is None or self.interface == interface


def load_layouts(*dirs: Path) -> Dict[str, DeviceLayout]:
    """Завантажує всі ``*.json`` з каталогів (пізніші каталоги перекривають
    ранішні за id). Файли, що починаються з ``_``, пропускаються (шаблони)."""
    out: Dict[str, DeviceLayout] = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            if p.name.startswith("_"):
                continue
            lay = DeviceLayout.from_file(p)
            out[lay.id] = lay
    return out


# --------------------------------------------------------------------------
# Компільований парсер звітів
# --------------------------------------------------------------------------
class ReportParser:
    """Максимально швидкий розпаковувач звітів, скомпільований з DeviceLayout.

    parse(raw) -> (src_word, vals)
      src_word : int, де біт i == «вхідна кнопка №i натиснута». Індекс біта —
                 це позиція біта в області кнопок звіту, а hat-напрямки
                 розташовані одразу за нею (`hat_base`..`hat_base+3`).
                 Python-int необмеженої довжини => ЖОДНИХ лімітів на кількість
                 одночасно натиснутих кнопок (unlimited key rollover).
      vals     : tuple числових полів; позицію поля за іменем дає `index`.
    """

    __slots__ = ("layout", "rid", "sync", "min_len", "_rb0", "_rb1", "_hat", "_hat_byte",
                 "_hat_shift", "_hat_mask", "_hat_lut", "hat_base", "region_bits",
                 "_fast", "_slow", "index", "src_names", "name_to_bit", "fields")

    def __init__(self, layout: DeviceLayout):
        self.layout = layout
        self.rid = layout.report_id
        self.sync = layout.sync
        self.min_len = layout.min_len

        # ---- кнопки: одна суцільна область байтів -> одне int.from_bytes ----
        if layout.buttons:
            self._rb0 = min(b.byte for b in layout.buttons.values())
            self._rb1 = max(b.byte for b in layout.buttons.values()) + 1
        else:
            self._rb0 = self._rb1 = 0
        self.region_bits = (self._rb1 - self._rb0) * 8
        self.hat_base = self.region_bits  # hat-біти йдуть одразу після області кнопок

        # Назви джерел за індексом біта (для UI, sniff та button_map).
        names: List[Optional[str]] = [None] * (self.region_bits + (4 if layout.hat else 0))
        for b in layout.buttons.values():
            idx = b.abs_bit - self._rb0 * 8
            if names[idx] is not None:
                raise LayoutError(f"кнопки {names[idx]} та {b.name} ділять один біт")
            names[idx] = b.name
        # ---- hat: LUT «значення нібла -> маска напрямків» ----
        self._hat = layout.hat
        self._hat_lut: Optional[List[int]] = None
        self._hat_byte = self._hat_shift = self._hat_mask = 0
        if layout.hat:
            h = layout.hat
            self._hat_byte, self._hat_shift, self._hat_mask = h.byte, h.shift, h.mask
            lut = [0] * (h.mask + 1)
            for v in range(h.mask + 1):
                n = v - h.first
                if 0 <= n < 8:  # поза 0..7 -> нейтраль (0 напрямків)
                    mask = 0
                    for d in _HAT8[n]:
                        mask |= _HAT_BIT[d]
                    lut[v] = mask << self.hat_base
            self._hat_lut = lut
            for k, nm in enumerate(HAT_SOURCE_NAMES):
                names[self.hat_base + k] = nm
        self.src_names = names
        self.name_to_bit = {n: i for i, n in enumerate(names) if n}

        # ---- числові поля ----
        self.fields = layout.fields
        fast: List[FieldDef] = []
        slow: List[FieldDef] = []
        pos = 0
        for f in sorted(layout.fields.values(), key=lambda f: f.offset):
            le_simple = f.fmt is not None and _NUMERIC_FMT[f.fmt][3] == "le"
            if le_simple and f.offset >= pos:
                fast.append(f)
                pos = f.offset + _NUMERIC_FMT[f.fmt][0]
            else:  # big-endian, бітові або перекриті поля — повільний шлях
                slow.append(f)
        fmt = "<"
        pos = 0
        for f in fast:
            if f.offset > pos:
                fmt += f"{f.offset - pos}x"  # пропуск байтів між полями
            fmt += _NUMERIC_FMT[f.fmt][1]
            pos = f.offset + _NUMERIC_FMT[f.fmt][0]
        self._fast = struct.Struct(fmt)
        self._slow: List[Callable[[bytes], int]] = [self._make_slow(f) for f in slow]
        self.index = {f.name: i for i, f in enumerate(fast + slow)}

    @staticmethod
    def _make_slow(f: FieldDef) -> Callable[[bytes], int]:
        if f.fmt is not None:
            size, code, signed, order = _NUMERIC_FMT[f.fmt]
            st = struct.Struct((">" if order == "be" else "<") + code)
            off = f.offset
            return lambda raw, st=st, off=off: st.unpack_from(raw, off)[0]
        # Бітове поле: вирізаємо потрібні байти, зсуваємо, маскуємо, знак.
        off, bo, bits, signed = f.offset, f.bit_offset, f.bits, f.signed
        nbytes = (bo + bits + 7) // 8
        mask = (1 << bits) - 1

        def read(raw, off=off, bo=bo, nbytes=nbytes, mask=mask, bits=bits, signed=signed):
            v = (int.from_bytes(raw[off:off + nbytes], "little") >> bo) & mask
            return v - (1 << bits) if signed and v >> (bits - 1) else v
        return read

    # ---- ГАРЯЧИЙ ШЛЯХ ----------------------------------------------------
    def parse(self, raw: bytes) -> Tuple[int, tuple]:
        # Одна операція: усі кнопки звіту -> одне ціле число (бітова маска).
        word = int.from_bytes(raw[self._rb0:self._rb1], "little")
        lut = self._hat_lut
        if lut is not None:
            # Hat (D-pad): нібл -> LUT -> 4 додаткові біти за областю кнопок.
            word |= lut[(raw[self._hat_byte] >> self._hat_shift) & self._hat_mask]
        # Одна операція: усі прості поля одним викликом C-коду.
        vals = self._fast.unpack_from(raw)
        if self._slow:
            vals += tuple([f(raw) for f in self._slow])
        return word, vals
