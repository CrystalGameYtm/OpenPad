# Layouts, handshakes and keymaps

A **layout** describes what a physical device sends: which byte and bit is each button, where the axes, triggers and gyroscope are, how to wake the device (handshake) and how to make it rumble. It is data, not code: supporting a new gamepad usually means writing one JSON file. What the daemon does with the values is described by a [profile](profiles.md).

## Where layouts come from

| Source | Location | Notes |
|--------|----------|-------|
| Built-in | `src/gamepad_remapd/layouts/*.json` | `flydigi_v1_2g4`, `nintendo_switch_pro`, `nintendo_switch_pro_simple`, `sony_ds4_bt`, `sony_ds4_usb`. |
| User | `<config>/devices/*.json` | Same id overrides the built-in one. Files whose name starts with `_` are skipped (templates, see `layouts/_template_2g4_receiver.json`). |
| Generated | in memory, or saved into `<config>/devices/` | Built from the HID report descriptor (`autolayout`, `auto_layout: true`) or from evdev capabilities. |

The layout id is the file name without `.json`.

Files written by the tools:

| File | Written by |
|------|------------|
| `auto_<vid>_<pid>[_ifN].json` | `autolayout --save`, the "layout from descriptor" action in the UI. Button and axis roles are heuristics; check them with `testpage`. |
| `calibrated_<vid>_<pid>[_ifN].json` | The calibration wizard (`calibrate --save`, or the wizard on the test page). |
| `learned_<vid>_<pid>_ifN.json` | `handshake-probe --save`: a stream layout of a known family bound to this device together with the handshake that was found. |

## Which layout is chosen

`Manager.pick_layout` for a hidraw device:

1. An entry of `overrides` in `daemon.json` (`vid`, `pid`, `layout`, optional `interface`) wins.
2. Otherwise all layouts whose `match` fits the device (`vid`, one of `pid`, `interface` and `bus` if given) are candidates. They are sorted by: a layout with an `interface` before one without, then higher `priority`, then the default of a mode group. A mode chosen by the user for this model (saved in `state.json`, key `modes`) is preferred among candidates.
3. If nothing matches and `auto_layout` is `true`, a layout is generated from the descriptor.

evdev devices always use their own generated layout. A layout made by the wizard gets `priority` 10, so it wins over a built-in one for the same device; it also inherits rumble, gyro and extra buttons from the built-in layout of the same VID/PID when that does not contradict what was learned (the note is stored in `notes`).

## File format

```json
{
  "name": "Example pad (USB, report 0x01)",
  "match": { "vid": "054c", "pid": ["05c4", "09cc"], "bus": "usb", "interface": 3 },
  "report_id": 1,
  "read_size": 64,
  "min_len": 10,
  "priority": 0,
  "sync": { "byte": 0, "value": "01" },

  "buttons": {
    "CROSS":  { "byte": 5, "bit": 5, "target": "A" },
    "L1":     { "byte": 6, "bit": 0, "target": "LB" },
    "TOUCH":  { "byte": 7, "bit": 1, "target": null }
  },
  "hat": { "byte": 5, "shift": 0, "mask": 15, "first": 0 },

  "fields": {
    "LX": { "offset": 1, "fmt": "u8", "center": 128 },
    "LY": { "offset": 2, "fmt": "u8", "center": 128 },
    "L2": { "offset": 8, "fmt": "u8" },
    "GYRO_X": { "offset": 13, "fmt": "s16le" },
    "GYRO_Y": { "offset": 15, "fmt": "s16le" }
  },
  "sticks":   { "left": ["LX", "LY"] },
  "triggers": { "left": "L2" },
  "gyro":     { "yaw": "GYRO_Y", "pitch": "GYRO_X", "dps_per_lsb": 0.061 },

  "handshake": [
    { "op": "output", "data": "05 ec" },
    { "op": "sleep_ms", "ms": 100 }
  ],
  "rumble": { "op": "output", "template": "05 01 00 00 {weak} {strong}", "pad_to": 32 }
}
```

**All `byte` and `offset` values are indexes into the array returned by `read()`, which includes the Report ID byte (byte 0) when the device uses numbered reports.**

### Top level

| Key | Meaning |
|-----|---------|
| `name` | Display name. |
| `match` | `vid` (hex string, required), `pid` (hex string or list of them, required), optional `interface` (number) and `bus` (`usb`, `bluetooth`/`bt`, or the kernel number 3/5). A layout without `vid` or `pid` never matches (it can still be chosen by id). |
| `report_id` | Report ID of state reports. Reports with another first byte are ignored. |
| `read_size` | Buffer size for one read (default 64). |
| `min_len` | Minimum report length; raised automatically to cover every described field. |
| `sync` | `{"byte": N, "value": "FE"}`: a signature. Reports where that byte differs are not parsed. Use it when the device sends service replies with the same Report ID. |
| `priority` | Higher wins among layouts that match equally (default 0). |
| `mode` | `{"group", "id", "label", "note", "default"}`. Layouts with the same `group` and the same VID/PID are modes of one gamepad; the user picks one in the UI (for example Switch Pro: full report with gyro or simple report). |
| `handshake`, `rumble` | See below. |
| `_comment`, `_learned` | Free notes. `_learned.method: "calibration"` marks a wizard result. |

### `buttons`

`"NAME": { "byte": B, "bit": 0..7, "target": T }`. `NAME` is any name you like (it is what `button_map` in a profile refers to); `DPAD_UP/DOWN/LEFT/RIGHT` are reserved for the hat. `target` is the default output of the button: a standard button, a D-pad direction, an extra button (`M1`…`M6`, `C`, `Z`, `ML`, `MR`), `LT`/`RT`, or `null` for "no default action". Two buttons cannot share one bit.

