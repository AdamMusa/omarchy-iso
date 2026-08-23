#!/bin/bash
#
# Every boot entry that loads the live system must pin copytoram=n. The
# archiso hook defaults to copytoram=auto, which copies the airootfs to RAM
# and unmounts the boot medium whenever the image is under 4 GiB and the
# machine has RAM to spare -- the root image the installer streams from that
# medium then vanishes (seen on a ThinkPad X200s booting from USB; the QEMU
# tests never hit it because they boot the ISO as an optical drive).

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

entries=$(grep -rhE '^\s*(APPEND|linux|options)\s.*archisosearchuuid=' \
  configs/syslinux/archiso_sys-linux.cfg configs/grub/grub.cfg configs/efiboot/loader/entries)

count=$(printf '%s\n' "$entries" | wc -l)
[ "$count" -ge 5 ] || { echo "expected at least 5 boot entries, found $count"; exit 1; }

fail=0
while IFS= read -r line; do
  if ! grep -qE '(^|[[:space:]])copytoram=n([[:space:]]|$)' <<<"$line"; then
    echo "boot entry without copytoram=n: $line"
    fail=1
  fi
done <<<"$entries"

[ "$fail" -eq 0 ] && echo "ok: $count boot entries pin copytoram=n"
exit "$fail"
