"""Parsing of the HID Report Descriptor (stdlib, no dependencies).

Why: the device describes the format of its own reports — which axes, buttons and hat switch it has, which bits
they occupy and the ranges their values change in. By parsing the descriptor we can automatically build a layout (see ``autolayout``)
for any standard HID gamepad without knowing its VID/PID in advance.

Descriptor sources:
  * Linux:   ``/sys/class/hidraw/hidrawN/device/report_descriptor`` (readable without root);
  * Windows: needs the HidP_* level of the HID API (not implemented yet — see the stage 1.5 README).

Specification: "Device Class Definition for HID 1.11", section 6.2.2 (item format).
Each descriptor item is a prefix byte ``tag(4) | type(2) | size(2)`` + 0/1/2/4
data bytes. Type: 0 = Main, 1 = Global, 2 = Local. Global state stays in effect until
changed (it can be pushed/popped on a stack via Push/Pop); Local state is reset
after every Main item.

All ``bit_offset`` values in ``HidField`` are counted from the start of the report DATA, i.e.
WITHOUT the Report ID byte. If the descriptor uses a Report ID, then in the raw report
returned by ``read()`` the first byte is the ID and the data starts at byte 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple
from ..i18n import tr


class DescriptorError(ValueError):
    """The descriptor is corrupt or truncated."""


# ---- Usage Page / Usage needed for gamepads ----
PAGE_GENERIC_DESKTOP = 0x01
PAGE_SIMULATION = 0x02
PAGE_BUTTON = 0x09
PAGE_CONSUMER = 0x0C
PAGE_VENDOR_MIN = 0xFF00            # 0xFF00..0xFFFF — "vendor" pages

USAGE_JOYSTICK = 0x04
USAGE_GAMEPAD = 0x05
USAGE_MULTI_AXIS = 0x08
USAGE_X, USAGE_Y, USAGE_Z = 0x30, 0x31, 0x32
USAGE_RX, USAGE_RY, USAGE_RZ = 0x33, 0x34, 0x35
USAGE_SLIDER, USAGE_DIAL, USAGE_WHEEL = 0x36, 0x37, 0x38
USAGE_HAT = 0x39
USAGE_SIM_ACCELERATOR, USAGE_SIM_BRAKE = 0xC4, 0xC5   

USAGE_NAMES = {
    (PAGE_GENERIC_DESKTOP, 0x30): "X", (PAGE_GENERIC_DESKTOP, 0x31): "Y",
    (PAGE_GENERIC_DESKTOP, 0x32): "Z", (PAGE_GENERIC_DESKTOP, 0x33): "Rx",
    (PAGE_GENERIC_DESKTOP, 0x34): "Ry", (PAGE_GENERIC_DESKTOP, 0x35): "Rz",
    (PAGE_GENERIC_DESKTOP, 0x36): "Slider", (PAGE_GENERIC_DESKTOP, 0x37): "Dial",
    (PAGE_GENERIC_DESKTOP, 0x38): "Wheel", (PAGE_GENERIC_DESKTOP, 0x39): "Hat",
    (PAGE_SIMULATION, 0xC4): "Accelerator", (PAGE_SIMULATION, 0xC5): "Brake",
}

# Main items (tag)
_INPUT, _OUTPUT, _COLLECTION, _FEATURE, _END_COLLECTION = 0x8, 0x9, 0xA, 0xB, 0xC

FLAG_CONSTANT, FLAG_VARIABLE, FLAG_RELATIVE = 0x01, 0x02, 0x04

_MAX_ITEM_BITS = 8 * 4096           


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HidField:
    """One variable field of an Input report (axis, button, hat, vendor value)."""
    report_id: int          # 0 = the descriptor does not use a Report ID
    bit_offset: int         
    bit_size: int
    usage_page: int
    usage: int
    lmin: int               # Logical Minimum
    lmax: int               # Logical Maximum
    flags: int              
    app: int                

    @property
    def signed(self) -> bool:
        
        return self.lmin < 0

    @property
    def relative(self) -> bool:
        return bool(self.flags & FLAG_RELATIVE)

    @property
    def name(self) -> str:
        n = USAGE_NAMES.get((self.usage_page, self.usage))
        if n:
            return n
        if self.usage_page == PAGE_BUTTON:
            return f"Button{self.usage}"
        return f"{self.usage_page:04X}:{self.usage:04X}"


@dataclass
class ParsedDescriptor:
    inputs: List[HidField] = field(default_factory=list)
    applications: List[Tuple[int, int]] = field(default_factory=list)  # (page, usage) of the top-level collections
    uses_report_ids: bool = False
    input_bits: Dict[int, int] = field(default_factory=dict)    # report_id -> data size in bits
    output_bits: Dict[int, int] = field(default_factory=dict)
    feature_bits: Dict[int, int] = field(default_factory=dict)

    def input_report_len(self, report_id: int) -> int:
        """The length of the raw report in bytes, as returned by read() (including the ID byte, if there is one)."""
        bits = self.input_bits.get(report_id, 0)
        return (1 if self.uses_report_ids else 0) + (bits + 7) // 8


@dataclass
class _Globals:
    usage_page: int = 0
    lmin: int = 0
    lmax_s: int = 0         # Logical Maximum as signed
    lmax_u: int = 0         # ... and as unsigned (see the resolution below)
    size: int = 0
    count: int = 0
    report_id: int = 0


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
def parse_report_descriptor(data: bytes) -> ParsedDescriptor:
    out = ParsedDescriptor()
    g = _Globals()
    stack: List[_Globals] = []
    usages: List[int] = []                 
    umin: Optional[int] = None
    umax: Optional[int] = None
    depth = 0
    cur_app = -1
    offsets = {_INPUT: out.input_bits, _OUTPUT: out.output_bits, _FEATURE: out.feature_bits}

    n, i = len(data), 0
    while i < n:
        prefix = data[i]
        i += 1
        if prefix == 0xFE:
            # Long item: [0xFE][bDataSize][bLongItemTag][data...]. HID 1.11 defines no
            # long items at all — we simply skip them.
            if i + 2 > n:
                raise DescriptorError(tr('truncated long item'))
            i += 2 + data[i]
            if i > n:
                raise DescriptorError(tr('truncated long item'))
            continue
        size = prefix & 3
        size = 4 if size == 3 else size    
        typ, tag = (prefix >> 2) & 3, prefix >> 4
        if i + size > n:
            raise DescriptorError(tr('truncated item at byte {0}', i - 1))
        raw = data[i:i + size]
        i += size
        u = int.from_bytes(raw, "little")
        s = int.from_bytes(raw, "little", signed=True)   # b"" -> 0

        if typ == 1:                                      # ---- Global
            if tag == 0:
                g.usage_page = u
            elif tag == 1:
                g.lmin = s
            elif tag == 2:
                g.lmax_s, g.lmax_u = s, u
            elif tag == 7:
                g.size = u
            elif tag == 8:
                if not 1 <= u <= 255:
                    raise DescriptorError(tr('Report ID {0} is outside 1..255', u))
                g.report_id = u
                out.uses_report_ids = True
            elif tag == 9:
                g.count = u
            elif tag == 10:                               # Push
                stack.append(replace(g))
            elif tag == 11:                               # Pop
                if not stack:
                    raise DescriptorError(tr('Pop without Push'))
                g = stack.pop()
        elif typ == 2:                                    # ---- Local
            # 1–2 bytes: the usage belongs to the current Usage Page; 4 bytes: full (page<<16|id).
            full = u if size == 4 else (g.usage_page << 16) | (u & 0xFFFF)
            if tag == 0:
                usages.append(full)
            elif tag == 1:
                umin = full
            elif tag == 2:
                umax = full
        elif typ == 0:                                    # ---- Main
            if tag in (_INPUT, _OUTPUT, _FEATURE):
                table = offsets[tag]
                pos = table.get(g.report_id, 0)
                total = g.size * g.count
                if total > _MAX_ITEM_BITS:
                    raise DescriptorError(tr('item too large ({0} bits)', total))
                
                if tag == _INPUT and u & FLAG_VARIABLE and not u & FLAG_CONSTANT and g.size:
                    
                    # (0xFF in 1 byte = 255, not -1) — kernel drivers do the same.
                    lmax = g.lmax_u if g.lmin >= 0 else g.lmax_s
                    ulist = usages
                    if umin is not None and umax is not None and umax >= umin:
                        ulist = list(range(umin, umin + min(g.count, umax - umin + 1)))
                    for k in range(g.count):
                        
                        uu = ulist[k] if k < len(ulist) else (ulist[-1] if ulist else 0)
                        out.inputs.append(HidField(g.report_id, pos + k * g.size, g.size,
                                                   uu >> 16, uu & 0xFFFF, g.lmin, lmax, u, cur_app))
                table[g.report_id] = pos + total
            elif tag == _COLLECTION:
                if depth == 0:                            
                    first = usages[0] if usages else (umin or 0)
                    out.applications.append((first >> 16, first & 0xFFFF))
                    cur_app = len(out.applications) - 1
                depth += 1
            elif tag == _END_COLLECTION:
                if depth == 0:
                    raise DescriptorError(tr('extra End Collection'))
                depth -= 1
                if depth == 0:
                    cur_app = -1
            # Local state lives only until the end of the Main item.
            usages, umin, umax = [], None, None
        # typ == 3 (reserved) is ignored
    return out


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
def extract_field(report: bytes, f: HidField, uses_report_ids: bool) -> int:
    """The value of a field in the raw report (as returned by read())."""
    abs_bit = (8 if uses_report_ids else 0) + f.bit_offset
    start, end = abs_bit >> 3, (abs_bit + f.bit_size + 7) >> 3
    if end > len(report):
        raise ValueError(tr('report is shorter than the field'))
    v = (int.from_bytes(report[start:end], "little") >> (abs_bit & 7)) & ((1 << f.bit_size) - 1)
    if f.signed and v >> (f.bit_size - 1):
        v -= 1 << f.bit_size
    return v


def format_fields(d: ParsedDescriptor) -> str:
    """Table of fields for the CLI: ``gamepad-remapd autolayout --fields``."""
    apps = ", ".join(f"{p:04X}:{u:04X}" for p, u in d.applications) or "—"
    lines = [f"applications: {apps}   report_ids: {tr('yes') if d.uses_report_ids else tr('no')}",
             f"{'rid':>3} {'bit':>5} {'size':>4}  {'usage':<14} {'min':>7} {'max':>7}  app"]
    for f in d.inputs:
        lines.append(f"{f.report_id:>3} {f.bit_offset:>5} {f.bit_size:>4}  {f.name:<14} "
                     f"{f.lmin:>7} {f.lmax:>7}  {f.app}")
    for rid, bits in sorted(d.input_bits.items()):
        lines.append(tr('input report {0}: {1} data bits, read() will return {2} bytes', rid, bits, d.input_report_len(rid)))
    return "\n".join(lines)
