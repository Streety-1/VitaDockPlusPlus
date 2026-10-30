#!/usr/bin/env bash

set -euo pipefail

REPO_SLUG="${VITADOCK_REPO:-Streety-1/VitaDockPlusPlus}"
BRANCH="${VITADOCK_BRANCH:-main}"

SHARE=/usr/local/share/vitadock
LIB=/usr/local/lib/vitadock
CONF=/etc/vitadock.conf
INFO=$SHARE/install-info
UNIT=/etc/systemd/system/vitadock.service

YES=no
UPDATE=no
RESET=no
REBOOT=ask

usage() {
  cat <<'EOF'
VitaDock++ installer. Run it on the Pi (Raspberry Pi OS Lite, 64-bit):

  curl -fsSL https://raw.githubusercontent.com/Streety-1/VitaDockPlusPlus/main/install.sh | bash

or from a copy of the repo:   bash install.sh

options:
  -y, --yes        don't ask anything
  --update         update an existing install (keeps your settings)
  --reset-config   put the settings back to default
  --no-reboot      don't reboot at the end
EOF
}

for arg in "$@"; do
  case "$arg" in
    -y|--yes)       YES=yes ;;
    --update)       UPDATE=yes ;;
    --reset-config) RESET=yes ;;
    --no-reboot)    REBOOT=no ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "don't know the option '$arg' (try --help)"; exit 1 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n:: %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { printf '\n!! %s\n' "$*" >&2; exit 1; }

ask() {
  local reply
  [ "$YES" = yes ] && return 0
  { : >/dev/tty; } 2>/dev/null || return 0
  printf '%s [Y/n] ' "$1" >/dev/tty
  read -r reply </dev/tty || return 0
  [[ ! "$reply" =~ ^[Nn] ]]
}

if [ "$(id -u)" -eq 0 ]; then SUDO=(); else SUDO=(sudo); fi
as_root() { "${SUDO[@]}" "$@"; }

cat <<'EOF'

  VitaDock++
  ----------
  play your PS Vita on the telly with a Raspberry Pi 5

EOF

command -v apt-get >/dev/null || die "this needs Raspberry Pi OS (Lite, 64-bit)."
command -v systemctl >/dev/null || die "this needs systemd."

MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "unknown")
say "this is a: $MODEL"
case "$MODEL" in
  *"Raspberry Pi 5"*|*"Compute Module 5"*) ;;
  *"Raspberry Pi 4"*|*"Compute Module 4"*)
    warn "I built this for the Pi 5. a Pi 4 should be ok but I haven't tried it." ;;
  *)
    warn "I built this for the Raspberry Pi 5, not this."
    ask "carry on anyway?" || exit 1 ;;
esac

[ "$(uname -m)" = aarch64 ] || warn "you're not on 64-bit Pi OS. it might work, but use 64-bit if you can."

