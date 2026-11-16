"""Control-channel protocol between the daemon and the Web process.

Transport: NDJSON (one JSON object per line, ``\n`` separated).
  * Linux/macOS: UNIX socket ``unix:/run/user/1000/gamepad-remapd/control.sock``,
    mode 0600 — only the owner has access (authentication = filesystem permissions).
  * Windows (and optionally anywhere): ``tcp:127.0.0.1:8764`` + a token from the file
    ``control.token`` (the client's first message: ``{"cmd":"auth","token":"…"}``).

Messages:
  request   {"id": 7, "cmd": "patch_profile", ...params}
  response  {"id": 7, "ok": true,  "result": {...}}
            {"id": 7, "ok": false, "error": "text", "code": "profile"}
  event     {"event": "state" | "devices" | "profile" | "stats", ...}   (no id)
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from typing import Any, Awaitable, Callable, Tuple
from ..i18n import tr

MAX_LINE = 1 << 20          # 1 MiB per message: protection against an endless line


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
    raise ValueError(tr('invalid endpoint {0!r}: expected unix:/path or tcp:host:port', ep))


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
        
        if os.path.exists(path) and stat.S_ISSOCK(os.stat(path).st_mode):
            try:
                _, w = await asyncio.open_unix_connection(path)
                w.close()
                raise RuntimeError(tr('the daemon is already running ({0})', path))
            except (ConnectionRefusedError, FileNotFoundError):
                os.unlink(path)                 # a "dead" socket left over from a previous run
        old = os.umask(0o177)                   
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
