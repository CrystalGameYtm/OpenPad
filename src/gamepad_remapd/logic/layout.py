"""Description of a device's raw HID report format (the JSON "layout") and its parser.

A layout is DATA, not code: to add a new gamepad or receiver, it is enough to
describe which byte/bit holds each button and at which offsets the axes are.
All ``byte`` / ``offset`` values are indexes into the byte array returned by
``read()`` (i.e. INCLUDING the Report ID byte if the device uses one).

The parser (`ReportParser`) is compiled once and on the hot path does:
  * ONE ``int.from_bytes`` call for the whole button area -> bit mask;
  * ONE ``struct.Struct.unpack_from`` call for all 8/16/32-bit fields;
  * ONE LUT index for the hat switch (D-pad).
No intermediate objects, dicts or frameworks.
"""
from __future__ import annotations

import json
import zlib
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..util import parse_hex, parse_id
from ..i18n import tr


class LayoutError(ValueError):
    """Error in a device description."""


# fmt -> (size, struct code (little-endian), signed, byte order)
_NUMERIC_FMT: Dict[str, Tuple[int, str, bool, str]] = {
    "u8": (1, "B", False, "le"), "s8": (1, "b", True, "le"),
    "u16le": (2, "H", False, "le"), "s16le": (2, "h", True, "le"),
    "u16be": (2, "H", False, "be"), "s16be": (2, "h", True, "be"),
    "u32le": (4, "I", False, "le"), "s32le": (4, "i", True, "le"),
}

# Hat8: values 0..7 clockwise, starting from "up".
_HAT8 = (("UP",), ("UP", "RIGHT"), ("RIGHT",), ("DOWN", "RIGHT"),
         ("DOWN",), ("DOWN", "LEFT"), ("LEFT",), ("UP", "LEFT"))
_HAT_BIT = {"UP": 1, "DOWN": 2, "LEFT": 4, "RIGHT": 8}

HAT_SOURCE_NAMES = ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")
HAT_DEFAULT_TARGETS = {"DPAD_UP": "UP", "DPAD_DOWN": "DOWN",
                       "DPAD_LEFT": "LEFT", "DPAD_RIGHT": "RIGHT"}


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ButtonDef:
    name: str
    byte: int
    bit: int
    target: Optional[str]  

    @property
    def abs_bit(self) -> int:
        """Absolute bit position in the report (little-endian numbering)."""
        return self.byte * 8 + self.bit


@dataclass(frozen=True)
class FieldDef:
    """A numeric report field: axis, trigger, gyroscope, accelerometer, etc."""
    name: str
    offset: int
    fmt: Optional[str]          # None for a bit field
    bit_offset: int = 0         
    bits: int = 0               
    signed: bool = False
    raw_min: int = 0
    raw_max: int = 255
    center: float = 127.5
    invert: bool = False

    @property
    def half_range(self) -> float:
        """Half-range used to normalize a stick to [-1, 1] around the center."""
        return max(self.center - self.raw_min, self.raw_max - self.center) or 1.0

    @property
    def end_byte(self) -> int:
        """Index of the byte AFTER the last byte of the field (used for min_len)."""
        if self.fmt is not None:
            return self.offset + _NUMERIC_FMT[self.fmt][0]
        return self.offset + (self.bit_offset + self.bits + 7) // 8


@dataclass(frozen=True)
class HatDef:
    byte: int
    shift: int = 0
    mask: int = 0x0F
    first: int = 0  


@dataclass(frozen=True)
class HandshakeStep:
    op: str                 # feature_set | feature_get | output | sleep_ms
    data: bytes = b""
    report_id: int = 0
    length: int = 64
    ms: int = 0
    bus: Optional[int] = None   # 3 = USB only, 5 = Bluetooth only; None = always


_BUS_NAMES = {"usb": 3, "bluetooth": 5, "bt": 5}


def _parse_bus(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str) and value.lower() in _BUS_NAMES:
        return _BUS_NAMES[value.lower()]
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LayoutError(tr('match.bus: usb | bluetooth | a number, got {0!r}', value)) from None


