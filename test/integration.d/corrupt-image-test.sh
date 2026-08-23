#!/bin/bash
#
# A corrupt install medium is refused before the disk is touched. The root
# image ships on the ISO with its sha256 beside it; omarchy-root-image-verify
# checks it at boot and the installer's pre-flight phase takes that verdict.
# Flip one digit of the recorded checksum on a copy of the ISO, autoinstall
# from it, and assert: the unit fails, the install halts in "Preparing install
# target" telling the user to re-flash, nothing after that phase ran, and the
# target disk still has no partition table.
#
# Boots the ISO itself, not the installed base image, so it needs no base.

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

CORRUPT_ISO="$BASE_DIR/corrupt.iso"
STREAM_SUM=arch/x86_64/omarchy-root.btrfs.sha256
VERIFY_UNIT=omarchy-root-image-verify.service
STATE=/run/omarchy-install/state.json

# ------------------------------------------------------------------ fixture

# A copy of the ISO with the first hex digit of the recorded checksum flipped.
# ISO9660 carries no per-file integrity data, so patching the byte in place
# leaves everything else on the medium intact; cp --reflink makes the copy
# free on btrfs.
corrupt_iso() {
  local digest offset flipped patched

  digest=$(bsdtar -xOf "$ISO" "$STREAM_SUM" | cut -d' ' -f1)
  [[ $digest =~ ^[0-9a-f]{64}$ ]] || { echo "no checksum on the ISO at $STREAM_SUM" >&2; return 1; }

  log "Copying the ISO and corrupting its root image checksum"
  rm -f "$CORRUPT_ISO"
  cp --reflink=auto "$ISO" "$CORRUPT_ISO"
  offset=$(grep -boa -m1 "$digest" "$CORRUPT_ISO" | cut -d: -f1)
  [[ -n $offset ]] || { echo "checksum not found in the ISO image" >&2; return 1; }
  flipped=1
  [[ ${digest:0:1} == 1 ]] && flipped=0
  printf '%s' "$flipped" | dd of="$CORRUPT_ISO" bs=1 seek="$offset" conv=notrunc status=none

  patched=$(bsdtar -xOf "$CORRUPT_ISO" "$STREAM_SUM" | cut -d' ' -f1)
  [[ $patched =~ ^[0-9a-f]{64}$ && $patched != "$digest" ]] ||
    { echo "patch did not take: $patched" >&2; return 1; }
  log "Recorded $digest, now $patched"
}

# -------------------------------------------------------------------- phases

install_from_corrupt_medium() {
  [[ -f $SSH_KEY ]] || ssh-keygen -t ed25519 -N "" -q -C "omarchy-integration" -f "$SSH_KEY"
  detect_packages
  build_cidata

  qemu-img create -f qcow2 "$RUN_DIR/disk.qcow2" 40G >/dev/null
  cp "$OVMF_VARS_TEMPLATE" "$RUN_DIR/OVMF_VARS.4m.fd"
  ACTIVE_OVMF="$RUN_DIR/OVMF_VARS.4m.fd"

  log "Autoinstalling from the corrupt medium (headless)"
  start_vm "$RUN_DIR/disk.qcow2" "$RUN_DIR/serial.log" \
    -drive "file=$CORRUPT_ISO,media=cdrom,if=none,format=raw,id=cdrom0" \
    -device ide-cd,drive=cdrom0,bootindex=2 \
    -drive "file=$CIDATA_IMG,format=raw,if=none,id=cidata" \
    -device usb-storage,drive=cidata

  bootstrap_live_root_ssh

  log "Waiting for the installer to stop"
  local waited=0
  until ssh_live_root 'grep -q "installer child exited" /var/log/omarchy-install.log' 2>/dev/null; do
    if ! vm_running; then
      echo "VM exited while waiting for the installer" >&2
      return 1
    fi
    if ((waited >= 600)); then
      capture_console "failure-installer-timeout"
      echo "Timed out waiting for the installer to stop" >&2
      return 1
    fi
    sleep 5
    ((waited += 5))
  done
  sleep 2
  capture_console "success-installer-stopped"
  ssh_live_root "cat /var/log/omarchy-install.log" >"$RUN_DIR/omarchy-install.log" 2>/dev/null || true
  ssh_live_root "cat $STATE" >"$RUN_DIR/state.json" 2>/dev/null || true
  ssh_live_root "journalctl -b -u $VERIFY_UNIT -o short-precise --no-pager" >"$RUN_DIR/verify-unit.journal" 2>/dev/null || true
}

screen_shows() {
  ocr_screen | grep -qi "$1"
}

assert_refused() {
  check "verify unit failed on the corrupt checksum" \
    ssh_live_root "systemctl show -p Result --value $VERIFY_UNIT | grep -qx exit-code"
  check "install halted in the pre-flight phase" \
    ssh_live_root "jq -e '[.phases[] | select(.status == \"failed\") | .name] == [\"Preparing install target\"]' $STATE"
  check "the error tells the user to re-flash the medium" \
    ssh_live_root "jq -r '.phases[] | select(.status == \"failed\") | .error' $STATE | grep -q 'install medium is corrupt: re-flash it'"
  check "the error carries sha256sum's verdict" \
    ssh_live_root "jq -r '.phases[] | select(.status == \"failed\") | .error' $STATE | grep -q 'did NOT match'"
  check "nothing ran after the pre-flight phase" \
    ssh_live_root "jq -e '[.phases[] | select(.status == \"ok\") | .name] == [\"Preparing live environment\"]' $STATE"
  check "target disk has no partition table" \
    ssh_live_root "! lsblk -rno TYPE /dev/vda | grep -qx part && ! blkid /dev/vda"
  check "dashboard shows the installation stopped" \
    screen_shows "installation stopped"
  check "dashboard shows the re-flash advice" \
    screen_shows "re-flash"
}

# ---------------------------------------------------------------------- main

corrupt_iso
install_from_corrupt_medium
assert_refused
rm -f "$CORRUPT_ISO"
finish
