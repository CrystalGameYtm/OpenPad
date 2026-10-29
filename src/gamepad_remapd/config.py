"""Конфігурація демона (``daemon.json``) та розташування файлів.

Порядок пошуку каталогу конфігурації:
  1. змінна середовища ``GAMEPAD_REMAPD_HOME``;
  2. Windows: ``%APPDATA%\\gamepad-remapd``;
  3. Linux/macOS: ``$XDG_CONFIG_HOME/gamepad-remapd`` або ``~/.config/gamepad-remapd``.

Усередині каталогу:
  daemon.json    — цей файл (усе необов'язкове, є розумні значення за замовчуванням)
  profiles/      — користувацькі профілі (*.json); перекривають вбудовані
  devices/       — користувацькі розкладки пристроїв (*.json); перекривають вбудовані
  keymaps/       — розкладки «клавіатура -> геймпад» (*.json); перекривають вбудовані
  state.json     — який профіль активний для якого пристрою (пише демон)
  control.token  — токен для TCP-каналу керування (Windows)
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

PACKAGE_DIR = Path(__file__).resolve().parent
BUILTIN_LAYOUTS = PACKAGE_DIR / "layouts"
BUILTIN_PROFILES = PACKAGE_DIR / "profiles"
BUILTIN_KEYMAPS = PACKAGE_DIR / "keymaps"


def default_config_dir() -> Path:
    env = os.environ.get("GAMEPAD_REMAPD_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "gamepad-remapd"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gamepad-remapd"


def default_runtime_dir() -> Path:
    """Каталог для UNIX-сокета: $XDG_RUNTIME_DIR (tmpfs, 0700, чиститься при виході)
    або запасний ``/tmp/gamepad-remapd-<uid>`` із правами 0700."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "gamepad-remapd"
    if hasattr(os, "getuid"):
        return Path(tempfile.gettempdir()) / f"gamepad-remapd-{os.getuid()}"
    return Path(tempfile.gettempdir()) / "gamepad-remapd"


class ConfigError(ValueError):
    pass


@dataclass
class KeyboardConfig:
    """Клавіатура як геймпад (Linux). УВІМКНЕНО ЛИШЕ ЯВНО, бо захоплення забирає клавіатуру в системи."""
    enabled: bool = False
    device: List[str] = field(default_factory=list)     # шлях /dev/input/eventN, підрядок імені або VID:PID
    keymap: object = "default"                           # ім'я файлу з keymaps/ або вбудований словник
    grab: bool = True                                    # забирати клавіатуру в системи, поки режим активний
    toggle: List[str] = field(default_factory=lambda: ["LEFTCTRL", "LEFTALT", "G"])


def parse_keyboard_config(raw: object, where: str = "keyboard") -> KeyboardConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: має бути об'єктом")
    known = {"enabled", "device", "keymap", "grab", "toggle"}
    kb = KeyboardConfig()
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if k not in known:
            raise ConfigError(f"{where}: невідомий ключ {k!r}; дозволені {sorted(known)}")
        if k in ("enabled", "grab") and not isinstance(v, bool):
            raise ConfigError(f"{where}.{k}: true/false")
        if k in ("device", "toggle"):
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                raise ConfigError(f"{where}.{k}: рядок або список рядків")
        if k == "keymap" and not isinstance(v, (str, dict)):
            raise ConfigError(f"{where}.keymap: ім'я keymap або об'єкт")
        setattr(kb, k, v)
    if kb.enabled and not kb.device:
        raise ConfigError(f"{where}.device обов'язковий, коли enabled=true (список: gamepad-remapd keyboards)")
    return kb


