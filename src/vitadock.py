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
SYS_SOUND = os.environ.get("VITADOCK_SYS_SOUND", "/sys/class/sound")
PROC_ASOUND = os.environ.get("VITADOCK_PROC_ASOUND", "/proc/asound")
DEV_GLOB = os.environ.get("VITADOCK_DEV_GLOB", "/dev/video*")
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
UPDATE_DIR = os.environ.get("VITADOCK_UPDATE_DIR", "/run/vitadock")
VT_ACTIVE = os.environ.get("VITADOCK_VT_ACTIVE", "/sys/class/tty/tty0/active")
UPDATE_PROGRESS = UPDATE_DIR + "/update-progress"
UPDATE_LOG = os.environ.get("VITADOCK_UPDATE_LOG", "/var/log/vitadock-update.log")
UPDATED_MARKER = os.environ.get("VITADOCK_UPDATED_MARKER", "/var/lib/vitadock/just-updated")
UPDATE_UNIT = "vitadock-update.service"
SOUND_DIR = os.environ.get("VITADOCK_SOUND_DIR", "/var/lib/vitadock/sounds")
FB_SYS = os.environ.get("VITADOCK_FB_SYS", "/sys/class/graphics/fb0")
FB_DEV = os.environ.get("VITADOCK_FB_DEV", "/dev/fb0")

DEFAULTS = {
    "VITA_MODE": "960x544",
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
    "USB_AUDIO": "auto",
    "SHARPNESS": "pixel",
    "COLOR_FIX": "on",
    "DOCK_SOUNDS": "on",
    "USB_BOOST": "off",
}

