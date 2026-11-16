# gamepad-remapd (OpenPad)

An async gamepad remapper in Python 3.11+: it reads raw HID reports (`/dev/hidraw*`, evdev, hidapi),
transforms them and passes them to games as a virtual gamepad (Linux — `uinput`, Windows — ViGEm).
It works with gamepads that have no driver "out of the box": 2.4 GHz receivers with a vendor handshake,
DualShock 4 clones, and devices with hidden buttons (M1–M6, C, Z, ML, MR).

* **Versatility.** The layout can be taken from a ready JSON file, built from the HID descriptor, or **learned by the
  calibration wizard** for any hidraw gamepad.
* **Performance.** Processing one report takes tens of microseconds (p99.9 within 1.5 ms; check with `bench`).
* **Remapping.** Buttons to buttons, to several targets, to **keyboard keys and mouse**; dead zones, stick curves,
  the gyro as a stick or mouse (live °/s in the UI, zero calibration in one click), profile switching without a restart.
* **"Protocol" tab in the Web UI:** protocol description, the choice of the gamepad's operating mode (where the device allows it,
  e.g. Switch Pro: full 0x30 with IMU / simple 0x3F), the choice of the output protocol (XInput emulation — recommended;
  native DS4/DInput — experimental) and the "Hide the original" switch.
* **Web UI** (`http://127.0.0.1:8765/`) — a live 2D canvas of buttons and sticks, profile editing, a hex inspector,
  a description of the device protocol (bus, report format, handshake, rumble, gyro).
* **Terminal tools:** monitor, hex sniffer, automatic handshake search, rumble test.

> The state of hardware verification is in [STATUS.md](STATUS.md). The Windows part (ViGEm, SendInput, HidHide)
> is written from documentation and verified only with simulations; see [packaging/windows/README.md](packaging/windows/README.md).

## Documentation

* [Architecture](docs/architecture.md)
* [Configuration reference](docs/configuration.md)
* [Command-line reference](docs/cli.md)
* [Profiles](docs/profiles.md)
* [Layouts, handshakes and keymaps](docs/layouts.md)
* [Calibration and bringing up a new gamepad](docs/calibration.md)
* [Control protocol and Web API](docs/api.md)
* [Troubleshooting](docs/troubleshooting.md)
* [Development](docs/development.md)
* [UI concept (future work)](docs/ui-concept.md)

## Installation (Linux)

```bash
pip install -e ".[web]"
sudo packaging/linux/install.sh    # udev rules, uinput, the input group (then log out and back in)
# (`setup-hide` regenerates 70-gamepad-remapd.rules for your devices — see the section on hiding)
```

Verification:

```bash
gamepad-remapd list
gamepad-remapd serve
```

Windows: `pip install aiohttp vgamepad` + the ViGEmBus driver; `hidapi` is not required — there is a built-in WinAPI backend
(see [packaging/windows/README.md](packaging/windows/README.md)).

**Canary builds.** Every push to `main` that passes the tests publishes a rolling pre-release named
`canary` on the GitHub Releases page (wheel + sdist, version `X.Y.Z.devN+gSHA`):
`pip install --pre <wheel URL>[web]`. These builds are unstable.

From the repository directory without installing: `env PYTHONPATH=src python3 -m gamepad_remapd <command>`.

Configuration — `~/.config/gamepad-remapd/` (example: [`config/daemon.example.json`](config/daemon.example.json)):
`daemon.json`, `profiles/`, `devices/` (custom and calibrated layouts), `keymaps/`.

## Commands