@dataclass
class DaemonConfig:
    config_dir: Path = field(default_factory=default_config_dir)

    # ---- вхід/вихід ----
    backend: str = "auto"                 # auto (hidraw+evdev) | hidraw | evdev | hidapi
    output_kind: str = "xbox360"          # xbox360 | ds4 (лише Linux; Windows = X360)
    output_name: Optional[str] = None     # ім'я віртуального пристрою (None = за замовчуванням)
    uinput_path: str = "/dev/uinput"
    # D-pad віртуального геймпада: "hat" (ABS_HAT0X/Y, як справжній Xbox 360 / DS4) або "buttons"
    # (BTN_DPAD_UP/DOWN/LEFT/RIGHT) — для ігор, де hat-D-pad на Linux не працює.
    dpad_mode: str = "hat"
    grab_siblings: bool = True            # EVIOCGRAB на evdev-«двійник» фізичного пристрою

    # ---- виявлення пристроїв ----
    auto_open: bool = True                # самі відкривати пристрої, для яких знайдено розкладку
    auto_layout: bool = True              # якщо готової розкладки немає — скласти з HID-дескриптора
    poll_interval_s: float = 1.0          # період hotplug-опитування sysfs/hidapi
    overrides: List[dict] = field(default_factory=list)   # [{"vid":"3537","pid":"1010","layout":"my_pad"}]
    default_profile: str = "default"

    # ---- продуктивність ----
    realtime: bool = True                 # спробувати SCHED_FIFO / високий пріоритет потоку
    realtime_priority: int = 50           # 1..99 (Linux)
    switch_interval_s: float = 0.0005     # sys.setswitchinterval: скільки потік тримає GIL

    # ---- IPC / Web ----
    control_endpoint: Optional[str] = None   # "unix:/path" | "tcp:127.0.0.1:8764"; None = авто
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_enabled: bool = True
    ui_hz: int = 60                       # частота оновлення live-стану для Web UI

    # ---- клавіатура як геймпад ----
    keyboard: KeyboardConfig = field(default_factory=KeyboardConfig)

    # ---- похідні шляхи ----
    @property
    def profiles_dir(self) -> Path:
        return self.config_dir / "profiles"

    @property
    def devices_dir(self) -> Path:
        return self.config_dir / "devices"

    @property
    def keymaps_dir(self) -> Path:
        return self.config_dir / "keymaps"

    def keymap_dirs(self) -> List[Path]:
        return [BUILTIN_KEYMAPS, self.keymaps_dir]

    @property
    def state_path(self) -> Path:
        return self.config_dir / "state.json"

    @property
    def token_path(self) -> Path:
        return self.config_dir / "control.token"

    def layout_dirs(self) -> List[Path]:
        """Пізніші каталоги перекривають ранішні (див. load_layouts)."""
        return [BUILTIN_LAYOUTS, self.devices_dir]

    def endpoint(self) -> str:
        if self.control_endpoint:
            return self.control_endpoint
        if os.name == "nt":
            return "tcp:127.0.0.1:8764"
        return f"unix:{default_runtime_dir() / 'control.sock'}"

    def allowed_hosts(self) -> List[str]:
        """Значення заголовка Host, які приймає Web-сервер (захист від DNS rebinding)."""
        p = self.web_port
        hosts = [f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}"]
        if self.web_host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
            hosts.append(f"{self.web_host}:{p}")
        return hosts


_INT_RANGES = {"realtime_priority": (1, 99), "web_port": (1, 65535), "ui_hz": (1, 240)}
_KNOWN = {f for f in DaemonConfig.__dataclass_fields__ if f != "config_dir"}


def load_config(path: Optional[Path] = None, config_dir: Optional[Path] = None) -> DaemonConfig:
    """Читає daemon.json (якщо він є) і повертає конфігурацію з перевіркою ключів."""
    cfg = DaemonConfig(config_dir=Path(config_dir) if config_dir else default_config_dir())
    p = Path(path) if path else cfg.config_dir / "daemon.json"
    if not p.is_file():
        return cfg
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p}: некоректний JSON: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: корінь має бути об'єктом")
    for k, v in raw.items():
        if k.startswith("_"):
            continue                      # "_comment" та ін.
        if k not in _KNOWN:
            raise ConfigError(f"{p}: невідомий ключ {k!r}; дозволені {sorted(_KNOWN)}")
        if k in _INT_RANGES:
            lo, hi = _INT_RANGES[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ConfigError(f"{p}: {k} має бути цілим {lo}..{hi}")
        if k == "keyboard":
            cfg.keyboard = parse_keyboard_config(v, f"{p}: keyboard")
            continue
        setattr(cfg, k, v)
    if cfg.backend not in ("auto", "hidraw", "evdev", "hidapi", "winhid"):
        raise ConfigError("backend: auto | hidraw | evdev | hidapi | winhid")
    return cfg


def read_or_create_token(path: Path) -> str:
    """Токен для TCP-каналу керування. Файл створюється з правами 0600 (де це можливо)."""
    try:
        t = path.read_text(encoding="utf-8").strip()
        if t:
            return t
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(t)
    return t
