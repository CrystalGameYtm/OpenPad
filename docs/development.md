# Development

## Set-up

Python 3.11 or newer. The core has no runtime dependencies; extras:

| Extra | Adds |
|-------|------|
| `web` | `aiohttp` (the Web UI) |
| `hidapi` | `hidapi` (Windows, or a fallback backend on Linux) |
| `windows` | `aiohttp`, `hidapi`, `vgamepad` |
| `speed` | `uvloop` (not on Windows) |
| `dev` | `pytest` (the suite itself is written with `unittest`) |

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[web]"
python3 -m gamepad_remapd --help          # or: gamepad-remapd --help
```

To keep experiments away from your own configuration, point the daemon at a scratch directory:

```sh
GAMEPAD_REMAPD_HOME=/tmp/gpr-dev python3 -m gamepad_remapd serve --no-web
```

On Linux, real hardware needs access to `/dev/hidraw*` and `/dev/uinput`: run `sudo packaging/linux/install.sh` (udev rules, the `uinput` module, the `input` group, real-time limits), then log out and in. `gamepad-remapd udev-rules` and `setup-hide` generate rules for your own devices.

## Tests

The suite uses `unittest` and needs no hardware, no `/dev/uinput` and no `aiohttp`: devices, output and time are faked (`tests/fakes.py`).

```sh
env PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -t .
```

This is exactly what CI runs on Python 3.11, 3.12 and 3.13 (`.github/workflows/ci.yml`). `-W error::ResourceWarning` turns an unclosed file or socket into a failure.

Real device captures used as fixtures live in `tests/data/`. When you add a layout, add a test that feeds it a few recorded reports and checks buttons, the hat, axes and (if present) gyro scaling. `tests/calib_sim.py` simulates a device for the calibration wizard.

## Continuous integration

| Workflow | When | What |
|----------|------|------|
| `ci.yml` | every push and pull request | tests on Python 3.11 – 3.13 |
| `canary.yml` | push to `main` | tests, then a rolling `canary` pre-release with a wheel/sdist versioned `X.Y.Z.devN+gSHA` |

## Adding support for a device

1. `gamepad-remapd list`, then see what the daemon does by itself (`autolayout PATH` shows the layout it would build).
2. If the interface is silent, find the handshake (`handshake-probe`; for rumble, `sdl-rumble --trace` shows what SDL writes to the device); add a candidate to `handshakes/library.json` and put the winning steps into the layout's `handshake`.
3. Write the layout in `<config>/devices/` first (it overrides built-ins), check it with `testpage` and `sniff`, then copy it to `src/gamepad_remapd/layouts/`. Format: [layouts](layouts.md). The wizard (`calibrate`) can produce a first version; see [calibration](calibration.md).
4. For rumble, try a template with `rumble-test` before adding `rumble` to the layout.
5. Check the whole path in a game or with `sdl-list`: the virtual gamepad should appear and the original should be hidden.


## Translations

* Web UI texts: `src/gamepad_remapd/web/static/locales/<code>.json`, a flat `{"key": "text"}` object (copy `en.json`).
* Texts produced by Python (CLI, logs, API errors, warnings): the source string in the code is English, passed through `tr("English text {0}", arg)`; `src/gamepad_remapd/messages/<code>.json` maps the English text to the translation and keeps the same `{0}` / `%s` placeholders. `messages/en.json` maps every message to itself and is the template for a new language. A missing entry falls back to English.
* New user-visible text in Python should always go through `tr(...)`; code comments and docstrings are English.
* `tests/test_i18n.py` and `tests/test_locales.py` check the catalogs, so run them after changing texts.

## Conventions for the hot path

The hot path (`logic/pipeline.py`, `logic/layout.py` parser, the compiled profile structures, `output/`) runs once per report on a dedicated thread. When changing it:

* No I/O, logging, JSON or locks per report; allocate as little as possible.
* Move work to profile compilation (`compile_profile`) and leave only reads of precomputed tables on the hot path.
* Hand state to the asyncio thread through atomic reference assignment or reads of plain attributes, and keep UI snapshots throttled (`ui_hz`).
* Measure with `gamepad-remapd bench` before and after.

Other rules the code relies on:

* All writes to `state.json`, profiles and layouts go through an atomic write (temporary file + `os.replace`); `control.token` is created with mode 0600.
* Profile names are validated with `PROFILE_NAME_RE` before they become file names (no path traversal).
* The Web server must keep the `Host` and `Origin` checks and the token check (`web/security.py`, `auth` in `web/app.py`) on every route, including `/ws`.
* The privileged daemon must never act on a path that it read from a file in a user-writable directory without a whitelist (see `restore_stale`), and a runtime directory in a shared place must be created with `ensure_private_dir`.
* A broken user file is logged and skipped; it must never stop the daemon.
* Keyboard grabbing (`EVIOCGRAB`) must always be released on close, on toggle and on exit.
