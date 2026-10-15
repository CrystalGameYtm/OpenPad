"""Розбір HID Report Descriptor (stdlib, без залежностей).

Навіщо: пристрій сам описує формат своїх звітів — які в ньому осі, кнопки та
hat-перемикач, у яких бітах вони лежать і в яких межах змінюються. Розібравши
дескриптор, ми можемо автоматично скласти розкладку (див. ``autolayout``) для
будь-якого стандартного HID-геймпада, не знаючи його VID/PID заздалегідь.

Джерела дескриптора:
  * Linux:   ``/sys/class/hidraw/hidrawN/device/report_descriptor`` (читається без прав root);
  * Windows: потребує HID-API рівня HidP_* (поки не реалізовано — див. README етапу 1.5).

Специфікація: «Device Class Definition for HID 1.11», розділ 6.2.2 (формат елементів).
Кожен елемент дескриптора — це префікс-байт ``tag(4) | type(2) | size(2)`` + 0/1/2/4
байти даних. Тип: 0 = Main, 1 = Global, 2 = Local. Global-стан діє, поки його не
змінять (його можна покласти на стек через Push/Pop), Local-стан скидається після
кожного Main-елемента.

Усі зміщення ``bit_offset`` у ``HidField`` рахуються від початку ДАНИХ звіту, тобто
БЕЗ байта Report ID. Якщо дескриптор використовує Report ID, то в сирому звіті,
який повертає ``read()``, перший байт — це ID, і дані починаються з байта 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple


class DescriptorError(ValueError):
    """Дескриптор пошкоджений або обрізаний."""


# ---- Usage Page / Usage, потрібні для геймпадів ------------------------------
PAGE_GENERIC_DESKTOP = 0x01
PAGE_SIMULATION = 0x02
PAGE_BUTTON = 0x09
PAGE_CONSUMER = 0x0C
PAGE_VENDOR_MIN = 0xFF00            # 0xFF00..0xFFFF — «вендорські» сторінки

USAGE_JOYSTICK = 0x04
USAGE_GAMEPAD = 0x05
USAGE_MULTI_AXIS = 0x08
USAGE_X, USAGE_Y, USAGE_Z = 0x30, 0x31, 0x32
USAGE_RX, USAGE_RY, USAGE_RZ = 0x33, 0x34, 0x35
USAGE_SLIDER, USAGE_DIAL, USAGE_WHEEL = 0x36, 0x37, 0x38
USAGE_HAT = 0x39
USAGE_SIM_ACCELERATOR, USAGE_SIM_BRAKE = 0xC4, 0xC5   # тригери на Xbox One (BT)

USAGE_NAMES = {
    (PAGE_GENERIC_DESKTOP, 0x30): "X", (PAGE_GENERIC_DESKTOP, 0x31): "Y",
    (PAGE_GENERIC_DESKTOP, 0x32): "Z", (PAGE_GENERIC_DESKTOP, 0x33): "Rx",
    (PAGE_GENERIC_DESKTOP, 0x34): "Ry", (PAGE_GENERIC_DESKTOP, 0x35): "Rz",
    (PAGE_GENERIC_DESKTOP, 0x36): "Slider", (PAGE_GENERIC_DESKTOP, 0x37): "Dial",
    (PAGE_GENERIC_DESKTOP, 0x38): "Wheel", (PAGE_GENERIC_DESKTOP, 0x39): "Hat",
    (PAGE_SIMULATION, 0xC4): "Accelerator", (PAGE_SIMULATION, 0xC5): "Brake",
}

# Main-елементи (tag)
_INPUT, _OUTPUT, _COLLECTION, _FEATURE, _END_COLLECTION = 0x8, 0x9, 0xA, 0xB, 0xC
# Прапорці Input-елемента (біти даних)
FLAG_CONSTANT, FLAG_VARIABLE, FLAG_RELATIVE = 0x01, 0x02, 0x04

_MAX_ITEM_BITS = 8 * 4096           # захист від абсурдних Report Size * Report Count


# --------------------------------------------------------------------------
# Результат розбору
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HidField:
    """Одне змінне поле Input-звіту (вісь, кнопка, hat, вендорське значення)."""
    report_id: int          # 0 = дескриптор не використовує Report ID
    bit_offset: int         # від початку даних звіту (без байта ID)
    bit_size: int
    usage_page: int
    usage: int
    lmin: int               # Logical Minimum
    lmax: int               # Logical Maximum
    flags: int              # прапорці Input-елемента
    app: int                # індекс верхньорівневої collection (-1 = поза нею)

    @property
    def signed(self) -> bool:
        # Спец. дозволяє від'ємні межі лише з доповнювальним кодом.
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
    applications: List[Tuple[int, int]] = field(default_factory=list)  # (page, usage) верхніх collection
    uses_report_ids: bool = False
    input_bits: Dict[int, int] = field(default_factory=dict)    # report_id -> розмір даних у бітах
    output_bits: Dict[int, int] = field(default_factory=dict)
    feature_bits: Dict[int, int] = field(default_factory=dict)

    def input_report_len(self, report_id: int) -> int:
        """Довжина сирого звіту в байтах, яку поверне read() (з байтом ID, якщо він є)."""
        bits = self.input_bits.get(report_id, 0)
        return (1 if self.uses_report_ids else 0) + (bits + 7) // 8


@dataclass
class _Globals:
    usage_page: int = 0
    lmin: int = 0
    lmax_s: int = 0         # Logical Maximum як знакове
    lmax_u: int = 0         # ... і як беззнакове (див. розв'язання нижче)
    size: int = 0
    count: int = 0
    report_id: int = 0


# --------------------------------------------------------------------------
# Парсер
# --------------------------------------------------------------------------
def parse_report_descriptor(data: bytes) -> ParsedDescriptor:
    out = ParsedDescriptor()
    g = _Globals()
    stack: List[_Globals] = []
    usages: List[int] = []                 # повні usage: (page << 16) | id
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
            # Long item: [0xFE][bDataSize][bLongItemTag][data...]. У HID 1.11 жодних
            # long-елементів не визначено — просто пропускаємо.
            if i + 2 > n:
                raise DescriptorError("обрізаний long-елемент")
            i += 2 + data[i]
            if i > n:
                raise DescriptorError("обрізаний long-елемент")
            continue
        size = prefix & 3
        size = 4 if size == 3 else size    # код 3 означає 4 байти
        typ, tag = (prefix >> 2) & 3, prefix >> 4
        if i + size > n:
            raise DescriptorError(f"обрізаний елемент на байті {i - 1}")
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
                    raise DescriptorError(f"Report ID {u} поза межами 1..255")
                g.report_id = u
                out.uses_report_ids = True
            elif tag == 9:
                g.count = u
            elif tag == 10:                               # Push
                stack.append(replace(g))
            elif tag == 11:                               # Pop
                if not stack:
                    raise DescriptorError("Pop без Push")
                g = stack.pop()
        elif typ == 2:                                    # ---- Local
            # 1–2 байти: usage належить поточній Usage Page; 4 байти: повний (page<<16|id).
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
                    raise DescriptorError(f"занадто великий елемент ({total} біт)")
                # Поле створюємо лише для Input, змінних (Variable) і не-«набивки» (Constant).
                if tag == _INPUT and u & FLAG_VARIABLE and not u & FLAG_CONSTANT and g.size:
                    # Якщо Logical Min >= 0, Logical Max трактується як беззнакове
                    # (0xFF у 1 байті = 255, а не -1) — так роблять і драйвери ядра.
                    lmax = g.lmax_u if g.lmin >= 0 else g.lmax_s
                    ulist = usages
                    if umin is not None and umax is not None and umax >= umin:
                        ulist = list(range(umin, umin + min(g.count, umax - umin + 1)))
                    for k in range(g.count):
                        # Менше usage, ніж полів: останній usage діє на всі, що лишились.
                        uu = ulist[k] if k < len(ulist) else (ulist[-1] if ulist else 0)
                        out.inputs.append(HidField(g.report_id, pos + k * g.size, g.size,
                                                   uu >> 16, uu & 0xFFFF, g.lmin, lmax, u, cur_app))
                table[g.report_id] = pos + total
            elif tag == _COLLECTION:
                if depth == 0:                            # верхній рівень = «application»
                    first = usages[0] if usages else (umin or 0)
                    out.applications.append((first >> 16, first & 0xFFFF))
                    cur_app = len(out.applications) - 1
                depth += 1
            elif tag == _END_COLLECTION:
                if depth == 0:
                    raise DescriptorError("зайвий End Collection")
                depth -= 1
                if depth == 0:
                    cur_app = -1
            # Local-стан живе лише до кінця Main-елемента.
            usages, umin, umax = [], None, None
        # typ == 3 (reserved) ігноруємо
    return out


# --------------------------------------------------------------------------
# Допоміжне: витяг значення поля зі звіту (для тестів, інспекції, CLI)
# --------------------------------------------------------------------------
def extract_field(report: bytes, f: HidField, uses_report_ids: bool) -> int:
    """Значення поля в сирому звіті (як його повертає read())."""
    abs_bit = (8 if uses_report_ids else 0) + f.bit_offset
    start, end = abs_bit >> 3, (abs_bit + f.bit_size + 7) >> 3
    if end > len(report):
        raise ValueError("звіт коротший за поле")
    v = (int.from_bytes(report[start:end], "little") >> (abs_bit & 7)) & ((1 << f.bit_size) - 1)
    if f.signed and v >> (f.bit_size - 1):
        v -= 1 << f.bit_size
    return v


def format_fields(d: ParsedDescriptor) -> str:
    """Таблиця полів для CLI: ``gamepad-remapd autolayout --fields``."""
    apps = ", ".join(f"{p:04X}:{u:04X}" for p, u in d.applications) or "—"
    lines = [f"applications: {apps}   report_ids: {'так' if d.uses_report_ids else 'ні'}",
             f"{'rid':>3} {'bit':>5} {'size':>4}  {'usage':<14} {'min':>7} {'max':>7}  app"]
    for f in d.inputs:
        lines.append(f"{f.report_id:>3} {f.bit_offset:>5} {f.bit_size:>4}  {f.name:<14} "
                     f"{f.lmin:>7} {f.lmax:>7}  {f.app}")
    for rid, bits in sorted(d.input_bits.items()):
        lines.append(f"input report {rid}: {bits} біт даних, read() поверне {d.input_report_len(rid)} байт")
    return "\n".join(lines)
