"""Small dependency-free utilities."""
from __future__ import annotations

import re
from typing import Iterable
from .i18n import tr

_HEX_SPLIT = re.compile(r"[\s,:;]+")


def parse_hex(text: str) -> bytes:
    """Parses a string like ``"05 07 0x00,ff"`` into bytes.

        Allowed separators: space, comma, colon, semicolon; bytes may have a ``0x``
        prefix. Invalid input -> ValueError.
    """
    out = bytearray()
    for tok in _HEX_SPLIT.split(text.strip()):
        if not tok:
            continue
        if tok.lower().startswith("0x"):
            tok = tok[2:]
        if not 1 <= len(tok) <= 2:
            raise ValueError(tr('invalid hex byte: {0!r}', tok))
        out.append(int(tok, 16))
    return bytes(out)


def hex_str(data: Iterable[int]) -> str:
    """Bytes -> ``"aa bb cc"`` (lowercase) for logs and the UI."""
    return " ".join(f"{b:02x}" for b in data)


def parse_id(value) -> int | None:
    """Accepts ``0x045e`` / ``"045e"`` / ``1118`` / None -> int | None.

        Strings without a prefix are read as hexadecimal (the way VID/PID are written
        in udev and lsusb).
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


class NotFoundError(Exception):
    """Marker base: "no such device/profile/layout" (the API answers 404 regardless of the message language)."""