if [ ${#SUDO[@]} -gt 0 ]; then
  say "need sudo for this bit, it might ask for your password."
  sudo -v || die "couldn't get sudo."
fi

INSTALLED=no
[ -f "$INFO" ] && INSTALLED=yes

if [ "$INSTALLED" = no ] && [ "$(systemctl get-default)" = graphical.target ]; then
  warn "this looks like the desktop version of Pi OS. VitaDock++ is meant for Lite."
  say "   it'll still work, but the desktop won't start any more while the dock is installed."
  ask "ok with that?" || exit 1
fi

if [ "$INSTALLED" = no ] && [ "$UPDATE" = no ]; then
  say ""
  say "this will:"
  say "  - install mpv, pygame and a few bits it needs"
  say "  - make the Pi boot straight into the dock (no login prompt on the tv)"
  say "  - hide the boot text so it looks tidy"
  say ""
  say "everything else (settings, bluetooth, uninstalling) is in the menu on the tv afterwards."
  say ""
  ask "go ahead?" || exit 1
fi

step "getting VitaDock++"
SRC=""
SELF=${BASH_SOURCE[0]:-}
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  d=$(cd "$(dirname "$SELF")" && pwd)
  [ -f "$d/src/vitadock.py" ] && SRC=$d
fi
if [ -z "$SRC" ]; then
  case "$REPO_SLUG" in Streety-1/*)
    die "this copy of install.sh doesn't know which github repo to download from. run it from inside the repo folder instead (bash install.sh)." ;;
  esac
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "https://github.com/$REPO_SLUG/archive/refs/heads/$BRANCH.tar.gz" \
    | tar -xz -C "$TMP" --strip-components=1 || die "download failed. is the Pi online?"
  SRC=$TMP
fi
for f in src/vitadock.py src/vitadock.conf src/vitadock.service src/bt-aplay uninstall.sh VERSION; do
  [ -f "$SRC/$f" ] || die "missing $f, the download looks broken."
done
NEW=$(cat "$SRC/VERSION")
say "version $NEW"

step "installing packages (takes a few minutes the first time)"
export DEBIAN_FRONTEND=noninteractive
as_root apt-get update -q
as_root apt-get install -y -q mpv v4l-utils python3 python3-pygame python3-evdev fonts-dejavu-core curl \
  || die "apt couldn't install the packages. check the Pi is online and try again."
as_root apt-get install -y -q bluez bluez-alsa-utils bluez-tools \
  || warn "couldn't install the bluetooth audio packages. everything else will still work."

step "copying files"
as_root install -d -m 755 "$SHARE" "$LIB"
as_root install -m 644 "$SRC/src/vitadock.py" "$LIB/vitadock.py"
as_root install -m 755 "$SRC/src/bt-aplay" "$LIB/bt-aplay"
as_root install -m 755 "$SRC/uninstall.sh" "$SHARE/uninstall.sh"
as_root install -m 644 "$SRC/VERSION" "$SHARE/VERSION"
printf '#!/bin/sh\nexec /usr/bin/python3 %s/vitadock.py "$@"\n' "$LIB" | as_root tee /usr/local/bin/vitadock >/dev/null
as_root chmod 755 /usr/local/bin/vitadock

if [ ! -f "$CONF" ] || [ "$RESET" = yes ]; then
  as_root install -m 644 "$SRC/src/vitadock.conf" "$CONF"
else
  while IFS= read -r line; do
    key=${line%%=*}
    grep -qE "^$key=" "$CONF" || printf '%s\n' "$line" | as_root tee -a "$CONF" >/dev/null
  done < <(grep -E '^[A-Z_]+=' "$SRC/src/vitadock.conf")
fi

TARGET_BEFORE=""
BOOT_DIR=""
for d in /boot/firmware /boot; do
  [ -f "$d/cmdline.txt" ] && { BOOT_DIR=$d; break; }
done
[ -f "$INFO" ] && TARGET_BEFORE=$(sed -n 's/^TARGET_BEFORE=//p' "$INFO")
[ -n "$TARGET_BEFORE" ] || TARGET_BEFORE=$(systemctl get-default)

as_root tee "$INFO" >/dev/null <<EOF
REPO_SLUG=$REPO_SLUG
TARGET_BEFORE=$TARGET_BEFORE
BOOT_DIR=$BOOT_DIR
VERSION=$NEW
EOF

step "setting the Pi up to boot into the dock"
sed "s|@REPO@|$REPO_SLUG|" "$SRC/src/vitadock.service" | as_root tee "$UNIT" >/dev/null
as_root systemctl daemon-reload
as_root systemctl enable vitadock.service >/dev/null 2>&1
as_root systemctl set-default multi-user.target >/dev/null 2>&1

if [ -n "$BOOT_DIR" ]; then
  CMD=$BOOT_DIR/cmdline.txt
  [ -f "$CMD.vitadock-bak" ] || as_root cp "$CMD" "$CMD.vitadock-bak"
  line=$(head -n1 "$CMD")
  for tok in quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0; do
    [[ " $line " == *" $tok "* ]] || line="$line $tok"
  done
  printf '%s\n' "$line" | as_root tee "$CMD" >/dev/null

  CFG=$BOOT_DIR/config.txt
  if [ -f "$CFG" ] && ! grep -q '^disable_splash=1' "$CFG"; then
    [ -f "$CFG.vitadock-bak" ] || as_root cp "$CFG" "$CFG.vitadock-bak"
    printf '\n[all]\n# VitaDock++: no rainbow screen at boot\ndisable_splash=1\n' | as_root tee -a "$CFG" >/dev/null
  fi
else
  warn "couldn't find cmdline.txt, so the boot text will still show. no big deal."
fi

if [ "$INSTALLED" = yes ]; then
  as_root systemctl restart vitadock.service
  step "updated to $NEW"
  exit 0
fi

step "done!"
cat <<'EOF'

   after the reboot you'll get a "plug your vita in" screen on the tv.
   plug the vita in with a usb cable and it should pop up.

   for the menu, plug a usb keyboard, mouse or usb pad into the pi:
     keyboard: enter (or esc mid-game)    mouse: click (right click mid-game)
     pad: A to pick, PS/home mid-game

EOF

case "$REBOOT" in
  no) say "reboot when you're ready: sudo reboot" ;;
  *)  if ask "reboot now?"; then as_root reboot; else say "reboot when you're ready: sudo reboot"; fi ;;
esac
