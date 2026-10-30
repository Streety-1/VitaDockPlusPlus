#!/usr/bin/env python3

import glob
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

CONF = os.environ.get("VITADOCK_CONF", "/etc/vitadock.conf")
SHARE = "/usr/local/share/vitadock"
LIB = "/usr/local/lib/vitadock"
INFO = SHARE + "/install-info"
SYS_DRM = os.environ.get("VITADOCK_SYS_DRM", "/sys/class/drm")
SYS_V4L = os.environ.get("VITADOCK_SYS_V4L", "/sys/class/video4linux")
DEV_GLOB = os.environ.get("VITADOCK_DEV_GLOB", "/dev/video*")
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

DEFAULTS = {
    "VITA_MODE": "864x488",
    "VITA_FPS": "60",
    "VITA_PIXFMT": "auto",
    "OUTPUT_MODE": "1920x1080@60",
    "SCALER": "lanczos",
    "DRM_DEVICE": "auto",
    "DRM_CONNECTOR": "auto",
    "BT_AUDIO": "no",
    "AUDIO_PCM": "auto",
    "EXTRA_MPV_ARGS": "",
    "PLAYER": "fast",
}

VIDEO_MODES = [
    ("smooth", "864x488", "60", "864x488 @ 60. smoothest, what I'd use"),
    ("sharp", "960x544", "30", "960x544 @ 30. native res, half the frames"),
    ("native60", "960x544", "60", "960x544 @ 60-ish. the plugin can't quite hold 60"),
    ("hd", "1280x720", "30", "1280x720 @ 30. needs sharpscale + a 720p game patch"),
]
SCALERS = [
    ("lanczos", "lanczos. sharpest"),
    ("spline36", "spline36. a touch softer"),
    ("bilinear", "bilinear. soft, least work for the pi"),
]
OUTPUTS = [
    ("1920x1080@60", "1080p"),
    ("1280x720@60", "720p. the tv does the rest of the scaling"),
    ("3840x2160@60", "4k. more gpu work, not much gain"),
    ("preferred", "whatever the tv asks for"),
]


def log(msg):
    print("[vitadock] " + msg, flush=True)


def version():
    try:
        with open(SHARE + "/VERSION") as f:
            return f.read().strip()
    except OSError:
        return "dev"


def read_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
                if m:
                    out[m.group(1)] = m.group(2).strip().strip('"')
    except OSError:
        pass
    return out


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(read_kv(CONF))
    if not re.match(r"^\d+x\d+$", cfg["VITA_MODE"]):
        cfg["VITA_MODE"] = DEFAULTS["VITA_MODE"]
    if not cfg["VITA_FPS"].isdigit():
        cfg["VITA_FPS"] = DEFAULTS["VITA_FPS"]
    if cfg["SCALER"] not in ("lanczos", "spline36", "bilinear", "nearest", "ewa_lanczossharp"):
        cfg["SCALER"] = "lanczos"
    return cfg


def set_config(key, value):
    try:
        with open(CONF) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    done = False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = "%s=%s" % (key, value)
            done = True
    if not done:
        lines.append("%s=%s" % (key, value))
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, CONF)


def mode_name(cfg):
    for name, size, fps, _ in VIDEO_MODES:
        if cfg["VITA_MODE"] == size and cfg["VITA_FPS"] == fps:
            return name
    return "custom"


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def find_display(cfg):
    if cfg["DRM_DEVICE"] != "auto" and cfg["DRM_CONNECTOR"] != "auto":
        dev, conn = cfg["DRM_DEVICE"], cfg["DRM_CONNECTOR"]
        return dev, conn, "%s/%s-%s" % (SYS_DRM, os.path.basename(dev), conn)
    for status in sorted(glob.glob(SYS_DRM + "/card*-*/status")):
        try:
            with open(status) as f:
                if f.read().strip() != "connected":
                    continue
        except OSError:
            continue
        d = os.path.dirname(status)
        card, conn = os.path.basename(d).split("-", 1)
        return "/dev/dri/" + card, conn, d
    return None


def pick_output_mode(cfg, sysdir):
    om = cfg["OUTPUT_MODE"]
    if om in ("auto", "preferred", "highest"):
        return "highest" if om == "highest" else "preferred"
    want = om.split("@")[0]
    try:
        with open(sysdir + "/modes") as f:
            if want in f.read().split():
                return om
    except OSError:
        pass
    return "preferred"


def v4l2(dev, *args):
    return sh(["v4l2-ctl", "-d", dev] + list(args), timeout=5).stdout or ""


def find_vita():
    for dev in sorted(glob.glob(DEV_GLOB)):
        node = SYS_V4L + "/" + os.path.basename(dev)
        if os.path.exists(node) and "/usb" not in os.path.realpath(node):
            continue
        if "Size: Discrete 960x544" in v4l2(dev, "--list-formats-ext"):
            return dev
    return None


def pick_pixfmt(cfg, dev):
    want = cfg["VITA_PIXFMT"]
    if want == "none":
        return ""
    if want != "auto":
        return want
    fmts = v4l2(dev, "--list-formats")
    if "'NV12'" in fmts:
        return "nv12"
    if "'YUYV'" in fmts:
        return "yuyv422"
    return ""


_GST_OK = None


def gst_available():
    global _GST_OK
    if _GST_OK is None:
        _GST_OK = bool(shutil.which("gst-launch-1.0")) and all(
            sh(["gst-inspect-1.0", el], timeout=10).returncode == 0 for el in ("v4l2src", "kmssink"))
    return _GST_OK


GST_FORMATS = {"nv12": "NV12", "yuyv422": "YUY2"}


def gst_command(cfg, vita, pixfmt):
    w, h = cfg["VITA_MODE"].split("x")
    caps = "video/x-raw,width=%s,height=%s,framerate=%s/1" % (w, h, cfg["VITA_FPS"])
    if GST_FORMATS.get(pixfmt):
        caps += ",format=" + GST_FORMATS[pixfmt]
    return ["gst-launch-1.0", "-q",
            "v4l2src", "device=" + vita, "io-mode=mmap", "do-timestamp=false", "!",
            caps, "!",
            "queue", "max-size-buffers=1", "max-size-bytes=0", "max-size-time=0", "leaky=downstream", "!",
            "kmssink", "sync=false", "can-scale=true"]


def mpv_command(cfg, disp, out_mode, vita, pixfmt):
    dev, conn, _ = disp
    cmd = [
        "mpv", "--no-config",
        "--vo=gpu", "--gpu-context=drm",
        "--drm-device=" + dev, "--drm-connector=" + conn, "--drm-mode=" + out_mode,
        "--profile=low-latency", "--untimed", "--cache=no", "--no-audio", "--hwdec=no",
        "--demuxer-thread=no", "--swapchain-depth=1",
        "--scale=" + cfg["SCALER"], "--keepaspect=yes",
        "--osc=no", "--osd-level=0", "--no-input-default-bindings", "--input-terminal=no",
        "--msg-level=all=warn",
        "--demuxer-lavf-o-add=video_size=" + cfg["VITA_MODE"],
        "--demuxer-lavf-o-add=framerate=" + cfg["VITA_FPS"],
    ]
    if pixfmt:
        cmd.append("--demuxer-lavf-o-add=input_format=" + pixfmt)
    cmd += shlex.split(cfg["EXTRA_MPV_ARGS"])
    cmd.append("av://v4l2:" + vita)
    return cmd


def boot_cmdline_path():
    d = read_kv(INFO).get("BOOT_DIR", "")
    for cand in ([d] if d else []) + ["/boot/firmware", "/boot"]:
        if cand and os.path.exists(cand + "/cmdline.txt"):
            return cand + "/cmdline.txt"
    return None


def apply_output_mode(cfg):
    path = boot_cmdline_path()
    if not path:
        return False
    with open(path) as f:
        tokens = f.read().split()
    keep = [t for t in tokens if not t.startswith("video=HDMI-A-") and t != "usbcore.autosuspend=-1"]
    om = cfg["OUTPUT_MODE"]
    if re.match(r"^\d+x\d+(@\d+)?$", om):
        mode = om if "@" in om else om + "@60"
        keep += ["video=HDMI-A-1:" + mode, "video=HDMI-A-2:" + mode]
    keep.append("usbcore.autosuspend=-1")
    if keep == tokens:
        return False
    with open(path + ".tmp", "w") as f:
        f.write(" ".join(keep) + "\n")
    os.replace(path + ".tmp", path)
    return True