### `hat`

`{ "byte", "shift" (0), "mask" (0x0F), "first" (0) }`: the D-pad as a hat switch. The value `(report[byte] >> shift) & mask` minus `first` is read as 0 = up, 1 = up-right, 2 = right, … 7 = up-left (clockwise); any other value means released. For a D-pad that is four separate bits, describe them as ordinary `buttons` with targets `UP`/`DOWN`/`LEFT`/`RIGHT`.

### `fields`

Numeric values: axes, triggers, gyroscope, accelerometer.

| Key | Meaning |
|-----|---------|
| `offset` | Index of the first byte. |
| `fmt` | `u8`, `s8`, `u16le`, `s16le`, `u16be`, `s16be`, `u32le`, `s32le` (default `u8`). |
| `bits`, `bit_offset`, `signed` | Alternative to `fmt` for bit-packed fields (1..32 bits starting at `bit_offset` of the little-endian value at `offset`). |
| `raw_min`, `raw_max` | Raw range (default: the full range of the type). `raw_max` must be greater. |
| `center` | Rest value of a stick (default: the middle of the range). |
| `invert` | Flip the sign (for axes where "up" is positive). |

`sticks` maps `left`/`right` to `[xField, yField]`, `triggers` maps `left`/`right` to a field name. `gyro` is `{"yaw": field, "pitch": field, "dps_per_lsb": number}` (and optionally `roll`): it names the fields that carry rotation and the scale from sensor units to °/s. Accelerometer fields may be described but are not used for output.

### `handshake`

Steps executed once when the device is opened, in order:

| `op` | Fields | Meaning |
|------|--------|---------|
| `output` | `data` (hex) | Write an output report; the first byte is the Report ID. |
| `feature_set` | `data` (hex) | Send a feature report. |
| `feature_get` | `report_id`, `length` | Read a feature report (some devices switch mode when it is read). |
| `sleep_ms` | `ms` | Wait. |

Any step may have `"bus": "usb"` or `"bluetooth"` to run only on that bus. The handshake is skipped when the device is already streaming reports.

### `rumble`

`{ "op": "output" | "feature", "template": "...", "pad_to": N, "crc32": {"seed": "a2", "at": 74} }`.

The template is a list of hex bytes and placeholders: `{strong}` and `{weak}` (one byte, 0..255) or `{strong16}` and `{weak16}` (two bytes, little-endian). `pad_to` pads the report with zeros. `crc32` appends a CRC-32 computed over `seed` + the report bytes before position `at`, stored little-endian at `at` (needed for the DualShock 4 over Bluetooth). Test templates with `rumble-test` before putting them into a layout. Not every device accepts rumble from the host (the Flydigi 2.4 GHz DInput receiver does not).

## Handshake library

`src/gamepad_remapd/handshakes/library.json` (plus `<config>/handshakes/*.json`) is the list of known wake-up sequences that the tools `handshake-probe`, `calibrate --broad` and the test page try on a device that stays silent. **The daemon itself does not read the library**: it runs the `handshake` written in the chosen layout. `handshake-probe --save` copies the sequence that worked into a `learned_*` layout.

```json
{
  "candidates": [
    {
      "id": "flydigi_v1",
      "name": "Flydigi V1 (2.4 GHz receiver, vendor interface 2)",
      "source": "where the bytes come from",
      "scope": "vendor",
      "vids": ["04b4"],
      "usage_pages": ["ffa0"],
      "layout": "flydigi_v1_2g4",
      "steps": [
        { "op": "output", "data": "05 ec", "pad_to": 12 },
        { "op": "sleep_ms", "ms": 100 }
      ],
      "expect": { "report_id": 4, "sync": { "byte": 1, "value": "fe" }, "min_len": 25 }
    }
  ]
}
```

| Key | Meaning |
|-----|---------|
| `id` | Unique id; a user file overrides a built-in entry with the same id. |
| `scope` | `any`: safe for every device (empty or read-only steps); `vendor`: only when the VID is in `vids` (and the interface usage page is in `usage_pages`, if given); `opt-in`: another vendor's packets, tried only with `--broad`. Default `opt-in`. |
| `steps` | `output` / `feature_set` (`data`, optional `pad_to`), `feature_get` (`report_id`, `length`), `sleep_ms` (`ms`, 0..2000). |
| `expect` | What a useful report looks like afterwards (`report_id`, `sync`, `min_len`, `magic`); used to tell real state reports from replies to the commands. |
| `layout` | Id of the stream layout to bind to the device if this candidate works. |

The library never invents bytes: it only tries what is written in it.

## Keymaps (keyboard as gamepad)

`src/gamepad_remapd/keymaps/default.json` and `<config>/keymaps/<name>.json`:

```json
{
  "name": "default",
  "keys": { "W": "LS_UP", "SPACE": "A", "Z": "LT", "I": "UP" },
  "options": { "walk_key": "LEFTALT", "walk_scale": 0.5, "circle": true }
}
```

Keys are kernel key names without the `KEY_` prefix. Targets are buttons, `LT`/`RT`, or the stick directions `LS_UP/DOWN/LEFT/RIGHT` and `RS_UP/DOWN/LEFT/RIGHT`. `walk_key` held scales the left stick by `walk_scale`; `circle` normalizes diagonals to the unit circle. Select the keymap and the keyboard in the `keyboard` section of `daemon.json` (see [configuration](configuration.md)).
