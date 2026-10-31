#!/usr/bin/env bash

set -euo pipefail

REPO_SLUG="${VITADOCK_REPO:-Streety-1/VitaDockPlusPlus}"
BRANCH="${VITADOCK_BRANCH:-main}"

SHARE=/usr/local/share/vitadock
LIB=/usr/local/lib/vitadock
CONF=/etc/vitadock.conf
INFO=$SHARE/install-info
UNIT=/etc/systemd/system/vitadock.service
STATE_DIR=/var/lib/vitadock
UPDATE_LOG=/var/log/vitadock-update.log

YES=no
UPDATE=no
RESET=no
REBOOT=ask
PROGRESS=""

usage() {
  cat <<'EOF'
VitaDock++ installer. Run it on the Pi (Raspberry Pi OS Lite, 64-bit):

  curl -fsSL https://raw.githubusercontent.com/Streety-1/VitaDockPlusPlus/main/install.sh | bash

or from a copy of the repo:   bash install.sh

options:
  -y, --yes          don't ask anything
  --update           update an existing install (keeps your settings)
  --reset-config     put the settings back to default
  --no-reboot        don't reboot at the end
  --progress=FILE    write progress for the tv menu's update screen (used by the dock)
EOF
}

for arg in "$@"; do
  case "$arg" in
    -y|--yes)       YES=yes ;;
    --update)       UPDATE=yes ;;
    --reset-config) RESET=yes ;;
    --no-reboot)    REBOOT=no ;;
    --progress=*)   PROGRESS=${arg#--progress=} ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "don't know the option '$arg' (try --help)"; exit 1 ;;
  esac
done

if [ -n "$PROGRESS" ]; then
  exec >>"$UPDATE_LOG" 2>&1
  echo
  echo "=== update started $(date)"
fi
exec 4>&1

progress_raw() {
  [ -n "$PROGRESS" ] || return 0
  printf '%s\n' "$1" >"$PROGRESS.tmp" && mv -f "$PROGRESS.tmp" "$PROGRESS"
}
progress() { progress_raw "$1|$2"; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n:: %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { printf '\n!! %s\n' "$*" >&2; progress_raw "error|$*"; exit 1; }

TMP=""
on_exit() {
  local rc=$?
  [ -n "$TMP" ] && rm -rf "$TMP"
  if [ "$rc" -ne 0 ] && [ -n "$PROGRESS" ] && ! grep -q '^error|' "$PROGRESS" 2>/dev/null; then
    progress_raw "error|the installer stopped (exit $rc). the log has the details."
  fi
}
trap on_exit EXIT

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

APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

apt_progress() {
  local lo=$1 hi=$2 mid=$(( ($1 + $2) / 2 )) kind _pkg pct desc
  while IFS=: read -r kind _pkg pct desc; do
    pct=${pct%%.*}
    [[ "$pct" =~ ^[0-9]+$ ]] || continue
    case "$kind" in
      dlstatus) progress $(( lo + pct * (mid - lo) / 100 )) "downloading packages: ${desc,,}" ;;
      pmstatus) progress $(( mid + pct * (hi - mid) / 100 )) "${desc,,}" ;;
    esac
  done
}

