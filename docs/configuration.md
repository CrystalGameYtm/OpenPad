# Configuration Reference

`gamepad-remapd` reads its runtime settings from `daemon.json` and stores user profiles, custom device layouts, keyboard keymaps, translation overrides, and persisted device state inside a single configuration directory. All configuration keys are optional; if `daemon.json` does not exist, built-in defaults are used.

## Configuration and Runtime Directories

### Configuration Directory Lookup Order

`default_config_dir()` in `src/gamepad_remapd/config.py` resolves the configuration directory in the following order (unless overridden on the command line with `--config-dir`):

1. `GAMEPAD_REMAPD_HOME` environment variable (if set and non-empty).

2. On Windows (`os.name == "nt"`): `%APPDATA%\gamepad-remapd` (falling back to `~/gamepad-remapd` if `APPDATA` is unset).

3. On POSIX (Linux/macOS): `$XDG_CONFIG_HOME/gamepad-remapd` (falling back to `~/.config/gamepad-remapd` if `XDG_CONFIG_HOME` is unset).

The path to `daemon.json` itself defaults to `<config_dir>/daemon.json` and can be overridden with `--config <path>`.

### Directory Contents

| Path | Purpose | Override Behavior | 
| ----- | ----- | ----- | 
| `daemon.json` | Main daemon and Web UI settings | Optional; missing file uses `DaemonConfig` defaults | 
| `profiles/*.json` | User remapping profiles | Overrides built-in profiles in `src/gamepad_remapd/profiles/` by stem name | 
| `devices/*.json` | User, auto-generated (`auto_*.json`), learned (`learned_*.json`), and calibrated (`calibrated_*.json`) layouts | Overrides built-in layouts in `src/gamepad_remapd/layouts/` by layout ID; files starting with `_` are ignored | 
| `keymaps/*.json` | Keyboard-to-gamepad mappings | Overrides built-in keymaps in `src/gamepad_remapd/keymaps/` by stem name | 
| `handshakes/*.json` | User handshake probe candidates | Overrides/extends built-in `src/gamepad_remapd/handshakes/library.json` by candidate `id` | 
| `locales/<code>.json` | Web UI translation overrides | Overrides/extends built-in `src/gamepad_remapd/web/static/locales/<code>.json` | 
| `messages/<code>.json` | Daemon, CLI, and API message translations | Overrides/extends built-in `src/gamepad_remapd/messages/<code>.json` | 
| `state.json` | Persisted active profile, protocol mode, and per-model options (`output_kind`, `hide`) | Written atomically by `ProfileStore` | 
| `control.token` | 32-character hex authentication token for TCP IPC | Created with mode `0600` when `control_endpoint` uses `tcp:` | 

### Runtime Directory

`default_runtime_dir()` in `src/gamepad_remapd/config.py` determines where transient runtime files are stored:

1. `$XDG_RUNTIME_DIR/gamepad-remapd` if `XDG_RUNTIME_DIR` is set.

2. `<tempdir>/gamepad-remapd-<uid>` on POSIX systems when `XDG_RUNTIME_DIR` is unset (for example `/tmp/gamepad-remapd-1000`).

3. `<tempdir>/gamepad-remapd` on non-POSIX systems.

This directory holds the default UNIX control socket (`control.sock`) and the Linux permission-hiding state file (`hidden.json`).

## `daemon.json` Schema

### General Settings

