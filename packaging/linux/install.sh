#!/usr/bin/env bash
# Установка системних файлів (udev, modules-load, limits). Потребує sudo. Запускати з каталогу packaging/linux.
set -euo pipefail
cd "$(dirname "$0")"

if [[ $EUID -ne 0 ]]; then
  echo "Потрібні права root: sudo $0" >&2
  exit 1
fi
TARGET_USER="${SUDO_USER:-}"

install -Dm644 udev/70-gamepad-remapd.rules        /etc/udev/rules.d/70-gamepad-remapd.rules
install -Dm644 modules-load.d/gamepad-remapd.conf  /etc/modules-load.d/gamepad-remapd.conf
install -Dm644 limits.d/99-gamepad-remapd.conf     /etc/security/limits.d/99-gamepad-remapd.conf

modprobe uinput || echo "УВАГА: не вдалося завантажити uinput (ядро без CONFIG_INPUT_UINPUT?)" >&2

if [[ -n "$TARGET_USER" ]]; then
  getent group input >/dev/null || groupadd --system input
  usermod -aG input "$TARGET_USER"
  echo "Користувача $TARGET_USER додано до групи input (потрібен повторний вхід у систему)."
fi

udevadm control --reload
udevadm trigger --subsystem-match=hidraw --subsystem-match=misc
echo "Готово. Перевірка: getfacl /dev/uinput /dev/hidraw*"