| Command | Purpose |
|---|---|
| `serve` / `daemon` / `web` | the daemon + Web UI / the daemon only / the UI only, for a running daemon |
| `list` | HID devices and the layouts that match them |
| `monitor /dev/hidrawN` | live monitor: which button/axis is recognized (`--raw` — raw reports) |
| `calibrate /dev/hidrawN` | calibration wizard for buttons and axes (`--save`, `--broad`, `--paddles` — add ML/MR) |
| `testpage` | a web page for checking buttons and axes without the daemon (includes the calibration wizard) |
| `sniff` | hex inspector for hidden buttons |
| `handshake-probe /dev/hidrawN` | automatic handshake search for a "silent" receiver interface |
| `autolayout` | layout from the HID descriptor |
| `keyboards` | keyboards usable as input (keyboard → gamepad) |
| `rumble-test /dev/hidrawN` | check rumble manually (`--template`, `--sweep`, `--crc32-at`) |
| `sdl-rumble [--trace]` | rumble through SDL3 as a reference; `--trace` shows what SDL writes to hidraw |
| `check`, `bench`, `udev-rules` | profile check, Pipeline latency, udev access rules (`--hide VID:PID` — evdev markers only) |
| `hidhide --vid-pid VID:PID` | Windows: HidHide commands to hide the original from games |
| `setup-hide` | Linux: automatic setup for hiding the original (udev + systemd service with `CAP_FOWNER`), `--uninstall` |
| `unhide` | Linux: restore permissions on the original's nodes after a daemon crash |
| `sdl-list` | what SDL3 really sees (a check of hiding) |
| `serve --privileged` | Linux: start with `CAP_FOWNER` (sudo + setpriv), so the daemon can hide the original |

## Quick start

1. `gamepad-remapd list` — is there a layout? If so, everything works.
2. No — `gamepad-remapd calibrate /dev/hidrawN --save`: the wizard measures the device's noise itself, asks you to press
   each button and move the sticks, and if needed wakes a silent device with a handshake from the SDL library.
   The result is `devices/calibrated_<vid>_<pid>.json` (it has higher priority than the built-in ones; what is missing — rumble, gyro,
   skipped M-buttons — is filled in from the built-in layout of the same model).
3. Check it in `testpage` or `monitor`.

## Profiles

A profile is JSON (`profiles/*.json`, editable in the Web UI without a restart):

```json
{
  "name": "shooter",
  "button_map": {
    "M1": "KEY_F",
    "M2": ["KEY_LEFTSHIFT", "B"],
    "TOUCHPAD": "MOUSE_LEFT",
    "SQUARE": "NONE"
  },
  "sticks": { "right": { "inner_deadzone": 0.08, "curve": { "type": "linear" } } }
}
```

The keys of `button_map` are the input button names of the layout (see `list`/`monitor`). A target is a button of the virtual
gamepad (`A B X Y LB RB LT RT BACK START GUIDE LS RS UP DOWN LEFT RIGHT M1..M6 C Z ML MR`), a **key**
`KEY_<name>` (`KEY_F`, `KEY_SPACE`, `KEY_LEFTSHIFT`, `KEY_F5`, `KEY_VOLUMEUP` …), a **mouse button**
`MOUSE_LEFT|RIGHT|MIDDLE|SIDE|EXTRA`, a list of targets, or `"NONE"`. Two buttons on one target work as "OR"
(released when both are released).

Keys and mouse are sent through a separate virtual device `Gamepad Remapper Virtual Keyboard`, which is created
only when the profile contains such targets. Held keys are released when the profile changes or the gamepad is disconnected.
Requires access to `/dev/uinput`. On Windows — through SendInput (see packaging/windows/README.md).

The reverse direction — **keyboard as a gamepad** — is described in `keymaps/default.json` and the `keyboard` section of
`daemon.json` (`gamepad-remapd keyboards`).

## D-pad as buttons

Some Linux games do not understand the hat-type D-pad of the virtual gamepad (`ABS_HAT0X/Y`). In `daemon.json`:
`"dpad_mode": "buttons"` — the D-pad is sent as the buttons `BTN_DPAD_UP/DOWN/LEFT/RIGHT`. Warning: SDL/Steam expect a hat for
Xbox 360 by default, so in some games "buttons" may instead break the D-pad — check it in the specific game.

## Hiding the original gamepad

SDL finds gamepads by **opening** the nodes `/dev/input/event*`, `/dev/input/js*` and `/dev/hidraw*`; a node that cannot be
opened is skipped. So after opening the device, the daemon closes (chmod 0600) **all nodes of this physical device**
(hidraw, event, js; keyboards and mice of composite receivers are not touched). Its own descriptors remain working, and SDL sees
only the virtual gamepad. Previously evdev was hidden only by grabbing (EVIOCGRAB) — SDL still opened the "dead" duplicate.

