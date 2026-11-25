#!/usr/bin/env bash
# One-command installer for Linux: ./install.sh
#   - a private Python environment in ~/.local/share/gamepad-remapd (nothing is installed system-wide with pip)
#   - the command ~/.local/bin/gamepad-remapd
#   - udev rules + uinput + the "input" group (asks for sudo once)
#   - a systemd user service that starts the daemon at login
# Options: --no-service  do not set up autostart      --no-system  skip the sudo step (udev/uinput/group)
#          --uninstall   remove everything this script installed (your config in ~/.config is kept)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/gamepad-remapd/venv"
BIN="$HOME/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE=1; SYSTEM=1; UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --no-service) SERVICE=0 ;;
    --no-system) SYSTEM=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $a (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

if [[ $EUID -eq 0 ]]; then
  fail "Run this as your normal user, not as root (the script asks for sudo itself when it is needed)."
fi

if [[ $UNINSTALL -eq 1 ]]; then
  say "Removing"
  systemctl --user disable --now gamepad-remapd 2>/dev/null || true
  rm -f "$UNIT_DIR/gamepad-remapd.service" "$BIN/gamepad-remapd"
  rm -rf "$(dirname "$VENV")"
  systemctl --user daemon-reload 2>/dev/null || true
  if command -v sudo >/dev/null; then
    sudo rm -f /etc/udev/rules.d/70-gamepad-remapd.rules /etc/modules-load.d/gamepad-remapd.conf \
               /etc/security/limits.d/99-gamepad-remapd.conf && sudo udevadm control --reload || true
  fi
  echo "Removed. Your profiles and settings in ~/.config/gamepad-remapd were left in place."
  exit 0
fi

[[ "$(uname -s)" == "Linux" ]] || fail "This installer is for Linux. For Windows see packaging/windows/README.md."

# 1. Python 3.11+
say "Looking for Python 3.11 or newer"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY="$c"; break; fi
done
if [[ -z "$PY" ]]; then
  fail "Python 3.11 or newer was not found. Install it first:
  Fedora:        sudo dnf install python3
  Ubuntu/Debian: sudo apt install python3 python3-venv
  Arch:          sudo pacman -S python"
fi
echo "Using $($PY --version) ($(command -v "$PY"))"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || fail "The Python venv module is missing. On Ubuntu/Debian: sudo apt install python3-venv"

# 2. Private environment + the program
say "Installing the program into $VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
if ! "$VENV/bin/python" -m pip install --quiet "$HERE[web,speed]" 2>/dev/null; then
  echo "(the optional speed-up package could not be installed, continuing without it)"
  "$VENV/bin/python" -m pip install --quiet "$HERE[web]" \
    || fail "pip could not install the dependencies. Check your internet connection and run ./install.sh again."
fi
mkdir -p "$BIN"
ln -sf "$VENV/bin/gamepad-remapd" "$BIN/gamepad-remapd"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "NOTE: $BIN is not in your PATH; add it to use the command 'gamepad-remapd' directly." ;; esac

# 3. Access to the devices (needs root once)
if [[ $SYSTEM -eq 1 ]]; then
  say "Setting up device access (udev rules, uinput, group 'input') — sudo password may be asked"
  command -v sudo >/dev/null || fail "sudo was not found. Run packaging/linux/install.sh as root, then re-run this script with --no-system."
  sudo "$HERE/packaging/linux/install.sh" || fail "The device access setup failed (see above)."
fi

# 4. Autostart
if [[ $SERVICE -eq 1 ]] && command -v systemctl >/dev/null; then
  say "Setting up autostart (systemd user service)"
  mkdir -p "$UNIT_DIR" "$HOME/.config/gamepad-remapd"
  install -m644 "$HERE/packaging/linux/systemd/gamepad-remapd.service" "$UNIT_DIR/gamepad-remapd.service"
  systemctl --user daemon-reload
  if id -nG | tr ' ' '\n' | grep -qx input; then
    systemctl --user enable --now gamepad-remapd && echo "The daemon is running."
  else
    systemctl --user enable gamepad-remapd
    echo "The service is enabled and will start after you log out and back in (the group 'input' becomes active then)."
  fi
fi

say "Done"
cat <<MSG
1. Log out and back in once (needed to get access to the gamepad), unless you did not change groups.
2. Plug the gamepad in and check:   gamepad-remapd list
3. Open the Web UI with this link:  $("$VENV/bin/gamepad-remapd" link 2>/dev/null || echo "http://127.0.0.1:8765/  (run: gamepad-remapd link)")
   (the link signs you in once; the browser remembers it)
No service? Start by hand:          gamepad-remapd serve
Problems:                           docs/troubleshooting.md
MSG
