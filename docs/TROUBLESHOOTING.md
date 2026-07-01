# Troubleshooting

The four landmines that cost real time during development, with full
recovery recipes. If something isn't working, walk this list top-to-bottom.

## Table of Contents
1. [Chip crash — `hci0: No such device`](#1-chip-crash--hci0-no-such-device)
2. [Boot race — hci0 stays DOWN after reboot](#2-boot-race--hci0-stays-down-after-reboot)
3. [Pair cache fragility](#3-pair-cache-fragility)
4. [Windows Code 10 — "HID Report Descriptor not byte aligned"](#4-windows-code-10--hid-descriptor-not-byte-aligned)
5. [Pair succeeded but no buttons respond](#5-pair-succeeded-but-no-buttons-respond)

---

## 1. Chip crash — `hci0: No such device`

### Symptoms
- `joy.cpl` shows SlothOS Controller paired + Status OK, but no buttons respond.
- `ssh root@device hciconfig hci0` → `No such device`.
- `bluetoothd` log: `Number of controllers: 0`.
- `rfkill list bluetooth` → `Soft blocked: yes`.

### Root cause
The Realtek RTL8821CS chip is hung in H5-sync state. `rfkill unblock` alone
does NOT clear this — it just unblocks the radio without power-cycling the
chip's internal state.

### Recovery recipe (the only thing that actually works)
```bash
ssh root@<device> '
  rfkill block bluetooth;    sleep 2
  rfkill unblock bluetooth;  sleep 2
  rtk_hciattach -n -s 115200 ttyS1 rtk_h5
'
```

The block/unblock cycle power-cycles the chip via the sunxi-bt regulator
and forces the Realtek chip to reset. You should see:
```
Get SYNC Resp Pkt
rtl8821c_fw download ... 54892 bytes
Device setup complete
```

### How to confirm you're in this failure mode
While `rtk_hciattach` is running in its stuck state, you'll see:
```
OP_H5_SYNC Transmission timeout
OP_H5_SYNC Transmission timeout
... (×11)
Retransmission exhausts
```
That's the smoking gun for "chip is hung, do the rfkill cycle."

---

## 2. Boot race — hci0 stays DOWN after reboot

### Symptoms
After a reboot, `rtk_hciattach` is running but `hciconfig hci0` shows the
adapter as DOWN. `bt_gamepad.service` fails because the agent can't register.

### Root cause
On the RG35XX H's stock firmware, `rtk_hciattach` finishes firmware load
about ~3 seconds after boot, but the boot script that runs `hciconfig hci0 up`
races with that delay and sometimes wins.

### Recovery recipe
```bash
ssh root@<device> '
  hciconfig hci0 up
  hciconfig hci0 auth
  systemctl restart bt_gamepad
'
```

Run this after every reboot if the service didn't auto-start.

### Making it stick
The `install.sh` script does this once at install time. For a permanent fix,
add the same commands to `/etc/rc.local` or write a systemd unit that runs
them after `bluetooth.service` with a 5-second delay.

---

## 3. Pair cache fragility

### Symptoms
A previously-paired host no longer sees the device, or pairing completes but
immediately disconnects. Reboot does not restore the link.

### Root cause
When the Realtek chip crashes hard (see #1), the device-side pair cache at
`/var/lib/bluetooth/<device-bdaddr>/` can lose host entries. The host OS
still thinks it's paired, but the device has no record of the host.

### Recovery recipe
```bash
# On device: remove the host entry (or nuke the whole cache)
ssh root@<device> 'rm -rf /var/lib/bluetooth/<device-bdaddr>/<host-bdaddr>'
# Or, if you want to start fresh:
ssh root@<device> 'rm -rf /var/lib/bluetooth/*'
systemctl restart bluetooth
```

Then **re-pair from the host OS** (Win+I → Bluetooth → Add → "SlothOS
Controller"). The DisplayYesNo agent on-device auto-confirms.

---

## 4. Windows Code 10 — "HID Report Descriptor not byte aligned"

### Symptoms
After pairing on Windows 11, `Get-PnpDevice` shows the Bluetooth HID Device
with **Problem Code 10** and message:
> The HID Report Descriptor failed validation. A report was not byte aligned.

### Root cause
Windows 11 HidBth validation requires every Input Data field in the HID
Report Descriptor to start on a byte boundary. A descriptor with 16 bits
of buttons + a 4-bit hat switch = 20 bits leaves the next field starting
mid-byte.

### Fix
This is already fixed in `hid_descriptor.py` — there's a 4-bit Constant pad
after the hat switch:
```c
0x75, 0x04,   // Report Size = 4 bits
0x95, 0x01,   // Report Count = 1
0x81, 0x03,   // Input (Constant) — padding to byte-align
```
Total descriptor length is 92 bytes, wire report is 10 bytes.

### When you'll see this error anyway
- You modified `hid_descriptor.py` and broke the alignment.
- You're running an old version of the stack. `git pull` and re-deploy.

### Verify on Windows
```powershell
Get-PnpDevice | Where-Object { $_.InstanceId -match '<bd_addr_hex>' }
Get-PnpDeviceProperty -InstanceId <id> -KeyName DEVPKEY_Device_DriverProblemDesc
```
Code 10 + "not byte aligned" → HID descriptor. Code 10 + "failed start"
generic → SDP record or L2CAP mode mismatch.

---

## 5. Pair succeeded but no buttons respond

### Symptoms
Pair completes, device shows as "Connected" in the host OS, `joy.cpl` shows
it as a HID-compliant game controller with Status OK. But no buttons or
sticks register any input.

### Diagnostic flow
1. **Is the service running?**
   ```bash
   ssh root@<device> 'systemctl status bt_gamepad'
   ```
   If not, `systemctl restart bt_gamepad` and check `/var/log/bt_gamepad.log`.

2. **Are L2CAP channels connected?**
   ```bash
   ssh root@<device> 'l2ping -c 3 <host-bdaddr>'
   ```
   If ping fails, the chip is in a bad state (see #1).

3. **Is the host sending config requests?**
   ```bash
   ssh root@<device> 'btmon -t'
   ```
   In another terminal, toggle a button. You should see `ACL RX` frames
   with HID report bytes (`a1010000…`) on the interrupt channel.

4. **Is evdev reading the physical buttons?**
   ```bash
   ssh root@<device> 'evtest /dev/input/event1'
   ```
   Press buttons — you should see events. If not, the input device path is
   wrong (some firmware revs use `event0` instead of `event1`). Edit
   `bt_gamepad.service` ExecStart to add `--device /dev/input/event0` and
   `systemctl daemon-reload && systemctl restart bt_gamepad`.

5. **Are INPUT reports being sent on the interrupt channel?**
   With verbose mode on (default in the shipped unit), `bt_gamepad.log`
   prints a line per report. If you see report bytes but the host doesn't
   react, the L2CAP interrupt channel may have been dropped — restart
   `bt_gamepad` to force a reconnect from the host side.

---

## Logs

```bash
# Live tail both logs
ssh root@<device> 'tail -f /var/log/bt_gamepad.log /var/log/bluetoothd.log'

# BT-level packet capture (detailed, verbose):
ssh root@<device> 'btmon -t -w /tmp/btmon.log'   # then reproduce
ssh root@<device> 'btmon -r /tmp/btmon.log'      # to read it back
```
