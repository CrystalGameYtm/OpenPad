# Control protocol and Web API

There are two interfaces, one on top of the other:

* the **control channel** of the daemon: newline-delimited JSON over a UNIX socket (or authenticated loopback TCP). The daemon exposes only this;
* the **Web process** (`gamepad-remapd web`, started by `serve`): an HTTP + WebSocket server that serves the browser UI and translates REST calls into control-channel commands. It never touches devices itself, so a Web crash does not interrupt remapping.

The browser UI uses only the REST API and `/ws`. Scripts can use either layer.

## 1. Control channel

### Endpoint

`control_endpoint` in `daemon.json` (see [configuration](configuration.md)). Default:

| Platform | Endpoint | Authentication |
|----------|----------|----------------|
| Linux / macOS | `unix:<runtime dir>/control.sock`, mode 0600 | file permissions |
| Windows | `tcp:127.0.0.1:8764` | token from `<config>/control.token` |

`<runtime dir>` is `$XDG_RUNTIME_DIR/gamepad-remapd`, or, when `XDG_RUNTIME_DIR` is not set (for example under plain `sudo`), `/run/user/<uid>/gamepad-remapd` if that private directory exists, otherwise `/tmp/gamepad-remapd-<uid>` (the CLI and the daemon must use the same directory). The daemon creates it with mode 0700 and refuses to use a directory that belongs to another user, is not a real directory (a symbolic link, say) or cannot be made private. The token is created with mode 0600 on first use. A TCP endpoint can be used on any platform if you set it explicitly.

### Framing

One JSON object per line (UTF-8, `\n`), at most 1 MiB per message.

```
request   {"id": 7, "cmd": "patch_profile", "device": "hidraw5", "patch": {...}}
success   {"id": 7, "ok": true,  "result": ...}
failure   {"id": 7, "ok": false, "error": "text", "code": "profile"}
event     {"event": "state" | "devices" | "profile" | "stats", ...}      (no id)
```

Error `code` values: `not_found`, `profile`, `store`, `layout`, `device`, `param` (missing or invalid parameter), `auth`, `internal`. On a TCP endpoint the first message must be `{"id": 1, "cmd": "auth", "token": "<token>"}`; a wrong token gets an `auth` error and the connection is closed.

Any request may carry `"lang": "uk"`: texts the daemon builds for that answer (errors, protocol descriptions) are produced in that language (see "Language" in the README).

### Subscriptions

| Command | Parameters | Result |
|---------|-----------|--------|
| `subscribe` | `events`: list, default `["state", "devices", "profile", "stats"]` | `{"events": [...]}`; from then on the connection receives those events. |
| `unsubscribe` | | `{}` |

Events:

| Event | Sent | Payload |
|-------|------|---------|
| `state` | at most `ui_hz` times per second, only when something changed, per open device | `dev` (device id), `src` (hex bitmask of pressed physical inputs), `out` (names of virtual buttons held), `ax` (last output values: LX, LY, RX, RY as int16, LT, RT as 0..255), `raw` (the four stick axes before dead zone and curve, normalized to −1..1, or `null`), `gyro` (`[yaw, pitch, roll]` in °/s when the layout has a gyro), `sniff` (when the hex sniffer runs). A slow client has stale `state` frames dropped. |
| `devices` | when devices appear, disappear, open, close or change options | `devices`: the same list as the `devices` command. |
| `profile` | a profile was activated or patched | `dev`, `name`, `data` (normalized profile), `warnings`. |
| `stats` | once per second | `stats`: `{device id: statistics}`. |

### Commands

`device` is a device id: `hidrawN` or `eventN` on Linux, `hid-<8 hex>` with the hidapi/Windows backends. `device` is required by `open`, `close`, `set_mode`, `set_options`, `autolayout` and `rumble`. For `active_profile`, `activate`, `patch_profile`, `calibrate_gyro`, `stats`, `stats_reset` and `sniff` it may be omitted when exactly one device is open.