def cpu_performance():
    for gov in glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"):
        try:
            with open(gov, "w") as f:
                f.write("performance")
        except OSError:
            pass


def ip_addresses():
    out = sh(["hostname", "-I"], timeout=3).stdout.split()
    return [a for a in out if ":" not in a] or out


def unit_exists(name):
    return bool(sh(["systemctl", "list-unit-files", name, "--no-legend"]).stdout.strip())


def bt_enable():
    binary = shutil.which("bluealsad") or shutil.which("bluealsa")
    svc = "bluealsad.service" if unit_exists("bluealsad.service") else "bluealsa.service"
    if not binary or not unit_exists(svc) or not unit_exists("bluealsa-aplay.service"):
        raise RuntimeError("bluez-alsa isn't installed. re-run the installer.")
    for d in ("/etc/systemd/system/%s.d" % svc, "/etc/systemd/system/bluealsa-aplay.service.d"):
        os.makedirs(d, exist_ok=True)
    with open("/etc/systemd/system/%s.d/vitadock.conf" % svc, "w") as f:
        f.write("[Service]\nExecStart=\nExecStart=%s -S -p a2dp-sink\n" % binary)
    with open("/etc/systemd/system/bluealsa-aplay.service.d/vitadock.conf", "w") as f:
        f.write("[Service]\nSupplementaryGroups=audio\nExecStart=\nExecStart=%s/bt-aplay\n" % LIB)

    main = "/etc/bluetooth/main.conf"
    if os.path.exists(main):
        if not os.path.exists(main + ".vitadock-bak"):
            shutil.copy2(main, main + ".vitadock-bak")
        with open(main) as f:
            text = f.read()
        for key, val in (("Name", "VitaDock++"), ("Class", "0x000414"), ("JustWorksRepairing", "always")):
            text, n = re.subn(r"(?m)^#?\s*%s\s*=.*$" % key, "%s = %s" % (key, val), text)
            if n == 0:
                text = text.replace("[General]", "[General]\n%s = %s" % (key, val), 1)
        with open(main, "w") as f:
            f.write(text)

    sh(["systemctl", "daemon-reload"])
    sh(["systemctl", "enable", "bluetooth.service", svc, "bluealsa-aplay.service"])
    sh(["systemctl", "restart", "bluetooth.service"])
    sh(["systemctl", "restart", svc, "bluealsa-aplay.service"])
    set_config("BT_AUDIO", "yes")


def bt_disable():
    for svc in ("bluealsad.service", "bluealsa.service", "bluealsa-aplay.service"):
        p = "/etc/systemd/system/%s.d/vitadock.conf" % svc
        if os.path.exists(p):
            os.remove(p)
    sh(["systemctl", "disable", "--now", "bluealsa-aplay.service"])
    main = "/etc/bluetooth/main.conf"
    if os.path.exists(main + ".vitadock-bak"):
        os.replace(main + ".vitadock-bak", main)
    sh(["systemctl", "daemon-reload"])
    sh(["systemctl", "restart", "bluetooth.service"])
    if os.path.exists(CONF):
        set_config("BT_AUDIO", "no")


def bt_paired():
    out = sh(["bluetoothctl", "devices", "Paired"]).stdout
    if "Device" not in out:
        out = sh(["bluetoothctl", "paired-devices"]).stdout
    found = {}
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[0] == "Device":
            found[parts[1]] = parts[2] if len(parts) > 2 else parts[1]
    return found