@dataclass(frozen=True)
class ModeDef:
    """An operating mode of the device protocol. Layouts with the same `group` (and the same VID/PID) are modes of ONE
    gamepad (e.g. Switch Pro: the full 0x30 report with gyro / the simple 0x3F one). The mode is chosen in the UI.
    """
    group: str
    id: str
    label: str
    note: str = ""
    default: bool = False


@dataclass(frozen=True)
class RumbleSpec:
    """Output report template for the rumble motors of a specific device."""
    op: str                             # "output" | "feature"
    tokens: Tuple[Tuple[str, object], ...]
    pad_to: int = 0
    
    
    crc32_seed: Optional[bytes] = None
    crc32_at: int = 0

    def build(self, strong16: int, weak16: int) -> bytes:
        """Builds the report. Placeholders: {strong}/{weak} (u8), {strong16}/{weak16} (u16 LE)."""
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
        if self.crc32_seed is not None:
            if len(buf) < self.crc32_at + 4:
                buf.extend(bytes(self.crc32_at + 4 - len(buf)))
            crc = zlib.crc32(self.crc32_seed + bytes(buf[:self.crc32_at])) & 0xFFFFFFFF
            buf[self.crc32_at:self.crc32_at + 4] = crc.to_bytes(4, "little")
        return bytes(buf)


