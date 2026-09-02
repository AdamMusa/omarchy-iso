#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
DROP_IN="$ROOT/configs/airootfs/etc/systemd/system/cloud-init-main.service.d/omarchy-console.conf"
CLOUD_CFG="$ROOT/configs/airootfs/etc/cloud/cloud.cfg.d/99-omarchy-installer-console.cfg"

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

[[ -f $CLOUD_CFG ]] || {
  echo "FAIL: cloud-init has no installer console configuration" >&2
  exit 1
}

grep -Eq '^no_ssh_fingerprints:[[:space:]]*true$' "$CLOUD_CFG" || {
  echo "FAIL: authorized-key fingerprints can still be written to the console" >&2
  exit 1
}

grep -Eq '^[[:space:]]+emit_keys_to_console:[[:space:]]*false$' "$CLOUD_CFG" || {
  echo "FAIL: SSH host keys can still be written directly to the console" >&2
  exit 1
}

echo "ok: cloud-init cannot write over the installer dashboard"
