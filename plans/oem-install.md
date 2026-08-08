# OEM Install + Factory Reset Plan

## Goal

An install mode that installs the entire system but defers user account setup to
first boot. On first power-on the machine asks for the user, creates it, and
lands in the already-installed system — the classic OEM out-of-box experience.
The same machinery doubles as a "reset computer" option for wiping a machine
before selling it.

## Core idea: "OEM state" as a first-class system state

Define one canonical state — *fully installed, no user, setup pending* — and
make both paths produce it:

- **ISO OEM mode** ends the install in OEM state instead of creating the user.
- **Reset** returns a running machine to OEM state.

OEM state is: a fully installed system plus `/var/lib/omarchy/oem/` containing:

| File | Purpose |
|------|---------|
| `pending` | Flag that arms the first-boot setup service |
| `packages/node-v*.tar.gz` | Node tarball copied off the ISO's `/opt/packages` (~30MB) so `omarchy-finalize-user` works offline at first boot |
| `groups` | Deferred `usermod -aG` groups written by system setup (see below) |
| `luks-key` | Throwaway LUKS passphrase for encrypted installs, staged for auto-unlock until first-boot re-key |

## Why the seam is nearly clean already

Everything in the orchestrator through `finalize_limine_boot` is
user-independent except five touch points:

1. `installer.create_users` — useradd + password (root gets the same password).
2. Three `usermod -aG` calls inside `omarchy-setup-system`'s scripts
   (`config/docker.sh`, `hardware/input-group.sh`, `hardware/apple/fix-t2.sh` —
   docker/input/video). This is the *only* reason `omarchy-setup-system`
   requires the user to exist (its getent check).
3. `run_chroot_finalizer` → `omarchy-finalize-user` as the user. Already
   offline-safe: `/etc/skel` does the heavy seeding at useradd time,
   `omarchy-mise-install` writes lazy wrappers, and the only external artifact
   is the bundled Node tarball from `/opt/packages` (`install/user/mise.sh`).
4. `configure_login` — SDDM `state.conf` and the encrypted-install autologin.
5. Encryption — the user's password *is* the LUKS passphrase.

Nothing in hardware setup, Limine, hibernation, snapper, or package
installation cares who the user is.

## First boot: `omarchy-oem-setup.service`

Ships in the omarchy runtime package (so reset can arm it without the ISO):

- `ConditionPathExists=/var/lib/omarchy/oem/pending`
- `Before=display-manager.service`, conflicts `getty@tty1`, TUI on tty1.
- Runs the user form extracted from the configurator (Step 2: username,
  password, full name, email). The configurator is bash+gum, so the form moves
  into a runtime-shipped `omarchy-oem-setup` script that reuses the exact ISO
  look (logo, Tokyo Night VT palette).

Then it does what the ISO would have done:

1. `useradd` with wheel + the groups from `/var/lib/omarchy/oem/groups`,
   set user + root password.
2. `omarchy-finalize-user` as the user, with a setup context that points at the
   stashed Node tarball (a sibling of the existing `iso-chroot` context).
3. The `configure_login` writes: SDDM `state.conf`, autologin for encrypted
   installs.
4. If `luks-key` exists: `cryptsetup luksChangeKey` to the user's password,
   delete the stash, remove the auto-unlock keyfile, rebuild initramfs.
5. Git identity from the form (the `OMARCHY_USER_NAME`/`OMARCHY_USER_EMAIL`
   path in `install/user/git.sh`).
6. Remove `/var/lib/omarchy/oem/`, hand off to SDDM.

The user lands in their session exactly like a normal install.

## Encryption: keep it, Pop!_OS-style

Do not force OEM installs unencrypted. Install encrypted with a generated
throwaway passphrase, stage a keyfile so boot auto-unlocks during the OEM
window, and have first-boot setup re-key to the user's chosen password
(step 4 above). Limine and mkinitcpio don't care what the passphrase is. This
preserves encrypted-by-default without the machine builder ever knowing the end
user's password.

