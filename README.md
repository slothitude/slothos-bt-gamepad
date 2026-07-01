# SlothOS Controller

Turn an **Anbernic RG35XX H** handheld into a Bluetooth HID gamepad. The host
(Windows / macOS / Android / Linux) pairs to the device and sees it as a
standard HID-compliant game controller. Every button, the D-pad, and both
analog sticks register normally — no driver install, no extra software on
the host.

Runs on-device alongside stock firmware. The device keeps working as a
retro handheld; this just adds a second identity as a wireless controller.

Tested on the RG35XX H (Allwinner H700, ARM64) with stock firmware. Probably
works on other Anbernic H700 devices (RG35XX Plus, RG34XX) with little or
no modification — the input evdev codes are the same.

## What it does

| | |
|---|---|
| **Pairing** | Standard BR/EDR Bluetooth 2.1 + ESSP, Type 5 Authenticated Combination Key |
| **HID descriptor** | 16 buttons, 4-bit hat (D-pad), 6 axes (X, Y, Z, Rz, Rx, Ry) |
| **Report rate** | ~120 Hz |
| **Wire report** | 10 bytes: ID + 2 btn + 1 hat-pad + 6 axes |
| **VID/PID** | `0x1209` / `0x5017` ([pid.codes](http://pid.codes/1209/5017/) — open-source registry) |
| **Latency** | Same as any BT Classic HID gamepad (~10–15 ms one-way) |

## Requirements

**On the device:**
- Anbernic RG35XX H, rooted, with WiFi on and SSH enabled
- Python 3.10+ (preinstalled on stock firmware)
- `python3-dbus`, `python3-gi` (preinstalled on stock firmware)
- `bluez` (preinstalled on stock firmware)

**On the host (your PC/phone):**
- Anything that pairs with Bluetooth Classic HID gamepads. No software to install.

## Install

From a machine that can SSH to the device:

```bash
git clone https://github.com/slothitude/slothos-bt-gamepad.git
cd slothos-bt-gamepad
./install.sh 192.168.0.77          # use your device's IP
```

The script is idempotent — re-run it any time to update or repair the install.
It will:
1. Verify SSH + Python + bluetoothd on the device
2. Install `python3-evdev` if missing (via apt or pip)
3. Copy the Python HID stack to `/usr/local/slothos/bt_gamepad/`
4. Drop the BlueZ `--compat` override into `/etc/systemd/system/bluetooth.service.d/`
5. Enable + start `bt_gamepad.service`
6. Bring `hci0` up + authenticated
7. Print the device's Bluetooth address and pairing instructions

Then pair from your host OS. The device appears as **SlothOS Controller**
(or whatever name BlueZ advertises on your firmware). On Windows, open
`joy.cpl` to verify every button.

### Launching from stock firmware (BT Mode splash)

`install.sh` also deploys an optional fullscreen splash app that gives
stock-firmware users a visible "Bluetooth Mode" indicator on the device
panel. While the splash is up:

- `bt_gamepad.service` is started automatically.
- All button/stick input still forwards to the paired host (the splash
  does not grab evdev, so it coexists with the BT stack).
- Press **Start + Select** together to stop BT mode and return to the
  launcher.

Smoke-test it over SSH:

```bash
ssh root@<device> '/usr/local/bin/slothos-bt-mode &'
```

To add a permanent entry to the stock Anbernic launcher, drop a `.sh`
file in your firmware's APP directory (e.g. `/mnt/app/`,
`/media/app/`, or `/mnt/SDCARD/APPS/` depending on firmware rev):

```sh
#!/bin/sh
exec /usr/local/bin/slothos-bt-mode
```

The exact APP directory varies by firmware revision — the install
output prints the common candidates. The splash app is purely
additive; if you don't want it, ignore the entry and the BT stack
keeps working as a background service exactly as before.

## Pairing

1. Put the device in discoverable mode (the install script's output shows how):
   ```bash
   ssh root@<device> 'hciconfig hci0 leadv on'
   ```
2. Host OS: open Bluetooth settings, scan, pair to the device.
3. The on-device BlueZ agent auto-confirms pairing (DisplayYesNo capability).
4. Open `joy.cpl` (Windows) or <https://gamepad-tester.com> (any OS).

## Uninstall

```bash
./install.sh --uninstall 192.168.0.77
```

Removes the systemd unit, the BlueZ override, and the stack directory. Pair
cache on the host OS clears on its next pair attempt.

## How it works

The stack lives in the device's BlueZ userspace — no kernel modifications.

```
/dev/input/event1 ─▶ evdev_reader ─▶ GameState ─▶ report-pump ─▶ L2CAP interrupt (PSM 19)
                                       │
                                       └─▶ Profile1 + SDP record (BlueZ --compat)
                                            L2CAP control   (PSM 17)
```

**Key design choices** (each one was a multi-day debugging session — see
`docs/TROUBLESHOOTING.md` for the full story):

- **BlueZ `--compat` mode** with an aggressive plugin blocklist. BlueZ's
  built-in `input` / `hog` plugins grab the HID PSMs (17 and 19) for
  themselves; we disable them so our userspace code owns those sockets.
- **Custom Profile UUID** (`1f16e7c0-b59b-11e3-95d2-0002a5d5c51b`). Registering
  `Profile1` under the standard HID UUID 0x1124 makes BlueZ install its own
  L2CAP listener on PSM 17/19 even with the input plugin disabled. Using a
  custom UUID with the HID UUID only in the SDP record's ServiceClassIDList
  is the workaround.
- **Raw `AF_BLUETOOTH` L2CAP sockets** via the stdlib `socket` module. Not
  PyBluez (its C extension raises `PY_SSIZE_T_CLEAN` errors on Python 3.10+).
- **L2CAP Basic mode pinned via `setsockopt(SOL_L2CAP, L2CAP_OPTIONS, …)`**
  before `listen()`. Linux 4.9 advertises Enhanced Retransmission Mode by
  default; Windows 11 rejects ERTM on HID PSMs as "unacceptable parameters"
  and resets the connection.
- **Hand-built HID SDP record** (`sdp_record_gamepad.xml`) with the HID
  Profile §5.3 attributes (0x0200–0x020E), and a 92-byte HID Report
  Descriptor that's byte-aligned per Windows HidBth validation.

## Files

| | |
|---|---|
| `main.py` | Entrypoint — loads the stack, runs the GLib main loop |
| `bt_l2cap_v2.py` | The meat. Raw L2CAP server + Profile1 registration |
| `BluezProfile.py` | `org.bluez.Profile1` dbus.service.Object |
| `BluezAgent.py` | DisplayYesNo agent for auto-confirm of pair requests |
| `hid_descriptor.py` | 92-byte HID Report Descriptor |
| `evdev_to_hid.py` | Map evdev `BTN_*` codes → HID button bits |
| `evdev_reader.py` | Read `/dev/input/event1`, produce `GameState` updates |
| `sdp_record_gamepad.xml` | Hand-built HID SDP record |
| `sdp_record_pnp.xml` | PnP SDP record (VID/PID source) |
| `set_did.py` | Set Bluetooth DeviceID (VID/PID) via raw mgmt socket |
| `bt_gamepad.service` | systemd unit |
| `bluetooth.service.d/exec.conf` | BlueZ `--compat` + plugin blocklist override |
| `install.sh` | One-command installer (run from host) |
| `app/bt_mode.py` | Optional BT Mode splash app (pygame + fbcon) |
| `app/splash.png` | 640×480 splash image shown on the device panel |
| `app/requirements.txt` | Splash app deps (pygame) |
| `bt_mode-launch.sh` | Wrapper for stock launcher to invoke the splash |

## Troubleshooting

If pair fails, buttons don't register, or the chip disappears, read
**[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)** — it has the four
device-specific landmines with full recovery recipes:

1. Realtek RTL8821CS chip crash → `hci0: No such device` (rfkill power-cycle)
2. Boot race → hci0 stays DOWN after reboot (`hciconfig hci0 up` + restart)
3. Pair cache fragility across chip crashes
4. Windows Code 10 "not byte aligned" → HID descriptor (already fixed)

## Project status

Built as Phase 4 of [SlothOS](https://github.com/slothitude/slothos), a
custom firmware overlay for the RG35XX H. This standalone repo contains
only the Bluetooth HID gamepad stack, so people who just want the gamepad
mode don't need the full SlothOS codebase.

## License

MIT — see [LICENSE](LICENSE).

The `0x1209` VID is assigned by [pid.codes](http://pid.codes/) to open-source
projects. PID `0x5017` for this project is registered at
<http://pid.codes/1209/5017/> (pending merge of
[PR #1235](https://github.com/pidcodes/pidcodes.github.com/pull/1235)).
