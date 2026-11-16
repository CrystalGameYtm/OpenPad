# Status

Last check — November 2026. "Verified" means it was run on a real device, not only in tests
(the repository has ~300 automated tests on simulators and fake devices).

## Verified on hardware

| Device | What works |
|---|---|
| Gamepro GPS13B (DS4 protocol, `054c:09cc`), USB and Bluetooth | calibration, button/stick/trigger input, output to games; rumble over Bluetooth (report `0x11` + CRC-32, byte 1 = `0xC4`, checked against an SDL3 trace) |
| Flydigi Direwolf 3 (2.4 GHz, DInput, `04b4:2412`) | handshake, input, calibration, M1/M2, output to games (XInput emulation: ScarletNX, CTR Turbocharged); hiding of the original (`serve --privileged`: 6 nodes closed, SDL does not hang); gyro in Pro Controller mode |

Software processing latency (p99): ~65 µs; the 1.5 ms budget (p99.9) is met (`gamepad-remapd bench`).

## Known limitations

* **The "Native: DInput/DS4" output is experimental and usually does not work in games**: for a virtual device SDL has no
  mapping (DS4 with the Sony VID:PID is handed to SDL's own HIDAPI). Use "XInput emulation". The UI and the log warn about it.
* **Hiding the original requires `CAP_FOWNER`** (`setup-hide --service` or `serve --privileged`); without it, it is not applied
  at all. Only `serve --privileged` is verified; installing the systemd service `setup-hide --service` on a real system has not been tested.
* **Flydigi 2.4G handshake** on Linux always gives `Errno 110` (the receiver does not answer the outgoing command), but the device
  is still read; session start is delayed by about 5 s.
* **Flydigi in DInput mode has no host rumble**: the receiver does not serve the OUT endpoint of interface 2
  (`Errno 110` both in our code and in SDL3). In XInput mode rumble works through the kernel (xpad); this mode is supported as input.
* **GPS13B**: in DS4 mode the extra buttons ML/MR are most likely not visible (they exist only in DInput mode);
  BACK/START on the clone are swapped relative to the reference (calibration accounts for this).
* **DS4 rumble over USB** (`05 01 …`) is implemented from hid-playstation, not verified on a real device.
* **Windows** (`output/vigem_windows.py`, `output/sendinput_windows.py`, `input/hidapi_backend.py`, `tools/hidhide.py`): Xbox 360
  output (ViGEm), keyboard/mouse (SendInput), hidapi bus, HidHide commands — written from documentation and covered by Linux
  simulations (`tests/test_windows.py`); not run on real Windows (including the built-in backend `input/winhid.py` without hidapi).
  Not implemented: DS4 output, auto-mapping from the HID descriptor (HidP_*), "keyboard as gamepad".
* Keyboard targets and keyboard-as-gamepad are verified on simulated uinput channels; the virtual keyboard on a real system
  (compositor/game) has not been verified yet (in AM2R the D-pad worked as arrows).

## Planned

* Gyro: DS4 layouts describe it, but live verification (axis sign, drift) and learning by the calibrator are not done yet.
* Flydigi V2 / heartbeat, handshake for 8BitDo/GameSir: the library exists, only V1 is verified.
* 12-bit axes in calibration (currently 8 and 16 bit).
* Stick → keys (WASD) as a profile target.
* Web UI: clickable gamepad diagram, curve preview, unsaved-changes indicator — see [docs/ui-concept.md](docs/ui-concept.md).
* Own XInput driver instead of ViGEm; choice of the output protocol per device (DInput/XInput/DS4).