Without the right to close nodes (`CAP_FOWNER` is needed), hiding is **not applied at all** (no half-measures) —
the status in the "Protocol" tab and the log will say so. To make it work:

    gamepad-remapd setup-hide --auto --service        # udev rules + systemd service with CAP_FOWNER (asks for sudo itself)
    gamepad-remapd setup-hide --hide 04b4:2412 --service
    gamepad-remapd setup-hide --auto --dry-run
    gamepad-remapd setup-hide --uninstall
    python3 -m gamepad_remapd serve --privileged

Checking and recovery:

    gamepad-remapd sdl-list
    gamepad-remapd unhide

Permissions are restored when the device is closed or the daemon stops; the bookkeeping is per node (several interfaces of one device). Bluetooth:
we touch only the nodes of the HID device itself, not the BT adapter. WARNING: SDL with HIDAPI does not show evdev devices with the DualShock VID:PID —
for the virtual output in emulators, choose XInput.

## Language

The Web UI texts live in JSON files: `src/gamepad_remapd/web/static/locales/<code>.json` (built-in: `en`, `uk`).
The language is chosen in this order: the picker in the header (remembered by the browser) → `"language"` in
`daemon.json` (`"auto"` by default, or a code such as `"uk"`) → the browser's languages → English.
To add or tweak a language, put `<config>/locales/<code>.json` next to your other config files: it is a flat
`{"key": "text"}` object (copy `en.json` as a template; `"_name"` is the name shown in the picker; `{name}` placeholders
are filled in by the UI; `**text**` makes bold text). Keys you omit fall back to English. Messages produced by the daemon itself (CLI output and `--help`, log lines, API errors, protocol descriptions,
warnings, calibration prompts, generated udev/systemd text) are translated separately, gettext-style: the source
text in the code is English and `src/gamepad_remapd/messages/<code>.json` maps `"English text"` → `"translation"`
(built-in: `uk`; `en.json` is a template with every message mapped to itself — copy it as the start of a new language). Their language is daemon-wide: `"language"` in `daemon.json`, `"auto"` (the default) means English; the header picker changes only the web page. A message missing from the
catalog is shown in English. To add a language or fix a phrase, put `<config>/messages/<code>.json` next to your
other config files (same format; keep the `{0}` / `%s` placeholders; your entries override the built-in ones).
Layout and profile `name` / `description` fields are plain data and stay in English.

## Output protocol

"Protocol" tab → "Output protocol" (set separately for each model; on Windows — only Xbox 360 through ViGEm):

* **XInput emulation (Xbox 360)** — recommended; works in SDL games and emulators (verified: ScarletNX, CTR Turbocharged).
* **Native: DualShock 4 / DInput (generic gamepad)** — experimental. For a virtual device SDL has no mapping
  (and DS4 with the Sony VID:PID is handed by SDL to its own HIDAPI driver), so most games do not recognize this output.
  The UI and the log warn about it.

## Gyro

The profile has a `gyro` section (to a stick or mouse, curves, inversions, activation by a button). The "Profile" tab shows live °/s
and has a "Calibrate zero" button (keep the gamepad still for ~1.5 s; the bias is saved in the profile).

## Rumble

Rumble from games is sent to the physical gamepad: through the hidraw template of the layout (DS4 over USB and Bluetooth with CRC-32),
and for evdev devices — through the kernel FF. Details and limitations are in [STATUS.md](STATUS.md).

## Tests

```bash
env PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -t .
```

## Project layout

```
src/gamepad_remapd/
  input/     hidraw, evdev, hidapi/winhid, keyboard — reading devices; hide_linux — hiding the original
  logic/     layouts, profiles, Pipeline, calibration, automatic handshake search
  output/    uinput (gamepad + virtual keyboard), ViGEm, null
  core/      the device and session manager (the reader runs in a separate thread)
  tools/     monitor, calibrate, testpage, sniffer, rumble-test, sdl-rumble, sdl-list, setup-hide
  web/       Web UI (aiohttp + WebSocket, static files)
  layouts/   built-in layouts (DS4 USB/BT, Flydigi V1 2.4G, Switch Pro)
packaging/   udev, systemd, install.sh, Windows notes
```

## License

MIT — see [LICENSE](LICENSE).
