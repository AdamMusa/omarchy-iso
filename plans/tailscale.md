# Tailscale Autoinstall Plan

## Goal

Let an autoinstall drive carry a Tailscale auth key so the installed machine
joins the tailnet on its own. Combined with `authorized_keys`, this completes
the disposable-VM story: create VM, boot, walk away, `ssh user@machine` over
the tailnet from anywhere — no LAN discovery, no port forwarding.

## Product Requirements

- Same trigger philosophy as the rest of autoinstall: presence of a
  `tailscale_authkey` file on the `cidata` drive, no new mode flag. No file,
  no change — byte-identical behavior to today.
- The join happens when the network is available, not when the installer runs.
- No package installs from the network on first boot.
- The auth key does not persist on the installed system after a successful join.

---

## Chosen Architecture

### The package is bundled, not fetched

The `tailscale` package rides in the offline mirror (~10MB on the ISO) and is
installed during the ordinary package phase — but only when the auth key file
is present, so a stock install stays stock.

The alternative — installing from the network on first boot — was rejected
outright. A bare `pacman -Sy tailscale` on a fresh machine is a partial
upgrade waiting to corrupt something, and the "correct" version, `pacman -Syu
tailscale`, means an unattended full system upgrade racing the user's first
login (potentially swapping the kernel out on a machine that has booted once).
It would also fight Omarchy's own update flow and make the join depend on
mirror availability. The offline mirror is a coherent snapshot; installing
from it keeps the target consistent and the install deterministic and
offline-capable.

### The join is a first-boot unit, not an install step

`tailscale up` needs a running tailscaled, and there is no systemd in the
chroot. So the installer only stages: the key at `/etc/tailscale/authkey`
(root, `0600`), `tailscaled.service` enabled, and a oneshot
`omarchy-tailscale-join.service` that runs `tailscale up --auth-key
file:/etc/tailscale/authkey` on first boot.

"When the network is available" lives in the unit, not the installer:
`After=network-online.target` for ordering, plus an in-unit retry loop
(`until tailscale up …; do sleep 10; done` under `TimeoutStartSec=10min`),
because network-online can be reached before there is real connectivity.
The systemd success/failure semantics do the rest for free:

- On success, `ExecStartPost` removes the key and disables the unit — the key
  never outlives the join, and the unit never runs again.
- On failure (no network, tailscaled not ready, key rejected), ExecStartPost
  is skipped: the key stays, the unit stays enabled, and the join retries on
  every boot until it succeeds. A machine installed offline joins whenever it
  first gets connectivity.

The machine appears on the tailnet under the hostname from
`user_configuration.json` — nothing extra to configure.

### Firewall

`ufw allow in on tailscale0`, the same write-the-rule-then-ignore-the-chroot-
exit-code dance as `configure_ssh_access` (ufw cannot reach netfilter in a
chroot but records the rule in `user.rules`, which is what first boot loads).
Without it the node joins and is then unreachable over the tailnet — worse
than not joining.

### Secrets

Same stance the autoinstall plan takes for the LUKS passphrase: whoever
builds the `cidata` drive owns the key on it. Docs recommend a reusable,
pre-authorized (tagged) key so one drive image serves N machines, and note
that ephemeral keys fit disposable VMs. No validation of the key beyond
"exactly one non-comment line" — Tailscale key formats are theirs to change.

---

## Implementation

- `builder/archinstall.packages`: add `tailscale` so it lands in the offline
  mirror on every build.
- `omarchy-cidata-load`: add `tailscale_authkey` to the optional copy list.
- `.automated_script.sh`: pass `--tailscale-authkey-file /root/tailscale_authkey`
  (always; the orchestrator no-ops when the file is absent, like
  `--authorized-keys-file`).
- `omarchy-iso-install`: parse the flag into
  `OMARCHY_INSTALL_TAILSCALE_AUTHKEY_FILE`.
- `context.py`: `tailscale_authkey_path: Path | None` via `_optional_path`.
- `phases_impl.py`:
  - `arch_install_system`: when the key is present, add `tailscale` to the
    additional packages while the offline mirror is still bind-mounted.
  - `configure_tailscale(ctx)`: no-op when the path is `None`; otherwise
    stage key + unit, enable services, open ufw. Fails loudly on an empty or
    ambiguous key file and when the tailscale binary is missing from the
    target (an ISO built before the package was bundled).
- `main.py`: register `("Configuring Tailscale", configure_tailscale)` after
  `configure_ssh_access`.
- `README.md`: file-table row + a Tailscale paragraph in the Autoinstall
  section.
- Tests: `tailscale_authkey` cases in `cidata-load-test.sh`;
  `test_configure_tailscale.py` mirroring the SSH phase tests.

## Acceptance criteria

- Drive with `tailscale_authkey`: machine appears on the tailnet under its
  configured hostname after first boot with network; `/etc/tailscale/authkey`
  is gone; `omarchy-tailscale-join.service` is disabled.
- First boot without network: no failure surfaced to the user; the join
  happens on the first boot that has connectivity.
- Drive without the file: no tailscale package on the target, no unit, no
  ufw rule — indistinguishable from today.
- Empty or multi-line key file: install stops in the Tailscale phase with a
  visible error.
- Interactive installs: unaffected; the flag is passed but the file never
  exists.
