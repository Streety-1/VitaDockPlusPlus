<p align="center">
  <img src=".github/assets/banner.png" alt="VitaDock++" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/raspberry%20pi-5-f0b648?style=flat-square&labelColor=16171b" alt="Raspberry Pi 5">
  <img src="https://img.shields.io/badge/pi%20os-lite%2064--bit-f0b648?style=flat-square&labelColor=16171b" alt="Pi OS Lite 64-bit">
  <a href="https://github.com/Streety-1/VitaDockPlusPlus/releases/latest"><img src="https://img.shields.io/github/v/release/Streety-1/VitaDockPlusPlus?style=flat-square&labelColor=16171b&color=e2dfd6&label=release" alt="latest release"></a>
  <img src="https://img.shields.io/badge/license-MIT-e2dfd6?style=flat-square&labelColor=16171b" alt="MIT licence">
</p>

<p align="center">
  <a href="#what-youll-need">what you'll need</a> ·
  <a href="#setting-up-your-vita">vita setup</a> ·
  <a href="#installing-on-the-pi">install</a> ·
  <a href="#using-it">using it</a> ·
  <a href="#troubleshooting">troubleshooting</a> ·
  <a href="#credits">credits</a>
</p>

# VitaDock++

VitaDock++ was created as a successor to SilentNightx's [VitaDock+](https://github.com/SilentNightx/VitaDockPlus), which seems to of been discontinued since its last update in November 2023, only works on Pis up to the Pi 4 and has a near 4GB image. Mine is exclusive to the Raspberry Pi 5 running Pi OS Lite, as this offers the best experience with minimal input delay and a one line installer.

<p align="center">
  <img src=".github/assets/how-it-works.png" alt="vita to pi 5 over usb, pi 5 to tv over hdmi" width="100%">
</p>

Your Vita runs xerpi's udcd_uvc plugin, which makes it show up as a USB webcam. The Pi picks that up, plays it full screen with mpv (straight to the screen, no desktop in the way) and upscales it to your TV. When the Vita isn't plugged in you get a waiting screen, and there's a settings menu on the TV for everything else, so you never need SSH or a desktop.

## What you'll need

- A **PS Vita** with HENkaku/taiHEN. If yours isn't hacked yet, follow [vita.hacks.guide](https://vita.hacks.guide/) first and come back.
- A **Raspberry Pi 5**, any RAM size. You'll need its official power supply too, as cheap ones cause all sorts of problems.
- A **micro SD card** (8GB or more), plus a **micro HDMI to HDMI cable**.
- A **USB cable** for the Vita: micro USB for the slim (PCH-2000), or Sony's own cable for the original (PCH-1000). Make sure it's a data cable and not a charge-only one.
- A **USB keyboard, mouse or USB pad** for the Pi. You need it for the install and for the settings menu. The controller you actually play with pairs with the Vita, not the Pi.

## Setting up your Vita

1. Download the plugin from [xerpi's releases](https://github.com/xerpi/vita-udcd-uvc/releases). There are three versions, so pick the one for your Vita:

   | file | for | what it does |
   |---|---|---|
   | `udcd_uvc_lcd_off.skprx` | slim (PCH-2000) | turns the Vita's screen off while docked |
   | `udcd_uvc_oled_off.skprx` | original (PCH-1000) | turns the Vita's screen off while docked |
   | `udcd_uvc.skprx` | either | leaves the screen on |

2. Get it into `ur0:tai/` on the Vita. VitaShell's USB mode only shows your memory card (or SD2Vita), so copy it onto that first. Then in VitaShell on the Vita, copy it from there into `ur0:tai/`.
3. Still in VitaShell, open `ur0:tai/config.txt` and add the plugin under `*KERNEL`, with the filename you used:
   ```
   *KERNEL
   ur0:tai/udcd_uvc_lcd_off.skprx
   ```
4. Reboot the Vita.

If the Vita won't boot after this, hold **L** while it starts up. That skips plugins so you can fix the config. To check it's working, plug the Vita into a PC and it should show up as a camera.

## Installing on the Pi

1. Flash **Raspberry Pi OS Lite (64-bit)** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/). In the settings, set a username and password, and add your Wi-Fi if you're not using ethernet. It needs internet for the install.
2. Put the card in the Pi, plug in a keyboard and the TV, and log in.
3. Run this:
   ```
   curl -fsSL https://raw.githubusercontent.com/Streety-1/VitaDockPlusPlus/main/install.sh | bash
   ```
4. Say yes when it asks, wait a few minutes for it to finish, and let it reboot.

That's the only typing you'll ever do on it. From then on the Pi boots straight into the dock.

<details>
<summary>installing from a downloaded copy instead</summary>

```
git clone https://github.com/Streety-1/VitaDockPlusPlus.git
cd VitaDockPlusPlus
bash install.sh
```

Options: `--yes` skips the questions, `--no-reboot` won't reboot at the end, and `--reset-config` puts the settings back to default.
</details>

## Using it

Plug the Vita into any USB port on the Pi and the game shows up on the TV. Unplug it and you're back to the waiting screen.

To get into the settings menu:

| | on the waiting screen | mid-game |
|---|---|---|
| keyboard | Enter | Esc, Tab or the Windows key |
| mouse | left click | right click |
| USB pad | A | PS/Home button, or Start + Select |

