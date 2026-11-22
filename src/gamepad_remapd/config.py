"""Daemon configuration (``daemon.json``) and file locations.

Config directory lookup order:
  1. the ``GAMEPAD_REMAPD_HOME`` environment variable;
  2. Windows: ``%APPDATA%\\gamepad-remapd``;
  3. Linux/macOS: ``$XDG_CONFIG_HOME/gamepad-remapd`` or ``~/.config/gamepad-remapd``.

Inside the directory:
  daemon.json    — this file (everything is optional; there are sensible defaults)
  profiles/      — user profiles (*.json); they override the built-in ones
  devices/       — user device layouts (*.json); they override the built-in ones
  keymaps/       — "keyboard -> gamepad" layouts (*.json); they override the built-in ones
  locales/       — Web UI translations (<code>.json); they override/extend the built-in ones
  state.json     — which profile is active for which device (written by the daemon)
  control.token  — token for the control TCP channel (Windows)
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from .i18n import tr

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
    """Directory for the UNIX socket: $XDG_RUNTIME_DIR (tmpfs, 0700, cleaned on logout)
        or the fallback ``/tmp/gamepad-remapd-<uid>`` with mode 0700.
    """
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
    """Keyboard as gamepad (Linux). ENABLED ONLY EXPLICITLY, because grabbing takes the keyboard away from the system."""
    enabled: bool = False
    device: List[str] = field(default_factory=list)     
    keymap: object = "default"                           
    grab: bool = True                                    
    toggle: List[str] = field(default_factory=lambda: ["LEFTCTRL", "LEFTALT", "G"])


def parse_keyboard_config(raw: object, where: str = "keyboard") -> KeyboardConfig:
    if not isinstance(raw, dict):
        raise ConfigError(tr('{0}: must be an object', where))
    known = {"enabled", "device", "keymap", "grab", "toggle"}
    kb = KeyboardConfig()
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if k not in known:
            raise ConfigError(tr('{0}: unknown key {1!r}; allowed {2}', where, k, sorted(known)))
        if k in ("enabled", "grab") and not isinstance(v, bool):
            raise ConfigError(f"{where}.{k}: true/false")
        if k in ("device", "toggle"):
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                raise ConfigError(tr('{0}.{1}: a string or a list of strings', where, k))
        if k == "keymap" and not isinstance(v, (str, dict)):
            raise ConfigError(tr('{0}.keymap: a keymap name or an object', where))
        setattr(kb, k, v)
    if kb.enabled and not kb.device:
        raise ConfigError(tr('{0}.device is required when enabled=true (list: gamepad-remapd keyboards)', where))
    return kb


@dataclass
class DaemonConfig:
    config_dir: Path = field(default_factory=default_config_dir)

    # ---- input/output ----
    backend: str = "auto"                 # auto (hidraw+evdev) | hidraw | evdev | hidapi
    xinput_backend: str = "evdev"         # Linux: "evdev" (through the kernel xpad driver) | "usb" (read wired XInput pads directly over USB)
    output_kind: str = "xbox360"          # xbox360 | ds4 | dinput (Linux only; Windows = X360); per device — the Protocol tab
    output_name: Optional[str] = None     
    uinput_path: str = "/dev/uinput"
    # Virtual gamepad D-pad: "hat" (ABS_HAT0X/Y, like a real Xbox 360 / DS4) or "buttons"
    # (BTN_DPAD_UP/DOWN/LEFT/RIGHT) — for games where the hat D-pad does not work on Linux.
    dpad_mode: str = "hat"
    grab_siblings: bool = True            

    # ---- device discovery ----
    auto_open: bool = True                
    auto_layout: bool = True              
    poll_interval_s: float = 1.0          
    overrides: List[dict] = field(default_factory=list)   # [{"vid":"3537","pid":"1010","layout":"my_pad"}]
    default_profile: str = "default"
    language: str = "auto"               # Web UI language: "auto" (browser) or a locale code such as "uk" / "en"

    # ---- performance ----
    realtime: bool = True                 
    realtime_priority: int = 50           # 1..99 (Linux)
    switch_interval_s: float = 0.0005     # sys.setswitchinterval: how long a thread holds the GIL

    # ---- IPC / Web ----
    control_endpoint: Optional[str] = None   # "unix:/path" | "tcp:127.0.0.1:8764"; None = auto
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_enabled: bool = True
    ui_hz: int = 60                       

    # ---- keyboard as gamepad ----
    keyboard: KeyboardConfig = field(default_factory=KeyboardConfig)

    # ---- derived paths ----
    @property
    def profiles_dir(self) -> Path:
        return self.config_dir / "profiles"

    @property
    def devices_dir(self) -> Path:
        return self.config_dir / "devices"

    @property
    def locales_dir(self) -> Path:
        return self.config_dir / "locales"

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
        """Later directories override earlier ones (see load_layouts)."""
        return [BUILTIN_LAYOUTS, self.devices_dir]

    def endpoint(self) -> str:
        if self.control_endpoint:
            return self.control_endpoint
        if os.name == "nt":
            return "tcp:127.0.0.1:8764"
        return f"unix:{default_runtime_dir() / 'control.sock'}"

    def allowed_hosts(self) -> List[str]:
        """Host header values the Web server accepts (protection against DNS rebinding)."""
        p = self.web_port
        hosts = [f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}"]
        if self.web_host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
            hosts.append(f"{self.web_host}:{p}")
        return hosts


_INT_RANGES = {"realtime_priority": (1, 99), "web_port": (1, 65535), "ui_hz": (1, 240)}
_KNOWN = {f for f in DaemonConfig.__dataclass_fields__ if f != "config_dir"}


def init_language(path: Optional[Path] = None, config_dir: Optional[Path] = None) -> str:
    """Sets the language of daemon/CLI messages from daemon.json ("language"; "auto" = English).
    Never raises: a broken config is reported later by load_config, in English."""
    from .i18n import set_language
    cdir = Path(config_dir) if config_dir else default_config_dir()
    lang = "auto"
    try:
        raw = json.loads((Path(path) if path else cdir / "daemon.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("language"), str):
            lang = raw["language"]
    except (OSError, ValueError):
        pass
    return set_language(lang, cdir / "messages")


def load_config(path: Optional[Path] = None, config_dir: Optional[Path] = None) -> DaemonConfig:
    """Reads daemon.json (if present) and returns the configuration with key validation."""
    cfg = DaemonConfig(config_dir=Path(config_dir) if config_dir else default_config_dir())
    p = Path(path) if path else cfg.config_dir / "daemon.json"
    if not p.is_file():
        return cfg
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(tr('{0}: invalid JSON: {1}', p, e)) from None
    if not isinstance(raw, dict):
        raise ConfigError(tr('{0}: the root must be an object', p))
    for k, v in raw.items():
        if k.startswith("_"):
            continue                      # "_comment" etc.
        if k not in _KNOWN:
            raise ConfigError(tr('{0}: unknown key {1!r}; allowed {2}', p, k, sorted(_KNOWN)))
        if k in _INT_RANGES:
            lo, hi = _INT_RANGES[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ConfigError(tr('{0}: {1} must be an integer {2}..{3}', p, k, lo, hi))
        if k == "keyboard":
            cfg.keyboard = parse_keyboard_config(v, f"{p}: keyboard")
            continue
        setattr(cfg, k, v)
    if not isinstance(cfg.language, str) or not (cfg.language == "auto" or __import__("re").fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", cfg.language)):
        raise ConfigError('language: "auto" or a locale code such as "uk", "en", "pt-BR"')
    if cfg.backend not in ("auto", "hidraw", "evdev", "hidapi", "winhid"):
        raise ConfigError("backend: auto | hidraw | evdev | hidapi | winhid")
    if cfg.xinput_backend not in ("evdev", "usb"):
        raise ConfigError('xinput_backend: "evdev" | "usb"')
    return cfg


def read_or_create_token(path: Path) -> str:
    """Token for the control TCP channel. The file is created with mode 0600 (where possible)."""
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
