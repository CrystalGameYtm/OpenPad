"""Протокол каналу керування між демоном і Web-процесом.

Транспорт: NDJSON (один JSON-об'єкт на рядок, ``\\n``-роздільник).
  * Linux/macOS: UNIX-сокет ``unix:/run/user/1000/gamepad-remapd/control.sock``,
    права 0600 — доступ має лише власник (автентифікація = права файлової системи).
  * Windows (та за бажанням будь-де): ``tcp:127.0.0.1:8764`` + токен із файлу
    ``control.token`` (перше повідомлення клієнта: ``{"cmd":"auth","token":"…"}``).

Повідомлення:
  запит     {"id": 7, "cmd": "patch_profile", ...параметри}
  відповідь {"id": 7, "ok": true,  "result": {...}}
            {"id": 7, "ok": false, "error": "текст", "code": "profile"}
  подія     {"event": "state" | "devices" | "profile" | "stats", ...}   (без id)
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from typing import Any, Awaitable, Callable, Tuple

MAX_LINE = 1 << 20          # 1 МіБ на повідомлення: захист від «нескінченного» рядка


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def loads(line: bytes) -> Any:
    return json.loads(line.decode("utf-8"))


def parse_endpoint(ep: str) -> Tuple[str, ...]:
    """'unix:/path' -> ('unix', '/path');  'tcp:host:port' -> ('tcp', host, port)."""
    kind, _, rest = ep.partition(":")
    if kind == "unix" and rest:
        return ("unix", rest)
    if kind == "tcp":
        host, _, port = rest.rpartition(":")
        if host and port.isdigit():
            return ("tcp", host, int(port))     # type: ignore[return-value]
    raise ValueError(f"некоректний endpoint {ep!r}: очікується unix:/шлях або tcp:host:port")


async def start_server(handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
                       endpoint: str) -> asyncio.AbstractServer:
    ep = parse_endpoint(endpoint)
    if ep[0] == "unix":
        path = ep[1]
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        # Не затираємо чужий сокет «живого» демона: якщо хтось відповідає — помилка.
        if os.path.exists(path) and stat.S_ISSOCK(os.stat(path).st_mode):
            try:
                _, w = await asyncio.open_unix_connection(path)
                w.close()
                raise RuntimeError(f"демон уже запущено ({path})")
            except (ConnectionRefusedError, FileNotFoundError):
                os.unlink(path)                 # «мертвий» сокет від минулого запуску
        old = os.umask(0o177)                   # сокет створюється одразу з правами 0600
        try:
            return await asyncio.start_unix_server(handler, path=path, limit=MAX_LINE)
        finally:
            os.umask(old)
    return await asyncio.start_server(handler, host=ep[1], port=ep[2], limit=MAX_LINE)


async def open_connection(endpoint: str) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    ep = parse_endpoint(endpoint)
    if ep[0] == "unix":
        return await asyncio.open_unix_connection(ep[1], limit=MAX_LINE)
    return await asyncio.open_connection(ep[1], ep[2], limit=MAX_LINE)
