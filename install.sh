#!/usr/bin/env bash
# install.sh — turn a rooted Anbernic RG35XX H into a Bluetooth HID gamepad.
#
# Prerequisites (NOT done by this script — see README.md):
#   1. Device is rooted.
#   2. WiFi is on and the device is reachable on your LAN.
#   3. SSH is enabled (Settings → Wireless → SSH on stock Anbernic firmware).
#
# Usage:
#   ./install.sh [--password [PW]] <device-ip> [ssh-user]
#   ./install.sh --uninstall <device-ip> [ssh-user]
#
# Examples:
#   ./install.sh 192.168.0.77                  # uses SSH key auth
#   ./install.sh --password 192.168.0.77       # default password "root"
#   ./install.sh --password mypass 192.168.0.77 root
#
# Re-run safely — the script is idempotent. To undo, run:
#   ./install.sh --uninstall <device-ip> [ssh-user]
#
# What this script handles for you (stock-firmware quirks discovered during
# testing on a fresh Dec-2025 RG35XX H):
#   - Device clock stuck in 2022 → synced from host before any HTTPS call
#   - /root ships as 777 → fixed to 755 (sshd StrictModes rejects key auth otherwise)
#   - No pip3, no ensurepip → bootstrapped via get-pip.py
#   - Jammy's python3-evdev built for Python 3.8, incompatible with Py 3.10
#     → we install evdev via pip (builds a wheel from source on device, ~10s)

set -euo pipefail

# ---------- helpers ----------
c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw()   { printf '\033[33m%s\033[0m\n' "$*"; }
c_blu()   { printf '\033[34m%s\033[0m\n' "$*"; }
log()     { printf '[*] %s\n' "$*"; }
log_ok()  { printf '[\033[32m✓\033[0m] %s\n' "$*"; }
log_err() { printf '[\033[31m✗\033[0m] %s\n' "$*" >&2; }
log_die() { log_err "$*"; exit 1; }

