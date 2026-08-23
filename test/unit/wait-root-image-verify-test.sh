#!/bin/bash
#
# omarchy-wait-root-image-verify is the single gate both disk-touching paths
# clear before formatting: the orchestrator (full-disk) and the configurator
# (free-space). It collects the boot-time hasher's verdict, waiting if it is
# still running and starting it if it never did. This drives it with a stubbed
# systemctl/findmnt/lsblk/journalctl and a fake boot medium on PATH, so the
# wait/verdict logic is checked without an ISO.

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
HELPER="$ROOT/configs/airootfs/usr/local/bin/omarchy-wait-root-image-verify"

fails=0
check() { # desc, expected_rc, actual_rc, [needle in output], [output]
  local desc=$1 want=$2 got=$3 needle=${4:-} out=${5:-}
  if [[ $got != "$want" ]]; then
    echo "FAIL: $desc (rc want=$want got=$got)"; fails=1; return
  fi
  if [[ -n $needle && $out != *"$needle"* ]]; then
    echo "FAIL: $desc (missing '$needle' in: $out)"; fails=1; return
  fi
  echo "ok: $desc"
}

# A sandbox with a fake boot medium and stub tools on PATH. ACTIVE_SEQ is the
# newline-separated ActiveState values `systemctl show ... ActiveState` returns
# on successive calls (last one repeats); LOADSTATE and START_RC tune the rest.
run_helper() { # loadstate, active_seq, start_rc  ->  sets RC and OUT
  local loadstate=$1 active_seq=$2 start_rc=$3
  local box; box=$(mktemp -d)
  mkdir -p "$box/bin" "$box/medium/arch/x86_64" "$box/sys/block/sdz/queue"
  : >"$box/medium/arch/x86_64/omarchy-root.btrfs"
  : >"$box/medium/arch/x86_64/omarchy-root.btrfs.sha256"
  echo "none mq-deadline kyber [bfq]" >"$box/sys/block/sdz/queue/scheduler"
  printf '%s\n' "$active_seq" >"$box/active_seq"

  cat >"$box/bin/findmnt" <<EOF
#!/bin/bash
echo /dev/sdz1
EOF
  cat >"$box/bin/lsblk" <<EOF
#!/bin/bash
echo sdz
EOF
  cat >"$box/bin/journalctl" <<'EOF'
#!/bin/bash
echo "omarchy-root.btrfs: FAILED"
EOF
  # systemctl show -p LoadState|ActiveState --value ; systemctl start ...
  cat >"$box/bin/systemctl" <<EOF
#!/bin/bash
box="$box"; start_rc=$start_rc; loadstate="$loadstate"
if [[ \$1 == show ]]; then
  case "\$*" in
    *LoadState*)  echo "\$loadstate" ;;
    *ActiveState*)
      # pop the first remaining line; keep the last forever
      mapfile -t seq <"\$box/active_seq"
      echo "\${seq[0]}"
      if ((\${#seq[@]} > 1)); then printf '%s\n' "\${seq[@]:1}" >"\$box/active_seq"; fi
      ;;
  esac
  exit 0
fi
if [[ \$1 == start ]]; then exit \$start_rc; fi
exit 0
EOF
  chmod +x "$box"/bin/*

  # Point the helper's hardcoded /run/archiso/bootmnt and /sys at the sandbox
  # by running it through a tiny shim that rewrites those roots.
  local shim="$box/run-helper"
  sed -e "s#/run/archiso/bootmnt#$box/medium#g" \
      -e "s#/sys/block#$box/sys/block#g" \
      "$HELPER" >"$shim"
  chmod +x "$shim"

  set +e
  OUT=$(PATH="$box/bin:$PATH" bash "$shim" 2>&1)
  RC=$?
  set -e
  rm -rf "$box"
}

# Already verified before we look: pass, and the scheduler line is emitted.
run_helper loaded "active" 0
check "active unit passes" 0 "$RC" "scheduler: none mq-deadline kyber [bfq]" "$OUT"

# Still hashing, then completes: waits, then passes.
run_helper loaded $'activating\nactivating\nactive' 0
check "waits for a running unit then passes" 0 "$RC" "waiting for" "$OUT"

# Hash failed: corrupt medium, re-flash message leads.
run_helper loaded "failed" 0
check "failed unit is a corrupt medium" 1 "$RC" "install medium is corrupt: re-flash it" "$OUT"

# Never started and start leaves it inactive: cannot verify.
run_helper loaded "inactive" 0
check "unit that will not run fails" 1 "$RC" "did not run" "$OUT"

# Unit not on this system.
run_helper not-found "inactive" 0
check "missing unit fails" 1 "$RC" "not on this live system" "$OUT"

[[ $fails -eq 0 ]] && echo "ok: omarchy-wait-root-image-verify gate behaves"
exit "$fails"