## ISO-side changes (this repo)

- **Configurator**: an OEM choice in the install-mode step that skips Step 2
  (user), plus a cidata `oem` marker file that replaces the
  `user_credentials.json` requirement — that's what actual OEM imaging rigs
  will use via autoinstall.
- **Orchestrator**: `oem: true` in the `omarchy_install` JSON. Swaps the three
  user phases (`create_users` inside `arch_install_system`,
  `run_chroot_finalizer`, the user parts of `configure_login`) for a
  "stage OEM state" phase that writes `/var/lib/omarchy/oem/` and enables
  `omarchy-oem-setup.service` in the target. `InstallContext.username` needs an
  OEM-safe fallback (creds are absent).
- **Factory snapshot**: at the end of *every* install (not just OEM), take a
  read-only snapshot of `@` kept at the top level as `@factory` — outside
  snapper's `.snapshots`, so cleanup timers and the Limine snapshot menu never
  touch it. Zero bytes at creation; grows only with drift. This is what makes
  reset a true factory reset.

## Runtime-side changes (omarchy repo)

- `omarchy-oem-setup` + `omarchy-oem-setup.service` (the OOBE above).
- `omarchy-system-factory-reset` (below).
- Relax `omarchy-setup-system`: when the install user doesn't exist yet, the
  three `usermod -aG` scripts append to `/var/lib/omarchy/oem/groups` instead
  of failing the getent check.

## Reset: `omarchy-system-factory-reset`

Behind a very explicit confirmation (typed, not y/n):

1. Point the default subvolume at a writable snapshot of `@factory`; stage a
   next-boot wipe unit.
2. On reboot: recreate `@home` (subvolume delete/create — instant, no rm -rf),
   clear `@log`, delete snapper snapshots, reset machine-id, SSH host keys,
   NetworkManager connections, Tailscale state, SDDM state; run `fstrim`.
3. Re-arm OEM state: `pending` flag, `groups`, Node tarball (kept from install
   or re-fetched into `@factory` at install time), and on encrypted machines a
   throwaway LUKS re-key — the buyer never needs the seller's passphrase.

Next boot is the same OOBE as a fresh OEM install. User-installed packages and
/etc drift are gone, which is what "wipe and sell" actually promises.

Machines installed before `@factory` existed get a degraded reset — keep the
current system, wipe users/state only — with that caveat surfaced in the
confirmation.

### Honest limitations (document these)

- On unencrypted disks this is deletion, not forensic erasure. `fstrim` helps
  on SSDs; offer `cryptsetup reencrypt` or `blkdiscard` + reinstall for the
  paranoid.
- `luksChangeKey` changes the passphrase but not the volume key; freed extents
  remain readable to someone with raw-device access and the new passphrase.
  Same remedy as above.
- A reset machine boots with day-one packages until it updates — same as a
  fresh install.

## Sequencing

The OOBE lives in the omarchy runtime package, but the ISO's OEM phase depends
on it existing. So:

1. Ship the runtime side first (`omarchy-oem-setup`, service, reset command,
   `omarchy-setup-system` relaxation).
2. ISO OEM mode then asserts the service file exists in the target before
   offering the mode.
3. Factory snapshot can land with either half; reset requires both.

## Validation

- VM: OEM install (encrypted + unencrypted) → reboot → OOBE → session; verify
  LUKS passphrase is the user's, throwaway key gone, groups applied,
  `omarchy-finalize-user` state marker present.
- VM: normal install → use system → `omarchy-system-factory-reset` → OOBE as new
  user; verify no trace of prior user (home, NM connections, tailscale,
  machine-id, host keys, snapper snapshots).
- Autoinstall: cidata drive with `oem` marker and no credentials → unattended
  OEM install.
- Negative: OEM ISO built against a runtime package lacking the service fails
  the install with a clear error, not a user-less boot loop.