| Key | Type | Default | Allowed / Range | Description | 
| ----- | ----- | ----- | ----- | ----- | 
| `backend` | `string` | `"auto"` | `"auto"`, `"hidraw"`, `"evdev"`, `"hidapi"`, `"winhid"` | Input enumeration and reading backend. `"auto"` selects `hidraw` + `evdev` on Linux when `/sys/class/hidraw` exists, and `hidapi` (falling back to `winhid` on Windows) otherwise. | 
| `xinput_backend` | `string` | `"evdev"` | `"evdev"`, `"usb"` | How wired Xbox 360-protocol XInput pads (class ff/5d/01) are read on Linux. `"evdev"` uses the kernel `xpad` driver; `"usb"` reads the device directly over usbfs (`/dev/bus/usb`), detaching `xpad` while the session is open, and falls back to evdev if the node is not accessible. Needs the udev rule from `packaging/linux/udev`. Hiding is not needed in this mode (the kernel creates no node). |
| `output_kind` | `string` | `"xbox360"` | `"xbox360"`, `"ds4"`, `"dinput"` | Default virtual gamepad protocol. On Windows only `"xbox360"` (ViGEm) is offered. Per-model overrides are saved in `state.json`. | 
| `output_name` | `string` or `null` | `null` | Any string or `null` | Custom virtual device name. When `null`, uses `"<layout.name> (remapped)"`. | 
| `uinput_path` | `string` | `"/dev/uinput"` | Filesystem path | Path to the Linux `uinput` device node. | 
| `dpad_mode` | `string` | `"hat"` | `"hat"`, `"buttons"` | Virtual gamepad D-pad encoding on Linux (`ABS_HAT0X`/`ABS_HAT0Y` vs. `BTN_DPAD_UP/DOWN/LEFT/RIGHT`). | 
| `grab_siblings` | `bool` | `true` | `true`, `false` | Default setting for hiding the physical device's original nodes from other applications (`SDL`, games) when no per-model override exists in `state.json`. | 
| `auto_open` | `bool` | `true` | `true`, `false` | Automatically open a remapping session during device scans whenever a layout matches. | 
| `auto_layout` | `bool` | `true` | `true`, `false` | Synthesize a layout automatically from the HID report descriptor (`hidraw`) or kernel capabilities (`evdev`) if no explicit layout matches. | 
| `poll_interval_s` | `float` | `1.0` | Positive number | Device hotplug scan interval in seconds. | 
| `default_profile` | `string` | `"default"` | Profile name | Profile loaded when a device model has no entry under `active` in `state.json`. | 
| `language` | `string` | `"auto"` | `"auto"` or `[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?` | Language of daemon, CLI, log and API-error messages; `"auto"` means English (the system locale is not consulted). The Web UI follows its own header picker first, then this key, and with `"auto"` the browser language. Translations: `messages/<code>.json`, see [Architecture](architecture.md) if present. | 
| `overrides` | `list[object]` | `[]` | List of override objects | Explicit VID/PID-to-layout bindings evaluated before automatic layout matching. | 

#### `overrides` Entry Keys

Each object in `overrides` is checked in `Manager.pick_layout()` (`src/gamepad_remapd/core/manager.py`):

| Key | Type | Required | Description | 
| ----- | ----- | ----- | ----- | 
| `vid` | `string` | Yes | Hexadecimal USB/Bluetooth Vendor ID (e.g. `"3537"`). | 
| `pid` | `string` | Yes | Hexadecimal Product ID (e.g. `"1010"`). | 
| `layout` | `string` | Yes | Layout ID to bind (must exist in loaded layouts). | 
| `interface` | `int` or `null` | No | Optional USB interface number filter; `null` or omitted matches any interface. | 

### Performance and Scheduling Settings

| Key | Type | Default | Allowed / Range | Description | 
| ----- | ----- | ----- | ----- | ----- | 
| `realtime` | `bool` | `true` | `true`, `false` | Attempt to elevate the reader thread's OS scheduling priority (`SCHED_FIFO` on Linux, `THREAD_PRIORITY_HIGHEST` + 1 ms timer on Windows). | 
| `realtime_priority` | `int` | `50` | `1..99` | Real-time `SCHED_FIFO` priority on Linux (clamped to `os.sched_get_priority_max(os.SCHED_FIFO)`). Falls back to `nice(-5)` if unprivileged. | 
| `switch_interval_s` | `float` | `0.0005` | Positive number | Python thread switch interval (`sys.setswitchinterval`) configured before reader threads start. | 

### IPC and Web UI Settings