VIDEO_MODES = [
    ("native", "960x544", "60", "native, 60fps",
     "the vita's own resolution, 960x544. sharpest picture, so it's the default. at this size "
     "the plugin sometimes dips just under 60fps, you'll rarely notice."),
    ("smooth", "864x488", "60", "smaller, 60fps",
     "864x488. a bit less to send down the usb cable, but the vita has to shrink its picture "
     "to make it, so it's softer. only worth trying if native stutters."),
    ("native30", "960x544", "30", "native, 30fps",
     "960x544 at a steady 30fps. for 30fps games if 60 gives you trouble."),
    ("hd", "1280x720", "30", "720p (sharpscale)",
     "real 720p from games that support it. needs the sharpscale plugin and a 720p patch for "
     "that game on the vita. 30fps only."),
]
PLAYERS = [
    ("superfast", "super fast",
     "the lowest delay i can get out of the pi. no buffering at all, realtime priority, usb and "
     "video kept on one cpu core, and 120hz output if your tv can do it (restart once after picking it)."),
    ("fast", "fast",
     "low delay. the pi 5's display hardware does the scaling, and old frames get dropped "
     "instead of queued. good for nearly everyone."),
    ("compatible", "compatible",
     "uses mpv. more delay, but it's the fallback if the other two don't work, and it lets "
     "you pick a smoothing filter."),
]
SHARPNESS = [
    ("pixel", "pixel perfect",
     "scales by a whole number (2x on a 1080p tv, 4x on 4k) with no smoothing, so every vita "
     "pixel stays sharp. trims 2 lines off the top and bottom to fit, which you won't see."),
    ("smooth", "smooth",
     "fills the screen and smooths the edges. softer, a bit like the original vitadock. "
     "use this if pixel perfect leaves a big border on your tv."),
]
OUTPUTS = [
    ("1920x1080@60", "1080p",
     "the best match for the vita. native mode scales up exactly 2x. (super fast bumps this to "
     "120hz when the tv supports it.)"),
    ("3840x2160@60", "4k",
     "native mode scales exactly 4x. only if your tv is 4k, and some tvs add delay at 4k."),
    ("1280x720@60", "720p",
     "the tv does the rest of the scaling. usually softer, only use it if the others don't work."),
    ("preferred", "tv's choice",
     "whatever the tv says it prefers."),
]
SCALERS = [
    ("lanczos", "lanczos", "sharpest of the three."),
    ("spline36", "spline36", "a touch softer, fewer halos round edges."),
    ("bilinear", "bilinear", "softest, least work for the pi."),
]
ON_OFF = [("on", "on", ""), ("off", "off", "")]
USB_AUDIO_OPTS = [
    ("auto", "on",
     "plays the vita's sound out of the tv when the plugin sends it over usb (vitastream, "
     "VitaUSBStream). does nothing with udcd_uvc."),
    ("off", "off", "ignore usb sound, for when you're using a cable or bluetooth instead."),
]
SOUND_OPTS = [
    ("on", "on", "the little chime when the dock starts and the blip when the vita connects."),
    ("off", "off", "no dock sounds."),
]
BOOST_OPTS = [
    ("off", "normal",
     "the pi 5 gives its usb ports 600mA between them, unless it sees the official 5A power "
     "supply. enough to keep the vita going, but it charges slowly or not at all while you play."),
    ("on", "boosted",
     "lets the usb ports give up to 1.6A. only turn this on with the official raspberry pi 27W "
     "(5A) supply, or the pi can brown out. needs a restart."),
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
    for name, size, fps, _, _ in VIDEO_MODES:
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


def usb_device_of(sysfs_node):
    real = os.path.realpath(sysfs_node)
    if "/usb" not in real:
        return None
    return os.path.dirname(real) if ":" in os.path.basename(real) else real


def vita_audio_card(vita):
    if not vita:
        return None
    usb = usb_device_of(os.path.join(SYS_V4L, os.path.basename(vita), "device"))
    if not usb:
        return None
    for card in sorted(glob.glob(SYS_SOUND + "/card*")):
        name = os.path.basename(card)
        if not re.match(r"^card\d+$", name):
            continue
        if usb_device_of(os.path.join(card, "device")) != usb:
            continue
        n = name[4:]
        if not glob.glob("%s/card%s/pcm*c" % (PROC_ASOUND, n)):
            continue
        try:
            with open("%s/card%s/id" % (PROC_ASOUND, n)) as f:
                cid = f.read().strip()
        except OSError:
            cid = ""
        return "plughw:CARD=%s,DEV=0" % cid if cid else "plughw:%s,0" % n
    return None


AUDIO_OUT_FILE = UPDATE_DIR + "/audio-out"


def playback_cards():
    out = []
    for d in glob.glob(PROC_ASOUND + "/card*"):
        name = os.path.basename(d)
        if not re.match(r"^card\d+$", name) or not glob.glob(d + "/pcm*p"):
            continue
        try:
            with open(d + "/id") as f:
                cid = f.read().strip()
        except OSError:
            continue
        if cid:
            out.append((int(name[4:]), cid))
    return sorted(out)


def pcm_candidates(cfg, disp):
    cands = []
    manual = cfg.get("AUDIO_PCM", "auto")
    if manual not in ("", "auto"):
        cands.append(manual)
    m = re.match(r"^HDMI-A-(\d+)$", disp[1] if disp else "")
    want = "vc4hdmi%d" % (int(m.group(1)) - 1) if m else ""
    ids = [cid for _, cid in playback_cards()]
    if want and want not in ids:
        ids.insert(0, want)
    ids.sort(key=lambda c: (c != want, not c.startswith("vc4hdmi")))
    for cid in ids:
        if cid.startswith("vc4hdmi"):
            cands += ["hdmi:CARD=%s,DEV=0" % cid, "default:CARD=%s" % cid]
        else:
            cands += ["plughw:CARD=%s,DEV=0" % cid, "default:CARD=%s" % cid]
    cands.append("default")
    seen = set()
    return [c for c in cands if not (c in seen or seen.add(c))]


def pcm_label(pcm):
    m = re.search(r"CARD=vc4hdmi(\d+)", pcm)
    if m:
        port = "hdmi %d" % (int(m.group(1)) + 1)
        return port + (" (direct)" if pcm.startswith("hdmi:") else " (default)")
    if pcm == "default":
        return "system default"
    m = re.search(r"CARD=([^,]+)", pcm)
    return (m.group(1) if m else pcm)[:22]


def _short_err(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    err = lines[-1]
    err = re.sub(r"^(aplay|alsaloop|ALSA lib)[^:]*:\s*", "", err)
    err = re.sub(r"^\S+\.c:\d+:\(\S+\)\s*", "", err)
    err = re.sub(r"^\w+:\d+:\s*", "", err)
    return err[:90]


def probe_pcm(pcm, sound="silence", timeout=4):
    path = sound_path(sound)
    if not path:
        return False, "couldn't make the test sound"
    try:
        r = subprocess.run(["aplay", "-q", "-D", pcm, path], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout, universal_newlines=True, errors="replace")
    except subprocess.TimeoutExpired:
        return False, "it hung (the output may be busy)"
    except OSError as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    return False, _short_err(r.stderr) or "aplay failed (exit %d)" % r.returncode


_AUDIO_OUT = {}


def forget_audio_out():
    _AUDIO_OUT.clear()


def audio_out(cfg, disp, refresh=False):
    key = (cfg.get("AUDIO_PCM", "auto"), disp[1] if disp else "")
    if not refresh and key in _AUDIO_OUT:
        return _AUDIO_OUT[key]
    cands = pcm_candidates(cfg, disp)
    pick = cands[0]
    if shutil.which("aplay"):
        for pcm in cands:
            ok, err = probe_pcm(pcm)
            if ok:
                pick = pcm
                break
            log("sound output %s didn't work: %s" % (pcm, err))
        else:
            log("no sound output worked, trying %s anyway" % pick)
    log("sound goes to %s" % pick)
    _AUDIO_OUT[key] = pick
    try:
        os.makedirs(UPDATE_DIR, exist_ok=True)
        with open(AUDIO_OUT_FILE, "w") as f:
            f.write(pick + "\n")
    except OSError:
        pass
    return pick


def spawn_logged(cmd, tag, on_fail=None):
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, universal_newlines=True, errors="replace")

    def pump():
        for line in proc.stderr:
            line = line.strip()
            if line:
                log("%s: %s" % (tag, line))
        rc = proc.wait()
        if rc > 0:
            log("%s stopped with exit %d" % (tag, rc))
            if on_fail:
                on_fail(rc)
    threading.Thread(target=pump, daemon=True).start()
    return proc


def audio_command(capture, playback):
    return ["alsaloop", "-C", capture, "-P", playback, "-c", "2", "-r", "48000",
            "-f", "S16_LE", "-t", "30000"]


def usb_audio_wanted(cfg):
    return cfg.get("USB_AUDIO", "auto") != "off"


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
_KMS_PROPS = None


def gst_available():
    global _GST_OK
    if _GST_OK is None:
        _GST_OK = bool(shutil.which("gst-launch-1.0")) and all(
            sh(["gst-inspect-1.0", el], timeout=10).returncode == 0 for el in ("v4l2src", "kmssink"))
    return _GST_OK


def kmssink_props():
    global _KMS_PROPS
    if _KMS_PROPS is None:
        out = sh(["gst-inspect-1.0", "kmssink"], timeout=10).stdout
        _KMS_PROPS = {p for p in ("skip-vsync", "render-rectangle", "plane-properties") if p in out}
        crop = sh(["gst-inspect-1.0", "videocrop"], timeout=10).returncode == 0
        if crop:
            _KMS_PROPS.add("videocrop")
    return _KMS_PROPS


def player_name(cfg):
    p = cfg.get("PLAYER", "fast")
    return p if p in ("superfast", "fast", "compatible") else "fast"


GST_FORMATS = {"nv12": "NV12", "yuyv422": "YUY2"}


def output_size(cfg, disp):
    sysdir = disp[2] if disp else ""
    modes = []
    try:
        with open(sysdir + "/modes") as f:
            modes = f.read().split()
    except OSError:
        pass
    m = re.match(r"^(\d+)x(\d+)", cfg.get("OUTPUT_MODE", ""))
    if m and (not modes or "%sx%s" % m.groups() in modes):
        return int(m.group(1)), int(m.group(2))
    if modes:
        w, h = modes[0].split("x")
        return int(w), int(h)
    return 1920, 1080


def scaling_plan(src_w, src_h, out_w, out_h, sharp):
    if sharp:
        for k in range(8, 0, -1):
            dw = src_w * k
            if dw > out_w:
                continue
            crop = 0
            over = src_h * k - out_h
            if over > 0:
                rows = -(-over // k)
                crop = -(-rows // 2)
                crop += crop % 2
                if crop * 2 > src_h * 0.03:
                    continue
            h = (src_h - 2 * crop) * k
            if dw < out_w * 0.85:
                break
            return {"nearest": True, "crop": crop, "k": k,
                    "rect": ((out_w - dw) // 2, (out_h - h) // 2, dw, h)}
    return {"nearest": False, "crop": 0, "k": 0, "rect": None}


def gst_command(cfg, vita, pixfmt, plan=None, superfast=False):
    props = kmssink_props()
    w, h = cfg["VITA_MODE"].split("x")
    caps = "video/x-raw,width=%s,height=%s,framerate=%s/1" % (w, h, cfg["VITA_FPS"])
    if GST_FORMATS.get(pixfmt):
        caps += ",format=" + GST_FORMATS[pixfmt]
    cmd = ["gst-launch-1.0", "-q",
           "v4l2src", "device=" + vita, "io-mode=mmap", "do-timestamp=false", "!", caps]
    plan = plan or {"nearest": False, "crop": 0, "rect": None}
    if plan["crop"] and "videocrop" in props:
        cmd += ["!", "videocrop", "top=%d" % plan["crop"], "bottom=%d" % plan["crop"]]
    if not superfast:
        cmd += ["!", "queue", "max-size-buffers=1", "max-size-bytes=0", "max-size-time=0", "leaky=downstream"]
    sink = ["kmssink", "sync=false", "can-scale=true"]
    if "skip-vsync" in props:
        sink.append("skip-vsync=true")
    if plan["rect"] and "render-rectangle" in props and (plan["crop"] == 0 or "videocrop" in props):
        sink.append("render-rectangle=<%d,%d,%d,%d>" % plan["rect"])
    plane = []
    if plan["nearest"]:
        plane.append("SCALING_FILTER=1")
    if cfg.get("COLOR_FIX", "on") != "off":
        plane += ["COLOR_ENCODING=0", "COLOR_RANGE=1"]
    if plane and "plane-properties" in props:
        sink.append("plane-properties=s," + ",".join(plane))
    cmd += ["!"] + sink
    if superfast:
        pre = []
        if shutil.which("chrt"):
            pre += ["chrt", "-f", "50"]
        if shutil.which("taskset"):
            pre += ["taskset", "-c", str(SUPERFAST_CPU)]
        cmd = pre + cmd
    return cmd


SUPERFAST_CPU = 3


def superfast_prepare():
    try:
        with open("/proc/interrupts") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        if "xhci" not in line and "dwc" not in line:
            continue
        irq = line.split(":", 1)[0].strip()
        if irq.isdigit():
            try:
                with open("/proc/irq/%s/smp_affinity_list" % irq, "w") as f:
                    f.write(str(SUPERFAST_CPU))
            except OSError:
                pass


def mpv_command(cfg, disp, out_mode, vita, pixfmt):
    dev, conn, _ = disp
    cmd = [
        "mpv", "--no-config",
        "--vo=gpu", "--gpu-context=drm",
        "--drm-device=" + dev, "--drm-connector=" + conn, "--drm-mode=" + out_mode,
        "--profile=low-latency", "--untimed", "--cache=no", "--no-audio", "--hwdec=no",
        "--demuxer-thread=no", "--swapchain-depth=1",
        "--scale=" + (cfg["SCALER"] if cfg["SCALER"] in [s[0] for s in SCALERS] else "lanczos"), "--keepaspect=yes",
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
        if player_name(cfg) == "superfast" and mode.startswith("1920x1080@"):
            hz = best_1080p_refresh(find_display(cfg))
            if hz > 60:
                mode = "1920x1080@%d" % hz
        keep += ["video=HDMI-A-1:" + mode, "video=HDMI-A-2:" + mode]
    keep.append("usbcore.autosuspend=-1")
    if keep == tokens:
        return False
    with open(path + ".tmp", "w") as f:
        f.write(" ".join(keep) + "\n")
    os.replace(path + ".tmp", path)
    return True


def edid_vics(disp):
    try:
        with open(disp[2] + "/edid", "rb") as f:
            edid = f.read()
    except (OSError, TypeError, IndexError):
        return set()
    vics = set()
    for b in range(128, len(edid) - 127, 128):
        block = edid[b:b + 128]
        if block[0] != 0x02:
            continue
        end = block[2] if 4 <= block[2] <= 127 else 4
        i = 4
        while i < end:
            tag, length = block[i] >> 5, block[i] & 0x1F
            if tag == 2:
                for v in block[i + 1:i + 1 + length]:
                    vics.add(v & 0x7F if 1 <= (v & 0x7F) <= 64 else v)
            i += 1 + length
    return vics


def best_1080p_refresh(disp):
    vics = edid_vics(disp) if disp else set()
    return 120 if 63 in vics else 100 if 64 in vics else 60


def boot_config_path():
    path = boot_cmdline_path()
    cfg_path = os.path.join(os.path.dirname(path), "config.txt") if path else None
    return cfg_path if cfg_path and os.path.exists(cfg_path) else None


BOOST_MARK = "# VitaDock++ usb power boost"


def set_usb_boost(on):
    path = boot_config_path()
    if not path:
        raise RuntimeError("couldn't find config.txt")
    with open(path) as f:
        lines = f.read().splitlines()
    out, skip = [], False
    for line in lines:
        if line.strip() == BOOST_MARK:
            skip = True
            continue
        if skip and line.strip() in ("[all]", "usb_max_current_enable=1"):
            continue
        skip = False
        out.append(line)
    if on:
        out += [BOOST_MARK, "[all]", "usb_max_current_enable=1"]
    if out == lines:
        return False
    with open(path + ".tmp", "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(path + ".tmp", path)
    return True


def psu_max_current():
    try:
        with open("/proc/device-tree/chosen/power/max_current", "rb") as f:
            raw = f.read()
        return int.from_bytes(raw[:4], "big") if len(raw) >= 4 else 0
    except OSError:
        return 0


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
        self.started = time.monotonic()

    def left(self):
        return max(0, int(self.SECONDS - (time.monotonic() - self.started)))

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


def _write_tone(path, notes, rate=48000):
    import math
    import struct
    import wave
    data = bytearray()
    for freq, dur, vol in notes:
        n = int(rate * dur)
        for i in range(n):
            t = i / rate
            env = min(1.0, i / (rate * 0.004)) * math.exp(-4.0 * t / dur)
            if freq:
                ph = (t * freq) % 1.0
                sq = 1.0 if ph < 0.5 else -1.0
                tri = 4 * abs(ph - 0.5) - 1
                v = (0.45 * sq + 0.55 * tri) * env * vol
            else:
                v = 0.0
            iv = int(max(-1.0, min(1.0, v)) * 32767 * 0.5)
            data += struct.pack("<hh", iv, iv)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(data))


SOUNDS = {
    "boot": [(659.3, 0.09, 0.8), (830.6, 0.09, 0.8), (987.8, 0.09, 0.8), (1318.5, 0.42, 0.9)],
    "connect": [(987.8, 0.06, 0.8), (1318.5, 0.16, 0.9)],
    "test": [(523.3, 0.14, 0.8), (659.3, 0.14, 0.8), (784.0, 0.14, 0.8), (1046.5, 0.3, 0.9),
             (0, 0.12, 0), (784.0, 0.14, 0.8), (1046.5, 0.5, 0.9)],
    "silence": [(0, 0.08, 0)],
}


def sound_path(name):
    path = os.path.join(SOUND_DIR, name + ".wav")
    if not os.path.exists(path):
        try:
            os.makedirs(SOUND_DIR, exist_ok=True)
            _write_tone(path + ".tmp", SOUNDS[name])
            os.replace(path + ".tmp", path)
        except (OSError, KeyError) as e:
            log("couldn't make the %s sound: %s" % (name, e))
            return None
    return path


def play_sound(name, cfg, disp, wait=False):
    if cfg.get("DOCK_SOUNDS", "on") == "off" or not shutil.which("aplay"):
        return
    path = sound_path(name)
    if not path:
        return
    try:
        proc = spawn_logged(["aplay", "-q", "-D", audio_out(cfg, disp), path], "aplay",
                            on_fail=lambda rc: forget_audio_out())
        if wait:
            proc.wait(3)
    except (OSError, subprocess.TimeoutExpired):
        pass


class FbSplash:

    def __init__(self):
        self.thread = None
        self.stop_ev = threading.Event()
        self.cache = {}

    def _fb_info(self):
        def rd(name):
            with open(os.path.join(FB_SYS, name)) as f:
                return f.read().strip()
        w, h = (int(x) for x in rd("virtual_size").split(","))
        bpp = int(rd("bits_per_pixel"))
        try:
            stride = int(rd("stride"))
        except (OSError, ValueError):
            stride = w * bpp // 8
        return w, h, bpp, stride

    def _to_fb(self, img, w, bpp, stride):
        raw = img.tobytes("raw", "BGRX") if bpp == 32 else img.tobytes("raw", "BGR;16")
        row = w * bpp // 8
        if stride == row:
            return raw
        pad = b"\0" * (stride - row)
        return b"".join(raw[i:i + row] + pad for i in range(0, len(raw), row))

    def frames(self, st, w, h, bpp, stride, n=4):
        key = (w, h, bpp, stride, st.ver)
        if key not in self.cache:
            out = []
            for i in range(n):
                g = PilGfx(w, h)
                draw_connecting(g, st, i)
                out.append(self._to_fb(g.img, w, bpp, stride))
            g = PilGfx(w, h)
            g.fill((0, 0, 0))
            out.append(self._to_fb(g.img, w, bpp, stride))
            self.cache[key] = out
        return self.cache[key]

    def start(self, st, seconds=4.0):
        self.stop()
        try:
            from PIL import Image
            w, h, bpp, stride = self._fb_info()
            if bpp not in (16, 32):
                return
            fb = os.open(FB_DEV, os.O_WRONLY)
        except (ImportError, OSError, ValueError) as e:
            log("no framebuffer splash: %s" % e)
            return
        self.stop_ev.clear()

        def loop():
            try:
                frames = self.frames(st, w, h, bpp, stride)
                end = time.monotonic() + seconds
                i = 0
                while not self.stop_ev.is_set() and time.monotonic() < end:
                    os.pwrite(fb, frames[i % (len(frames) - 1)], 0)
                    i += 1
                    self.stop_ev.wait(0.18)
                os.pwrite(fb, frames[-1], 0)
            except OSError as e:
                log("framebuffer splash stopped: %s" % e)
            finally:
                os.close(fb)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def stop(self):
        if self.thread:
            self.stop_ev.set()
            self.thread.join(2)
            self.thread = None


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


def draw_handheld_partial(g, x, y, px, color, frac):
    rows = len(HANDHELD)
    upto = frac * rows
    for r, row in enumerate(HANDHELD):
        if r >= upto:
            break
        for c, ch in enumerate(row):
            if ch == "#":
                g.rect(x + c * px, y + r * px, px, px, color)


def draw_intro(g, st, t):
    u = g.u
    g.fill(BG)
    px = max(3, int(12 * u))
    iw = len(HANDHELD[0]) * px
    ih = len(HANDHELD) * px
    x = (g.w - iw) / 2
    y = g.h / 2 - ih - 20 * u
    draw_handheld_partial(g, x, y, px, ACCENT, min(1.0, t / 0.55))
    title = "VitaDock++"
    shown = title[:max(0, int((t - 0.45) / 0.4 * len(title) + 0.999))] if t > 0.45 else ""
    if shown:
        g.text(g.w / 2, y + ih + 40 * u, shown, "big", FG, "c")
    if t > 0.85:
        g.text(g.w / 2, y + ih + 100 * u, st.ver, "small", DIM, "c")


def draw_connecting(g, st, frame):
    u = g.u
    g.fill(BG)
    px = max(3, int(9 * u))
    iw = len(HANDHELD[0]) * px
    ih = len(HANDHELD) * px
    x = (g.w - iw) / 2
    y = g.h / 2 - ih
    draw_handheld(g, x, y, px, ACCENT)
    cx = x + 11.5 * px
    for i in range(4):
        on = i == frame % 4
        g.rect(cx - px / 2, y + ih + (i * 1.6 + 0.6) * px, px * 2, px, ACCENT if on else FAINT)
    g.text(g.w / 2, y + ih + 9 * px, "getting the picture" + "." * (frame % 4), "body", DIM, "c")


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
        self.vita_audio = None
        self.error = ""
        self.notice = ""
        self.notice_until = 0
        self.upd = None
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
        self.menu = "main"
        self.menu_stack = []
        self.menu_note = ""
        self.choice = None
        self.scroll = 0


MENU_TITLES = {"main": "settings", "picture": "picture", "sound": "sound",
               "power": "power and charging", "system": "system"}


def option_label(options, value, fallback="custom"):
    for v, label, _ in options:
        if v == value:
            return label
    return fallback


def choice_def(st, key):
    cfg = st.cfg
    if key == "mode":
        return ("video from the vita", [(n, l, d) for n, _, _, l, d in VIDEO_MODES], mode_name(cfg))
    if key == "sharp":
        return ("sharpness", SHARPNESS, cfg.get("SHARPNESS", "pixel"))
    if key == "player":
        return ("speed", PLAYERS, player_name(cfg))
    if key == "output":
        return ("tv resolution", OUTPUTS, cfg["OUTPUT_MODE"])
    if key == "scaler":
        return ("smoothing filter", SCALERS, cfg["SCALER"])
    if key == "usbaudio":
        return ("sound over usb", USB_AUDIO_OPTS, "off" if cfg.get("USB_AUDIO") == "off" else "auto")
    if key == "sounds":
        return ("dock sounds", SOUND_OPTS, cfg.get("DOCK_SOUNDS", "on"))
    if key == "boost":
        return ("usb power for the vita", BOOST_OPTS, cfg.get("USB_BOOST", "off"))
    raise KeyError(key)


def choice_value_label(st, key):
    title, options, cur = choice_def(st, key)
    if key == "mode":
        return option_label(options, cur, "%s @ %s" % (st.cfg["VITA_MODE"], st.cfg["VITA_FPS"]))
    return option_label(options, cur, cur)


def build_menu(st):
    cfg = st.cfg
    m = st.menu
    c = lambda key, label: ("choice:" + key, label, choice_value_label(st, key))
    if m == "picture":
        items = [c("mode", "video from the vita")]
        if player_name(cfg) == "compatible":
            items.append(c("scaler", "smoothing filter"))
        else:
            items.append(c("sharp", "sharpness"))
        items += [c("player", "speed"), c("output", "tv resolution")]
    elif m == "sound":
        items = [c("usbaudio", "sound over usb"),
                 ("bt", "bluetooth audio", "on" if cfg["BT_AUDIO"] == "yes" else "off")]
        if cfg["BT_AUDIO"] == "yes":
            items.append(("pair", "pair the vita over bluetooth", ""))
        items.append(c("sounds", "dock sounds"))
        items.append(("soundtest", "test the sound", ""))
    elif m == "power":
        items = [c("boost", "usb power for the vita"),
                 ("chargehelp", "charging while you play", ""),
                 ("reboot", "restart the pi", ""),
                 ("shutdown", "shut down the pi", "")]
    elif m == "system":
        items = [("info", "what's plugged in", ""),
                 ("update", "check for updates", ""),
                 ("restartdock", "restart the dock", ""),
                 ("uninstall", "uninstall vitadock++", "")]
    else:
        st.menu = "main"
        pic = "%s, %s" % (option_label(SHARPNESS, cfg.get("SHARPNESS", "pixel"), "")
                          if player_name(cfg) != "compatible" else "mpv",
                          option_label(PLAYERS, player_name(cfg), ""))
        items = [("back", "back to game" if st.vita else "close menu", ""),
                 ("sub:picture", "picture", pic),
                 ("sub:sound", "sound", audio_desc(st)),
                 ("wifi", "wi-fi", st.wifi_label or "..."),
                 ("sub:power", "power and charging", ""),
                 ("sub:system", "system", st.ver),
                 ("help", "help", "")]
    st.items = items
    st.sel = max(0, min(st.sel, len(items) - 1))


def item_help(st, key):
    cfg = st.cfg
    if key.startswith("choice:"):
        title, options, cur = choice_def(st, key[7:])
        for v, _, desc in options:
            if v == cur:
                extra = ""
                if key == "choice:output" and player_name(cfg) == "superfast":
                    hz = best_1080p_refresh(st.disp)
                    extra = (" your tv can do %dhz, so super fast uses that." % hz) if hz > 60 else \
                            " your tv doesn't list 1080p above 60hz."
                return desc + extra
        return "set by hand in vitadock.conf."
    return {
        "back": "back to the game, right where you left it." if st.vita else "back to the waiting screen.",
        "sub:picture": "how sharp and how quick the picture is: resolution, sharpness, speed and tv mode.",
        "sub:sound": "where the vita's sound comes from, bluetooth pairing, and the dock's own sounds.",
        "wifi": "see which network the pi is on, or join another one.",
        "sub:power": "charging the vita while it's docked, and restarting or shutting down the pi.",
        "sub:system": "what's connected, updates, and uninstalling.",
        "help": "the controls, and which vita plugin does what.",
        "bt": "the vita connects to the dock like a bluetooth speaker and the sound comes out of the tv. "
              "for udcd_uvc, which only sends the picture. still a bit experimental.",
        "pair": "do this once. after that, just connect from the vita's bluetooth menu.",
        "chargehelp": "what you need to do on the vita so it charges while you play.",
        "reboot": "turns the pi off and on again.",
        "shutdown": "wait for the green light to stop flashing before you pull the power.",
        "soundtest": "no sound? this plays a tune on each of the pi's sound outputs so you can find "
                     "the one your tv hears, and shows what went wrong if one doesn't work.",
        "info": "the tv mode, the vita's video and sound devices, the player in use and the pi's ip address.",
        "update": "checks github for a new version. the pi needs to be online. your settings are kept.",
        "restartdock": "restarts just the dock software, handy if something's got stuck.",
        "uninstall": "removes vitadock++ and its settings, then restarts into plain pi os.",
    }.get(key, "")


def wrap_text(g, text, font, width):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if g.measure(trial, font) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


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


def picture_desc(st):
    if player_name(st.cfg) == "compatible" or not gst_available() or not st.disp:
        return "mpv, " + st.cfg["SCALER"]
    sw, sh_ = (int(v) for v in st.cfg["VITA_MODE"].split("x"))
    ow, oh = output_size(st.cfg, st.disp)
    plan = scaling_plan(sw, sh_, ow, oh, st.cfg.get("SHARPNESS", "pixel") == "pixel")
    return ("pixel perfect %dx on %dx%d" % (plan["k"], ow, oh)) if plan["nearest"] else \
        "smooth on %dx%d" % (ow, oh)


def audio_desc(st):
    if st.vita_audio and usb_audio_wanted(st.cfg):
        return "usb, from the vita"
    if st.cfg["BT_AUDIO"] == "yes":
        return "bluetooth"
    return "cable from the vita"


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
    g.text(tx, y + lh, "the vita needs a usb video plugin (see the readme).", "body", DIM)

    y = top + 180 * u
    width = g.w - tx - 120 * u
    tv = "%s, %s" % (st.disp[1].lower(), st.out_mode) if st.disp else "none"
    dotted(g, tx, y, "tv", tv, width)
    dotted(g, tx, y + lh * 1.3, "vita", st.vita or "not plugged in", width, FG if st.vita else DIM)
    dotted(g, tx, y + lh * 2.6, "mode", "%s (%s @ %s)" % (mode_name(st.cfg), st.cfg["VITA_MODE"], st.cfg["VITA_FPS"]), width)
    dotted(g, tx, y + lh * 3.9, "audio", audio_desc(st), width)
    if st.error:
        g.text(tx, y + lh * 5.6, st.error, "small", BAD)
    elif st.notice and time.monotonic() < st.notice_until:
        g.text(tx, y + lh * 5.6, st.notice, "small", GOOD)


def draw_list(g, st, rows, sel, title, help_text, note=""):
    u, W, H = g.u, g.w, g.h
    st.hits = []
    x = 120 * u
    w = W - 240 * u
    y = 92 * u
    g.text(x, y, title, "small", DIM)
    y += 34 * u
    row = 42 * u
    pad = 14 * u
    panel_top = H - 62 * u - 112 * u
    fits = max(3, int((panel_top - 12 * u - y) / row))
    top = max(0, min(sel - fits // 2, len(rows) - fits))
    if top > 0:
        g.text(x + w, y - 34 * u, "more above", "small", DIM, "r")
    for i in range(top, min(len(rows), top + fits)):
        label, value, color, marker = rows[i]
        on = i == sel
        if on:
            g.rect(x - pad, y - 6 * u, w + 2 * pad, row - 2 * u, ACCENT)
        g.text(x, y, (marker or ("> " if on else "  ")) + label, "body", ACCENT_FG if on else color)
        if value:
            g.text(x + w, y, value, "body", ACCENT_FG if on else DIM, "r")
        st.hits.append((x - pad, y - 6 * u, w + 2 * pad, row - 2 * u, i))
        y += row
    if top + fits < len(rows):
        g.text(x, y, "  more below", "small", DIM)
    g.rect(x - pad, panel_top, w + 2 * pad, max(1, int(2 * u)), FAINT)
    ty = panel_top + 16 * u
    lh = g.line_h("small") * 1.25
    for line in wrap_text(g, help_text, "small", w)[:3]:
        g.text(x, ty, line, "small", FG)
        ty += lh
    if note:
        g.text(x, ty + 4 * u, note, "small", ACCENT)


def draw_menu(g, st):
    is_main = st.menu == "main"
    draw_chrome(g, st, "up/down move   enter open   left/right change   esc " + ("close" if is_main else "back"))
    rows = []
    for key, label, value in st.items:
        color = BAD if key == "uninstall" else FG
        if key.startswith("choice:") and st.items[st.sel][0] == key:
            value = "< %s >" % value
        elif key.startswith("sub:") or key in ("wifi", "help"):
            value = (value + "  >") if value else ">"
        rows.append((label, value, color, ""))
    title = "settings" if is_main else "settings / " + MENU_TITLES.get(st.menu, st.menu)
    key = st.items[st.sel][0] if st.items else ""
    draw_list(g, st, rows, st.sel, title, item_help(st, key), st.menu_note)


def draw_choose(g, st):
    draw_chrome(g, st, "up/down move   enter pick   esc back")
    key = st.choice["key"]
    title, options, cur = choice_def(st, key)
    rows = [(label, "current" if v == cur else "", FG, "(*) " if v == cur else "( ) ")
            for v, label, _ in options]
    sel = st.choice["sel"]
    crumb = "settings / %s / %s" % (MENU_TITLES.get(st.menu, st.menu), title)
    draw_list(g, st, rows, sel, crumb, options[sel][2] if options else "")


HELP_TEXT = [
    ("opening the menu", [
        "keyboard  enter. mid-game: esc, tab or the windows key",
        "mouse     click. mid-game: right click",
        "usb pad   A. mid-game: the PS/home button, or start + select",
    ]),
    ("getting around", [
        "up/down to move, enter or A to pick, esc or B to go back.",
        "left/right changes a setting without opening it.",
    ]),
    ("vita plugins", [
        "udcd_uvc            picture only. sound by cable or bluetooth",
        "vitastream          picture and sound over usb",
        "VitaUSBStream       picture and sound, turn it on in its app first",
    ]),
    ("charging", [
        "on the vita: settings > system > usb power supply, turn it on.",
    ]),
]


def draw_help(g, st):
    u = g.u
    draw_chrome(g, st, "enter / esc . . . back")
    x, y = 120 * u, 96 * u
    for head, lines in HELP_TEXT:
        g.text(x, y, head, "body", ACCENT)
        y += g.line_h("body") * 1.15
        for line in lines:
            g.text(x + 24 * u, y, line, "small", FG)
            y += g.line_h("small") * 1.3
        y += 14 * u


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
        ("player", option_label(PLAYERS, player_name(st.cfg)) if player_name(st.cfg) != "compatible"
         and gst_available() else "compatible (mpv)"),
        ("picture", picture_desc(st)),
        ("sound from", "usb (%s)" % st.vita_audio if st.vita_audio and usb_audio_wanted(st.cfg)
         else "the vita isn't sending usb sound" if st.vita else audio_desc(st)),
        ("sound to", pcm_label(audio_out(st.cfg, st.disp))),
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


def draw_soundtest(g, st):
    draw_chrome(g, st, "up/down move   enter play   esc back")
    t = st.stest
    rows = []
    for pcm in t["pcms"]:
        res = t["res"].get(pcm, "")
        value = "in use" if pcm == t["saved"] else ""
        if res == "ok":
            value = "played" + (", in use" if value else "")
        elif res:
            value = "didn't work"
        rows.append((pcm_label(pcm), value, BAD if res not in ("", "ok") else FG,
                     "(*) " if pcm == t["saved"] else "( ) "))
    rows.append(("let the dock pick (automatic)", "in use" if t["saved"] == "auto" else "", FG,
                 "(*) " if t["saved"] == "auto" else "( ) "))
    sel = t["sel"]
    if sel < len(t["pcms"]):
        pcm = t["pcms"][sel]
        res = t["res"].get(pcm, "")
        if res == "ok":
            help_text = ("did you hear the tune? press enter again to keep using %s. "
                         "if not, try the next one." % pcm_label(pcm))
        elif res:
            help_text = "%s: %s" % (pcm, res)
        else:
            help_text = "press enter to play a tune on %s (alsa name %s)." % (pcm_label(pcm), pcm)
    else:
        help_text = "tries the outputs itself and uses the first one that works. this is the default."
    note = "tv on mute, or the volume down? the tune plays at a normal level." if not t["res"] else ""
    draw_list(g, st, rows, sel, "settings / sound / test the sound", help_text, note)


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


def read_update_progress():
    try:
        with open(UPDATE_PROGRESS) as f:
            line = f.read().strip().splitlines()[-1]
    except (OSError, IndexError):
        return None
    kind, _, msg = line.partition("|")
    return kind.strip(), msg.strip()


def tail_update_log(n=4):
    try:
        with open(UPDATE_LOG, errors="replace") as f:
            lines = [l.rstrip() for l in f.readlines() if l.strip()]
    except OSError:
        return []
    return [l[:90] for l in lines[-n:]]


def draw_update(g, st):
    u, W = g.u, g.w
    up = st.upd or {}
    state = up.get("state", "running")
    draw_chrome(g, st, "" if state == "running" else "enter / esc . . . back")
    x, y = 120 * u, 150 * u
    bw = W - 240 * u
    title = {"running": "updating to %s" % up.get("to", "?"),
             "done": "update finished",
             "error": "update failed"}[state]
    g.text(x, y, title, "big", BAD if state == "error" else FG)
    y += 80 * u
    bh = 30 * u
    pct = max(0, min(100, up.get("pct", 0)))
    g.rect(x, y, bw, bh, FAINT)
    g.rect(x + 3 * u, y + 3 * u, bw - 6 * u, bh - 6 * u, BG)
    fill = (bw - 10 * u) * pct / 100.0
    if fill > 0:
        g.rect(x + 5 * u, y + 5 * u, fill, bh - 10 * u, BAD if state == "error" else ACCENT)
    y += bh + 16 * u
    g.text(x, y, "%d%%" % pct, "body", FG)
    secs = int(time.monotonic() - up.get("started", time.monotonic()))
    g.text(x + bw, y, "%d:%02d" % (secs // 60, secs % 60), "body", DIM, "r")
    y += g.line_h("body") * 1.5
    msg = up.get("msg", "")
    g.text(x, y, msg[:80], "body", BAD if state == "error" else DIM)
    y += g.line_h("body") * 1.8
    if state == "running":
        lines = ["the first update after a new release can take a few minutes,",
                 "it has to download new packages. the dock restarts itself at the end."]
        if up.get("slow"):
            lines = ["still going. this step can sit for a few minutes without the bar",
                     "moving, that's normal. leave it be and don't turn the pi off."]
        for line in lines:
            g.text(x, y, line, "small", ACCENT if up.get("slow") else DIM)
            y += g.line_h("small") * 1.3
    elif state == "error":
        for line in up.get("log", []):
            g.text(x, y, line, "small", DIM)
            y += g.line_h("small") * 1.3
        g.text(x, y + 10 * u, "full log: " + UPDATE_LOG, "small", DIM)
    else:
        g.text(x, y, "restart the pi if the dock doesn't come back on its own.", "small", DIM)


def draw_intro_screen(g, st):
    draw_intro(g, st, st.intro_t if hasattr(st, "intro_t") else 1.0)


def draw(g, st):
    {"home": draw_home, "menu": draw_menu, "confirm": draw_confirm, "msg": draw_message,
     "busy": draw_message, "pair": draw_pair, "info": draw_info,
     "wifi": draw_wifi, "text": draw_text, "update": draw_update,
     "choose": draw_choose, "help": draw_help, "intro": draw_intro_screen,
     "soundtest": draw_soundtest}[st.screen](g, st)


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
            if t == pg.KEYDOWN and pg.K_F1 <= e.key <= pg.K_F12 \
                    and e.mod & pg.KMOD_CTRL and e.mod & pg.KMOD_ALT:
                out.append(("VT", e.key - pg.K_F1 + 1))
            elif t == pg.TEXTINPUT:
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
            if time.monotonic() - last_scan > 2:
                last_scan = time.monotonic()
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


def active_vt():
    try:
        with open(VT_ACTIVE) as f:
            return f.read().strip()
    except OSError:
        return ""


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
        self.fb = FbSplash()
        self.played_once = False
        self.last_connect_sound = 0
        self.intro_done = False
        self.intro_skip = False

    def note(self, msg):
        if msg != self.last_note:
            log(msg)
            self.last_note = msg


    def start_audio(self):
        st = self.st
        if not usb_audio_wanted(st.cfg):
            return None
        st.vita_audio = vita_audio_card(st.vita)
        if not st.vita_audio or not shutil.which("alsaloop"):
            return None
        out = audio_out(st.cfg, st.disp)
        log("usb audio %s -> %s" % (st.vita_audio, out))
        try:
            return spawn_logged(audio_command(st.vita_audio, out), "alsaloop",
                                on_fail=lambda rc: forget_audio_out())
        except OSError as e:
            log("couldn't start usb audio: %s" % e)
            return None

    def stop_audio(self, audio):
        if audio and audio.poll() is None:
            audio.terminate()
            try:
                audio.wait(2)
            except subprocess.TimeoutExpired:
                audio.kill()

    def pick_player(self):
        p = player_name(self.st.cfg)
        if p in ("fast", "superfast") and not self.gst_broken and gst_available():
            return p
        return "mpv"

    def play(self):
        st = self.st
        player = self.pick_player()
        plan = {"nearest": False, "crop": 0, "rect": None}
        if player in ("fast", "superfast"):
            sw, sh_ = (int(v) for v in st.cfg["VITA_MODE"].split("x"))
            ow, oh = output_size(st.cfg, st.disp)
            plan = scaling_plan(sw, sh_, ow, oh, st.cfg.get("SHARPNESS", "pixel") == "pixel")
            if player == "superfast":
                superfast_prepare()
            cmd = gst_command(st.cfg, st.vita, st.pixfmt, plan, superfast=player == "superfast")
        else:
            cmd = mpv_command(st.cfg, st.disp, st.out_mode, st.vita, st.pixfmt)
        self.player_used = player
        log("playing %s with %s: %s @ %sfps (%s) -> %s %s, %s" % (
            st.vita, player, st.cfg["VITA_MODE"], st.cfg["VITA_FPS"], st.pixfmt or "default",
            st.disp[1], st.out_mode,
            "pixel perfect %dx" % plan["k"] if plan.get("nearest") else "smooth"))
        self.screen.close()
        if not self.played_once or time.monotonic() - self.last_connect_sound > 30:
            play_sound("connect", st.cfg, st.disp, wait=True)
            self.last_connect_sound = time.monotonic()
        self.played_once = True
        self.fb.start(st)
        started = time.monotonic()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        audio = self.start_audio()
        stop, hit = threading.Event(), threading.Event()
        t = threading.Thread(target=self.hotkeys.watch, args=(stop, hit), daemon=True)
        t.start()
        audio_tries, next_audio = 0, time.monotonic() + 1
        while proc.poll() is None:
            now = time.monotonic()
            if now >= next_audio and usb_audio_wanted(st.cfg) and (audio is None or audio.poll() is not None):
                audio_tries += 1
                if audio is not None:
                    log("usb audio stopped, starting it again")
                audio = self.start_audio()
                next_audio = now + (1 if audio_tries < 10 else 5)
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
        self.stop_audio(audio)
        self.fb.stop()
        ran = time.monotonic() - started
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
                    self.retry_at = time.monotonic() + 1
                else:
                    st.error = "starting the fast player again..."
                    self.retry_at = time.monotonic() + 2
            else:
                self.fails += 1
                st.error = "vita found but no picture (try %d). trying again..." % self.fails
                self.retry_at = time.monotonic() + (3 if self.fails < 3 else 10)
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
        st.menu, st.menu_stack, st.menu_note = "main", [], ""
        st.screen, st.sel = "menu", sel
        build_menu(st)

    def show_menu(self):
        st = self.st
        st.screen = "menu"
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
        elif where == "menu-wifi":
            self.open_menu(1)
            keys = [k for k, _, _ in self.st.items]
            self.st.sel = keys.index("wifi") if "wifi" in keys else 0
        else:
            self.show_menu()


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

    def soundtest_ok(self):
        st = self.st
        t = st.stest
        if t["sel"] >= len(t["pcms"]):
            set_config("AUDIO_PCM", "auto")
            t["saved"] = "auto"
        else:
            pcm = t["pcms"][t["sel"]]
            if t["res"].get(pcm) == "ok" and t["saved"] != pcm:
                set_config("AUDIO_PCM", pcm)
                t["saved"] = pcm
            else:
                st.screen, st.busy = "busy", "playing a tune on " + pcm_label(pcm)
                self.redraw(force=True)
                ok, err = probe_pcm(pcm, "test", timeout=8)
                log("sound test %s: %s" % (pcm, "played" if ok else err))
                t["res"][pcm] = "ok" if ok else (err or "didn't work")
                st.screen = "soundtest"
        st.cfg = load_config()
        forget_audio_out()

    def set_choice(self, key, value):
        st = self.st
        cfg = st.cfg
        note = ""
        if key == "mode":
            for n, size, fps, _, _ in VIDEO_MODES:
                if n == value:
                    set_config("VITA_MODE", size)
                    set_config("VITA_FPS", fps)
        elif key == "sharp":
            set_config("SHARPNESS", value)
        elif key == "scaler":
            set_config("SCALER", value)
        elif key == "usbaudio":
            set_config("USB_AUDIO", value)
        elif key == "sounds":
            set_config("DOCK_SOUNDS", value)
        elif key in ("player", "output"):
            set_config("PLAYER" if key == "player" else "OUTPUT_MODE", value)
            self.gst_broken = False
            try:
                if apply_output_mode(load_config()):
                    note = "restart the pi for the new tv mode to kick in."
            except OSError as e:
                log("couldn't update cmdline.txt: %s" % e)
        elif key == "boost":
            set_config("USB_BOOST", value)
            try:
                if set_usb_boost(value == "on"):
                    note = "restart the pi for this to kick in."
            except (OSError, RuntimeError) as e:
                note = "couldn't change config.txt: %s" % e
        st.cfg = load_config()
        st.menu_note = note
        build_menu(st)

    def cycle(self, key, step):
        title, options, cur = choice_def(self.st, key)
        vals = [v for v, _, _ in options]
        i = (vals.index(cur) + step) % len(vals) if cur in vals else 0
        self.set_choice(key, vals[i])

    def activate(self, key):
        st = self.st
        st.menu_note = ""
        if key == "back":
            st.screen = "home"
            self.retry_at = 0
        elif key.startswith("sub:"):
            st.menu_stack.append((st.menu, st.sel))
            st.menu, st.sel = key[4:], 0
            build_menu(st)
        elif key.startswith("choice:"):
            k = key[7:]
            title, options, cur = choice_def(st, k)
            vals = [v for v, _, _ in options]
            st.choice = {"key": k, "sel": vals.index(cur) if cur in vals else 0}
            st.screen = "choose"
        elif key == "bt":
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
        elif key == "soundtest":
            st.stest = {"pcms": pcm_candidates(dict(st.cfg, AUDIO_PCM="auto"), st.disp),
                        "res": {}, "sel": 0, "saved": st.cfg.get("AUDIO_PCM", "auto") or "auto"}
            if st.stest["saved"] != "auto" and st.stest["saved"] not in st.stest["pcms"]:
                st.stest["pcms"].insert(0, st.stest["saved"])
            st.screen = "soundtest"
        elif key == "help":
            st.screen = "help"
        elif key == "wifi":
            self.open_wifi(True)
        elif key == "chargehelp":
            psu = psu_max_current()
            self.message("charging while you play",
                         "1. on the vita: settings > system > usb power supply, turn it on.",
                         "2. use the screen-off version of the plugin (lcd_off or oled_off).",
                         "   the screen is the biggest drain, so more power goes to the battery.",
                         "3. a plain pc-style usb port only gives the vita about 500mA, so it",
                         "   charges slowly while you play. turning usb power boost on helps",
                         "   if you have the official 5A supply%s." % (
                             " (the pi says you do)" if psu >= 5000 else
                             " (the pi says yours is %dmA)" % psu if psu else ""))
        elif key == "update":
            self.check_update()
        elif key == "reboot":
            self.ask(["restart the pi?"], lambda: self.power("reboot"))
        elif key == "shutdown":
            self.ask(["shut down the pi?", "", "unplug it once the green light stops flashing."],
                     lambda: self.power("poweroff"))
        elif key == "restartdock":
            self.ask(["restart the dock?", "", "takes a few seconds."],
                     lambda: sh(["systemctl", "restart", "vitadock.service"]))
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
            self.show_menu()

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
                  "you'll see a progress bar. the dock restarts by itself when it's done."],
                 lambda: self.run_update(slug, latest))

    def run_update(self, slug, latest="?"):
        st = self.st
        try:
            os.makedirs(UPDATE_DIR, exist_ok=True)
            with open(UPDATE_PROGRESS, "w") as f:
                f.write("1|starting the updater\n")
        except OSError as e:
            self.message("couldn't start the update", str(e)[:70])
            return
        url = "https://raw.githubusercontent.com/%s/main/install.sh" % slug
        script = ('curl -fsSL --retry 2 "{url}" -o "{d}/install.sh" '
                  '|| {{ echo "error|couldn\'t download the installer. is the pi online?" > "{p}"; exit 1; }}; '
                  'exec /bin/bash "{d}/install.sh" --update --yes --progress="{p}"').format(
                      url=url, d=UPDATE_DIR, p=UPDATE_PROGRESS)
        sh(["systemctl", "stop", UPDATE_UNIT])
        sh(["systemctl", "reset-failed", UPDATE_UNIT])
        r = sh(["systemd-run", "--collect", "--unit=" + UPDATE_UNIT.replace(".service", ""),
                "/bin/bash", "-c", script])
        if r.returncode != 0:
            self.message("couldn't start the update", (r.stderr or r.stdout).strip()[:70])
            return
        now = time.monotonic()
        st.upd = {"to": latest, "pct": 1, "msg": "starting the updater", "state": "running",
                  "started": now, "changed": now, "polled": 0, "checked": now, "log": []}
        st.screen = "update"
        log("update to %s started" % latest)

    def poll_update(self):
        st = self.st
        up = st.upd
        if not up or up["state"] != "running":
            return
        now = time.monotonic()
        if now - up["polled"] < 0.5:
            return
        up["polled"] = now
        got = read_update_progress()
        if got:
            kind, msg = got
            if kind == "error":
                up.update(state="error", msg=msg or "something went wrong", log=tail_update_log())
            elif kind == "done":
                up.update(state="done", pct=100, msg=msg or "done")
            elif kind.isdigit() and (int(kind), msg) != (up["pct"], up["msg"]):
                up.update(pct=int(kind), msg=msg, changed=now, slow=False)
        self.dirty = True
        if up["state"] != "running":
            log("update %s: %s" % (up["state"], up["msg"]))
            return
        if now - up["checked"] >= 3:
            up["checked"] = now
            state = sh(["systemctl", "is-active", UPDATE_UNIT], timeout=5).stdout.strip()
            if state not in ("active", "activating", "reloading"):
                got = read_update_progress()
                if got and got[0] in ("error", "done"):
                    return
                up.update(state="error", log=tail_update_log(),
                          msg="couldn't download the installer. is the pi online?" if up["pct"] <= 1
                          else "the updater stopped before it finished.")
                log("update unit ended early (%s)" % state)
        if now - up["changed"] > 3 * 60:
            up["slow"] = True
        if now - up["changed"] > 90 * 60:
            up.update(state="error", log=tail_update_log(),
                      msg="nothing's moved for an hour and a half, so it looks stuck.")

    def uninstall(self):
        r = sh(["systemd-run", "--collect", "--unit=vitadock-uninstall-%d" % int(time.monotonic()),
                "/bin/bash", SHARE + "/uninstall.sh", "--yes", "--purge", "--reboot"])
        if r.returncode != 0:
            self.message("couldn't start the uninstall", (r.stderr or r.stdout).strip()[:70])
            return
        self.st.screen, self.st.busy = "busy", "uninstalling, the pi will restart"


    def handle(self, action):
        st = self.st
        kind = action[0]
        self.dirty = True
        if kind == "HOVER":
            i = self.hit(action[1])
            if i is not None:
                if st.screen == "menu":
                    st.sel = i
                elif st.screen == "choose":
                    st.choice["sel"] = i
                elif st.screen == "wifi":
                    st.wsel = i
                elif st.screen == "confirm":
                    st.confirm["sel"] = i
                elif st.screen == "soundtest":
                    st.stest["sel"] = i
                elif st.screen == "text":
                    st.kb_r, st.kb_c = i
            return
        if kind == "CLICK":
            i = self.hit(action[1])
            if st.screen == "menu" and i is not None:
                st.sel = i
                kind = "OK"
            elif st.screen == "choose" and i is not None:
                st.choice["sel"] = i
                kind = "OK"
            elif st.screen == "wifi" and i is not None:
                st.wsel = i
                kind = "OK"
            elif st.screen == "confirm" and i is not None:
                st.confirm["sel"] = i
                kind = "OK"
            elif st.screen == "soundtest" and i is not None:
                st.stest["sel"] = i
                kind = "OK"
            elif st.screen == "text" and i is not None:
                st.kb_r, st.kb_c = i
                self.kb_press()
                return
            elif st.screen in ("home", "msg", "info", "help"):
                kind = "OK"
            else:
                return
        if kind == "VT":
            self.switch_vt(action[1])
            return
        if st.screen == "text":
            self.handle_text(action)
            return
        if kind == "TEXT":
            return
        if kind == "SUBMIT":
            kind = "OK"

        if st.screen == "intro":
            self.intro_skip = True
        elif st.screen == "home":
            self.open_menu()
        elif st.screen == "menu":
            n = len(st.items)
            key = st.items[st.sel][0]
            if kind == "UP":
                st.sel = (st.sel - 1) % n
                st.menu_note = ""
            elif kind == "DOWN":
                st.sel = (st.sel + 1) % n
                st.menu_note = ""
            elif kind in ("LEFT", "RIGHT") and key.startswith("choice:"):
                self.cycle(key[7:], -1 if kind == "LEFT" else 1)
            elif kind == "OK":
                self.activate(key)
            elif kind == "BACK":
                st.menu_note = ""
                if st.menu_stack:
                    st.menu, st.sel = st.menu_stack.pop()
                    build_menu(st)
                else:
                    st.screen = "home"
                    self.retry_at = 0
        elif st.screen == "choose":
            c = st.choice
            title, options, cur = choice_def(st, c["key"])
            if kind == "UP":
                c["sel"] = (c["sel"] - 1) % len(options)
            elif kind == "DOWN":
                c["sel"] = (c["sel"] + 1) % len(options)
            elif kind == "OK":
                self.set_choice(c["key"], options[c["sel"]][0])
                st.screen = "menu"
            elif kind == "BACK":
                self.show_menu()
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
                self.go_back(back)
        elif st.screen == "soundtest":
            n = len(st.stest["pcms"]) + 1
            if kind == "UP":
                st.stest["sel"] = (st.stest["sel"] - 1) % n
            elif kind == "DOWN":
                st.stest["sel"] = (st.stest["sel"] + 1) % n
            elif kind == "OK":
                self.soundtest_ok()
            elif kind == "BACK":
                self.show_menu()
        elif st.screen == "msg":
            if kind in ("OK", "BACK"):
                self.go_back((st.msg or {}).get("back", "menu"))
        elif st.screen in ("info", "help"):
            if kind in ("OK", "BACK"):
                self.show_menu()
        elif st.screen == "update":
            if st.upd and st.upd["state"] != "running" and kind in ("OK", "BACK"):
                self.open_menu()
        elif st.screen == "pair":
            if kind in ("BACK", "OK"):
                if st.pairing:
                    st.pairing.stop()
                st.pairing = None
                self.show_menu()

    def switch_vt(self, n):
        if n == 1:
            return
        log("switching to console tty%d (alt+f1 comes back)" % n)
        self.screen.close()
        try:
            import fcntl
            fd = os.open("/dev/tty0", os.O_RDWR)
            try:
                fcntl.ioctl(fd, 0x5606, n)
            finally:
                os.close(fd)
        except OSError as e:
            log("couldn't switch console: %s" % e)
            self.screen.open(self.st.disp)
            return
        time.sleep(1)
        while active_vt() not in ("tty1", ""):
            time.sleep(0.5)
        log("back on tty1")
        time.sleep(0.3)
        self.screen.open(self.st.disp)
        self.dirty = True

    def hit(self, pos):
        x, y = pos
        for hx, hy, hw, hh, i in self.st.hits:
            if hx <= x < hx + hw and hy <= y < hy + hh:
                return i
        return None


    def redraw(self, force=False):
        if not self.screen.is_open():
            return
        if force or self.dirty or time.monotonic() - self.last_draw > 0.5:
            draw(self.screen.gfx, self.st)
            self.screen.present()
            self.dirty = False
            self.last_draw = time.monotonic()

    def intro(self):
        st = self.st
        self.intro_done = True
        prev = st.screen
        st.screen, self.intro_skip = "intro", False
        play_sound("boot", st.cfg, st.disp)
        start = time.monotonic()
        length = 2.2
        while not self.intro_skip:
            st.intro_t = min(1.0, (time.monotonic() - start) / length)
            draw(self.screen.gfx, st)
            self.screen.present()
            for action in self.screen.input.actions():
                self.handle(action)
            if st.intro_t >= 1.0 and time.monotonic() - start > length + 0.6:
                break
            time.sleep(1 / 30.0)
        st.screen = prev if prev != "intro" else "home"
        self.dirty = True

    def tick(self):
        st = self.st
        if st.screen == "update":
            self.poll_update()
        p = st.pairing
        if st.screen == "pair" and p and not p.result:
            if int(time.monotonic()) % 2 == 0:
                p.poll()
            if p.result or p.left() == 0:
                p.stop()
                if not p.result:
                    self.message("nothing paired", "the vita didn't connect in time.",
                                 "try again, and make sure bluetooth is on in the vita's settings.")

    def run(self):
        st = self.st
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        if shutil.which("plymouth"):
            sh(["plymouth", "quit", "--retain-splash"], timeout=5)
        cpu_performance()
        try:
            with open(UPDATED_MARKER) as f:
                newver = f.read().strip()
            os.remove(UPDATED_MARKER)
            st.notice, st.notice_until = "updated to %s." % (newver or st.ver), time.monotonic() + 180
            log("just updated to %s" % newver)
        except OSError:
            pass
        try:
            apply_output_mode(load_config())
        except OSError as e:
            log("couldn't update cmdline.txt: %s" % e)
        log("VitaDock++ %s starting" % self.st.ver)
        while True:
            st.cfg = load_config()
            st.disp = find_display(st.cfg)
            if not st.disp:
                self.screen.close()
                self.note("waiting for a tv on hdmi")
                time.sleep(3)
                continue
            st.out_mode = pick_output_mode(st.cfg, st.disp[2])

            now = time.monotonic()
            if now - self.last_vita_check >= 1:
                self.last_vita_check = now
                vita = find_vita()
                if vita != st.vita:
                    st.vita = vita
                    st.pixfmt = pick_pixfmt(st.cfg, vita) if vita else ""
                    st.vita_audio = vita_audio_card(vita)
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
            if not self.intro_done:
                self.intro()
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
    print("bt     %s" % ("on" if cfg["BT_AUDIO"] == "yes" else "off"))
    print("player %s" % (player_name(cfg) if player_name(cfg) != "compatible" and gst_available() else "compatible (mpv)"))
    if disp:
        sw, sh_ = (int(v) for v in cfg["VITA_MODE"].split("x"))
        ow, oh = output_size(cfg, disp)
        plan = scaling_plan(sw, sh_, ow, oh, cfg.get("SHARPNESS", "pixel") == "pixel")
        print("scale  %s" % (("pixel perfect %dx, %d rows trimmed each side, at %s" % (plan["k"], plan["crop"], plan["rect"]))
                             if plan["nearest"] else "smooth, fit to %dx%d" % (ow, oh)))
        print("1080p  up to %dhz" % best_1080p_refresh(disp))
    va = vita_audio_card(vita)
    print("sound  %s" % ("usb from the vita, %s" % va if va and usb_audio_wanted(cfg)
                         else "bluetooth" if cfg["BT_AUDIO"] == "yes" else "no usb audio from the vita"))
    for pcm in pcm_candidates(cfg, disp):
        ok, err = probe_pcm(pcm)
        print("  out  %-28s %s" % (pcm, "ok" if ok else err))


PLYMOUTH_SCRIPT = """\
Window.SetBackgroundTopColor(0.086, 0.090, 0.106);
Window.SetBackgroundBottomColor(0.086, 0.090, 0.106);

logo.image = Image("logo.png");
logo.sprite = Sprite(logo.image);
logo.x = Window.GetWidth() / 2 - logo.image.GetWidth() / 2;
logo.y = Window.GetHeight() / 2 - logo.image.GetHeight() / 2;
logo.sprite.SetPosition(logo.x, logo.y, 1);

on_img = Image("dot-on.png");
off_img = Image("dot-off.png");
for (i = 0; i < 4; i++) {
    dot[i] = Sprite(off_img);
    dot[i].SetPosition(Window.GetWidth() / 2 - off_img.GetWidth() / 2,
                       logo.y + %(cable_y)d + i * %(step)d, 2);
}

frame = 0;
fun refresh_callback() {
    frame++;
    lit = Math.Int(frame / 7) %% 4;
    for (i = 0; i < 4; i++) {
        if (i == lit) dot[i].SetImage(on_img);
        else dot[i].SetImage(off_img);
    }
}
Plymouth.SetRefreshFunction(refresh_callback);
"""


def cmd_plymouth_theme(outdir):
    from PIL import Image, ImageDraw, ImageFont
    os.makedirs(outdir, exist_ok=True)
    px = 12
    rows = HANDHELD[:11]
    w = len(rows[0]) * px
    font = ImageFont.truetype(os.path.join(FONT_DIR, FONT_FILES["bold"]), 46)
    title_h = 70
    img = Image.new("RGB", (max(w, 360), len(rows) * px + 4 * 22 + 40 + title_h), BG)
    d = ImageDraw.Draw(img)
    ox = (img.width - w) // 2
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch == "#":
                d.rectangle([ox + c * px, r * px, ox + (c + 1) * px - 1, (r + 1) * px - 1], fill=ACCENT)
    cable_y = len(rows) * px + 6
    ty = cable_y + 4 * 22 + 24
    tw = d.textlength("VitaDock++", font=font)
    d.text(((img.width - tw) / 2, ty), "VitaDock++", font=font, fill=FG)
    img.save(os.path.join(outdir, "logo.png"))
    for name, col in (("dot-on.png", ACCENT), ("dot-off.png", FAINT)):
        Image.new("RGB", (px * 2, px), col).save(os.path.join(outdir, name))
    with open(os.path.join(outdir, "vitadock.script"), "w") as f:
        f.write(PLYMOUTH_SCRIPT % {"cable_y": cable_y, "step": 22})
    with open(os.path.join(outdir, "vitadock.plymouth"), "w") as f:
        f.write("[Plymouth Theme]\nName=VitaDock++\nDescription=VitaDock++ boot screen\n"
                "ModuleName=script\n\n[script]\nImageDir=%s\nScriptFile=%s/vitadock.script\n" % (outdir, outdir))
    print("plymouth theme written to " + outdir)


def cmd_screenshot(outdir):
    os.makedirs(outdir, exist_ok=True)
    st = State()
    st.cfg = dict(DEFAULTS)
    st.disp = ("/dev/dri/card1", "HDMI-A-1", "")
    st.out_mode = "1920x1080@60"
    st.ips = ["192.168.1.42"]
    st.wifi_label = "BT-Hub6-7G2K"

    def shot(name, w=1920, h=1080):
        g = PilGfx(w, h)
        draw(g, st)
        g.img.save(os.path.join(outdir, name + ".png"))
        print("wrote " + name)

    for i, t in enumerate((0.3, 0.7, 1.0)):
        st.screen, st.intro_t = "intro", t
        shot("intro-%d" % i)
    g = PilGfx(1920, 1080)
    draw_connecting(g, st, 2)
    g.img.save(os.path.join(outdir, "connecting.png"))
    st.screen = "home"
    st.notice, st.notice_until = "updated to %s." % st.ver, time.monotonic() + 60
    shot("home")
    st.notice = ""
    st.vita, st.pixfmt, st.vita_audio = "/dev/video0", "nv12", "hw:CARD=vitastream,DEV=0"
    st.screen, st.menu, st.sel = "menu", "main", 1
    build_menu(st)
    shot("menu")
    for m in ("picture", "sound", "power", "system"):
        st.menu, st.sel = m, 0
        build_menu(st)
        shot("menu-" + m)
    st.menu, st.sel = "picture", 2
    build_menu(st)
    shot("menu-picture-speed")
    st.screen, st.choice = "choose", {"key": "player", "sel": 0}
    shot("choose-speed")
    st.choice = {"key": "sharp", "sel": 0}
    shot("choose-sharpness")
    st.screen = "help"
    shot("help")
    st.screen = "info"
    shot("info")
    st.screen = "soundtest"
    pcms = pcm_candidates(dict(st.cfg, AUDIO_PCM="auto"), st.disp)
    st.stest = {"pcms": pcms, "res": {}, "sel": 0, "saved": "auto"}
    shot("soundtest")
    st.stest["res"] = {pcms[0]: "audio open error: Device or resource busy", pcms[1]: "ok"}
    st.stest["sel"] = 1
    shot("soundtest-played")
    st.screen = "confirm"
    st.confirm = {"lines": ["uninstall vitadock++?", "", "this removes it and its settings, then restarts the pi",
                            "into plain pi os. you'd need to reinstall to get it back."], "yes": None, "sel": 0}
    shot("confirm")
    st.wifi_nets = [
        {"ssid": "BT-Hub6-7G2K", "signal": 82, "secure": True, "active": True, "known": True},
        {"ssid": "SKY4F1A2", "signal": 64, "secure": True, "active": False, "known": False},
        {"ssid": "TP-Link_Guest", "signal": 22, "secure": False, "active": False, "known": False},
    ]
    build_wifi(st)
    st.screen, st.wsel = "wifi", 1
    shot("wifi")
    st.screen = "update"
    st.upd = {"to": "1.4.0", "pct": 47, "msg": "installing gstreamer1.0-plugins-bad (arm64)", "state": "running",
              "started": time.monotonic() - 83, "log": []}
    shot("update")
    st.screen, st.vita = "home", None
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
    elif cmd == "plymouth-theme" and len(argv) == 3:
        cmd_plymouth_theme(argv[2])
    elif cmd == "screenshot" and len(argv) == 3:
        cmd_screenshot(argv[2])
    elif cmd == "audio-out":
        try:
            with open(AUDIO_OUT_FILE) as f:
                pick = f.read().strip()
        except OSError:
            pick = ""
        cfg = load_config()
        print(pick or audio_out(cfg, find_display(cfg)))
    elif cmd in ("version", "--version"):
        print(version())
    else:
        print(__doc__ or "", end="")
        print("usage: vitadock run | status | set KEY VALUE | bluetooth on|off | screenshot DIR | version")


if __name__ == "__main__":
    main(sys.argv)
