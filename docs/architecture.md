# Architecture

`gamepad-remapd` reads a physical gamepad, changes what it sends according to a profile, and exposes the result as a virtual gamepad (plus a virtual keyboard and mouse when a profile asks for key or mouse targets). Reading and writing happen on a dedicated thread with almost no work per report; everything else (device discovery, the UI, profile compilation) is kept away from it.

## Processes and threads

```
                         ┌──────────────────────────── daemon process ────────────────────────────┐
 physical gamepad ──────►│ reader thread (one per device)                                          │
 (hidraw/evdev/hidapi)   │   read() → ReportParser → Pipeline → output backend ──► virtual gamepad │
                         │                                                                         │
                         │ asyncio thread                                                          │
                         │   Manager (hotplug scan, sessions, profiles, options)                   │
                         │   ControlServer (NDJSON over unix socket / tcp)                         │
                         └───────────────▲─────────────────────────────────────────────────────────┘
                                         │ control channel
                         ┌───────────────┴──────── web process (spawned) ────────────┐
 browser ◄──── HTTP / WebSocket ────────►│ aiohttp: REST → control commands, events → /ws │
                                         └──────────────────────────────────────────┘
```

* `gamepad-remapd serve` / `daemon` starts the daemon; `serve` also spawns the **Web process** (`multiprocessing`, spawn mode, so it inherits nothing from the daemon). A crash or restart of the Web process does not affect remapping, and the daemon runs without it (and without `aiohttp` installed).
* In the daemon, one **reader thread per open device** does the hot path. The **asyncio thread** does everything else and never touches a report on the hot path. The only thing the asyncio thread does to a running pipeline is an atomic reference assignment (`pipeline.cp = new_compiled_profile`).
* The Web process (and any script) talks to the daemon through the control channel; see [API](api.md). The diagnostic CLI commands (`list`, `monitor`, `sniff`, `calibrate`, …) do not use it: they open devices directly, so they work without the daemon (and may share a device with it).

## Package map (`src/gamepad_remapd/`)

| Path | Role |
|------|------|
| `cli.py`, `daemon.py`, `__main__.py` | Commands, process start-up, supervision of the Web process. |
| `config.py`, `store.py`, `constants.py`, `util.py`, `i18n.py` | `daemon.json`, profile files and `state.json` (atomic writes), kernel constants and canonical button names, helpers, message catalogs. |
| `core/manager.py` | Hotplug polling (`poll_interval_s`), layout choice (`pick_layout`), session lifecycle, options, live-state ticks, keyboard devices. |
| `core/session.py` | One open device: handshake, reader thread, pipeline, output backend, hiding, snapshots. |
| `core/realtime.py` | Interpreter tuning (`sys.setswitchinterval`, `gc.freeze`) and thread priority (`SCHED_FIFO`, or `nice -5`; `THREAD_PRIORITY_HIGHEST` and a 1 ms timer on Windows). |
| `core/sysops.py` | Operations that used to be CLI-only and are also available over the control channel (system info, SDL list, unhide, udev rules, autolayout, rumble). |
| `input/` | Input backends: `hidraw_linux`, `evdev_linux`, `hidapi_backend`, `winhid` (Windows HID via ctypes), `keyboard_linux`; `hide_linux` (hiding the original); `base` (the `InputDevice` contract, `DeviceInfo`, `is_streaming`). |
| `input/xusb_linux.py` | Direct-USB reading of Xbox 360-protocol pads (usbfs, async URBs) as synthetic evdev-format reports; opt-in via `xinput_backend: "usb"`. |
| `logic/layout.py` | `DeviceLayout` and `ReportParser`: layout JSON → fast report decoder. |
| `logic/profile.py`, `curves.py` | Profile validation (`normalize_profile`), compilation into the structures the hot path reads (`compile_profile`), JSON merge patch, response curves as lookup tables. |
| `logic/pipeline.py` | The hot path: report → virtual gamepad state. |
| `logic/autolayout.py`, `hid_descriptor.py`, `evdev_layout.py` | Layouts built from a HID report descriptor or evdev capabilities. |
| `logic/calibrate.py`, `gyrocal.py`, `handshake_lib.py`, `sniffer.py` | Calibration wizard, gyro zero, handshake library, hex sniffer. |
| `logic/keyboard.py`, `keytargets.py`, `outputs.py`, `protocol.py` | Keyboard-as-gamepad keymaps, key/mouse target names, which output protocols to offer, language-neutral description of a device's protocol. |
| `output/` | Virtual devices: `uinput_linux` (Xbox 360, DualShock 4, generic DInput), `uinput_keyboard` (keys and mouse), `vigem_windows` (Xbox 360 through ViGEm), `sendinput_windows`, `dry`, `null`. |
| `ipc/` | Control channel: `protocol` (framing, endpoints), `server` (commands), `client`. |
| `web/` | `app.py` (REST, WebSocket), `security.py` (Host/Origin checks), `locales.py`, `static/` (single-page UI, test page, `locales/*.json`). |
| `tools/` | Implementations of the diagnostic commands (`monitor`, `testpage`, `calibrate`, `handshake-probe`, `setup-hide`, `udev`, `sdl_list`, `sdl_rumble`, `rumble_test`, `bench`, `hidhide`). |
| `layouts/`, `profiles/`, `keymaps/`, `handshakes/`, `messages/` | Built-in data files. |