| Key | Type | Default | Allowed / Range | Description | 
| ----- | ----- | ----- | ----- | ----- | 
| `control_endpoint` | `string` or `null` | `null` | `"unix:/path"`, `"tcp:host:port"`, or `null` | Control channel endpoint (see [CLI](cli.md) `serve`). When `null`, defaults to `"unix:<runtime_dir>/control.sock"` on POSIX and `"tcp:127.0.0.1:8764"` on Windows. | 
| `web_enabled` | `bool` | `true` | `true`, `false` | Start the Web UI subprocess when running `gamepad-remapd serve`. Overridden by `--no-web`. | 
| `web_host` | `string` | `"127.0.0.1"` | Host/IP string | Bind address for the Web UI HTTP/WebSocket server. Non-loopback hosts are automatically added to `DaemonConfig.allowed_hosts()`. | 
| `web_port` | `int` | `8765` | `1..65535` | TCP port for the Web UI server. Overridden by `--port`. | 
| `ui_hz` | `int` | `60` | `1..240` | Maximum broadcast frequency (Hz) for live device state snapshots over IPC/WebSocket. | 

### `keyboard` Section (Keyboard as Gamepad)

On Linux, `gamepad-remapd` can read one or more physical keyboard event nodes and emit virtual gamepad inputs. To prevent accidentally locking the user's keyboard, `keyboard.enabled` defaults to `false`.

| Key | Type | Default | Allowed / Range | Description | 
| ----- | ----- | ----- | ----- | ----- | 
| `enabled` | `bool` | `false` | `true`, `false` | Enable keyboard-as-gamepad mode. Requires `keyboard.device` to be non-empty when `true`. | 
| `device` | `string` or `list[str]` | `[]` | Non-empty string(s) | Selector(s) matched against `/dev/input/eventN` path, case-insensitive substring of device name, or `vid:pid` hex string. | 
| `keymap` | `string` or `object` | `"default"` | Keymap name or inline object | Name of a keymap JSON file (without `.json`) in `<config_dir>/keymaps/` or built-in `keymaps/`, or an inline keymap object. | 
| `grab` | `bool` | `true` | `true`, `false` | Exclusively grab (`EVIOCGRAB`) matched keyboard nodes once all keys are released. | 
| `toggle` | `string` or `list[str]` | `["LEFTCTRL", "LEFTALT", "G"]` | Key name(s) or evdev code(s) | Key chord that toggles keyboard-as-gamepad mode on/off and immediately releases `EVIOCGRAB` when disabled. | 

## Validation Rules in `config.py`

`load_config()` and `parse_keyboard_config()` enforce the following rules when parsing `daemon.json`:

1. **Root type**: The root JSON value must be an object (`if not isinstance(raw, dict): raise ConfigError(...)`).

2. **Comment keys**: Any key starting with `_` (such as `"_comment"`) is ignored both at the root level and inside `keyboard`.

3. **Unknown keys**: Any key not in `DaemonConfig` fields (excluding `config_dir`) raises `ConfigError` listing all allowed keys. Inside `keyboard`, only `{"enabled", "device", "keymap", "grab", "toggle"}` (and `_*`) are accepted.

4. **Integer bounds**: `realtime_priority` (`1..99`), `web_port` (`1..65535`), and `ui_hz` (`1..240`) must be integers (`isinstance(v, int) and not isinstance(v, bool)`) within their inclusive bounds.

5. **Language format**: `language` must be `"auto"` or match `^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$`.

6. **Backend enum**: `backend` must be one of `("auto", "hidraw", "evdev", "hidapi", "winhid")`.

7. **Keyboard types and required device**:

   * `keyboard` must be a JSON object.

   * `keyboard.enabled` and `keyboard.grab` must be booleans.

   * `keyboard.device` and `keyboard.toggle` accept a single string (coerced to a 1-element list) or a list of non-empty strings.

   * `keyboard.keymap` must be a string or an object.

   * If `keyboard.enabled` is `true`, `keyboard.device` must not be empty (`if kb.enabled and not kb.device: raise ConfigError(...)`).

## Keymap Files (`keymaps/*.json`)

Keymaps used by `keyboard.keymap` are validated by `KeyMap.from_dict()` in `src/gamepad_remapd/logic/keyboard.py`:

| Key | Type | Required | Description | 
| ----- | ----- | ----- | ----- | 
| `name` | `string` | No | Display name (defaults to the file stem or `"inline"`). | 
| `keys` | `object` | Yes (non-empty) | Map of keyboard key name (`"W"`, `"KEY_W"`, `"SPACE"`, `"LEFTSHIFT"`, etc., or integer evdev key code) to gamepad target: standard/dpad/extra button (`A`, `B`, `X`, `Y`, `LB`, `RB`, `BACK`, `START`, `GUIDE`, `LS`, `RS`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `M1`–`M6`, `C`, `Z`, `ML`, `MR`), trigger (`LT`, `RT`), or stick direction (`LS_UP`, `LS_DOWN`, `LS_LEFT`, `LS_RIGHT`, `RS_UP`, `RS_DOWN`, `RS_LEFT`, `RS_RIGHT`). | 
| `options` | `object` | No | Optional movement modifiers: `walk_key` (key name/code, default `null`), `walk_scale` (float `0.05..1.0`, default `0.5`), `circle` (bool, normalizes diagonal stick vectors by $\sqrt{0.5}$, default `true`). | 

## Persisted State File (`state.json`)

`ProfileStore` (`src/gamepad_remapd/store.py`) persists per-model selections in `<config_dir>/state.json` keyed by `<vid>:<pid>` (lowercase 4-digit hex, e.g. `"054c:09cc"`):

```
{
  "active": {
    "054c:09cc": "default"
  },
  "modes": {
    "057e:2009": "nintendo_switch_pro"
  },
  "opts": {
    "04b4:2412": {
      "output_kind": "xbox360",
      "hide": true
    }
  }
}

```

## Environment Variables

| Variable | Module | Effect | 
| ----- | ----- | ----- | 
| `GAMEPAD_REMAPD_HOME` | `config.py` | Overrides the configuration directory path. | 
| `XDG_CONFIG_HOME` | `config.py` | Base config directory on POSIX when `GAMEPAD_REMAPD_HOME` is unset. | 
| `APPDATA` | `config.py` | Base config directory on Windows when `GAMEPAD_REMAPD_HOME` is unset. | 
| `XDG_RUNTIME_DIR` | `config.py` | Base directory for the UNIX control socket (`control.sock`) and `hidden.json`. | 
| `GAMEPAD_REMAPD_GROUP` | `input/hidraw_linux.py` | Group name checked for legacy `71-…hide-hidraw` udev rules (default `"gamepad-remapd"`). | 
| `GAMEPAD_REMAPD_NO_REEXEC` | `cli.py` | Disables the automatic re-execution (`sudo`/`setpriv`/group) in `serve`. | 
| `SUDO_USER` | `cli.py` | Target username when elevating via `sudo` in `serve` or `setup-hide`. | 

## Example `daemon.json`

Every key below is accepted by `load_config()` and `parse_keyboard_config()` in `src/gamepad_remapd/config.py`:

```
{
  "_comment": "User configuration in ~/.config/gamepad-remapd/daemon.json",
  "backend": "auto",
  "output_kind": "xbox360",
  "output_name": null,
  "uinput_path": "/dev/uinput",
  "dpad_mode": "hat",
  "grab_siblings": true,
  "auto_open": true,
  "auto_layout": true,
  "poll_interval_s": 1.0,
  "default_profile": "default",
  "language": "auto",
  "overrides": [
    {
      "vid": "04b4",
      "pid": "2412",
      "interface": 2,
      "layout": "flydigi_v1_2g4"
    }
  ],
  "realtime": true,
  "realtime_priority": 50,
  "switch_interval_s": 0.0005,
  "control_endpoint": null,
  "web_enabled": true,
  "web_host": "127.0.0.1",
  "web_port": 8765,
  "ui_hz": 60,
  "keyboard": {
    "enabled": false,
    "device": ["HELLYBERRY"],
    "keymap": "default",
    "grab": true,
    "toggle": ["LEFTCTRL", "LEFTALT", "G"]
  }
}

```