| Command | Parameters | Result / effect |
|---------|-----------|-----------------|
| `ping` | | `{"pong": true}` |
| `devices` | | List of devices. Each item has `id`, `path`, `vid`, `pid`, `name`, `interface`, `bus`, `backend`, `open`, `layout`, `layout_name`, `protocol` (description of the device protocol), `modes`, `options` (`output_kind`, `output_choices`, `hide`); an open device also has `profile`, `warnings`, `running`, `error`, `rt`, `errors`, `sniff_only`, `output_kind`, `hide_state`, `inputs`, `default_targets`, `has_gyro`. |
| `rescan` | | Force a hotplug scan; returns the list. |
| `layouts` | | Summaries of all layouts: `id`, `name`, `vid`, `pid`, `buttons`, `gyro`, `rumble`, `mode`, `handshake_steps`, `generated`. |
| `layout` | `id` | Details of one layout: `buttons` (byte, bit, target), `fields`, `sticks`, `triggers`, `notes`. |
| `open` | `device`, optional `layout` | Open (or reopen) a session, optionally with a chosen layout. |
| `close` | `device` | Close the session and remove the virtual device. A closed device is not reopened automatically until it is replugged or opened again. |
| `set_mode` | `device`, `layout` | Choose the protocol mode (a layout of the same `mode.group`); remembered per model; the open device is reopened. |
| `set_options` | `device`, `output_kind`, `hide` | Output protocol (`xbox360`, `ds4`, `dinput`; Linux only, among `output_choices`) and hiding of the original device; remembered per model in `state.json`; the open device is reopened. |
| `profiles` | | List of built-in and user profiles. |
| `profile_get` | `name` | The stored profile. |
| `profile_save` | `name`, `data` | Validate and write `<config>/profiles/<name>.json`. |
| `profile_delete` | `name` | Delete the user file (a built-in profile cannot be deleted). |
| `active_profile` | `device` | `{name, data, warnings}` of the profile in use. |
| `activate` | `device`, `name` | Hot-swap the profile and remember it for the model. |
| `patch_profile` | `device`, `patch` (object), `save` (bool) | Apply a JSON merge patch (RFC 7386) to the active profile; with `save` write it to disk as well. |
| `check_profile` | `data`, optional `layout` | Validate (and with a layout, compile) without applying: `{ok, warnings}`. |
| `calibrate_gyro` | `device`, `duration` (0.5 … 5, default 1.5), `save` | Measure the gyro zero into `gyro.bias` of the active profile (see [calibration](calibration.md)). Returns `bias`, `samples`, `noise_dps`, `saved`, `data`. |
| `stats`, `stats_reset` | `device` | Processing-time statistics of the pipeline (count, dropped, mean, p50, p99, p99.9, max in µs) / reset them. |
| `sniff` | `device`, `action` (`start`, `summary`, `reset`, `learn`, `stop`), `name`, `baseline_s` | Hex sniffer for finding hidden buttons. |
| `autolayout` | `device`, `save` | Build a layout from the descriptor; with `save` write `auto_*.json` into `<config>/devices/` and rescan. |
| `rumble` | `device`, `strong`, `weak` (0 … 1, default 0.6), `ms` (default 400) | Test rumble through the layout's template. |
| `system_info` | | Platform, backend, config and devices directories, currently hidden nodes, keyboard settings. |
| `sdl_list` | | What SDL sees (`rc`, `text`). |
| `unhide` | | Restore permissions of hidden nodes (Linux). |
| `udev_rules` | `vid_pid` (list), `hide` (list), `group` | Generate udev rules text. |
| `keyboards` | | Keyboards found by the keyboard-as-gamepad feature. |
| `hidhide` | `vid`, `pid` (hex) | HidHide commands for the Windows setup. |

There is no command to turn the keyboard feature on and off at runtime: it is configured in `daemon.json` (`keyboard`) and toggled from the keyboard itself with the chord in `keyboard.toggle` (default Ctrl+Alt+G).

## 2. Web API

The server listens on `web_host:web_port` (default `127.0.0.1:8765`). Bodies are JSON. The browser UI is served at `/` and its static files at `/static/`.

### Security

* Every request, including the WebSocket upgrade, is checked against `DaemonConfig.allowed_hosts()`: the `Host` header must be `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>` or `<web_host>:<port>` when `web_host` is a specific non-loopback address (`0.0.0.0` and `::` add nothing: use a real address or name). Otherwise the answer is `403`. This blocks DNS rebinding.
* If an `Origin` header is present it must be one of the same hosts (`null` is refused). This blocks cross-site requests from pages open in the browser.
* **Every route, `/ws` included, needs the Web token** (unless `"web_auth": false`), because a TCP port on `127.0.0.1` is open to every user of the machine, not only to the owner of the daemon. The token is a random string in `<config>/web.token` (mode 0600, created on first start). A browser signs in once with `http://127.0.0.1:8765/?token=<token>` (the daemon prints this link in its log): the answer sets an `HttpOnly`, `SameSite=Strict` cookie `gpr_session` and redirects to `/`. Scripts send `Authorization: Bearer <token>`. Without it the answer is `401` (`{"error": ..., "code": "auth"}`; `/` shows a short page that says where the token is).
* Responses carry a strict `Content-Security-Policy` and `Cache-Control: no-store`.

