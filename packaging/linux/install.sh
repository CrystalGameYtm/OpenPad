#!/usr/bin/env bash

set -euo pipefail
cd "$(dirname "$0")"

if [[ $EUID -ne 0 ]]; then
  echo "Root privileges required: sudo $0" >&2
  exit 1
fi
TARGET_USER="${SUDO_USER:-}"

install -Dm644 udev/70-gamepad-remapd.rules        /etc/udev/rules.d/70-gamepad-remapd.rules
install -Dm644 modules-load.d/gamepad-remapd.conf  /etc/modules-load.d/gamepad-remapd.conf
install -Dm644 limits.d/99-gamepad-remapd.conf     /etc/security/limits.d/99-gamepad-remapd.conf

modprobe uinput || echo "WARNING: could not load uinput (kernel without CONFIG_INPUT_UINPUT?)" >&2

if [[ -n "$TARGET_USER" ]]; then
  getent group input >/dev/null || groupadd --system input
  usermod -aG input "$TARGET_USER"
  echo "User $TARGET_USER added to group input (log out and back in)."
fi

udevadm control --reload
udevadm trigger --subsystem-match=hidraw --subsystem-match=misc --subsystem-match=usb
echo "Done. Verification: getfacl /dev/uinput /dev/hidraw*"
