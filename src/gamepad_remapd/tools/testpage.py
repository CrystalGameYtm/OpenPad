"""Тестова веб-сторінка: перевірка розкладки геймпада в браузері.

    gamepad-remapd testpage                 # відкриє http://127.0.0.1:8766/
    gamepad-remapd testpage /dev/hidraw10   # одразу відкрити пристрій

Не потребує ні демона, ні /dev/uinput, ні aiohttp (лише стандартна бібліотека):
читає сирі звіти hidraw так само, як `monitor`, і показує в реальному часі, які кнопки
розпізнано, куди вони мапляться, положення стіків і тригерів, а також сирий звіт по
байтах — зі підсвіткою змінених байтів (так знаходяться приховані кнопки, яких немає
в розкладці). Нічого не емулює і не змінює; hidraw можуть читати кілька процесів.

Безпека: слухає лише loopback, перевіряє Host/Origin (захист від DNS-rebinding і
чужих сторінок у браузері).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional
from urllib.parse import urlsplit

from ..input import DeviceGone, drop_shadowed_evdev, enumerate_devices, open_device, run_handshake
from ..logic.layout import HAT_DEFAULT_TARGETS, DeviceLayout, ReportParser
from ..logic.autolayout import try_layout  # noqa: F401  (для _find_layout)
from .monitor_cli import _find_layout

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
PAGE_FILES = {"/": ("testpage.html", "text/html; charset=utf-8"),
              "/testpage.js": ("testpage.js", "text/javascript; charset=utf-8"),
              "/testpage.css": ("testpage.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
AUTO_PERIOD_S = 1.0             # як часто автовиявлення опитує пристрої
SILENT_S = 6.0                  # скільки чекати перший звіт, перш ніж спробувати наступний інтерфейс
ERROR_RETRY_S = 5.0             # пауза перед повтором після помилки відкриття (немає прав тощо)
MIN_FRAME_S = 1 / 60            # SSE-кадри не частіше 60 Гц; події натискань при цьому не губляться
EVENT_BACKLOG = 512


# ===========================================================================
# ЧИТАЧ ПРИСТРОЮ (окремий потік; стан віддається снапшотом)
# ===========================================================================
class Probe:
    """Відкритий пристрій + потік читання. Єдине місце, що змінюється з двох потоків —
    `snap` (замінюється цілим об'єктом) і `events` (deque з монотонним лічильником)."""

    def __init__(self, info, layout: DeviceLayout, open_fn: Callable):
        self.info, self.layout = info, layout
        self.pr = ReportParser(layout)
        self._open_fn = open_fn
        self.cond = threading.Condition()
        self.seq = 0
        self.snap: dict = {"raw": "", "buttons": [], "axes": {}, "n": 0, "hz": 0, "len": 0}
        self.events: Deque[dict] = deque(maxlen=EVENT_BACKLOG)
        self.ev_count = 0
        self.status = {"state": "starting", "message": ""}
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.dev = None
        self.thread = threading.Thread(target=self._run, name="testpage-reader", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        dev = self.dev
        if dev is not None:
            try:
                dev.wake()
            except Exception:  # noqa: BLE001
                pass
        self.thread.join(timeout=2)

    def _set_status(self, state: str, message: str = "") -> None:
        with self.cond:
            self.status = {"state": state, "message": message}
            self.seq += 1
            self.cond.notify_all()

    def _run(self) -> None:
        pr, lay = self.pr, self.layout
        try:
            self.dev = self._open_fn(self.info, lay.read_size)
        except PermissionError as e:
            self._set_status("error", f"немає прав на {self.info.path}: {e}. Перевірте udev/ACL.")
            return
        except Exception as e:  # noqa: BLE001
            self._set_status("error", f"не вдалося відкрити {self.info.path}: {e}")
            return
        dev = self.dev
        try:
            if lay.handshake:
                run_handshake(dev, lay.handshake)
            self._set_status("running")
            prev_word: Optional[int] = None
            n, tick, tick_n = 0, time.monotonic(), 0
            while not self.stop.is_set():
                raw = dev.read()
                if raw is None:
                    continue
                if len(raw) < pr.min_len or (pr.rid is not None and raw[0] != pr.rid) \
                        or (pr.sync is not None and raw[pr.sync[0]] != pr.sync[1]):
                    continue                                   # службовий звіт іншого типу
                word, vals = pr.parse(raw)
                n += 1
                tick_n += 1
                now = time.monotonic()
                hz = self.snap["hz"]
                if now - tick >= 1.0:
                    hz, tick, tick_n = round(tick_n / (now - tick)), now, 0
                if prev_word is not None:
                    changed = word ^ prev_word
                    while changed:
                        low = changed & -changed
                        bit = low.bit_length() - 1
                        changed ^= low
                        name = pr.src_names[bit] if bit < len(pr.src_names) else None
                        if name is None:
                            continue
                        target = lay.buttons[name].target if name in lay.buttons \
                            else HAT_DEFAULT_TARGETS.get(name)
                        self.ev_count += 1
                        self.events.append({"id": self.ev_count, "t": round(time.time(), 3),
                                            "name": name, "target": target,
                                            "down": bool(word >> bit & 1)})
                prev_word = word
                pressed = [nm for i, nm in enumerate(pr.src_names) if nm and word >> i & 1]
                axes = {nm: vals[i] for nm, i in pr.index.items()}
                with self.cond:
                    self.snap = {"raw": raw.hex(), "buttons": pressed, "axes": axes,
                                 "n": n, "hz": hz, "len": len(raw)}
                    self.seq += 1
                    self.cond.notify_all()
        except DeviceGone:
            self._set_status("gone", "пристрій відключено")
        except Exception as e:  # noqa: BLE001
            log.exception("помилка читання")
            self._set_status("error", str(e))
        finally:
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- опис розкладки для UI ----
    def describe(self) -> dict:
        lay, pr = self.layout, self.pr
        names = [nm for nm in pr.src_names if nm]
        covered = sorted({b.byte for b in lay.buttons.values()}
                         | {f.offset + k for f in lay.fields.values()
                            for k in range(_width(f))}
                         | ({lay.hat.byte} if lay.hat else set())
                         | ({0} if lay.report_id is not None else set())
                         | ({lay.sync[0]} if lay.sync else set()))
        return {
            "device": self.info.as_dict(), "layout": lay.id, "layout_name": lay.name,
            "generated": lay.generated, "notes": lay.notes,
            "buttons": [{"name": nm,
                         "target": (lay.buttons[nm].target if nm in lay.buttons
                                    else HAT_DEFAULT_TARGETS.get(nm))} for nm in names],
            "fields": {nm: {"min": f.raw_min, "max": f.raw_max, "center": f.center}
                       for nm, f in lay.fields.items()},
            "sticks": {k: list(v) for k, v in lay.sticks.items()},
            "triggers": dict(lay.triggers),
            "covered_bytes": covered,
        }


def _width(f) -> int:
    from ..logic.layout import _NUMERIC_FMT
    return _NUMERIC_FMT[f.fmt][0] if f.fmt is not None else (f.bit_offset + f.bits + 7) // 8


# ===========================================================================
# СЕРВЕР
# ===========================================================================
class TestPage:
    """Стан сторінки: поточний Probe + автовиявлення (потік стежить за пристроями)."""

    def __init__(self, layouts: Dict[str, DeviceLayout], backend: str = "auto", *,
                 enumerate_fn: Callable = enumerate_devices, open_fn: Callable = open_device,
                 auto: bool = True):
        self.layouts, self.backend = layouts, backend
        self.enumerate_fn, self.open_fn = enumerate_fn, open_fn
        self.lock = threading.RLock()
        self.probe: Optional[Probe] = None
        self.gen = 0                      # лічильник відкриттів; SSE-клієнти скидають стан при зміні
        self.auto = auto
        self._rejected: set = set()       # шляхи, що мовчали/впали; скидається при зміні набору пристроїв
        self._settled = False             # усі кандидати мовчали -> лишаємось на найкращому
        self._last_paths: set = set()
        self._retry_at = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- перелік і ранжування ----
    def _layout_of(self, info) -> Optional[DeviceLayout]:
        try:
            return _find_layout(info, self.layouts)
        except Exception:  # noqa: BLE001
            return None

    def _scan(self):
        """-> (visible, candidates): visible — для списку в UI (без evdev-дублів), candidates —
        [(info, layout)] за якістю: 0 готова розкладка, 1 авто (hidraw/evdev), 3 «затінений» evdev."""
        infos = list(self.enumerate_fn(self.backend))
        lay = {i.path: self._layout_of(i) for i in infos}
        visible = drop_shadowed_evdev(infos, lambda i: lay[i.path] is not None)
        vis = {i.path for i in visible}

        def rank(i) -> int:
            if i.path not in vis:
                return 3
            return 0 if lay[i.path] and not lay[i.path].generated else 1
        cands = sorted(((rank(i), n, i) for n, i in enumerate(infos) if lay[i.path]), key=lambda t: t[:2])
        return visible, lay, [(i, lay[i.path]) for _, _, i in cands]

    def devices(self) -> List[dict]:
        visible, lay, _ = self._scan()
        out = []
        for i in visible:
            d = i.as_dict()
            d["layout"] = lay[i.path].id if lay[i.path] else None
            d["generated"] = bool(lay[i.path] and lay[i.path].generated)
            out.append(d)
        return out

    # ---- відкриття/закриття ----
    def _close_locked(self) -> None:
        if self.probe:
            self.probe.close()
            self.probe = None
            self.gen += 1

    def _open_locked(self, info, lay: DeviceLayout) -> dict:
        self._close_locked()
        self.probe = Probe(info, lay, self.open_fn)
        self.gen += 1
        self.probe.start()
        return {"gen": self.gen, **self.probe.describe()}

    def open(self, path: str, layout_id: Optional[str]) -> dict:
        """Ручне відкриття (з UI/CLI): вимикає автовиявлення, щоб воно не перебило вибір."""
        info = next((i for i in self.enumerate_fn(self.backend) if i.path == path), None)
        if info is None:
            raise LookupError(f"пристрій {path} не знайдено")
        lay = self.layouts.get(layout_id) if layout_id else _find_layout(info, self.layouts)
        if lay is None:
            raise ValueError("для цього інтерфейсу немає розкладки (і він не схожий на геймпад); "
                             "спробуйте інший вузол того ж пристрою")
        with self.lock:
            self.auto = False
            return self._open_locked(info, lay)

    def current(self) -> dict:
        with self.lock:
            return {"gen": self.gen, **self.probe.describe()} if self.probe else {}

    def close(self) -> None:
        with self.lock:
            self.auto = False
            self._close_locked()

    def set_auto(self, on: bool) -> None:
        with self.lock:
            self.auto = bool(on)
            self._rejected.clear()
            self._settled = False
            self._retry_at = 0.0

    def layout_ids(self) -> List[dict]:
        return [{"id": l.id, "name": l.name, "generated": l.generated}
                for l in sorted(self.layouts.values(), key=lambda l: l.id)]

    # ---- автовиявлення ----
    def auto_tick(self, now: Optional[float] = None) -> None:
        """Один крок: знайти геймпад, відкрити найкращий вузол, перейти на інший, якщо цей мовчить,
        перепідключитись після hotplug. Викликається потоком раз на AUTO_PERIOD_S (і з тестів)."""
        now = time.monotonic() if now is None else now
        visible, lay, cands = self._scan()
        paths = {i.path for i, _ in cands}
        with self.lock:
            if paths != self._last_paths:                   # підключили/відключили — починаємо спочатку
                self._rejected.clear()
                self._settled = False
                self._last_paths = paths
            if not self.auto:
                return
            p = self.probe
            if p is not None:
                st = p.status["state"]
                if p.info.path not in paths and st != "starting":
                    self._close_locked()                    # вузол зник (відключено/перепідключено)
                elif st == "error":
                    if self._retry_at == 0.0:
                        self._retry_at = now + ERROR_RETRY_S    # лишаємо повідомлення на екрані
                    elif now >= self._retry_at:
                        self._rejected.add(p.info.path)
                        self._retry_at = 0.0
                        self._close_locked()
                elif st == "gone":
                    self._close_locked()
                elif st == "running" and p.snap["n"] == 0 and not self._settled \
                        and time.monotonic() - p.started > SILENT_S and len(cands) > 1:
                    self._rejected.add(p.info.path)         # цей вузол мовчить — пробуємо наступний
                    self._close_locked()
                p = self.probe
            if p is None:
                pick = next(((i, l) for i, l in cands if i.path not in self._rejected), None)
                if pick is None and cands:                  # усі мовчали: лишаємось на найкращому
                    self._rejected.clear()
                    self._settled = True
                    pick = cands[0]
                if pick:
                    log.info("автовиявлення: відкриваю %s (%s)", pick[0].path, pick[1].id)
                    self._open_locked(*pick)

    def start_auto(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.auto_tick()
                except Exception:  # noqa: BLE001
                    log.exception("автовиявлення")
                self._stop.wait(AUTO_PERIOD_S)
        self._thread = threading.Thread(target=loop, name="testpage-auto", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.close()


def _make_handler(tp: TestPage, allowed_hosts: set):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "gamepad-testpage"

        def log_message(self, *a) -> None:      # тиша
            pass

        # ---- допоміжне ----
        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            origin = self.headers.get("Origin")
            if host not in allowed_hosts or (origin and urlsplit(origin).netloc.lower() not in allowed_hosts):
                self._json({"error": "forbidden origin/host"}, HTTPStatus.FORBIDDEN)
                return False
            return True

        def _headers(self, ctype: str, length: Optional[int], status=HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)

        def _json(self, obj, status=HTTPStatus.OK) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self._headers("application/json; charset=utf-8", len(body), status)
            self.end_headers()
            self.wfile.write(body)

        # ---- GET ----
        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = urlsplit(self.path).path
            if path in PAGE_FILES:
                fname, ctype = PAGE_FILES[path]
                try:
                    body = (STATIC / fname).read_bytes()
                except OSError:
                    return self._json({"error": "файл сторінки не знайдено"}, HTTPStatus.NOT_FOUND)
                self._headers(ctype, len(body))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/devices":
                self._json({"devices": tp.devices(), "layouts": tp.layout_ids(), "auto": tp.auto,
                            "active": tp.probe.info.path if tp.probe else None})
            elif path == "/api/current":
                self._json(tp.current())
            elif path == "/api/events":
                self._sse()
            else:
                self._json({"error": "не знайдено"}, HTTPStatus.NOT_FOUND)

        # ---- POST ----
        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = urlsplit(self.path).path
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("очікується JSON-об'єкт")
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": f"некоректне тіло: {e}"}, HTTPStatus.BAD_REQUEST)
            try:
                if path == "/api/open":
                    self._json(tp.open(str(body.get("path", "")), body.get("layout") or None))
                elif path == "/api/close":
                    tp.close()
                    self._json({})
                elif path == "/api/auto":
                    tp.set_auto(bool(body.get("on", True)))
                    self._json({"auto": tp.auto})
                else:
                    self._json({"error": "не знайдено"}, HTTPStatus.NOT_FOUND)
            except LookupError as e:
                self._json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except ValueError as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

        # ---- Server-Sent Events ----
        def _sse(self) -> None:
            self._headers("text/event-stream", None)
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.close_connection = True
            last_seq, last_ev, gen, last_sent = -1, 0, -1, 0.0
            try:
                while True:
                    probe = tp.probe
                    if probe is None or tp.gen != gen:
                        gen = tp.gen
                        last_seq, last_ev = -1, 0
                        if probe is None:
                            self._send("idle", {"gen": gen})
                            time.sleep(0.3)
                            continue
                    with probe.cond:
                        if probe.seq == last_seq:
                            probe.cond.wait(timeout=1.0)
                        seq, snap, status = probe.seq, probe.snap, probe.status
                    if seq == last_seq:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    wait = MIN_FRAME_S - (time.monotonic() - last_sent)
                    if wait > 0:
                        time.sleep(wait)
                        with probe.cond:
                            seq, snap, status = probe.seq, probe.snap, probe.status
                    evs = [e for e in list(probe.events) if e["id"] > last_ev]
                    if evs:
                        last_ev = evs[-1]["id"]
                    last_seq, last_sent = seq, time.monotonic()
                    self._send("state", {"gen": gen, "status": status, "events": evs, **snap})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _send(self, event: str, data: dict) -> None:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())
            self.wfile.flush()

    return Handler


def make_server(tp: TestPage, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address) -> None:
            import sys
            if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                return                      # браузер закрив вкладку/SSE — це нормально
            super().handle_error(request, client_address)

    srv = Server((host, port), lambda *a, **k: None)      # тимчасовий; обробник створимо після bind
    real = srv.server_address[1]
    allowed = {f"127.0.0.1:{real}", f"localhost:{real}", f"[::1]:{real}"}
    srv.RequestHandlerClass = _make_handler(tp, allowed)
    return srv


def run(path: Optional[str], layouts: Dict[str, DeviceLayout], layout_id: Optional[str] = None,
        backend: str = "auto", port: int = 8766, open_browser: bool = True) -> int:
    tp = TestPage(layouts, backend, auto=not path)
    try:
        srv = make_server(tp, "127.0.0.1", port)
    except OSError as e:
        print(f"не вдалося зайняти порт {port}: {e}  (інший порт: --port N)")
        return 1
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    if path:
        try:
            tp.open(path, layout_id)
        except (LookupError, ValueError) as e:
            print(f"{path}: {e}")
            srv.server_close()
            return 1
    if tp.auto:
        tp.start_auto()
    print(f"Тестова сторінка: {url}   (Ctrl-C — вихід)"
          + ("\nАвтовиявлення увімкнено: геймпад буде знайдено й відкрито сам." if tp.auto else ""))
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        tp.shutdown()
        srv.server_close()
    return 0