### Language

Send `X-Lang: <code>` (REST) or `?lang=<code>` (WebSocket) to receive texts in that language.

### Errors

`{"error": "text", "code": "..."}` with the status from the control-channel code: `param`, `profile`, `layout`, `store` → 400; `auth` → 401; `not_found` → 404; `device` → 409; `internal` → 500; `unavailable` (daemon not reachable) → 503; `timeout` → 504.

### Endpoints

| Method and path | Body / query | Control command |
|-----------------|--------------|-----------------|
| `GET /api/health` | | `{web: true, daemon: bool}` (no command) |
| `GET /api/devices` | | `devices` |
| `POST /api/rescan` | | `rescan` |
| `POST /api/devices/{dev}/open` | `{"layout": id or null}` | `open` |
| `POST /api/devices/{dev}/close` | | `close` |
| `POST /api/devices/{dev}/mode` | `{"layout": id}` | `set_mode` |
| `POST /api/devices/{dev}/options` | `{"output_kind": ..., "hide": bool}` | `set_options` |
| `GET /api/devices/{dev}/profile` | | `active_profile` |
| `PUT /api/devices/{dev}/profile` | `{"name": profile}` | `activate` |
| `PATCH /api/devices/{dev}/profile?save=1` | JSON merge patch | `patch_profile` |
| `POST /api/devices/{dev}/gyro/calibrate` | `{"save": true}` (default true) | `calibrate_gyro` |
| `GET /api/devices/{dev}/stats`, `POST .../stats/reset` | | `stats`, `stats_reset` |
| `GET /api/devices/{dev}/sniff` | | `sniff` (`summary`) |
| `POST /api/devices/{dev}/sniff` | `{"action", "name", "baseline_s"}` | `sniff` |
| `POST /api/devices/{dev}/autolayout` | `{"save": false}` | `autolayout` |
| `POST /api/devices/{dev}/rumble` | `{"strong", "weak", "ms"}` | `rumble` |
| `GET /api/profiles` | | `profiles` |
| `GET /api/profiles/{name}` | | `profile_get` |
| `PUT /api/profiles/{name}` | the profile object | `profile_save` |
| `DELETE /api/profiles/{name}` | | `profile_delete` |
| `POST /api/profiles-check` | `{"data": profile, "layout": id}` | `check_profile` |
| `GET /api/layouts`, `GET /api/layouts/{id}` | | `layouts`, `layout` |
| `GET /api/system` | | `system_info` |
| `POST /api/system/sdl-list` | | `sdl_list` |
| `POST /api/system/unhide` | | `unhide` |
| `POST /api/system/udev-rules` | `{"vid_pid": [], "hide": [], "group": null}` | `udev_rules` |
| `GET /api/system/keyboards` | | `keyboards` |
| `POST /api/system/hidhide` | `{"vid": "054c", "pid": "09cc"}` | `hidhide` |
| `GET /api/locales` | | `{configured, available}` (language list, no command) |
| `GET /api/locale/{code}` | | The merged UI translation of that language (built-in + `<config>/locales/<code>.json`), or 404 |

### WebSocket `GET /ws`

A read-only stream of the daemon's events for the browser. The first message is `{"event": "daemon", "connected": bool}`; it is sent again whenever the Web process loses or regains the daemon. After that the client receives the `state`, `devices`, `profile` and `stats` events described above. Messages sent by the client are ignored (limit 4 KiB); the server pings every 20 s. Each client has its own bounded queue: a slow browser loses old `state` frames and never delays the others.

## Example

```sh
# list devices
TOKEN=$(cat ~/.config/gamepad-remapd/web.token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/devices | python3 -m json.tool

# lower the left stick's inner dead zone of the open device, without saving
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/devices/hidraw5/profile \
     -H 'Content-Type: application/json' \
     -d '{"sticks": {"left": {"inner_deadzone": 0.03}}}'
```

The same through the control channel (Linux):

```sh
printf '{"id":1,"cmd":"devices"}\n' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/gamepad-remapd/control.sock
```
