#!/bin/bash
#
# Every boot entry that loads the live system must pin copytoram=n. The
# archiso hook defaults to copytoram=auto, which copies the airootfs to RAM
# and unmounts the boot medium whenever the image is under 4 GiB and the
# machine has RAM to spare -- the root image the installer streams from that
# medium then vanishes (seen on a ThinkPad X200s booting from USB; the QEMU
# tests never hit it because they boot the ISO as an optical drive).
#
# loopback.cfg counts: mkarchiso ships it at /boot/grub/loopback.cfg, which is
# how Ventoy and a hand-written GRUB entry boot the ISO as a file on a disk.
# That path loop-mounts the image, so archisodevice is /dev/loopN rather than
# /dev/sr*, and the auto rule fires there too. It is matched on archisobasedir
# rather than archisosearchuuid because it finds the medium by img_dev/img_loop.

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

entries=$(grep -rhE '^\s*(APPEND|linux|options)\s.*archisobasedir=' \
  configs/syslinux/archiso_sys-linux.cfg configs/grub/grub.cfg \
  configs/grub/loopback.cfg configs/efiboot/loader/entries)

count=$(printf '%s\n' "$entries" | wc -l)
[ "$count" -ge 7 ] || { echo "expected at least 7 boot entries, found $count"; exit 1; }

fail=0
while IFS= read -r line; do
  if ! grep -qE '(^|[[:space:]])copytoram=n([[:space:]]|$)' <<<"$line"; then
    echo "boot entry without copytoram=n: $line"
    fail=1
  fi
done <<<"$entries"

[ "$fail" -eq 0 ] && echo "ok: $count boot entries pin copytoram=n"
exit "$fail"
