#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
DROP_IN="$ROOT/configs/airootfs/etc/systemd/system/cloud-init-main.service.d/omarchy-console.conf"

[[ -f $DROP_IN ]] || {
  echo "FAIL: cloud-init main has no Omarchy console drop-in" >&2
  exit 1
}

grep -qx 'StandardOutput=journal' "$DROP_IN" || {
  echo "FAIL: cloud-init stdout is not confined to the journal" >&2
  exit 1
}

grep -qx 'StandardError=journal' "$DROP_IN" || {
  echo "FAIL: cloud-init stderr is not confined to the journal" >&2
  exit 1
}

if grep -Eq '^Standard(Output|Error)=.*console' "$DROP_IN"; then
  echo "FAIL: cloud-init output still names the installer console" >&2
  exit 1
fi

echo "ok: cloud-init cannot write over the installer dashboard"
