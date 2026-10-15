"""Сесія одного фізичного пристрою: hidraw -> парсинг -> мапінг -> віртуальний геймпад.

Це ядро демона. Потокова модель:

    потік-читач (цей модуль, `_reader`)           asyncio-потік (manager/control)
    ──────────────────────────────────           ───────────────────────────────
    poll()/read()  — блокує в ядрі               compile_profile(...)   (дорога робота)
    Pipeline.process(raw)  — синхронно           pipeline.cp = new_cp   (атомарна підміна)
    write(uinput)  — один виклик на звіт         rumble: FF-подія -> write_output()

Потік-читач не знає про asyncio і ніколи не чекає жодного замка: взаємодія з
керуючим потоком — це присвоєння посилань (`pipeline.cp`, `pipeline.sniffer`),
які в CPython атомарні. Тому ні профіль, ні sniffer не додають затримки.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..config import DaemonConfig
from ..input import DeviceGone, DeviceInfo, InputDevice, open_device as _default_open, run_handshake
from ..logic.layout import DeviceLayout, ReportParser
from ..logic.pipeline import Pipeline
from ..logic.profile import CompiledProfile, ProfileError, compile_profile, merge_patch
from ..logic.sniffer import HexSniffer
from ..output import create_output as _default_create_output
from ..output.base import OutputBackend
from ..output.null import NullPad
from ..store import ProfileStore, StoreError
from .realtime import boost_current_thread

log = logging.getLogger(__name__)


def device_id(info: DeviceInfo) -> str:
    """Стабільний короткий id для API: ``hidraw3`` (Linux) або ``hid-1a2b3c4d`` (hidapi)."""
    if info.backend in ("hidraw", "evdev"):
        return os.path.basename(info.path)
    return "hid-" + hashlib.sha1(info.path.encode()).hexdigest()[:8]


def device_key(info: DeviceInfo) -> str:
    """Ключ для «активного профілю» — модель пристрою, а не конкретний hidrawN."""
    return f"{info.vid:04x}:{info.pid:04x}"


class Session:
    def __init__(self, info: DeviceInfo, layout: Optional[DeviceLayout], cfg: DaemonConfig,
                 store: ProfileStore, *,
                 open_fn: Callable[[DeviceInfo, int], InputDevice] = _default_open,
                 output_fn: Callable[..., OutputBackend] = _default_create_output,
                 on_dead: Optional[Callable[["Session"], None]] = None):
        self.id = device_id(info)
        self.info = info
        self.layout = layout                       # None => режим лише-sniffing
        self.cfg = cfg
        self.store = store
        self._open_fn = open_fn
        self._output_fn = output_fn
        self._on_dead = on_dead                    # викликається З ПОТОКУ-ЧИТАЧА
        self.dev: Optional[InputDevice] = None
        self.out: OutputBackend = NullPad()
        self.parser: Optional[ReportParser] = None
        self.pipeline: Optional[Pipeline] = None
        self.profile_name: Optional[str] = None
        self.warnings: List[str] = []
        self.rt_mode = "n/a"
        self.error: Optional[str] = None
        self.errors = 0                            # винятки в process() (рахуємо, не падаємо)
        self.started_at = 0.0
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._wlock = threading.Lock()             # серіалізує write_output/feature (rumble)
        self._last_snap: Optional[dict] = None     # для відсікання однакових кадрів UI

    # ===================================================================
    # ЖИТТЄВИЙ ЦИКЛ
    # ===================================================================
    def prepare(self) -> None:
        """БЛОКУЮЧА підготовка (викликати в executor): відкриває пристрій, робить
        handshake, створює віртуальний геймпад та компілює початковий профіль."""
        lay = self.layout
        # 1) відкриваємо /dev/hidrawN (або hidapi-шлях) з потрібним розміром звіту
        self.dev = self._open_fn(self.info, lay.read_size if lay else 64)
        try:
            # 2) handshake: активуємо vendor-режим 2.4 ГГц ресивера (feature/output-звіти)
            if lay and lay.handshake:
                run_handshake(self.dev, lay.handshake)
            if lay:
                self.parser = ReportParser(lay)
                # 3) ексклюзивно захоплюємо системні evdev-вузли цього ж пристрою, щоб гра
                #    бачила ЛИШЕ віртуальний геймпад (інакше кнопки двічі)
                if self.cfg.grab_siblings:
                    n = self.dev.grab_siblings()
                    if n:
                        log.info("%s: захоплено %d системних evdev-вузлів", self.id, n)
                # 4) створюємо віртуальний геймпад (uinput / ViGEm)
                self.out = self._output_fn(self.cfg.output_kind,
                                           name=self.cfg.output_name or f"{lay.name} (remapped)",
                                           uinput_path=self.cfg.uinput_path)
                self.out.open()
                self.out.on_rumble = self._on_rumble
            self.pipeline = Pipeline(self.out)
            if lay:
                self._load_initial_profile()
            else:
                # без розкладки — лише дослідження сирих байтів
                self.pipeline.sniffer = HexSniffer(numbered=False)
        except BaseException:
            self._teardown()
            raise
        self.started_at = time.time()

    def activate(self, loop) -> None:
        """У asyncio-потоці: підписує FF-читання та запускає потік-читач."""
        self.out.attach_loop(loop)
        self._running = True
        self._thread = threading.Thread(target=self._reader, name=f"reader-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Зупиняє читач, відпускає всі кнопки й знищує віртуальний пристрій."""
        self._running = False
        if self.dev is not None:
            self.dev.wake()                         # розбудити блокуюче poll()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._teardown()

    def _teardown(self) -> None:
        try:
            if self.pipeline is not None:
                self.out.release_all()              # гра не повинна лишитися з «залипшими» кнопками
        except Exception:  # noqa: BLE001
            pass
        for obj in (self.out, self.dev):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                log.debug("помилка закриття", exc_info=True)
        self.dev = None

    # ===================================================================
    # ГАРЯЧИЙ ШЛЯХ (потік-читач)
    # ===================================================================
    def _reader(self) -> None:
        # Піднімаємо пріоритет саме цього потоку (SCHED_FIFO), не всього процесу.
        if self.cfg.realtime:
            self.rt_mode = boost_current_thread(self.cfg.realtime_priority)
            log.info("%s: потік-читач, режим планувальника: %s", self.id, self.rt_mode)
        # Локальні змінні: у CPython доступ до локальної змінної швидший за атрибут.
        read = self.dev.read
        process = self.pipeline.process
        last_log = 0.0
        try:
            while self._running:
                raw = read()                        # блокує в ядрі: 0% CPU у спокої
                if raw is None:                     # wake() або таймаут -> перевірити _running
                    continue
                try:
                    process(raw)                    # парсинг + мапінг + write(uinput)
                except Exception:  # noqa: BLE001  # один поганий звіт не повинен вбити потік
                    self.errors += 1
                    now = time.monotonic()
                    if now - last_log > 5.0:        # обмежуємо спам у лозі
                        last_log = now
                        log.exception("%s: помилка обробки звіту (усього %d)", self.id, self.errors)
        except DeviceGone as e:
            self.error = f"пристрій відключено: {e}"
            log.warning("%s: %s", self.id, self.error)
        except Exception as e:  # noqa: BLE001
            self.error = f"збій читача: {e}"
            log.exception("%s: збій читача", self.id)
        finally:
            self._running = False
            cb = self._on_dead
            if cb is not None:
                cb(self)                            # менеджер прибере сесію (thread-safe)

    # ===================================================================
    # ПРОФІЛІ (asyncio-потік)
    # ===================================================================
    def _load_initial_profile(self) -> None:
        """Активний профіль для цієї моделі -> default із конфігурації -> порожній."""
        names = [self.store.get_active(device_key(self.info)), self.cfg.default_profile]
        for name in names:
            if not name:
                continue
            try:
                self.set_profile(self.store.get(name))
                return
            except (ProfileError, StoreError) as e:
                log.warning("%s: профіль %r не застосовано: %s", self.id, name, e)
        self.set_profile({"name": "fallback"})

    def set_profile(self, data: Dict[str, Any]) -> CompiledProfile:
        """Компілює профіль (поза гарячим потоком) і ГАРЯЧЕ підміняє його.

        Дорога робота (LUT кривих, таблиці бітів) виконується тут; потік-читач
        побачить новий `pipeline.cp` на наступному звіті й сам скине стан.
        """
        if self.layout is None or self.parser is None:
            raise ProfileError("сесія без розкладки: профілі недоступні (лише sniffing)")
        cp = compile_profile(data, self.layout, self.parser, self.out)
        self.pipeline.cp = cp                       # атомарне присвоєння посилання
        self.profile_name = cp.name
        self.warnings = list(cp.warnings)
        return cp

    def patch_profile(self, patch: Dict[str, Any]) -> CompiledProfile:
        """JSON merge-patch (RFC 7386) поверх активного профілю: «змінив повзунок — діє»."""
        cp = self.pipeline.cp
        if cp is None:
            raise ProfileError("профіль ще не завантажено")
        merged = merge_patch(cp.data, patch)
        merged["name"] = cp.data["name"]            # ім'я змінювати патчем не дозволяємо
        return self.set_profile(merged)

    @property
    def profile_data(self) -> Optional[Dict[str, Any]]:
        cp = self.pipeline.cp if self.pipeline else None
        return cp.data if cp else None

    # ===================================================================
    # SNIFFER
    # ===================================================================
    def sniff(self, action: str, name: Optional[str] = None, baseline_s: Optional[float] = None) -> dict:
        p = self.pipeline
        if action == "start":
            lay = self.layout
            p.sniffer = HexSniffer(
                baseline_s=baseline_s or 2.5,
                numbered=(lay.report_id is not None) if lay else False,
                report_id=lay.report_id if lay else None)
        elif action == "stop":
            p.sniffer = None
            return {"phase": "off"}
        elif p.sniffer is None:
            raise ProfileError("sniffer не запущено (action=start)")
        elif action == "reset":
            p.sniffer.reset()
        elif action == "learn":
            if not name:
                raise ProfileError("learn: потрібне ім'я кнопки (name)")
            p.sniffer.learn(name)
        elif action != "summary":
            raise ProfileError(f"невідома дія sniffer: {action!r}")
        return p.sniffer.summary()

    # ===================================================================
    # RUMBLE (FF-запит гри -> фізичний пристрій)
    # ===================================================================
    def _on_rumble(self, strong16: int, weak16: int) -> None:
        lay, dev = self.layout, self.dev
        if lay is None or lay.rumble is None or dev is None:
            return
        spec = lay.rumble
        try:
            report = spec.build(strong16, weak16)
            with self._wlock:                       # не змішуємо дві вібро-команди
                if spec.op == "feature":
                    dev.send_feature(report)
                else:
                    dev.write_output(report)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: rumble не передано: %s", self.id, e)

    # ===================================================================
    # СТАН ДЛЯ UI / API
    # ===================================================================
    def snapshot(self) -> Dict[str, Any]:
        """Живий стан для Web UI. Викликається з asyncio-потоку ~60 Гц, лише ЧИТАЄ
        атрибути пайплайна (атомарні в CPython) — гарячий шлях не сповільнюється.

        src : hex-рядок бітової маски вхідних кнопок (довільної довжини — без
              обмеження кількості; JS розбирає через BigInt)
        out : імена віртуальних кнопок, що зараз утримуються
        ax  : останні віддані LX LY RX RY (int16) та LT RT (0..255)
        raw : СИРІ стіки, нормалізовані в [-1,1] (до deadzone/кривих) — щоб в UI
              було видно, що саме робить мертва зона
        """
        p = self.pipeline
        snap: Dict[str, Any] = {"dev": self.id, "src": format(p.src_prev, "x"),
                                "ax": list(p.last), "out": [], "raw": None}
        cp = p.cp
        if cp is not None:
            try:
                names = cp.target_names
                snap["out"] = [names[i] for i, r in enumerate(list(p.ref)) if r > 0 and i < len(names) and names[i]]
                raw = p.last_raw
                if raw and len(raw) >= cp.parser.min_len:
                    _, vals = cp.parser.parse(raw)
                    snap["raw"] = [self._norm(cp.left, vals, 0), self._norm(cp.left, vals, 1),
                                   self._norm(cp.right, vals, 0), self._norm(cp.right, vals, 1)]
            except Exception:  # noqa: BLE001 — стан UI не має валити демон
                pass
        sn = p.sniffer
        if sn is not None:
            s = sn.summary()
            snap["sniff"] = {"phase": s["phase"], "progress": s["progress"], "hex": s["hex"]}
        return snap

    @staticmethod
    def _norm(stick, vals: tuple, axis: int) -> Optional[float]:
        if stick is None:
            return None
        if axis == 0:
            return round((vals[stick.ix] - stick.cx) * stick.kx, 3)
        return round((vals[stick.iy] - stick.cy) * stick.ky, 3)

    def describe(self) -> Dict[str, Any]:
        """Статичний опис сесії для списку пристроїв (JSON-сумісний)."""
        d: Dict[str, Any] = {**self.info.as_dict(), "id": self.id, "open": True,
                             "layout": self.layout.id if self.layout else None,
                             "layout_name": self.layout.name if self.layout else None,
                             "profile": self.profile_name, "warnings": self.warnings,
                             "running": self._running, "error": self.error, "rt": self.rt_mode,
                             "errors": self.errors, "sniff_only": self.layout is None}
        if self.parser is not None:
            d["inputs"] = [[i, n] for i, n in enumerate(self.parser.src_names) if n]
            d["default_targets"] = {n: b.target for n, b in self.layout.buttons.items()}
            d["has_gyro"] = bool(self.layout.gyro)
        return d

    def stats(self) -> Dict[str, Any]:
        return self.pipeline.stats() if self.pipeline else {}
