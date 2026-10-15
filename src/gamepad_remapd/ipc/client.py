"""Асинхронний клієнт каналу керування (використовується Web-процесом і CLI).

* `call(cmd, **params)` — запит/відповідь із зіставленням за `id`;
* події (`state`, `devices` …) віддаються у `on_event(dict)`;
* `run()` — фонова задача: сама перепідключається з експоненційною паузою, тож
  Web UI переживає перезапуск демона без перезапуску самого Web-процесу.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from . import protocol

log = logging.getLogger(__name__)


class ControlError(Exception):
    def __init__(self, message: str, code: str = "internal"):
        super().__init__(message)
        self.code = code


class ControlUnavailable(ControlError):
    """Демон недоступний (не запущений або перепідключення триває)."""

    def __init__(self, message: str = "демон недоступний"):
        super().__init__(message, "unavailable")


class ControlClient:
    def __init__(self, endpoint: str, token: Optional[str] = None,
                 on_event: Optional[Callable[[dict], None]] = None,
                 on_connection: Optional[Callable[[bool], None]] = None,
                 subscribe: Optional[list] = None):
        self.endpoint = endpoint
        self.token = token
        self.on_event = on_event
        self.on_connection = on_connection
        self.subscribe_events = subscribe
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._task: Optional[asyncio.Task] = None
        self.connected = asyncio.Event()

    # ---- життєвий цикл ------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="control-client")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._drop()

    def _drop(self) -> None:
        w, self._writer, self._reader = self._writer, None, None
        self.connected.clear()
        if w is not None:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ControlUnavailable("з'єднання з демоном втрачено"))
        self._pending.clear()

    async def _run(self) -> None:
        delay = 0.2
        while True:
            try:
                self._reader, self._writer = await protocol.open_connection(self.endpoint)
                reader_task = asyncio.create_task(self._read_loop(self._reader))
                try:
                    if self.token:
                        await self._call("auth", {"token": self.token})
                    if self.subscribe_events is not None:
                        await self._call("subscribe", {"events": self.subscribe_events})
                    self.connected.set()
                    delay = 0.2
                    if self.on_connection:
                        self.on_connection(True)
                    await reader_task                        # до розриву з'єднання
                finally:
                    reader_task.cancel()
            except asyncio.CancelledError:
                raise
            except (OSError, ControlError):
                pass                                          # демон ще не слухає
            was = self.connected.is_set()
            self._drop()
            if was and self.on_connection:
                self.on_connection(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 3.0)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                msg = protocol.loads(line)
            except ValueError:
                continue
            if "event" in msg:
                if self.on_event:
                    try:
                        self.on_event(msg)
                    except Exception:  # noqa: BLE001
                        log.debug("on_event збій", exc_info=True)
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(ControlError(msg.get("error", "помилка"), msg.get("code", "internal")))

    # ---- виклики ----------------------------------------------------------------
    async def _call(self, cmd: str, params: dict, timeout: float = 10.0) -> Any:
        w = self._writer
        if w is None:
            raise ControlUnavailable()
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        w.write(protocol.dumps({"id": rid, "cmd": cmd, **params}))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise ControlError("таймаут відповіді демона", "timeout") from None

    async def call(self, cmd: str, **params: Any) -> Any:
        if not self.connected.is_set():
            raise ControlUnavailable()
        return await self._call(cmd, params)