## Life of a device

1. **Discovery.** Every `poll_interval_s` the Manager enumerates devices through the configured backend (`auto` = hidraw + evdev on Linux) and compares the set with the previous one. A `devices` event is emitted when it changed. evdev nodes that duplicate a device already read through hidraw with a layout are dropped.
2. **Layout.** `pick_layout` chooses the layout (explicit `overrides`, then a `match`, then one generated from the descriptor); see [layouts](layouts.md).
3. **Open** (automatically when `auto_open` is on, or on request). The Session opens the device, runs the layout's handshake (skipped if the device is already streaming), creates the output backend for the chosen protocol, compiles the active profile and starts the reader thread. Device options (`output_kind`, `hide`) are read from `state.json`, falling back to `daemon.json`.
4. **Run.** The reader thread loops: blocking read → decode → pipeline → write one report to the output. At most `ui_hz` times per second the asyncio thread takes a snapshot of the pipeline's state for the UI.
5. **Close.** On unplug, on `close`, or when the daemon stops: the reader stops, the virtual device is destroyed, hidden nodes get their permissions back.

## The hot path

For each input report, the reader thread does the following and nothing else:

1. **Filter** by length, Report ID and `sync` byte.
2. **Decode** with a precompiled `ReportParser`: one `int.from_bytes` for all buttons (a Python int of any size, so there is no limit on simultaneously pressed buttons), one lookup table for the hat, one `struct.unpack_from` for all numeric fields.
3. **Map**: pressed buttons through the compiled button table to targets (virtual buttons, `LT`/`RT`, keys, mouse buttons). Targets are reference-counted, so two sources on one target do not release each other.
4. **Sticks and triggers:** centre, dead zone, curve (lookup table with interpolation), sensitivity, inversion; trigger stages with hysteresis. A stick whose raw input did not change returns its cached result.
5. **Gyro:** rotation rate minus bias, dead zone, curve, smoothing, added to the right or left stick or sent as mouse motion.
6. **Emit** a single `write()` with all events and `SYN_REPORT` to the virtual device (Linux), or one report to ViGEm (Windows).

Everything expensive (curve tables, bit tables, name resolution) is done while compiling the profile, outside the hot thread. The interpreter is tuned at start-up: a short GIL switch interval (0.5 ms by default), `gc.freeze()` and a large GC threshold, so collections do not pause the reader. `gamepad-remapd bench` measures the processing time of one report; the target is a p99.9 below 1.5 ms.

## Output

| Platform | Output |
|----------|--------|
| Linux | `uinput`: `xbox360` (recommended: games and SDL recognise it), `ds4` and `dinput` (experimental: SDL has no suitable mapping, see "Output protocol" in the README). `dpad_mode` chooses a hat or four buttons for the D-pad. Key and mouse targets use a second virtual device created on demand. |
| Windows | ViGEm virtual Xbox 360 (`vgamepad`); keys and mouse through `SendInput`. |

Rumble comes back from the game to the virtual device (force feedback) and is written to the physical device through the layout's `rumble` template.

## Hiding the original device

If both the physical and the virtual gamepad are visible, games see two. SDL discovers gamepads by opening `/dev/input/event*`, `/dev/input/js*` and `/dev/hidraw*`, so the daemon makes all nodes of the physical device unopenable for others (`chmod 0600`; it needs `CAP_FOWNER`) while its own descriptors keep working. Every node it changed is recorded in `<runtime dir>/hidden.json` and restored on close, on exit, at the next start after a crash, or with `gamepad-remapd unhide`. Without `CAP_FOWNER` hiding is not applied at all (the Protocol tab and the log say so). `setup-hide --auto --service` sets up udev rules and a systemd service with the capability; `serve --privileged` does it for one run. On Windows, HidHide does the same (`hidhide` prints the commands). Details in the README and the [command-line reference](cli.md).

## Control channel and Web UI

The control channel is NDJSON over a UNIX socket (or token-protected loopback TCP), described in [API](api.md). The Web process keeps one connection to the daemon, forwards REST calls as commands and forwards events to every browser through `/ws` with per-client bounded queues. Requests are checked against allowed `Host` and `Origin` values (protection from DNS rebinding and cross-site requests). The language of a page is sent with each request, so texts that the daemon builds come back in the page's language.

## Design rules

* The hot path allocates little and does no I/O, no logging and no JSON; the UI gets throttled snapshots.
* Layouts and profiles are data. New hardware should need a JSON file, not code.
* All writes to `state.json`, profiles and layouts are atomic (temporary file + `os.replace`).
* A broken user file (layout, profile, keymap) is logged and skipped; it does not stop the daemon.
