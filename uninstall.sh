#!/usr/bin/env bash

set -u

SHARE=/usr/local/share/vitadock
LIB=/usr/local/lib/vitadock
CONF=/etc/vitadock.conf
INFO=$SHARE/install-info

YES=no; PURGE=no; REBOOT=no
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=yes ;;
    --purge)  PURGE=yes ;;
    --reboot) REBOOT=yes ;;
  esac
done

as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

if [ "$YES" = no ] && [ -t 0 ]; then
  read -r -p "remove VitaDock++? [y/N] " a
  [[ "$a" =~ ^[Yy] ]] || exit 0
fi

TARGET_BEFORE=multi-user.target
BOOT_DIR=""
[ -r "$INFO" ] && . "$INFO"

echo "stopping the dock"
as_root systemctl disable --now vitadock.service >/dev/null 2>&1
as_root rm -f /etc/systemd/system/vitadock.service

if grep -q '^BT_AUDIO=yes' "$CONF" 2>/dev/null && [ -f "$LIB/vitadock.py" ]; then
  echo "undoing the bluetooth audio setup"
  as_root /usr/bin/python3 "$LIB/vitadock.py" bluetooth off
fi

echo "putting the boot settings back"
as_root systemctl set-default "${TARGET_BEFORE:-multi-user.target}" >/dev/null 2>&1
if [ -n "$BOOT_DIR" ]; then
  for f in cmdline.txt config.txt; do
    [ -f "$BOOT_DIR/$f.vitadock-bak" ] && as_root mv "$BOOT_DIR/$f.vitadock-bak" "$BOOT_DIR/$f"
  done
fi

echo "removing files"
as_root rm -f /usr/local/bin/vitadock
as_root rm -rf "$LIB" "$SHARE"
[ "$PURGE" = yes ] && as_root rm -f "$CONF"
as_root systemctl daemon-reload

echo "VitaDock++ is gone."
if [ "$REBOOT" = yes ]; then
  as_root reboot
else
  echo "reboot to get your normal login back: sudo reboot"
fi