apt_install() {
  local lo=$1 hi=$2
  shift 2
  if [ -n "$PROGRESS" ] && [ ${#SUDO[@]} -eq 0 ]; then
    apt-get install "${APT_OPTS[@]}" -o APT::Status-Fd=3 "$@" 3>&1 1>&4 2>&4 | apt_progress "$lo" "$hi"
  else
    as_root apt-get install "${APT_OPTS[@]}" "$@"
  fi
}

cat <<'EOF'

  VitaDock++
  ----------
  play your PS Vita on the telly with a Raspberry Pi 5

EOF

progress 3 "checking the pi"
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
  say "  - install mpv, gstreamer, pygame and a few bits it needs"
  say "  - make the Pi boot straight into the dock (no login prompt on the tv)"
  say "  - hide the boot text so it looks tidy"
  say ""
  say "everything else (settings, wi-fi, bluetooth, updates, uninstalling) is in the menu on the tv afterwards."
  say ""
  ask "go ahead?" || exit 1
fi

step "getting VitaDock++"
progress 6 "downloading vitadock++"
SRC=""
SELF=${BASH_SOURCE[0]:-}
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  d=$(cd "$(dirname "$SELF")" && pwd)
  [ -f "$d/src/vitadock.py" ] && SRC=$d
fi
if [ -z "$SRC" ]; then
  TMP=$(mktemp -d)
  curl -fsSL --retry 2 "https://github.com/$REPO_SLUG/archive/refs/heads/$BRANCH.tar.gz" \
    | tar -xz -C "$TMP" --strip-components=1 || die "couldn't download vitadock++. is the pi online?"
  SRC=$TMP
fi
for f in src/vitadock.py src/vitadock.conf src/vitadock.service src/bt-aplay uninstall.sh VERSION; do
  [ -f "$SRC/$f" ] || die "missing $f, the download looks broken."
done
NEW=$(cat "$SRC/VERSION")
say "version $NEW"

step "installing packages (takes a few minutes the first time)"
export DEBIAN_FRONTEND=noninteractive
progress 10 "checking for package updates"
as_root apt-get update -q || die "couldn't reach the package servers. is the pi online?"
progress 14 "installing packages"
apt_install 14 50 mpv v4l-utils alsa-utils python3 python3-pygame python3-evdev python3-pil fonts-dejavu-core curl \
  || die "apt couldn't install the packages it needs. check the pi is online and try again."
progress 50 "installing the fast player"
apt_install 50 76 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  || warn "couldn't install gstreamer, so the dock will use the slower mpv player."
progress 76 "installing bluetooth audio bits"
apt_install 76 81 bluez bluez-alsa-utils bluez-tools \
  || warn "couldn't install the bluetooth audio packages. everything else will still work."
progress 81 "installing the boot splash"
HAVE_PLYMOUTH=yes
apt_install 81 85 plymouth || { HAVE_PLYMOUTH=no; warn "couldn't install plymouth, so no boot animation. no big deal."; }

step "copying files"
progress 86 "copying files"
as_root install -d -m 755 "$SHARE" "$LIB" "$STATE_DIR"
as_root install -m 644 "$SRC/src/vitadock.py" "$LIB/vitadock.py"
as_root install -m 755 "$SRC/src/bt-aplay" "$LIB/bt-aplay"
as_root install -m 755 "$SRC/uninstall.sh" "$SHARE/uninstall.sh"
as_root install -m 644 "$SRC/VERSION" "$SHARE/VERSION"
printf '#!/bin/sh\nexec /usr/bin/python3 %s/vitadock.py "$@"\n' "$LIB" | as_root tee /usr/local/bin/vitadock >/dev/null
as_root chmod 755 /usr/local/bin/vitadock

if [ ! -f "$CONF" ] || [ "$RESET" = yes ]; then
  as_root install -m 644 "$SRC/src/vitadock.conf" "$CONF"
else
  if ! grep -q '^SHARPNESS=' "$CONF" && grep -q '^VITA_MODE=864x488' "$CONF"; then
    as_root sed -i 's/^VITA_MODE=864x488/VITA_MODE=960x544/' "$CONF"
    say "switched the video mode to native 960x544, it's much sharper"
  fi
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
progress 93 "setting up boot"
sed "s|@REPO@|$REPO_SLUG|" "$SRC/src/vitadock.service" | as_root tee "$UNIT" >/dev/null
as_root systemctl daemon-reload
as_root systemctl enable vitadock.service >/dev/null 2>&1
as_root systemctl set-default multi-user.target >/dev/null 2>&1

if [ -n "$BOOT_DIR" ]; then
  CMD=$BOOT_DIR/cmdline.txt
  [ -f "$CMD.vitadock-bak" ] || as_root cp "$CMD" "$CMD.vitadock-bak"
  line=$(head -n1 "$CMD")
  toks="quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0 usbcore.autosuspend=-1"
  [ "$HAVE_PLYMOUTH" = yes ] && toks="$toks splash plymouth.ignore-serial-consoles"
  for tok in $toks; do
    [[ " $line " == *" $tok "* ]] || line="$line $tok"
  done
  printf '%s\n' "$line" | as_root tee "$CMD" >/dev/null
  as_root /usr/bin/python3 "$LIB/vitadock.py" apply-output >/dev/null || warn "couldn't set the tv mode at boot."

  CFG=$BOOT_DIR/config.txt
  if [ -f "$CFG" ] && ! grep -q '^disable_splash=1' "$CFG"; then
    [ -f "$CFG.vitadock-bak" ] || as_root cp "$CFG" "$CFG.vitadock-bak"
    printf '\n[all]\n# VitaDock++: no rainbow screen at boot\ndisable_splash=1\n' | as_root tee -a "$CFG" >/dev/null
  fi
else
  warn "couldn't find cmdline.txt, so the boot text will still show. no big deal."
fi

if [ "$HAVE_PLYMOUTH" = yes ] && command -v plymouth-set-default-theme >/dev/null; then
  progress 96 "setting up the boot animation"
  THEME=/usr/share/plymouth/themes/vitadock
  if as_root /usr/bin/python3 "$LIB/vitadock.py" plymouth-theme "$THEME" >/dev/null; then
    if ls "${BOOT_DIR:-/boot/firmware}"/initramfs* >/dev/null 2>&1; then
      as_root plymouth-set-default-theme -R vitadock || warn "couldn't rebuild the initramfs for the boot animation."
    else
      as_root plymouth-set-default-theme vitadock || warn "couldn't set the boot animation."
    fi
  else
    warn "couldn't make the boot animation."
  fi
fi

if [ "$INSTALLED" = yes ]; then
  step "updated to $NEW"
  printf '%s\n' "$NEW" | as_root tee "$STATE_DIR/just-updated" >/dev/null
  progress_raw "done|updated to $NEW, restarting the dock"
  as_root systemctl restart vitadock.service
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
