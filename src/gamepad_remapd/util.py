"""Дрібні утиліти без залежностей."""
from __future__ import annotations

import re
from typing import Iterable

_HEX_SPLIT = re.compile(r"[\s,:;]+")


def parse_hex(text: str) -> bytes:
    """Розбирає рядок на кшталт ``"05 07 0x00,ff"`` у байти.

    Дозволені роздільники: пробіл, кома, двокрапка, крапка з комою; байти
    можуть мати префікс ``0x``. Некоректний ввід -> ValueError.
    """
    out = bytearray()
    for tok in _HEX_SPLIT.split(text.strip()):
        if not tok:
            continue
        if tok.lower().startswith("0x"):
            tok = tok[2:]
        if not 1 <= len(tok) <= 2:
            raise ValueError(f"некоректний hex-байт: {tok!r}")
        out.append(int(tok, 16))
    return bytes(out)


def hex_str(data: Iterable[int]) -> str:
    """Байти -> ``"aa bb cc"`` (нижній регістр) для логів та UI."""
    return " ".join(f"{b:02x}" for b in data)


def parse_id(value) -> int | None:
    """Приймає ``0x045e`` / ``"045e"`` / ``1118`` / None -> int | None.

    Рядки без префікса трактуються як шістнадцяткові (так записують VID/PID
    в udev та lsusb).
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    if not s:
        return None
    return int(s, 16)


# --------------------------------------------------------------------------
# Кодування номерів ioctl (asm-generic/ioctl.h): dir(2 біти) | size(14) | type(8) | nr(8).
# Коректно для x86/ARM/RISC-V; на PowerPC/MIPS/SPARC біти напрямку інші.
# --------------------------------------------------------------------------
IOC_WRITE, IOC_READ = 1, 2


def IOC(direction: int, typ: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def IO(typ: str, nr: int) -> int:
    return IOC(0, typ, nr, 0)


def IOR(typ: str, nr: int, size: int) -> int:
    return IOC(IOC_READ, typ, nr, size)


def IOW(typ: str, nr: int, size: int) -> int:
    return IOC(IOC_WRITE, typ, nr, size)


def IOWR(typ: str, nr: int, size: int) -> int:
    return IOC(IOC_WRITE | IOC_READ, typ, nr, size)