class Pairing:
    SECONDS = 180

    def __init__(self):
        self.agent = None
        self.started = 0
        self.before = {}
        self.result = None

    def start(self):
        for args in (["power", "on"], ["pairable", "on"], ["system-alias", "VitaDock++"],
                     ["discoverable-timeout", str(self.SECONDS)], ["discoverable", "on"]):
            sh(["bluetoothctl"] + args)
        self.before = bt_paired()
        if shutil.which("bt-agent"):
            self.agent = subprocess.Popen(["bt-agent", "-c", "NoInputNoOutput"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started = time.time()

    def left(self):
        return max(0, int(self.SECONDS - (time.time() - self.started)))

    def poll(self):
        new = {mac: name for mac, name in bt_paired().items() if mac not in self.before}
        for mac in new:
            sh(["bluetoothctl", "trust", mac])
        if new:
            self.result = ", ".join(new.values())
        return self.result

    def stop(self):
        if self.agent and self.agent.poll() is None:
            self.agent.terminate()
        sh(["bluetoothctl", "discoverable", "off"])


def wifi_available():
    return shutil.which("nmcli") is not None


def nm_split(line):
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def wifi_radio_on():
    return sh(["nmcli", "radio", "wifi"]).stdout.strip() == "enabled"


def wifi_set_radio(on):
    r = sh(["nmcli", "radio", "wifi", "on" if on else "off"])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "nmcli failed")
    if on:
        time.sleep(4)


def wifi_device():
    for line in sh(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"]).stdout.splitlines():
        f = nm_split(line)
        if len(f) >= 2 and f[1] == "wifi":
            return f[0]
    return "wlan0"


def wifi_known():
    known = {}
    for line in sh(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"]).stdout.splitlines():
        f = nm_split(line)
        if len(f) >= 2 and f[1] in ("802-11-wireless", "wifi"):
            ssid = nm_split(sh(["nmcli", "-t", "-g", "802-11-wireless.ssid",
                                "connection", "show", f[0]]).stdout.strip())[0]
            known[ssid or f[0]] = f[0]
    return known


def wifi_scan(rescan=True):
    out = sh(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
              "--rescan", "yes" if rescan else "no"], timeout=30).stdout
    known = wifi_known()
    nets = {}
    for line in out.splitlines():
        f = nm_split(line)
        if len(f) < 4 or not f[1]:
            continue
        ssid = f[1]
        sig = int(f[2]) if f[2].isdigit() else 0
        old = nets.get(ssid, {"signal": 0, "active": False})
        nets[ssid] = {"ssid": ssid,
                      "signal": max(sig, old["signal"]),
                      "secure": f[3].strip() not in ("", "--"),
                      "active": f[0].strip() == "*" or old["active"],
                      "known": ssid in known}
    return sorted(nets.values(), key=lambda n: (not n["active"], -n["signal"], n["ssid"].lower()))


def wifi_label():
    if not wifi_available():
        return "n/a"
    if not wifi_radio_on():
        return "off"
    for n in wifi_scan(rescan=False):
        if n["active"]:
            return n["ssid"] if len(n["ssid"]) <= 22 else n["ssid"][:21] + "~"
    return "not connected"


def nm_error(text):
    t = (text or "").strip()
    low = t.lower()
    if "secrets were required" in low or "802-1x" in low or "password" in low or "authentication" in low:
        return "wrong password, or the network turned it down."
    if "no network with ssid" in low:
        return "can't see that network any more. scan again?"
    if "timeout" in low or "timed out" in low:
        return "it took too long to connect. is the signal ok?"
    line = t.splitlines()[-1] if t else "nmcli failed"
    return line.replace("Error: ", "")[:70].lower()


def wifi_connect(ssid, password=None):
    known = wifi_known()
    if password is None and ssid in known:
        r = sh(["nmcli", "connection", "up", "id", known[ssid]], timeout=60)
    else:
        if ssid in known:
            sh(["nmcli", "connection", "delete", "id", known[ssid]])
        cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", wifi_device()]
        if password:
            cmd += ["password", password]
        r = sh(cmd, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(nm_error(r.stderr or r.stdout))


def wifi_forget(ssid):
    known = wifi_known()
    if ssid in known:
        r = sh(["nmcli", "connection", "delete", "id", known[ssid]])
    else:
        r = sh(["nmcli", "device", "disconnect", wifi_device()])
    if r.returncode != 0:
        raise RuntimeError(nm_error(r.stderr or r.stdout))


def signal_bars(sig):
    n = 4 if sig >= 75 else 3 if sig >= 50 else 2 if sig >= 30 else 1
    return "\u2582\u2584\u2586\u2588"[:n] + " " * (4 - n)


KB_LAYERS = {
    "abc": ["1234567890", "qwertyuiop", "asdfghjkl@", "zxcvbnm._-"],
    "ABC": ["1234567890", "QWERTYUIOP", "ASDFGHJKL@", "ZXCVBNM._-"],
    "#+=": ["1234567890", "!\"#$%&'()*", "+,/:;<=>?@", "[\\]^_`{|}~"],
}
KB_NEXT = {"abc": "ABC", "ABC": "#+=", "#+=": "abc"}
KB_SPECIAL = ["layer", "space", "del", "show", "connect", "cancel"]


def kb_cols(row):
    return len(KB_SPECIAL) if row == 4 else 10


BG = (22, 23, 27)
FG = (226, 223, 214)
DIM = (120, 122, 128)
FAINT = (52, 54, 60)
ACCENT = (240, 182, 72)
ACCENT_FG = (22, 23, 27)
BAD = (232, 108, 86)
GOOD = (140, 196, 120)

FONT_FILES = {"mono": "DejaVuSansMono.ttf", "bold": "DejaVuSansMono-Bold.ttf"}
FONT_SPECS = {"big": ("bold", 34), "body": ("mono", 22), "small": ("mono", 17), "head": ("bold", 20)}


class PygameGfx:
    def __init__(self, surface):
        import pygame
        self.pg = pygame
        self.surf = surface
        self.w, self.h = surface.get_size()
        self.u = self.h / 720.0
        self.fonts = {}
        for key, (face, size) in FONT_SPECS.items():
            path = os.path.join(FONT_DIR, FONT_FILES[face])
            px = max(10, int(size * self.u))
            try:
                self.fonts[key] = pygame.font.Font(path, px)
            except (OSError, pygame.error):
                self.fonts[key] = pygame.font.Font(None, int(px * 1.3))

    def fill(self, color):
        self.surf.fill(color)

    def rect(self, x, y, w, h, color):
        self.pg.draw.rect(self.surf, color, (int(x), int(y), int(w), int(h)))

    def measure(self, s, font):
        return self.fonts[font].size(s)[0]

    def text(self, x, y, s, font, color, align="l"):
        img = self.fonts[font].render(s, True, color)
        w = img.get_width()
        x = x - w if align == "r" else x - w / 2 if align == "c" else x
        self.surf.blit(img, (int(x), int(y)))
        return w

    def line_h(self, font):
        return self.fonts[font].get_linesize()


class PilGfx:
    def __init__(self, w, h):
        from PIL import Image, ImageDraw, ImageFont
        self.img = Image.new("RGB", (w, h), BG)
        self.d = ImageDraw.Draw(self.img)
        self.w, self.h = w, h
        self.u = h / 720.0
        self.fonts = {}
        for key, (face, size) in FONT_SPECS.items():
            self.fonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, FONT_FILES[face]),
                                                 max(10, int(size * self.u)))

    def fill(self, color):
        self.d.rectangle([0, 0, self.w, self.h], fill=color)

    def rect(self, x, y, w, h, color):
        if w > 0 and h > 0:
            self.d.rectangle([int(x), int(y), int(x + w) - 1, int(y + h) - 1], fill=color)

    def measure(self, s, font):
        return int(self.d.textlength(s, font=self.fonts[font]))

    def text(self, x, y, s, font, color, align="l"):
        w = self.measure(s, font)
        x = x - w if align == "r" else x - w / 2 if align == "c" else x
        self.d.text((int(x), int(y)), s, font=self.fonts[font], fill=color)
        return w

    def line_h(self, font):
        a, b = self.fonts[font].getmetrics()
        return a + b


HANDHELD = [
    "....################....",
    "..##................##..",
    ".#....############....#.",
    "#..#..#..........#..#..#",
    "#.###.#..........#.#.#.#",
    "#..#..#..........#..#..#",
    "#.....#..........#.....#",
    "#.....############.....#",
    ".#....................#.",
    "..##................##..",
    "....################....",
    "...........##...........",
    "...........##...........",
    "...........##...........",
]


def draw_handheld(g, x, y, px, color):
    for r, row in enumerate(HANDHELD):
        for c, ch in enumerate(row):
            if ch == "#":
                g.rect(x + c * px, y + r * px, px, px, color)


class State:
    def __init__(self):
        self.screen = "home"
        self.sel = 0
        self.items = []
        self.cfg = dict(DEFAULTS)
        self.disp = None
        self.out_mode = ""
        self.vita = None
        self.pixfmt = ""
        self.error = ""
        self.confirm = None
        self.msg = None
        self.busy = ""
        self.pairing = None
        self.hits = []
        self.ver = version()
        self.ips = []
        self.wifi_label = ""
        self.wifi_on = True
        self.wifi_nets = []
        self.wifi_items = []
        self.wsel = 0
        self.wifi_status = ""
        self.text = ""
        self.text_target = ""
        self.text_error = ""
        self.show_pw = False
        self.kb_layer = "abc"
        self.kb_r = 1
        self.kb_c = 0


def build_menu(st):
    cfg = st.cfg
    items = [("back", "back to game" if st.vita else "close menu", "")]
    items.append(("mode", "video mode", mode_name(cfg)))
    fast = cfg["PLAYER"] == "fast" and gst_available()
    items.append(("player", "player", "fast" if cfg["PLAYER"] == "fast" else "compatible"))
    if not fast:
        items.append(("scaler", "upscaling", cfg["SCALER"]))
    items.append(("output", "tv resolution", cfg["OUTPUT_MODE"]))
    items.append(("wifi", "wi-fi", st.wifi_label or "..."))
    items.append(("audio", "bluetooth audio", "on" if cfg["BT_AUDIO"] == "yes" else "off"))
    if cfg["BT_AUDIO"] == "yes":
        items.append(("pair", "pair the vita for audio", ""))
    items += [("info", "info + help", ""),
              ("update", "check for updates", ""),
              ("reboot", "restart the pi", ""),
              ("shutdown", "shut down the pi", ""),
              ("uninstall", "uninstall vitadock++", "")]
    st.items = items
    st.sel = min(st.sel, len(items) - 1)


def item_help(st, key):
    cfg = st.cfg
    if key == "mode":
        for name, _, _, text in VIDEO_MODES:
            if name == mode_name(cfg):
                return text
        return "custom mode from vitadock.conf"
    if key == "scaler":
        return dict(SCALERS).get(cfg["SCALER"], "")
    if key == "output":
        return dict(OUTPUTS).get(cfg["OUTPUT_MODE"], "custom, from vitadock.conf") + ". applies after a restart"
    return {
        "back": "the game picks up where it was" if st.vita else "back to the waiting screen",
        "wifi": "join a network, or see which one the pi is on",
        "audio": "vita sound over bluetooth, out of the tv. still a bit experimental",
        "pair": "do this once, then connect from the vita's bluetooth menu",
        "info": "ip address, what's plugged in, controls",
        "update": "needs the pi on the internet",
        "player": ("fast: lowest delay, the pi's display hardware does the scaling"
                   if cfg["PLAYER"] == "fast" and gst_available() else
                   "fast isn't installed, using mpv. re-run the installer to get it"
                   if cfg["PLAYER"] == "fast" else
                   "compatible: mpv. more delay, but lets you pick the upscaling filter"),
        "reboot": "",
        "shutdown": "wait for the green light to stop before pulling the power",
        "uninstall": "removes vitadock++ and restarts into plain pi os",
    }.get(key, "")


def draw_chrome(g, st, hints):
    u, W, H = g.u, g.w, g.h
    m = 40 * u
    g.fill(BG)
    g.text(m, 26 * u, "VitaDock++", "head", FG)
    g.text(m + g.measure("VitaDock++ ", "head"), 29 * u, st.ver, "small", DIM)
    if st.disp:
        g.text(W - m, 29 * u, "%s  %s" % (st.disp[1].lower(), st.out_mode), "small", DIM, "r")
    g.rect(m, 62 * u, W - 2 * m, max(1, int(2 * u)), FAINT)
    if hints:
        g.rect(m, H - 62 * u, W - 2 * m, max(1, int(2 * u)), FAINT)
        g.text(m, H - 46 * u, hints, "small", DIM)


def dotted(g, x, y, key, value, width, color=FG):
    g.text(x, y, key, "body", DIM)
    vw = g.measure(value, "body")
    kw = g.measure(key + " ", "body")
    dot_w = g.measure(".", "body")
    n = int((width - kw - vw - dot_w) / dot_w)
    if n > 1:
        g.text(x + kw, y, "." * n, "body", FAINT)
    g.text(x + width, y, value, "body", color, "r")


def draw_home(g, st):
    u = g.u
    draw_chrome(g, st, "enter / click / (A) . . . settings")
    px = max(2, int(9 * u))
    left = 120 * u
    top = 150 * u
    draw_handheld(g, left, top, px, ACCENT)

    tx = left + len(HANDHELD[0]) * px + 70 * u
    if st.vita:
        g.text(tx, top, "vita found, starting...", "big", FG)
    else:
        g.text(tx, top, "plug your vita in.", "big", FG)
    lh = g.line_h("body")
    y = top + 62 * u
    g.text(tx, y, "usb cable into any port on the pi.", "body", DIM)
    g.text(tx, y + lh, "the vita needs the udcd_uvc plugin.", "body", DIM)

    y = top + 180 * u
    width = g.w - tx - 120 * u
    tv = "%s, %s" % (st.disp[1].lower(), st.out_mode) if st.disp else "none"
    dotted(g, tx, y, "tv", tv, width)
    dotted(g, tx, y + lh * 1.3, "vita", st.vita or "not plugged in", width, FG if st.vita else DIM)
    dotted(g, tx, y + lh * 2.6, "mode", "%s (%s @ %s)" % (mode_name(st.cfg), st.cfg["VITA_MODE"], st.cfg["VITA_FPS"]), width)
    dotted(g, tx, y + lh * 3.9, "audio", "bluetooth" if st.cfg["BT_AUDIO"] == "yes" else "cable from the vita", width)
    if st.error:
        g.text(tx, y + lh * 5.6, st.error, "small", BAD)


def draw_menu(g, st):
    u, W = g.u, g.w
    draw_chrome(g, st, "up/down move   left/right change   enter select   esc back")
    st.hits = []
    x = 120 * u
    w = W - 240 * u
    y = 96 * u
    g.text(x, y, "settings", "small", DIM)
    y += 34 * u
    row = 40 * u
    pad = 14 * u
    for i, (key, label, value) in enumerate(st.items):
        sel = i == st.sel
        if sel:
            g.rect(x - pad, y - 6 * u, w + 2 * pad, row, ACCENT)
        color = ACCENT_FG if sel else (BAD if key == "uninstall" else FG)
        g.text(x, y, ("> " if sel else "  ") + label, "body", color)
        if value:
            v = "< %s >" % value if sel else value
            g.text(x + w, y, v, "body", ACCENT_FG if sel else DIM, "r")
        st.hits.append((x - pad, y - 6 * u, w + 2 * pad, row, i))
        y += row
    tip = item_help(st, st.items[st.sel][0]) if st.items else ""
    if tip:
        g.text(x, y + 18 * u, tip, "small", DIM)


def draw_confirm(g, st):
    u = g.u
    c = st.confirm
    draw_chrome(g, st, "left/right choose   enter ok   esc cancel")
    st.hits = []
    x, y = 120 * u, 150 * u
    for line in c["lines"]:
        g.text(x, y, line, "body", FG)
        y += g.line_h("body") * 1.2
    y += 40 * u
    bx = x
    for i, label in enumerate(("no", "yes")):
        bw = g.measure(label, "body") + 48 * u
        sel = c["sel"] == i
        if sel:
            g.rect(bx, y - 8 * u, bw, 44 * u, ACCENT)
        g.text(bx + 24 * u, y, label, "body", ACCENT_FG if sel else FG)
        st.hits.append((bx, y - 8 * u, bw, 44 * u, i))
        bx += bw + 24 * u


def draw_message(g, st):
    u = g.u
    draw_chrome(g, st, "" if st.screen == "busy" else "enter / esc . . . back")
    x, y = 120 * u, 150 * u
    if st.screen == "busy":
        g.text(x, y, st.busy, "big", FG)
        g.text(x, y + 60 * u, "hang on a sec.", "body", DIM)
        return
    g.text(x, y, st.msg["title"], "big", FG)
    y += 70 * u
    for line in st.msg["lines"]:
        g.text(x, y, line, "body", DIM)
        y += g.line_h("body") * 1.3


def draw_pair(g, st):
    u = g.u
    p = st.pairing
    draw_chrome(g, st, "esc . . . stop pairing")
    x, y = 120 * u, 150 * u
    lh = g.line_h("body") * 1.3
    if p and p.result:
        g.text(x, y, "paired: " + p.result, "big", GOOD)
        g.text(x, y + 70 * u, "from now on just connect from the vita's bluetooth menu.", "body", DIM)
        return
    left = p.left() if p else 0
    g.text(x, y, "pairing mode  %d:%02d" % (left // 60, left % 60), "big", FG)
    y += 70 * u
    for line in ("on the vita go to settings > devices > bluetooth devices",
                 "and pick 'VitaDock++' from the list.",
                 "",
                 "if it asks to confirm a code, say yes."):
        g.text(x, y, line, "body", DIM)
        y += lh


def draw_info(g, st):
    u = g.u
    draw_chrome(g, st, "enter / esc . . . back")
    x, y = 120 * u, 110 * u
    width = g.w - 240 * u
    lh = g.line_h("body") * 1.3
    rows = [
        ("version", st.ver),
        ("ip address", ", ".join(st.ips) or "not on a network"),
        ("tv", "%s at %s" % (st.disp[1], st.out_mode) if st.disp else "none"),
        ("vita", "%s (%s)" % (st.vita, st.pixfmt or "default format") if st.vita else "not plugged in"),
        ("player", ("fast" if st.cfg["PLAYER"] == "fast" and gst_available() else "compatible (mpv)")),
        ("settings file", CONF),
    ]
    for k, v in rows:
        dotted(g, x, y, k, v, width)
        y += lh
    y += 30 * u
    g.text(x, y, "opening this menu mid-game:", "body", FG)
    y += lh
    for line in ("keyboard ...... esc, tab or the windows key",
                 "mouse ......... right click",
                 "usb pad ....... ps/home button, or start + select",
                 "or just unplug the vita."):
        g.text(x + 24 * u, y, line, "body", DIM)
        y += lh


def build_wifi(st):
    items = [("radio", "wi-fi", "on" if st.wifi_on else "off", None)]
    if st.wifi_on:
        for n in st.wifi_nets:
            name = n["ssid"] if len(n["ssid"]) <= 30 else n["ssid"][:29] + "~"
            if n["active"]:
                value = "connected"
            else:
                tag = "saved" if n["known"] else "locked" if n["secure"] else "open"
                value = "%s  %s" % (tag, signal_bars(n["signal"]))
            items.append(("net", name, value, n))
        items.append(("scan", "scan again", "", None))
    items.append(("back", "back", "", None))
    st.wifi_items = items
    st.wsel = min(st.wsel, len(items) - 1)
    cur = [n["ssid"] for n in st.wifi_nets if n["active"]]
    if not st.wifi_on:
        st.wifi_status = "wi-fi is off"
    elif cur:
        st.wifi_status = "on " + cur[0]
    else:
        st.wifi_status = "not connected"


def wifi_help(st):
    if not st.wifi_items:
        return ""
    key, _, _, n = st.wifi_items[st.wsel]
    if key == "radio":
        return "turn it off if you only use ethernet. updates need some kind of internet."
    if key == "scan":
        return "look for networks again"
    if key == "net" and n:
        if n["active"]:
            return "select to forget this network"
        if n["known"]:
            return "saved already, select to connect"
        return "select to type the password" if n["secure"] else "no password needed"
    return ""


def draw_wifi(g, st):
    u, W, H = g.u, g.w, g.h
    draw_chrome(g, st, "up/down move   enter select   esc back")
    st.hits = []
    x = 120 * u
    w = W - 240 * u
    y = 96 * u
    g.text(x, y, "wi-fi", "small", DIM)
    g.text(x + w, y, st.wifi_status, "small", DIM, "r")
    y += 34 * u
    row = 40 * u
    pad = 14 * u
    items = st.wifi_items
    fits = max(3, int((H - 140 * u - y) / row))
    top = max(0, min(st.wsel - fits // 2, len(items) - fits))
    for i in range(top, min(len(items), top + fits)):
        key, label, value, n = items[i]
        sel = i == st.wsel
        if sel:
            g.rect(x - pad, y - 6 * u, w + 2 * pad, row, ACCENT)
        color = ACCENT_FG if sel else FG
        g.text(x, y, ("> " if sel else "  ") + label, "body", color)
        if value:
            vcol = ACCENT_FG if sel else (GOOD if value == "connected" else DIM)
            g.text(x + w, y, value, "body", vcol, "r")
        st.hits.append((x - pad, y - 6 * u, w + 2 * pad, row, i))
        y += row
    if top + fits < len(items):
        g.text(x, y, "  ...", "body", DIM)
    tip = wifi_help(st)
    if tip:
        g.text(x, y + 18 * u, tip, "small", DIM)


def kb_label(st, c):
    k = KB_SPECIAL[c]
    if k == "layer":
        return KB_NEXT[st.kb_layer]
    if k == "show":
        return "hide" if st.show_pw else "show"
    return k


def draw_text(g, st):
    u, W = g.u, g.w
    draw_chrome(g, st, "type, or use the keys   enter connect   (B) delete   esc cancel")
    st.hits = []
    x = 120 * u
    fw = W - 240 * u
    y = 96 * u
    g.text(x, y, "password for '%s'" % st.text_target, "body", FG)
    y += 44 * u
    fh = 56 * u
    g.rect(x, y, fw, fh, FAINT)
    g.rect(x + 2 * u, y + 2 * u, fw - 4 * u, fh - 4 * u, BG)
    shown = st.text if st.show_pw else "*" * len(st.text)
    g.text(x + 16 * u, y + 14 * u, shown + "_", "body", FG)
    g.text(x + fw - 16 * u, y + 17 * u, "%d" % len(st.text), "small", DIM, "r")
    if st.text_error:
        g.text(x, y + fh + 10 * u, st.text_error, "small", BAD)
    y += fh + 48 * u
    kh = 46 * u
    gap = 8 * u
    rows = KB_LAYERS[st.kb_layer]
    for r in range(5):
        n = kb_cols(r)
        kw = fw / n
        for c in range(n):
            label = rows[r][c] if r < 4 else kb_label(st, c)
            kx = x + c * kw
            ky = y + r * (kh + gap)
            sel = (st.kb_r, st.kb_c) == (r, c)
            g.rect(kx + 3 * u, ky, kw - 6 * u, kh, ACCENT if sel else FAINT)
            g.text(kx + kw / 2, ky + 10 * u, label, "body" if r < 4 else "small",
                   ACCENT_FG if sel else FG, "c")
            st.hits.append((kx, ky, kw, kh, (r, c)))


def draw(g, st):
    {"home": draw_home, "menu": draw_menu, "confirm": draw_confirm, "msg": draw_message,
     "busy": draw_message, "pair": draw_pair, "info": draw_info,
     "wifi": draw_wifi, "text": draw_text}[st.screen](g, st)


class PygameInput:

    def __init__(self, pygame):
        self.pg = pygame
        self.ctl = None
        self.pads = {}
        self.ctl_ids = set()
        self.axis = {}
        self.text_mode = False
        try:
            from pygame._sdl2 import controller
            controller.init()
            self.ctl = controller
        except Exception:
            self.ctl = None
        pygame.joystick.init()
        pygame.key.set_repeat(350, 70)

    def _add_pad(self, index):
        try:
            if self.ctl and self.ctl.is_controller(index):
                c = self.ctl.Controller(index)
                iid = c.as_joystick().get_instance_id()
                self.ctl_ids.add(iid)
                self.pads[iid] = c
            else:
                j = self.pg.joystick.Joystick(index)
                self.pads[j.get_instance_id()] = j
        except Exception as e:
            log("couldn't open gamepad %s: %s" % (index, e))

    def _stick(self, key, value):
        old = self.axis.get(key, 0)
        new = -1 if value < -0.6 else 1 if value > 0.6 else 0
        self.axis[key] = new
        return new if new != old else 0

    def set_text_mode(self, on):
        if on != self.text_mode:
            self.text_mode = on
            try:
                (self.pg.key.start_text_input if on else self.pg.key.stop_text_input)()
            except Exception:
                pass

    def actions(self):
        pg = self.pg
        out = []
        events = pg.event.get()
        got_text = any(e.type == pg.TEXTINPUT for e in events)
        for e in events:
            t = e.type
            if t == pg.TEXTINPUT:
                if self.text_mode:
                    out.append(("TEXT", e.text))
            elif t == pg.KEYDOWN and self.text_mode:
                k = e.key
                if k == pg.K_UP:
                    out.append(("UP",))
                elif k == pg.K_DOWN:
                    out.append(("DOWN",))
                elif k == pg.K_LEFT:
                    out.append(("LEFT",))
                elif k == pg.K_RIGHT:
                    out.append(("RIGHT",))
                elif k in (pg.K_RETURN, pg.K_KP_ENTER):
                    out.append(("OK", "key"))
                elif k == pg.K_ESCAPE:
                    out.append(("BACK", "esc"))
                elif k == pg.K_BACKSPACE:
                    out.append(("BACK", "bksp"))
                elif not got_text and getattr(e, "unicode", "") and e.unicode.isprintable():
                    out.append(("TEXT", e.unicode))
            elif t == pg.KEYDOWN:
                k = e.key
                if k in (pg.K_UP, pg.K_w):
                    out.append(("UP",))
                elif k in (pg.K_DOWN, pg.K_s, pg.K_TAB):
                    out.append(("DOWN",))
                elif k in (pg.K_LEFT, pg.K_a):
                    out.append(("LEFT",))
                elif k in (pg.K_RIGHT, pg.K_d):
                    out.append(("RIGHT",))
                elif k in (pg.K_RETURN, pg.K_KP_ENTER, pg.K_SPACE):
                    out.append(("OK", "key"))
                elif k == pg.K_ESCAPE:
                    out.append(("BACK", "esc"))
                elif k == pg.K_BACKSPACE:
                    out.append(("BACK", "bksp"))
                else:
                    out.append(("ANY",))
            elif t == pg.MOUSEMOTION:
                pg.mouse.set_visible(True)
                out.append(("HOVER", e.pos))
            elif t == pg.MOUSEBUTTONDOWN and e.button == 1:
                out.append(("CLICK", e.pos))
            elif t == pg.MOUSEBUTTONDOWN and e.button == 3:
                out.append(("BACK", "esc"))
            elif t == pg.MOUSEWHEEL:
                out.append(("UP",) if e.y > 0 else ("DOWN",))
            elif t == pg.JOYDEVICEADDED:
                self._add_pad(e.device_index)
            elif t == pg.JOYDEVICEREMOVED:
                self.pads.pop(e.instance_id, None)
                self.ctl_ids.discard(e.instance_id)
            elif t == getattr(pg, "CONTROLLERBUTTONDOWN", -1):
                b = e.button
                m = {pg.CONTROLLER_BUTTON_A: ("OK",), pg.CONTROLLER_BUTTON_START: ("SUBMIT",),
                     pg.CONTROLLER_BUTTON_B: ("BACK",), pg.CONTROLLER_BUTTON_BACK: ("BACK", "esc"),
                     pg.CONTROLLER_BUTTON_GUIDE: ("BACK", "esc"),
                     pg.CONTROLLER_BUTTON_DPAD_UP: ("UP",), pg.CONTROLLER_BUTTON_DPAD_DOWN: ("DOWN",),
                     pg.CONTROLLER_BUTTON_DPAD_LEFT: ("LEFT",), pg.CONTROLLER_BUTTON_DPAD_RIGHT: ("RIGHT",)}
                out.append(m.get(b, ("ANY",)))
            elif t == getattr(pg, "CONTROLLERAXISMOTION", -1):
                if e.axis == pg.CONTROLLER_AXIS_LEFTY:
                    d = self._stick(("c", e.instance_id, "y"), e.value / 32767.0)
                    if d:
                        out.append(("DOWN",) if d > 0 else ("UP",))
                elif e.axis == pg.CONTROLLER_AXIS_LEFTX:
                    d = self._stick(("c", e.instance_id, "x"), e.value / 32767.0)
                    if d:
                        out.append(("RIGHT",) if d > 0 else ("LEFT",))
            elif t == pg.JOYHATMOTION and e.instance_id not in self.ctl_ids:
                hx, hy = e.value
                if hy:
                    out.append(("UP",) if hy > 0 else ("DOWN",))
                if hx:
                    out.append(("RIGHT",) if hx > 0 else ("LEFT",))
            elif t == pg.JOYBUTTONDOWN and e.instance_id not in self.ctl_ids:
                out.append((("OK", "BACK")[e.button] if e.button < 2 else "OK",))
            elif t == pg.JOYAXISMOTION and e.instance_id not in self.ctl_ids and e.axis < 2:
                d = self._stick(("j", e.instance_id, e.axis), e.value)
                if d:
                    if e.axis == 1:
                        out.append(("DOWN",) if d > 0 else ("UP",))
                    else:
                        out.append(("RIGHT",) if d > 0 else ("LEFT",))
        return out


class Screen:

    def __init__(self):
        self.pg = None
        self.gfx = None
        self.input = None
        self.card = None

    def is_open(self):
        return self.gfx is not None

    def open(self, disp):
        if self.gfx and self.card == disp[0]:
            return True
        self.close()
        os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
        os.environ["SDL_KMSDRM_DEVICE_INDEX"] = re.sub(r"\D", "", disp[0]) or "0"
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        try:
            import pygame
            pygame.display.init()
            pygame.font.init()
            surf = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            pygame.mouse.set_visible(False)
            self.pg = pygame
            self.gfx = PygameGfx(surf)
            self.input = PygameInput(pygame)
            self.card = disp[0]
            return True
        except Exception as e:
            log("couldn't open the screen: %s" % e)
            self.close()
            return False

    def close(self):
        if self.pg:
            try:
                self.pg.joystick.quit()
                self.pg.display.quit()
            except Exception:
                pass
        self.pg = self.gfx = self.input = self.card = None

    def present(self):
        self.pg.display.flip()


class HotkeyWatcher:

    def __init__(self):
        try:
            import evdev
            self.ev = evdev
        except ImportError:
            self.ev = None
            log("python3-evdev missing, so no mid-game menu key (unplug the vita instead)")

    def watch(self, stop, hit):
        if not self.ev:
            return
        ec = self.ev.ecodes
        names = ("KEY_ESC", "KEY_TAB", "KEY_LEFTMETA", "KEY_RIGHTMETA", "KEY_COMPOSE",
                 "BTN_RIGHT", "BTN_MIDDLE", "BTN_MODE")
        single = {getattr(ec, n) for n in names if hasattr(ec, n)}
        combo = {ec.BTN_START, ec.BTN_SELECT}
        wanted = single | combo
        devices = {}
        held = set()
        last_scan = 0
        while not stop.is_set():
            if time.time() - last_scan > 2:
                last_scan = time.time()
                for path in self.ev.list_devices():
                    if path in devices:
                        continue
                    try:
                        d = self.ev.InputDevice(path)
                        if wanted & set(d.capabilities().get(ec.EV_KEY, [])):
                            devices[path] = d
                        else:
                            d.close()
                    except OSError:
                        pass
            if not devices:
                stop.wait(0.5)
                continue
            try:
                ready, _, _ = select.select(list(devices.values()), [], [], 0.3)
            except (OSError, ValueError):
                ready = []
            for d in ready:
                try:
                    for e in d.read():
                        if e.type != ec.EV_KEY:
                            continue
                        if e.value == 1:
                            held.add(e.code)
                            if e.code in single or combo <= held:
                                hit.set()
                        elif e.value == 0:
                            held.discard(e.code)
                except OSError:
                    devices.pop(d.path, None)
        for d in devices.values():
            try:
                d.close()
            except OSError:
                pass


class Dock:
    def __init__(self):
        self.st = State()
        self.screen = Screen()
        self.hotkeys = HotkeyWatcher()
        self.fails = 0
        self.retry_at = 0
        self.last_vita_check = 0
        self.last_note = ""
        self.dirty = True
        self.last_draw = 0
        self.gst_fails = 0
        self.gst_broken = False
        self.player_used = ""

    def note(self, msg):
        if msg != self.last_note:
            log(msg)
            self.last_note = msg


    def pick_player(self):
        if self.st.cfg["PLAYER"] == "fast" and not self.gst_broken and gst_available():
            return "fast"
        return "mpv"

    def play(self):
        st = self.st
        player = self.pick_player()
        if player == "fast":
            cmd = gst_command(st.cfg, st.vita, st.pixfmt)
        else:
            cmd = mpv_command(st.cfg, st.disp, st.out_mode, st.vita, st.pixfmt)
        self.player_used = player
        log("playing %s with %s: %s @ %sfps (%s) -> %s %s" % (
            st.vita, player, st.cfg["VITA_MODE"], st.cfg["VITA_FPS"], st.pixfmt or "default",
            st.disp[1], st.out_mode))
        self.screen.close()
        started = time.time()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        stop, hit = threading.Event(), threading.Event()
        t = threading.Thread(target=self.hotkeys.watch, args=(stop, hit), daemon=True)
        t.start()
        while proc.poll() is None:
            if hit.is_set():
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(0.05)
        stop.set()
        t.join(2)
        ran = time.time() - started
        self.last_note = ""
        if hit.is_set():
            self.fails = 0
            st.error = ""
            self.open_menu()
        elif ran < 5:
            log("%s stopped after %.1fs (exit %s)" % (player, ran, proc.returncode))
            if player == "fast":
                self.gst_fails += 1
                if self.gst_fails >= 2:
                    self.gst_broken = True
                    log("fast player keeps failing, falling back to mpv")
                    st.error = "fast player didn't work here, using the compatible one."
                    self.retry_at = time.time() + 1
                else:
                    st.error = "starting the fast player again..."
                    self.retry_at = time.time() + 2
            else:
                self.fails += 1
                st.error = "vita found but no picture (try %d). trying again..." % self.fails
                self.retry_at = time.time() + (3 if self.fails < 3 else 10)
        else:
            if player == "fast":
                self.gst_fails = 0
            self.fails = 0
            st.error = ""
            log("stream ended")
        self.last_vita_check = 0
        self.dirty = True


    def open_menu(self, sel=0):
        st = self.st
        try:
            st.wifi_label = wifi_label()
        except Exception as e:
            log("wi-fi status failed: %s" % e)
            st.wifi_label = "?"
        st.screen, st.sel = "menu", sel
        build_menu(st)

    def message(self, title, *lines, back="menu"):
        self.st.msg = {"title": title, "lines": list(lines), "back": back}
        self.st.screen = "msg"

    def ask(self, lines, yes, back="menu"):
        self.st.confirm = {"lines": lines, "yes": yes, "sel": 0, "back": back}
        self.st.screen = "confirm"

    def go_back(self, where):
        if where == "wifi":
            build_wifi(self.st)
            self.st.screen = "wifi"
        else:
            keys = [k for k, _, _ in self.st.items]
            self.open_menu(keys.index("wifi") if where == "menu-wifi" and "wifi" in keys else 0)


    def open_wifi(self, rescan=True):
        st = self.st
        if not wifi_available():
            self.message("can't manage wi-fi here", "this pi isn't using networkmanager (nmcli),",
                         "which pi os lite normally does.")
            return

        def scan():
            st.wifi_on = wifi_radio_on()
            st.wifi_nets = wifi_scan(rescan) if st.wifi_on else []

        if self.busy("looking for wi-fi networks" if rescan else "checking wi-fi", scan):
            st.wsel = 0
            self.go_back("wifi")

    def wifi_activate(self):
        st = self.st
        key, _, _, n = st.wifi_items[st.wsel]
        if key == "radio":
            on = not st.wifi_on
            if self.busy("turning wi-fi " + ("on" if on else "off"), lambda: wifi_set_radio(on)):
                self.open_wifi(rescan=on)
        elif key == "scan":
            self.open_wifi(True)
        elif key == "back":
            self.go_back("menu-wifi")
        elif key == "net":
            ssid = n["ssid"]
            if n["active"]:
                self.ask(["forget '%s'?" % ssid, "",
                          "the pi disconnects and won't join it again",
                          "until you put the password back in."],
                         lambda: self.wifi_forget(ssid), back="wifi")
            elif n["known"] or not n["secure"]:
                self.wifi_join(ssid)
            else:
                self.start_text(ssid)

    def start_text(self, ssid, error=""):
        st = self.st
        st.text, st.text_target, st.text_error = "", ssid, error
        st.show_pw, st.kb_layer, st.kb_r, st.kb_c = False, "abc", 1, 0
        st.screen = "text"

    def wifi_join(self, ssid, password=None):
        st = self.st
        st.screen, st.busy = "busy", "connecting to " + ssid
        self.redraw(force=True)
        try:
            wifi_connect(ssid, password)
        except Exception as e:
            log("wi-fi connect to %s failed: %s" % (ssid, e))
            if password is not None or any(n["ssid"] == ssid and n["secure"] for n in st.wifi_nets):
                self.start_text(ssid, str(e) if password is not None else
                                "the saved password didn't work, type it again.")
            else:
                self.message("couldn't connect", str(e), back="wifi")
            return
        time.sleep(2)
        try:
            st.wifi_nets = wifi_scan(rescan=False)
        except Exception:
            pass
        st.ips = ip_addresses()
        self.message("connected to " + ssid,
                     "ip address: " + (", ".join(st.ips) or "still getting one"), back="wifi")

    def wifi_forget(self, ssid):
        if self.busy("forgetting " + ssid, lambda: wifi_forget(ssid)):
            self.open_wifi(rescan=False)

    def kb_press(self):
        st = self.st
        if st.kb_r < 4:
            self.type_text(KB_LAYERS[st.kb_layer][st.kb_r][st.kb_c])
            return
        k = KB_SPECIAL[st.kb_c]
        if k == "layer":
            st.kb_layer = KB_NEXT[st.kb_layer]
        elif k == "space":
            self.type_text(" ")
        elif k == "del":
            st.text = st.text[:-1]
        elif k == "show":
            st.show_pw = not st.show_pw
        elif k == "connect":
            self.submit_text()
        elif k == "cancel":
            self.go_back("wifi")

    def type_text(self, s):
        st = self.st
        st.text = (st.text + s)[:63]
        st.text_error = ""

    def submit_text(self):
        st = self.st
        if len(st.text) < 8:
            st.text_error = "wi-fi passwords are at least 8 characters."
            return
        self.wifi_join(st.text_target, st.text)

    def handle_text(self, action):
        st = self.st
        kind = action[0]
        extra = action[1] if len(action) > 1 else None
        if kind == "TEXT":
            self.type_text(action[1])
        elif kind in ("UP", "DOWN"):
            old = kb_cols(st.kb_r)
            st.kb_r = (st.kb_r + (1 if kind == "DOWN" else -1)) % 5
            st.kb_c = min(int((st.kb_c + 0.5) * kb_cols(st.kb_r) / old), kb_cols(st.kb_r) - 1)
        elif kind in ("LEFT", "RIGHT"):
            st.kb_c = (st.kb_c + (1 if kind == "RIGHT" else -1)) % kb_cols(st.kb_r)
        elif kind == "SUBMIT" or (kind == "OK" and extra == "key"):
            self.submit_text()
        elif kind == "OK":
            self.kb_press()
        elif kind == "BACK" and extra == "esc":
            self.go_back("wifi")
        elif kind == "BACK":
            st.text = st.text[:-1]

    def busy(self, text, fn):
        st = self.st
        st.screen, st.busy = "busy", text
        self.redraw(force=True)
        try:
            fn()
            return True
        except Exception as e:
            log("%s failed: %s" % (text, e))
            self.message("that didn't work", str(e)[:70], "", "vitadock's log has more: journalctl -u vitadock")
            return False

    def cycle(self, key, step):
        cfg = self.st.cfg
        if key == "mode":
            names = [m[0] for m in VIDEO_MODES]
            cur = mode_name(cfg)
            i = (names.index(cur) + step) % len(names) if cur in names else 0
            _, size, fps, _ = VIDEO_MODES[i]
            set_config("VITA_MODE", size)
            set_config("VITA_FPS", fps)
        elif key in ("scaler", "output"):
            opts = [o[0] for o in (SCALERS if key == "scaler" else OUTPUTS)]
            conf_key = "SCALER" if key == "scaler" else "OUTPUT_MODE"
            cur = cfg[conf_key]
            i = (opts.index(cur) + step) % len(opts) if cur in opts else 0
            set_config(conf_key, opts[i])
            if key == "output":
                try:
                    apply_output_mode(load_config())
                except OSError as e:
                    log("couldn't update cmdline.txt: %s" % e)
        elif key == "player":
            set_config("PLAYER", "compatible" if cfg["PLAYER"] == "fast" else "fast")
            self.gst_broken = False
        self.st.cfg = load_config()
        build_menu(self.st)

    def activate(self, key):
        st = self.st
        if key == "back":
            st.screen = "home"
            self.retry_at = 0
        elif key in ("mode", "scaler", "output", "player"):
            self.cycle(key, 1)
        elif key == "audio":
            if st.cfg["BT_AUDIO"] == "yes":
                self.ask(["turn bluetooth audio off?"], self.audio_off)
            else:
                self.ask(["turn on bluetooth audio?",
                          "",
                          "the vita connects to the dock like a speaker and the sound",
                          "comes out of the tv. it's a bit experimental."], self.audio_on)
        elif key == "pair":
            st.pairing = Pairing()
            if self.busy("getting bluetooth ready", st.pairing.start):
                st.screen = "pair"
        elif key == "info":
            st.ips = ip_addresses()
            st.screen = "info"
        elif key == "wifi":
            self.open_wifi(True)
        elif key == "update":
            self.check_update()
        elif key == "reboot":
            self.ask(["restart the pi?"], lambda: self.power("reboot"))
        elif key == "shutdown":
            self.ask(["shut down the pi?", "", "unplug it once the green light stops flashing."],
                     lambda: self.power("poweroff"))
        elif key == "uninstall":
            self.ask(["uninstall vitadock++?",
                      "",
                      "this removes it and its settings, then restarts the pi",
                      "into plain pi os. you'd need to reinstall to get it back."],
                     self.uninstall)

    def audio_on(self):
        if self.busy("turning bluetooth audio on", bt_enable):
            self.st.cfg = load_config()
            self.message("bluetooth audio is on",
                         "next: pick 'pair the vita for audio' in the menu.")

    def audio_off(self):
        if self.busy("turning bluetooth audio off", bt_disable):
            self.st.cfg = load_config()
            self.open_menu()

    def power(self, what):
        self.busy("restarting" if what == "reboot" else "shutting down", lambda: None)
        sh(["systemctl", what])
        time.sleep(30)

    def repo(self):
        slug = read_kv(INFO).get("REPO_SLUG", "")
        return "" if not slug or slug.startswith("YOUR-") else slug

    def check_update(self):
        slug = self.repo()
        if not slug:
            self.message("updates aren't set up", "this copy wasn't installed from github,",
                         "so there's nowhere to check.")
            return
        self.st.screen, self.st.busy = "busy", "checking for updates"
        self.redraw(force=True)
        try:
            socket.create_connection(("raw.githubusercontent.com", 443), timeout=4).close()
            url = "https://raw.githubusercontent.com/%s/main/VERSION" % slug
            with urllib.request.urlopen(url, timeout=8) as r:
                latest = r.read().decode().strip()
        except Exception:
            self.message("can't reach github", "is the pi on wi-fi or ethernet?",
                         "you can set wi-fi up in raspberry pi imager when you flash the card.")
            return
        if latest == self.st.ver:
            self.message("you're up to date", "running %s, the latest." % latest)
            return
        self.ask(["update %s -> %s?" % (self.st.ver, latest), "",
                  "the dock restarts by itself when it's done."], lambda: self.run_update(slug))

    def run_update(self, slug):
        cmd = "curl -fsSL https://raw.githubusercontent.com/%s/main/install.sh | bash -s -- --update --yes" % slug
        self.busy("updating", lambda: sh(["systemd-run", "--collect",
                                           "--unit=vitadock-update-%d" % int(time.time()),
                                           "/bin/bash", "-c", cmd]))
        time.sleep(120)

    def uninstall(self):
        self.busy("uninstalling", lambda: sh(["systemd-run", "--collect",
                                               "--unit=vitadock-uninstall-%d" % int(time.time()),
                                               "/bin/bash", SHARE + "/uninstall.sh",
                                               "--yes", "--purge", "--reboot"]))
        time.sleep(120)


    def handle(self, action):
        st = self.st
        kind = action[0]
        self.dirty = True
        if kind == "HOVER":
            i = self.hit(action[1])
            if i is not None:
                if st.screen == "menu":
                    st.sel = i
                elif st.screen == "wifi":
                    st.wsel = i
                elif st.screen == "confirm":
                    st.confirm["sel"] = i
                elif st.screen == "text":
                    st.kb_r, st.kb_c = i
            return
        if kind == "CLICK":
            i = self.hit(action[1])
            if st.screen == "menu" and i is not None:
                st.sel = i
                kind = "OK"
            elif st.screen == "wifi" and i is not None:
                st.wsel = i
                kind = "OK"
            elif st.screen == "confirm" and i is not None:
                st.confirm["sel"] = i
                kind = "OK"
            elif st.screen == "text" and i is not None:
                st.kb_r, st.kb_c = i
                self.kb_press()
                return
            elif st.screen in ("home", "msg", "info"):
                kind = "OK"
            else:
                return
        if st.screen == "text":
            self.handle_text(action)
            return
        if kind == "TEXT":
            return
        if kind == "SUBMIT":
            kind = "OK"

        if st.screen == "home":
            self.open_menu()
        elif st.screen == "menu":
            n = len(st.items)
            key = st.items[st.sel][0]
            if kind == "UP":
                st.sel = (st.sel - 1) % n
            elif kind == "DOWN":
                st.sel = (st.sel + 1) % n
            elif kind in ("LEFT", "RIGHT") and key in ("mode", "scaler", "output", "player"):
                self.cycle(key, -1 if kind == "LEFT" else 1)
            elif kind == "OK":
                self.activate(key)
            elif kind == "BACK":
                st.screen = "home"
                self.retry_at = 0
        elif st.screen == "wifi":
            n = len(st.wifi_items)
            if kind == "UP":
                st.wsel = (st.wsel - 1) % n
            elif kind == "DOWN":
                st.wsel = (st.wsel + 1) % n
            elif kind == "OK":
                self.wifi_activate()
            elif kind == "BACK":
                self.go_back("menu-wifi")
        elif st.screen == "confirm":
            c = st.confirm
            back = c.get("back", "menu")
            if kind in ("LEFT", "RIGHT", "UP", "DOWN"):
                c["sel"] = 1 - c["sel"]
            elif kind == "OK" and c["sel"] == 1:
                st.screen = "wifi" if back == "wifi" else "menu"
                c["yes"]()
            elif kind in ("OK", "BACK"):
                if back == "wifi":
                    self.go_back("wifi")
                else:
                    st.screen = "menu"
                    build_menu(st)
        elif st.screen == "msg":
            if kind in ("OK", "BACK"):
                self.go_back((st.msg or {}).get("back", "menu"))
        elif st.screen == "info":
            if kind in ("OK", "BACK"):
                self.open_menu()
        elif st.screen == "pair":
            if kind in ("BACK", "OK"):
                if st.pairing:
                    st.pairing.stop()
                st.pairing = None
                self.open_menu()

    def hit(self, pos):
        x, y = pos
        for hx, hy, hw, hh, i in self.st.hits:
            if hx <= x < hx + hw and hy <= y < hy + hh:
                return i
        return None


    def redraw(self, force=False):
        if not self.screen.is_open():
            return
        if force or self.dirty or time.time() - self.last_draw > 0.5:
            draw(self.screen.gfx, self.st)
            self.screen.present()
            self.dirty = False
            self.last_draw = time.time()

    def tick(self):
        st = self.st
        p = st.pairing
        if st.screen == "pair" and p and not p.result:
            if int(time.time()) % 2 == 0:
                p.poll()
            if p.result or p.left() == 0:
                p.stop()
                if not p.result:
                    self.message("nothing paired", "the vita didn't connect in time.",
                                 "try again, and make sure bluetooth is on in the vita's settings.")

    def run(self):
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        cpu_performance()
        try:
            apply_output_mode(load_config())
        except OSError as e:
            log("couldn't update cmdline.txt: %s" % e)
        log("VitaDock++ %s starting" % self.st.ver)
        st = self.st
        while True:
            st.cfg = load_config()
            st.disp = find_display(st.cfg)
            if not st.disp:
                self.screen.close()
                self.note("waiting for a tv on hdmi")
                time.sleep(3)
                continue
            st.out_mode = pick_output_mode(st.cfg, st.disp[2])

            now = time.time()
            if now - self.last_vita_check >= 1:
                self.last_vita_check = now
                vita = find_vita()
                if vita != st.vita:
                    st.vita = vita
                    st.pixfmt = pick_pixfmt(st.cfg, vita) if vita else ""
                    self.dirty = True
                    if st.screen == "menu":
                        build_menu(st)
                    if not vita:
                        st.error = ""
                        self.fails = 0

            if st.vita and st.screen == "home" and now >= self.retry_at:
                self.play()
                continue

            if not self.screen.open(st.disp):
                time.sleep(3)
                continue
            self.note("menu up on %s, vita %s" % (st.disp[1], "connected" if st.vita else "not connected"))
            self.screen.input.set_text_mode(st.screen == "text")
            for action in self.screen.input.actions():
                self.handle(action)
            self.tick()
            self.redraw()
            time.sleep(1 / 30.0)


def cmd_status():
    cfg = load_config()
    disp = find_display(cfg)
    vita = find_vita()
    print("VitaDock++ " + version())
    print("tv     %s" % ("%s %s (%s)" % (disp[0], disp[1], pick_output_mode(cfg, disp[2])) if disp else "none"))
    print("vita   %s" % ("%s (%s)" % (vita, pick_pixfmt(cfg, vita) or "default") if vita else "not plugged in"))
    print("mode   %s (%s @ %s)" % (mode_name(cfg), cfg["VITA_MODE"], cfg["VITA_FPS"]))
    print("audio  %s" % ("bluetooth" if cfg["BT_AUDIO"] == "yes" else "off"))
    print("player %s" % ("fast (gstreamer)" if cfg["PLAYER"] == "fast" and gst_available() else "compatible (mpv)"))


def cmd_screenshot(outdir):
    os.makedirs(outdir, exist_ok=True)
    st = State()
    st.cfg = dict(DEFAULTS)
    st.disp = ("/dev/dri/card1", "HDMI-A-1", "")
    st.out_mode = "1920x1080@60"
    st.ips = ["192.168.1.42"]

    def shot(name, w=1920, h=1080):
        g = PilGfx(w, h)
        draw(g, st)
        g.img.save(os.path.join(outdir, name + ".png"))
        print("wrote " + name)

    shot("home")
    st.vita, st.pixfmt = "/dev/video0", "nv12"
    build_menu(st)
    st.screen, st.sel = "menu", 1
    shot("menu")
    st.screen = "info"
    shot("info")
    st.screen = "confirm"
    st.confirm = {"lines": ["uninstall vitadock++?", "", "this removes it and its settings, then restarts the pi",
                            "into plain pi os. you'd need to reinstall to get it back."], "yes": None, "sel": 0}
    shot("confirm")
    st.screen = "pair"
    st.pairing = Pairing()
    st.pairing.started = time.time() - 42
    shot("pair")
    st.wifi_nets = [
        {"ssid": "BT-Hub6-7G2K", "signal": 82, "secure": True, "active": True, "known": True},
        {"ssid": "SKY4F1A2", "signal": 64, "secure": True, "active": False, "known": False},
        {"ssid": "Fred's iPhone", "signal": 41, "secure": True, "active": False, "known": True},
        {"ssid": "TP-Link_Guest", "signal": 22, "secure": False, "active": False, "known": False},
    ]
    build_wifi(st)
    st.screen, st.wsel = "wifi", 1
    shot("wifi")
    st.screen, st.text_target, st.text, st.kb_r, st.kb_c = "text", "SKY4F1A2", "hunter2", 2, 3
    st.text_error = "wi-fi passwords are at least 8 characters."
    shot("wifi-password")
    st.text_error = ""
    st.vita = None
    st.screen = "home"
    st.error = "vita found but no picture (try 2). trying again..."
    shot("home-error")
    st.error = ""
    shot("home-720p", 1280, 720)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "run":
        Dock().run()
    elif cmd == "status":
        cmd_status()
    elif cmd == "set" and len(argv) == 4:
        if argv[2] not in DEFAULTS:
            sys.exit("unknown setting %s. known: %s" % (argv[2], " ".join(DEFAULTS)))
        set_config(argv[2], argv[3])
        print("set %s=%s (takes effect next time the vita connects)" % (argv[2], argv[3]))
    elif cmd == "bluetooth" and len(argv) == 3 and argv[2] in ("on", "off"):
        (bt_enable if argv[2] == "on" else bt_disable)()
        print("bluetooth audio " + argv[2])
    elif cmd == "apply-output":
        print("changed" if apply_output_mode(load_config()) else "already set")
    elif cmd == "screenshot" and len(argv) == 3:
        cmd_screenshot(argv[2])
    elif cmd in ("version", "--version"):
        print(version())
    else:
        print(__doc__ or "", end="")
        print("usage: vitadock run | status | set KEY VALUE | bluetooth on|off | screenshot DIR | version")


if __name__ == "__main__":
    main(sys.argv)