# ---------- arg parsing ----------
MODE="install"
PASSWORD=""
DEVICE=""
SSH_USER="root"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall)
      MODE="uninstall"; shift ;;
    --password)
      # Accept "--password" (defaults to root), "--password PW", or "--password=PW"
      if [[ $# -ge 2 && "${2:-}" != -* ]]; then
        PASSWORD="$2"; shift 2
      else
        PASSWORD="root"; shift
      fi ;;
    --password=*)
      PASSWORD="${1#*=}"; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      if [[ -z "$DEVICE" ]]; then
        DEVICE="$1"; shift
      else
        SSH_USER="$1"; shift
      fi ;;
  esac
done

[[ -n "$DEVICE" ]] || log_die "Usage: $0 [--uninstall] [--password [PW]] <device-ip> [ssh-user]"

# ---------- ssh / scp wrappers ----------
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
SSH_WRAPPER=()
if [[ -n "$PASSWORD" ]]; then
  command -v sshpass >/dev/null \
    || log_die "--password requires sshpass on host. Install: apt install sshpass | brew install hudochenkov/sshpass/sshpass"
  SSH_WRAPPER=(sshpass -p "$PASSWORD")
fi
SSH=("${SSH_WRAPPER[@]}" ssh "${SSH_OPTS[@]}")
SCP=("${SSH_WRAPPER[@]}" scp "${SSH_OPTS[@]}")

AUTH_LABEL="key"
[[ -n "$PASSWORD" ]] && AUTH_LABEL="password"
log "Target: ${SSH_USER}@${DEVICE}  (mode: ${MODE}, auth: ${AUTH_LABEL})"

# ---------- paths ----------
HERE="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="/usr/local/slothos/bt_gamepad"
SERVICE_SRC="${HERE}/bt_gamepad.service"
SERVICE_DST="/etc/systemd/system/bt_gamepad.service"
DROPIN_SRC="${HERE}/bluetooth.service.d/exec.conf"
DROPIN_DST="/etc/systemd/system/bluetooth.service.d/exec.conf"

# ---------- connectivity ----------
log "Testing SSH…"
"${SSH[@]}" "${SSH_USER}@${DEVICE}" 'echo ok' >/dev/null 2>&1 \
  || log_die "SSH to ${SSH_USER}@${DEVICE} failed. Check IP, credentials, and that SSH is enabled on the device."
log_ok "SSH works"

# =====================================================================
# UNINSTALL
# =====================================================================
if [[ "$MODE" == "uninstall" ]]; then
  log "Stopping + disabling bt_gamepad…"
  "${SSH[@]}" "${SSH_USER}@${DEVICE}" '
    systemctl stop bt_gamepad 2>/dev/null || true
    systemctl disable bt_gamepad 2>/dev/null || true
    rm -f /etc/systemd/system/bt_gamepad.service
    rm -rf /etc/systemd/system/bluetooth.service.d
    systemctl daemon-reload
    systemctl restart bluetooth 2>/dev/null || true
    rm -rf '"${REMOTE_DIR}"'
  ' || log_die "Uninstall commands failed."
  log_ok "Uninstalled. Pair cache on host OS will clear on next pair attempt."
  exit 0
fi

# =====================================================================
# INSTALL
# =====================================================================

# ---------- stock-firmware quirk: /root perms (sshd StrictModes) ----------
log "Fixing /root permissions (stock firmware ships 777, breaks sshd StrictModes)…"
"${SSH[@]}" "${SSH_USER}@${DEVICE}" 'chmod 755 /root' || log "warning: chmod 755 /root failed (continuing)"

# ---------- stock-firmware quirk: clock stuck in 2022 ----------
log "Syncing device clock from host (stock boots with stale RTC)…"
HOST_TIME=$(date -u '+%Y-%m-%d %H:%M:%S')
"${SSH[@]}" "${SSH_USER}@${DEVICE}" "
  date -s '${HOST_TIME}' >/dev/null || true
  hwclock --systohc 2>/dev/null || true
  date
" || log "warning: clock sync failed (continuing — HTTPS may break)"

# ---------- optional: install host pubkey for future keyless access ----------
if [[ -f "${HOME}/.ssh/id_ed25519.pub" ]]; then
  log "Installing host SSH key for keyless access (optional)…"
  "${SCP[@]}" -q "${HOME}/.ssh/id_ed25519.pub" "${SSH_USER}@${DEVICE}:/tmp/_host_pub.key" 2>/dev/null \
    && "${SSH[@]}" "${SSH_USER}@${DEVICE}" '
      mkdir -p ~/.ssh && chmod 700 ~/.ssh
      touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
      grep -qF "$(cat /tmp/_host_pub.key)" ~/.ssh/authorized_keys 2>/dev/null \
        || cat /tmp/_host_pub.key >> ~/.ssh/authorized_keys
      rm -f /tmp/_host_pub.key
    ' 2>/dev/null && log_ok "Host key installed" || log "warning: key install skipped (non-fatal)"
fi

# ---------- detect prerequisites on device ----------
log "Checking on-device Python + bluetoothd…"
DET=$("${SSH[@]}" "${SSH_USER}@${DEVICE}" '
  PY=$(command -v python3 || echo "")
  PY_VER=""
  [[ -n "$PY" ]] && PY_VER=$("$PY" -c "import sys; print(\"%d.%d\" % sys.version_info[:2])" 2>/dev/null || echo "")
  BD=$(ls /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd 2>/dev/null | head -1 || true)
  HAS_EVDEV=$(python3 -c "from evdev import InputDevice" 2>/dev/null && echo yes || echo no)
  HAS_DBUS=$(python3 -c "import dbus" 2>/dev/null && echo yes || echo no)
  HAS_GI=$(python3 -c "import gi.repository.GLib" 2>/dev/null && echo yes || echo no)
  HAS_PIP=$(python3 -m pip --version 2>/dev/null | head -1 || echo "")
  echo "py=${PY}"
  echo "py_ver=${PY_VER}"
  echo "bluetoothd=${BD}"
  echo "evdev=${HAS_EVDEV}"
  echo "dbus=${HAS_DBUS}"
  echo "gi=${HAS_GI}"
  echo "pip=${HAS_PIP}"
')

declare -A DET_KV
while IFS='=' read -r k v; do DET_KV["$k"]="$v"; done <<<"$DET"

[[ -n "${DET_KV[py]:-}" ]] || log_die "python3 not found on device. Install python3 first."
log_ok "Python ${DET_KV[py_ver]:-unknown} at ${DET_KV[py]}"

[[ -n "${DET_KV[bluetoothd]:-}" ]] || log_die "bluetoothd not found at /usr/libexec/bluetooth/bluetoothd or /usr/lib/bluetooth/bluetoothd. Install bluez."
BD_PATH="${DET_KV[bluetoothd]}"
log_ok "bluetoothd: ${BD_PATH}"

# Patch the drop-in if bluetoothd is at the non-default path.
if [[ "$BD_PATH" != "/usr/libexec/bluetooth/bluetoothd" ]]; then
  log "bluetoothd at non-default path; patching exec.conf in flight…"
  TMPDROP="$(mktemp)"
  sed "s|/usr/libexec/bluetooth/bluetoothd|${BD_PATH}|g" "$DROPIN_SRC" > "$TMPDROP"
  DROPIN_SRC="$TMPDROP"
fi

if [[ "${DET_KV[dbus]}" != "yes" ]]; then
  log_die "python3-dbus missing. Install on device with: apt-get install -y python3-dbus"
fi
if [[ "${DET_KV[gi]}" != "yes" ]]; then
  log_die "PyGObject (python3-gi) missing. Install on device with: apt-get install -y python3-gi"
fi

# ---------- install evdev via pip (apt path is unreliable on stock firmware) ----------
if [[ "${DET_KV[evdev]}" != "yes" ]]; then
  log "evdev missing on device — installing via pip"

  # Bootstrap pip if it's missing (stock firmware ships no pip3, no ensurepip)
  if [[ -z "${DET_KV[pip]:-}" ]]; then
    log "pip missing on device — bootstrapping via get-pip.py…"
    GETPIP_HOST="$(mktemp)"
    if ! curl -sSL -o "$GETPIP_HOST" https://bootstrap.pypa.io/get-pip.py; then
      rm -f "$GETPIP_HOST"
      log_die "couldn't fetch get-pip.py from host. Check host internet + curl."
    fi
    "${SCP[@]}" -q "$GETPIP_HOST" "${SSH_USER}@${DEVICE}:/tmp/get-pip.py" \
      || { rm -f "$GETPIP_HOST"; log_die "scp of get-pip.py failed"; }
    rm -f "$GETPIP_HOST"
    "${SSH[@]}" "${SSH_USER}@${DEVICE}" \
      'python3 /tmp/get-pip.py --root-user-action=ignore 2>&1 | tail -3' \
      || log_die "get-pip.py failed on device. See /tmp/get-pip.py output above."
    log_ok "pip bootstrapped"
  else
    log_ok "pip present: ${DET_KV[pip]}"
  fi

  # Install evdev — builds a wheel from source on device (~10s on H700)
  log "Installing evdev (may take ~10s — building C extension on device)…"
  "${SSH[@]}" "${SSH_USER}@${DEVICE}" \
    'python3 -m pip install --root-user-action=ignore evdev 2>&1 | tail -3' \
    || log_die "pip install evdev failed."

  # Verify the C extension actually loads (catches Py3.8/3.10 ABI mismatches)
  "${SSH[@]}" "${SSH_USER}@${DEVICE}" 'python3 -c "from evdev import InputDevice, ecodes"' \
    || log_die "evdev install claimed success but C extension import failed (likely Python ABI mismatch)."
  log_ok "evdev installed (C extension loads)"
else
  log_ok "evdev present"
fi

# ---------- copy stack ----------
log "Copying stack to ${REMOTE_DIR}/…"
"${SSH[@]}" "${SSH_USER}@${DEVICE}" "mkdir -p ${REMOTE_DIR}"
"${SCP[@]}" -q \
  "${HERE}"/{main.py,bt_l2cap_v2.py,BluezProfile.py,BluezAgent.py,hid_descriptor.py,evdev_to_hid.py,evdev_reader.py,sdp_record_gamepad.xml,sdp_record_pnp.xml,set_did.py,requirements.txt} \
  "${SSH_USER}@${DEVICE}:${REMOTE_DIR}/" \
  || log_die "scp of stack files failed."
log_ok "Stack deployed"

# ---------- systemd units + bluetooth drop-in ----------
log "Installing systemd units…"
"${SSH[@]}" "${SSH_USER}@${DEVICE}" "mkdir -p /etc/systemd/system/bluetooth.service.d"
"${SCP[@]}" -q "$SERVICE_SRC" "${SSH_USER}@${DEVICE}:${SERVICE_DST}" \
  || log_die "scp of bt_gamepad.service failed."
"${SCP[@]}" -q "$DROPIN_SRC" "${SSH_USER}@${DEVICE}:${DROPIN_DST}" \
  || log_die "scp of bluetooth drop-in failed."

# Cleanup patched temp dropin if we made one
[[ -n "${TMPDROP:-}" && -f "$TMPDROP" ]] && rm -f "$TMPDROP"

"${SSH[@]}" "${SSH_USER}@${DEVICE}" 'systemctl daemon-reload'
log_ok "systemd units installed"

# ---------- restart bluetooth with the new drop-in ----------
log "Restarting bluetooth with --compat override…"
"${SSH[@]}" "${SSH_USER}@${DEVICE}" '
  systemctl restart bluetooth
  sleep 3
  if ! systemctl is-active --quiet bluetooth; then
    echo "bluetooth.service failed to start. Recent log:" >&2
    journalctl -u bluetooth -n 20 --no-pager >&2 || tail -20 /var/log/bluetoothd.log >&2 || true
    exit 1
  fi
' || log_die "bluetooth restart failed. Check /var/log/bluetoothd.log on device."
log_ok "bluetooth active with --compat"

# ---------- bring hci0 up (workaround for known boot race) ----------
log "Bringing hci0 up…"
"${SSH[@]}" "${SSH_USER}@${DEVICE}" '
  if ! hciconfig hci0 up 2>/dev/null; then
    echo "hciconfig hci0 up failed — chip may be in a stuck H5-sync state." >&2
    echo "Try the rfkill power-cycle recipe in docs/TROUBLESHOOTING.md." >&2
    exit 1
  fi
  hciconfig hci0 auth
  hciconfig hci0
' || log_die "hci0 bringup failed."
log_ok "hci0 up + auth"

# ---------- enable + start our service ----------
log "Enabling + starting bt_gamepad…"
"${SSH[@]}" "${SSH_USER}@${DEVICE}" '
  systemctl daemon-reload
  systemctl enable bt_gamepad
  systemctl restart bt_gamepad
  sleep 2
  if ! systemctl is-active --quiet bt_gamepad; then
    echo "bt_gamepad.service failed. Recent log:" >&2
    tail -40 /var/log/bt_gamepad.log >&2 || true
    exit 1
  fi
' || log_die "bt_gamepad failed to start. Check /var/log/bt_gamepad.log."
log_ok "bt_gamepad running"

# ---------- show status ----------
log "Status on device:"
"${SSH[@]}" "${SSH_USER}@${DEVICE}" '
  echo "---- hciconfig ----"
  hciconfig hci0
  echo "---- bdaddr ----"
  hcitool dev 2>/dev/null || true
  echo "---- services ----"
  systemctl --no-pager --lines=0 status bt_gamepad bluetooth 2>/dev/null | head -20 || true
'

# ---------- pair instructions ----------
DEVICE_BDADDR=$("${SSH[@]}" "${SSH_USER}@${DEVICE}" 'hcitool dev | tail -n +2 | awk "{print \$2}" | head -1')
echo
c_grn "=== Installed. ==="
echo
echo "Next step — pair from your host OS (Windows/macOS/Android/Linux):"
echo
echo "  1. Open Bluetooth settings."
echo "  2. Put the device in discoverable mode (it should already be):"
echo "       ssh ${SSH_USER}@${DEVICE} 'hciconfig hci0 leadv on'"
echo "  3. Look for a device named 'SlothOS Controller'."
[[ -n "$DEVICE_BDADDR" ]] && echo "     Its address will be: ${DEVICE_BDADDR}"
echo "  4. Pair. The agent on-device auto-confirms (DisplayYesNo)."
echo "  5. Open joy.cpl (Windows) or gamepad-tester.com to verify input."
echo
echo "If pair fails or buttons don't register, read docs/TROUBLESHOOTING.md."
echo "Logs: ssh ${SSH_USER}@${DEVICE} 'tail -f /var/log/bt_gamepad.log /var/log/bluetoothd.log'"
echo
echo "To uninstall:  $0 --uninstall ${DEVICE} ${SSH_USER}"