The menu has:

- **Video mode.** This is what the Pi asks the Vita to send:

  | mode | from the Vita | notes |
  |---|---|---|
  | `smooth` | 864x488 @ 60fps | the default. best for most games |
  | `sharp` | 960x544 @ 30fps | native res but half the frames |
  | `native60` | 960x544 @ ~60fps | native res, but the plugin can't quite hold 60 so expect some judder |
  | `hd` | 1280x720 @ 30fps | only works with Sharpscale and a 720p patch for that game |

- **Upscaling.** lanczos (sharpest), spline36 or bilinear (softest).
- **TV resolution.** 1080p by default. You can also pick 720p, 4K or whatever your TV prefers.
- **Bluetooth audio** and pairing (see [sound](#sound)).
- **Info + help.** Shows what the Pi can see: the TV, the Vita and its IP address.
- **Check for updates**, restart, shut down and uninstall.

## Sound

The plugin only sends video, so sound needs its own route to the TV. There are two ways:

- **A 3.5mm cable** from the Vita's headphone socket to your TV, soundbar or speakers. This is the most reliable way. If you hear a hum, turn off *Settings > System > USB Power Supply* on the Vita.
- **Bluetooth (experimental).** Turn on *bluetooth audio* in the menu, then pick *pair the vita for audio*. On the Vita, go to *Settings > Devices > Bluetooth Devices* and choose **VitaDock++**. After that you just connect from that menu on the Vita each time, and the sound comes out of the TV over HDMI.

## Controllers

Pair your controller with the **Vita**, since that's what's running the game. DualShock 3 and 4 work out of the box. [MiniVitaTV](https://github.com/TheOfficialFloW/MiniVitaTV) is worth installing too, as it makes games use their PS TV control layouts. Touch screen and rear touch obviously won't work while it's on the TV, so pick games that are fine on buttons.

## Updating

Pick *check for updates* in the menu (the Pi needs to be online). It tells you if there's a new version and updates itself. Your settings are kept.

## Uninstalling

Pick *uninstall vitadock++* in the menu. It removes everything, puts the boot settings back and restarts into plain Pi OS Lite.

## Troubleshooting

<details>
<summary><b>the TV says no signal / black screen</b></summary>

Turn the TV on before the Pi. Use the HDMI port next to the Pi's power socket (HDMI 0). If it's still blank, use the menu to set *tv resolution* to *preferred*.
</details>

<details>
<summary><b>the waiting screen shows but the Vita never appears</b></summary>

- Try a different USB cable. Loads of them are charge-only.
- Reboot the Vita. Having lots of plugins, or a slow SD2Vita/storage plugin, can stop udcd_uvc from connecting. If you're on StorageMgr, switching to yamt-vita can help.
- Make sure it shows up as a camera on a PC. If it doesn't there either, it's the Vita side.
</details>

<details>
<summary><b>"vita found but no picture"</b></summary>

Usually the video format. Edit `/etc/vitadock.conf` (Ctrl+Alt+F2 on a keyboard gets you a login), change `VITA_PIXFMT=auto` to `VITA_PIXFMT=yuyv422` and reboot.
</details>

<details>
<summary><b>it feels laggy</b></summary>

Turn on your TV's **game mode** for that HDMI input first, as that's usually the biggest difference. Then use `smooth` mode, and try 720p output so the TV does the last bit of scaling.
</details>

<details>
<summary><b>bluetooth audio crackles or won't connect</b></summary>

It is experimental. It tends to settle down after the first minute. If it won't connect at all, turn bluetooth audio off and on again in the menu, then pair again. The cable is always the safe option.
</details>

If none of that helps, [open an issue](https://github.com/Streety-1/VitaDockPlusPlus/issues) and include a photo of the *info + help* screen. If you can get to a terminal, the output of `vitadock status` and `journalctl -u vitadock -n 50` helps a lot too.

## Credits

None of this would exist without these people:

- **[xerpi](https://github.com/xerpi)** for [vita-udcd-uvc](https://github.com/xerpi/vita-udcd-uvc), the plugin that gets video out of the Vita in the first place.
- **[SilentNightx](https://github.com/SilentNightx)** and the original VitaDock team (Bu, ZoidBerg, Si, David-OX and Crash) for [VitaDock+](https://github.com/SilentNightx/VitaDockPlus) and the original VitaDock, which this is based on the idea of.
- **[TheOfficialFloW](https://github.com/TheOfficialFloW)**, Team Molecule and everyone behind HENkaku and taiHEN.
- **[mpv](https://mpv.io/)**, **[pygame](https://www.pygame.org/)** and **[bluez-alsa](https://github.com/arkq/bluez-alsa)**, which do the heavy lifting on the Pi.

## Disclaimer

This is a fan project. It isn't affiliated with, endorsed by or connected to Sony Interactive Entertainment or Raspberry Pi Ltd. PlayStation and PS Vita are trademarks of Sony Interactive Entertainment, and Raspberry Pi is a trademark of Raspberry Pi Ltd.

VitaDock++ doesn't include, download or help you get any games or copyrighted material. It only shows what your own Vita is outputting. Modding your Vita is at your own risk, and so is everything else here. It's provided as-is with no warranty, so if something breaks, that's on you, not me.

## Licence

[MIT](LICENSE). Do what you like with it, just keep the licence file in.