def _parse_rumble(d: dict) -> RumbleSpec:
    op = d.get("op", "output")
    if op not in ("output", "feature"):
        raise LayoutError(tr("rumble.op must be 'output' or 'feature'"))
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
        raise LayoutError(tr('rumble.template is empty'))
    seed, at = None, 0
    if d.get("crc32"):
        try:
            seed = parse_hex(str(d["crc32"].get("seed", "")))
            at = int(d["crc32"]["at"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise LayoutError(tr('rumble.crc32: expected {"seed": "a2", "at": 74}')) from None
        if at < 0:
            raise LayoutError("rumble.crc32.at < 0")
    return RumbleSpec(op, tuple(tokens), int(d.get("pad_to", 0)), seed, at)


def _parse_field(name: str, d: dict) -> FieldDef:
    if not isinstance(d, dict) or "offset" not in d:
        raise LayoutError(tr("fields.{0}: an object with 'offset' is required", name))
    offset = int(d["offset"])
    invert = bool(d.get("invert", False))
    if "bits" in d:  
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
            raise LayoutError(tr('fields.{0}.fmt: {1!r} is not one of {2}', name, fmt, sorted(_NUMERIC_FMT)))
        size, code, signed, _ = _NUMERIC_FMT[fmt]
        bits, bit_offset = 0, 0
        lo = -(1 << (8 * size - 1)) if signed else 0
        hi = (1 << (8 * size - 1)) - 1 if signed else (1 << (8 * size)) - 1
    raw_min = int(d.get("raw_min", lo))
    raw_max = int(d.get("raw_max", hi))
    if raw_max <= raw_min:
        raise LayoutError(tr('fields.{0}: raw_max must be > raw_min', name))
    center = float(d.get("center", (raw_min + raw_max) / 2))
    return FieldDef(name, offset, fmt, bit_offset, bits, signed, raw_min, raw_max, center, invert)


@dataclass
class DeviceLayout:
    id: str
    name: str
    vid: Optional[int] = None
    pid: Optional[int] = None            
    pids: Tuple[int, ...] = ()           
    interface: Optional[int] = None
    bus: Optional[int] = None            # 3 = USB, 5 = Bluetooth (match.bus: "usb"/"bluetooth"/number); None = any
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
    mode: Optional[ModeDef] = None
    source_path: Optional[str] = None
    
    
    
    generated: bool = False
    notes: List[str] = field(default_factory=list)
    # "Signature" of state reports: (byte index, value). Some devices send on the same
    # Report ID also service replies (e.g. to the handshake) that must not be parsed as state.
    sync: Optional[Tuple[int, int]] = None
    
    
    priority: int = 0
    learned: bool = False                
    inherited: List[str] = field(default_factory=list)   

    # ---- loading ----
    @classmethod
    def from_dict(cls, d: dict, layout_id: str) -> "DeviceLayout":
        if not isinstance(d, dict):
            raise LayoutError(tr('the layout root must be an object'))
        match = d.get("match") or {}
        # "pid" is a string ("09cc") or a list (["05c4", "09cc"]): one layout for several revisions.
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
            priority=int(d.get("priority", 0)),
            
            
            learned=(d.get("_learned") or {}).get("method") == "calibration" or int(d.get("priority", 0)) > 0,
        )
        for bname, bd in (d.get("buttons") or {}).items():
            if bname in HAT_SOURCE_NAMES:
                raise LayoutError(tr('buttons.{0}: the name is reserved for the hat', bname))
            if not isinstance(bd, dict) or "byte" not in bd or "bit" not in bd:
                raise LayoutError(tr("buttons.{0}: 'byte' and 'bit' are required", bname))
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
                raise LayoutError(tr('sticks: keys left/right, values [xField, yField]'))
            for n in pair:
                if n not in lay.fields:
                    raise LayoutError(tr('sticks.{0}: field {1!r} is not described in fields', side, n))
            lay.sticks[side] = (pair[0], pair[1])
        for side, fname in (d.get("triggers") or {}).items():
            if side not in ("left", "right") or fname not in lay.fields:
                raise LayoutError(tr('triggers.{0}: unknown field {1!r}', side, fname))
            lay.triggers[side] = fname
        g = d.get("gyro")
        if g:
            for axis in ("pitch", "yaw"):
                if g.get(axis) not in lay.fields:
                    raise LayoutError(tr('gyro.{0}: unknown field {1!r}', axis, g.get(axis)))
            lay.gyro = dict(g)
            lay.gyro.setdefault("dps_per_lsb", 1.0)
        sy = d.get("sync")
        if sy is not None:
            try:
                lay.sync = (int(sy["byte"]), parse_hex(str(sy["value"]))[0])
                if lay.sync[0] < 0:
                    raise ValueError("byte < 0")
            except (KeyError, ValueError, TypeError, IndexError):
                raise LayoutError(tr('sync: expected {"byte": N, "value": "FE"}')) from None
        m = d.get("mode")
        if m:
            try:
                lay.mode = ModeDef(str(m["group"]), str(m["id"]), str(m.get("label", m["id"])),
                                   str(m.get("note", "")), bool(m.get("default", False)))
            except (KeyError, TypeError):
                raise LayoutError(tr('mode: expected {"group": "...", "id": "...", "label": "..."}')) from None
        for i, st in enumerate(d.get("handshake") or []):
            op = st.get("op")
            if op not in ("feature_set", "feature_get", "output", "sleep_ms"):
                raise LayoutError(f"handshake[{i}].op: {op!r}")
            try:
                lay.handshake.append(HandshakeStep(
                    op, parse_hex(st.get("data", "")), int(st.get("report_id", 0)),
                    int(st.get("length", 64)), int(st.get("ms", 0)), _parse_bus(st.get("bus"))))
            except ValueError as e:
                raise LayoutError(f"handshake[{i}].data: {e}") from None
        if d.get("rumble"):
            lay.rumble = _parse_rumble(d["rumble"])
        
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
            raise LayoutError(tr('{0}: invalid JSON: {1}', path, e)) from None
        lay = cls.from_dict(data, Path(path).stem)
        lay.source_path = str(path)
        return lay

    @property
    def rumble_or_more(self) -> bool:
        """Has something to share (rumble, gyro or extra buttons)."""
        return bool(self.rumble or self.gyro or any(b.target in EXTRA_TARGETS for b in self.buttons.values()))

    def matches(self, vid: int, pid: int, interface: Optional[int], bus: Optional[int] = None) -> bool:
        if self.vid is None or not self.pids:
            return False  
        if self.vid != vid or pid not in self.pids:
            return False
        if self.bus is not None and bus is not None and self.bus != bus:
            return False  
        return self.interface is None or interface is None or self.interface == interface


EXTRA_TARGETS = ("M1", "M2", "M3", "M4", "M5", "M6", "C", "Z", "ML", "MR")


def _bit_span(fields) -> set:
    return {k for f in fields for k in range(f.offset, f.end_byte)}


def inherit_from_builtin(layouts: Dict[str, "DeviceLayout"]) -> None:
    """A layout built by the calibration wizard knows only the buttons and axes the user went through. Everything else
    usually known for this model (rumble, gyro, extra buttons the user skipped) is taken from the regular (priority 0)
    layout of the same VID/PID/Report ID — but only if it does not contradict the learned data (other bits/bytes).
    This way calibration does not "lose" rumble and gyro.
    """
    for lay in layouts.values():
        if not lay.learned or lay.vid is None or not lay.pids:
            continue
        refs = [r for r in layouts.values()
                if r is not lay and not r.learned and r.priority == 0 and r.rumble_or_more
                and r.vid == lay.vid and set(r.pids) & set(lay.pids)
                and (r.interface is None or lay.interface is None or r.interface == lay.interface)
                and (r.bus is None or lay.bus is None or r.bus == lay.bus)
                and (r.report_id is None or lay.report_id is None or r.report_id == lay.report_id)]
        if not refs:
            continue
        refs.sort(key=lambda r: (r.interface is None, r.bus is None))       
        ref = refs[0]
        got: List[str] = []
        if lay.rumble is None and ref.rumble is not None:
            lay.rumble = ref.rumble
            got.append("rumble")
        used = _bit_span(lay.fields.values())
        if not lay.gyro and ref.gyro:
            names = {v for v in ref.gyro.values() if isinstance(v, str)} | \
                {n for n in ref.fields if n.startswith(("GYRO", "ACC"))}
            add = {n: ref.fields[n] for n in names if n in ref.fields and n not in lay.fields}
            if add and not (_bit_span(add.values()) & used):
                lay.fields.update(add)
                lay.gyro = dict(ref.gyro)
                got.append("gyro")
                lay.min_len = max([lay.min_len] + [f.end_byte for f in add.values()])
        taken = {(b.byte, b.bit) for b in lay.buttons.values()}
        have_targets = {b.target for b in lay.buttons.values()}
        for name, b in ref.buttons.items():                                
            if b.target in EXTRA_TARGETS and b.target not in have_targets and name not in lay.buttons \
                    and (b.byte, b.bit) not in taken:
                lay.buttons[name] = b
                taken.add((b.byte, b.bit))
                got.append(name)
        if lay.min_len < max([b.byte + 1 for b in lay.buttons.values()] + [1]):
            lay.min_len = max(b.byte + 1 for b in lay.buttons.values())
        lay.inherited = got
        if got:
            lay.notes.append(tr('inherited from {0}: {1}', ref.id, ', '.join(got)))


def load_layouts(*dirs: Path) -> Dict[str, DeviceLayout]:
    """Loads all ``*.json`` files from the directories (later directories override earlier ones by id).
    Files starting with ``_`` are skipped (templates).
    """
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
    inherit_from_builtin(out)
    return out


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
class ReportParser:
    """The fastest report unpacker, compiled from a DeviceLayout.

        parse(raw) -> (src_word, vals)
          src_word : int where bit i == "input button #i is pressed". The bit index is the
                     position of the bit in the button area of the report; the hat directions
                     sit right after it (`hat_base`..`hat_base+3`). A Python int of arbitrary
                     length => NO limit on the number of simultaneously pressed buttons
                     (unlimited key rollover).
          vals     : tuple of numeric fields; `index` gives the position of a field by name.
    """

    __slots__ = ("layout", "rid", "sync", "min_len", "_rb0", "_rb1", "_hat", "_hat_byte",
                 "_hat_shift", "_hat_mask", "_hat_lut", "hat_base", "region_bits",
                 "_fast", "_slow", "index", "src_names", "name_to_bit", "fields")

    def accepts(self, raw: bytes) -> bool:
        """True if the report passes the same length / Report ID / sync filter as the hot path."""
        return not (len(raw) < self.min_len or (self.rid is not None and raw[0] != self.rid)
                    or (self.sync is not None and raw[self.sync[0]] != self.sync[1]))

    @property
    def is_distinctive(self) -> bool:
        """True if a Report ID or sync byte lets us tell state reports from other traffic."""
        return self.rid is not None or self.sync is not None

    def __init__(self, layout: DeviceLayout):
        self.layout = layout
        self.rid = layout.report_id
        self.sync = layout.sync
        self.min_len = layout.min_len

        # ---- buttons: one contiguous byte range -> a single int.from_bytes ----
        if layout.buttons:
            self._rb0 = min(b.byte for b in layout.buttons.values())
            self._rb1 = max(b.byte for b in layout.buttons.values()) + 1
        else:
            self._rb0 = self._rb1 = 0
        self.region_bits = (self._rb1 - self._rb0) * 8
        self.hat_base = self.region_bits  # hat bits follow right after the button area

        
        names: List[Optional[str]] = [None] * (self.region_bits + (4 if layout.hat else 0))
        for b in layout.buttons.values():
            idx = b.abs_bit - self._rb0 * 8
            if names[idx] is not None:
                raise LayoutError(tr('buttons {0} and {1} share one bit', names[idx], b.name))
            names[idx] = b.name
        # ---- hat: LUT "nibble value -> direction mask" ----
        self._hat = layout.hat
        self._hat_lut: Optional[List[int]] = None
        self._hat_byte = self._hat_shift = self._hat_mask = 0
        if layout.hat:
            h = layout.hat
            self._hat_byte, self._hat_shift, self._hat_mask = h.byte, h.shift, h.mask
            lut = [0] * (h.mask + 1)
            for v in range(h.mask + 1):
                n = v - h.first
                if 0 <= n < 8:  
                    mask = 0
                    for d in _HAT8[n]:
                        mask |= _HAT_BIT[d]
                    lut[v] = mask << self.hat_base
            self._hat_lut = lut
            for k, nm in enumerate(HAT_SOURCE_NAMES):
                names[self.hat_base + k] = nm
        self.src_names = names
        self.name_to_bit = {n: i for i, n in enumerate(names) if n}

        # ---- numeric fields ----
        self.fields = layout.fields
        fast: List[FieldDef] = []
        slow: List[FieldDef] = []
        pos = 0
        for f in sorted(layout.fields.values(), key=lambda f: f.offset):
            le_simple = f.fmt is not None and _NUMERIC_FMT[f.fmt][3] == "le"
            if le_simple and f.offset >= pos:
                fast.append(f)
                pos = f.offset + _NUMERIC_FMT[f.fmt][0]
            else:  # big-endian, bit-packed or overlapping fields take the slow path
                slow.append(f)
        fmt = "<"
        pos = 0
        for f in fast:
            if f.offset > pos:
                fmt += f"{f.offset - pos}x"  
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
        
        off, bo, bits, signed = f.offset, f.bit_offset, f.bits, f.signed
        nbytes = (bo + bits + 7) // 8
        mask = (1 << bits) - 1

        def read(raw, off=off, bo=bo, nbytes=nbytes, mask=mask, bits=bits, signed=signed):
            v = (int.from_bytes(raw[off:off + nbytes], "little") >> bo) & mask
            return v - (1 << bits) if signed and v >> (bits - 1) else v
        return read

    # ---- HOT PATH ----
    def parse(self, raw: bytes) -> Tuple[int, tuple]:
        
        word = int.from_bytes(raw[self._rb0:self._rb1], "little")
        lut = self._hat_lut
        if lut is not None:
            # Hat (D-pad): nibble -> LUT -> 4 extra bits after the button area.
            word |= lut[(raw[self._hat_byte] >> self._hat_shift) & self._hat_mask]
        
        vals = self._fast.unpack_from(raw)
        if self._slow:
            vals += tuple([f(raw) for f in self._slow])
        return word, vals
