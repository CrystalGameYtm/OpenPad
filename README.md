# gamepad-remapd (OpenPad)

A gamepad remapper for Linux and Windows with a web interface. It reads your gamepad, changes what the buttons and sticks do,
and shows the result to games as a normal virtual gamepad (an Xbox 360 one by default).

It is made for gamepads that "do not quite work" out of the box: 2.4 GHz receivers, DualShock 4 clones, Switch Pro clones,
and pads with extra buttons (M1–M6, C, Z, paddles).

* Remap buttons to other buttons, several buttons, **keyboard keys and mouse buttons**.
* Dead zones, stick and trigger curves, gyro as a stick or mouse, profiles you can switch without a restart.
* A web page (`http://127.0.0.1:8765/`): a live picture of the gamepad — click a button to change what it does.
* Learns an unknown gamepad with a calibration wizard; hides the original gamepad from games so they see only the virtual one.
* Low latency: processing a report takes tens of microseconds.

> What has been tested on real hardware is in [STATUS.md](STATUS.md). The Windows part is written from documentation and has only been
> tested with simulations: expect small bugs there.

## Install (Linux)

You need Python 3.11+ and `git`. Then, in a terminal:

```bash
git clone https://github.com/CrystalGameYtm/OpenPad.git
cd OpenPad
./install.sh
```

The script puts the program in its own folder (`~/.local/share/gamepad-remapd`), asks for your `sudo` password once to allow access to
gamepads, and starts the program at login. **Log out and back in once** afterwards. To remove it all: `./install.sh --uninstall`
(your profiles are kept). Options: `--no-service` (no autostart), `--no-system` (skip the `sudo` step).

Then plug in the gamepad and open the link that this command prints:

```bash
gamepad-remapd list     # is the gamepad found?
gamepad-remapd link     # the sign-in link of the web page (open it once per browser)
```

Without the script: `pip install -e ".[web]"` and `sudo packaging/linux/install.sh`; run it with `gamepad-remapd serve`.
Pre-release builds (the `canary` release on GitHub) install with `pip install --pre <wheel URL>[web]`.

## Install (Windows)

Install the [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases) driver and restart, install Python 3.11+, then
`pip install ".[windows]"` and `gamepad-remapd serve`. Details and hiding the original: [packaging/windows/README.md](packaging/windows/README.md).

## First steps

1. Open the link from `gamepad-remapd link`. Pick your gamepad at the top: the picture moves when you press buttons.
2. **No layout for your gamepad?** (`list` shows none, or buttons are wrong) run `gamepad-remapd calibrate /dev/hidrawN --save`
   (the `N` is in the `list` output). The wizard asks you to press each button and move the sticks, and saves the result.
3. In the **Buttons** tab click a button on the picture (or in the list) and type what it should do, for example `KEY_SPACE`,
   `MOUSE_LEFT`, `A,RT` or `NONE`. The change works at once; press **Save** to keep the profile.
4. In the game or emulator choose the gamepad named `Gamepad Remapper Virtual X360 Pad` (Xbox 360).

A profile is a small JSON file in `~/.config/gamepad-remapd/profiles/`, also editable by hand
(see [Profiles](docs/profiles.md)):

```json
{
  "name": "shooter",
  "button_map": { "M1": "KEY_F", "M2": ["KEY_LEFTSHIFT", "B"], "SQUARE": "NONE" },
  "sticks": { "right": { "inner_deadzone": 0.08, "curve": { "type": "linear" } } }
}
```

## Settings worth knowing (`~/.config/gamepad-remapd/daemon.json`)

All settings are in the [configuration reference](docs/configuration.md). The ones people ask about:

| Setting | When you need it |
|---|---|
| `"xinput_backend": "usb"` | A wired Xbox 360-type (XInput) pad hangs or stops after replugging with the kernel `xpad` driver, or has no rumble: read it directly over USB instead. Plug it into a USB 3.0 port, not a USB 2.0 hub (rumble). |
| `"web_auth": false` | Do not ask for the sign-in link. Only on a computer that nobody else uses. |
| `"dpad_mode": "buttons"` | A game ignores the D-pad of the virtual gamepad. |
| `"language"` | `"uk"` for Ukrainian messages (the web page also follows the language picker in its header). |

**The game sees two gamepads (the original and the virtual one)?** Allow the daemon to hide the original:
`gamepad-remapd setup-hide --auto --service` (asks for `sudo`). If it was stopped badly and the gamepad stays unavailable:
`gamepad-remapd unhide`. Check what games see with `gamepad-remapd sdl-list`.

## Something does not work

See [Troubleshooting](docs/troubleshooting.md) — the common cases: "no access to /dev/uinput", the gamepad is not listed,
no rumble, the web page asks for a token, a game sees the gamepad twice.

## More

* [Command-line reference](docs/cli.md) · [Configuration](docs/configuration.md) · [Profiles](docs/profiles.md)
* [Layouts, handshakes and keymaps](docs/layouts.md) · [Calibration of a new gamepad](docs/calibration.md)
* [Architecture](docs/architecture.md) · [Web API](docs/api.md) · [Development, tests and translations](docs/development.md)
* [Web UI concept](docs/ui-concept.md) · [Changelog](CHANGELOG.md)

Run from the repository without installing: `env PYTHONPATH=src python3 -m gamepad_remapd <command>`.
Tests: `env PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -t .`

## License

MIT — see [LICENSE](LICENSE).
