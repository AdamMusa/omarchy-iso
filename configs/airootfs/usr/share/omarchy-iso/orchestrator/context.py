"""Install context: parsed configurator output, invocation paths, and a
mutable `state` dict for objects that live across phases (e.g., the
archinstall config handler and mirror list handler)."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class InstallContext:
    config_path: Path
    creds_path: Path
    full_name: str
    email: str
    encrypt: bool
    authorized_keys_path: Path | None
    tailscale_authkey_path: Path | None

    user_configuration: dict
    user_credentials: dict
    arch_config_path: Path
    omarchy_install: dict[str, Any]
    oem: bool = False

    target: Path = Path("/mnt")
    omarchy_path: Path = Path("/usr/share/omarchy")
    state_dir: Path = Path("/run/omarchy-install")
    log_path: Path = Path("/var/log/omarchy-install.log")
    target_log_path: Path = Path("/mnt/var/log/omarchy-install.log")

    # Mutable per-run state shared across phases (e.g., 'arch_config_handler',
    # 'mirror_handler'). Phases populate as needed; later phases read.
    state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "InstallContext":
        config_str = os.environ.get("OMARCHY_INSTALL_CONFIG")
        creds_str = os.environ.get("OMARCHY_INSTALL_CREDS")
        if not config_str or not creds_str:
            raise RuntimeError("OMARCHY_INSTALL_CONFIG and OMARCHY_INSTALL_CREDS must be set")

        config_path = Path(config_str)
        creds_path = Path(creds_str)
        user_configuration = json.loads(config_path.read_text())
        omarchy_install = user_configuration.get("omarchy_install") or _default_omarchy_install(user_configuration)

        # OEM mode: the whole system installs but user creation is deferred to
        # first boot. Selected by the configurator (omarchy_install.oem) or by
        # an `oem` marker file on an autoinstall drive, which also replaces the
        # user_credentials.json requirement.
        oem_marker = _optional_path(os.environ.get("OMARCHY_INSTALL_OEM_FILE"))
        oem = bool(omarchy_install.get("oem")) or oem_marker is not None
        omarchy_install["oem"] = oem

        if creds_path.exists():
            user_credentials = json.loads(creds_path.read_text())
        elif oem:
            user_credentials = {"users": []}
        else:
            raise RuntimeError(f"credentials file missing: {creds_path}")

        arch_configuration = dict(user_configuration)
        arch_configuration.pop("omarchy_install", None)
        state_dir = Path(os.environ.get("OMARCHY_INSTALL_STATE_DIR", "/run/omarchy-install"))
        state_dir.mkdir(parents=True, exist_ok=True)

        if oem:
            # Encrypted OEM installs have no user password to protect LUKS
            # with. Generate a throwaway passphrase (staged for first-boot
            # re-key by the stage_oem_state phase) and hand it to archinstall
            # through both places it may look: the disk_encryption block and
            # the credentials file.
            _inject_oem_encryption_password(arch_configuration, user_credentials)
            creds_path = state_dir / "oem-user_credentials.json"
            creds_path.write_text(json.dumps(user_credentials, indent=2) + "\n")
            creds_path.chmod(0o600)

        arch_config_path = state_dir / "archinstall-user_configuration.json"
        arch_config_path.write_text(json.dumps(arch_configuration, indent=2) + "\n")

        ctx = cls(
            config_path=config_path,
            creds_path=creds_path,
            full_name=_read_text(os.environ.get("OMARCHY_INSTALL_FULL_NAME_FILE")),
            email=_read_text(os.environ.get("OMARCHY_INSTALL_EMAIL_FILE")),
            encrypt=_read_text(os.environ.get("OMARCHY_INSTALL_ENCRYPT_FILE")).lower() in ("true", "yes", "1"),
            authorized_keys_path=_optional_path(os.environ.get("OMARCHY_INSTALL_AUTHORIZED_KEYS_FILE")),
            tailscale_authkey_path=_optional_path(os.environ.get("OMARCHY_INSTALL_TAILSCALE_AUTHKEY_FILE")),
            user_configuration=user_configuration,
            user_credentials=user_credentials,
            arch_config_path=arch_config_path,
            omarchy_install=omarchy_install,
            oem=oem,
            state_dir=state_dir,
        )
        disk_config = user_configuration.get("disk_config", {})
        target_mount = omarchy_install.get("target_mount") or disk_config.get("mountpoint")
        if target_mount:
            ctx.target = Path(target_mount)
        return ctx

    @property
    def username(self) -> str:
        users = self.user_credentials.get("users") or []
        if users:
            return users[0]["username"]
        if self.oem:
            return ""
        raise RuntimeError("user_credentials.json contains no users")

    @property
    def mode(self) -> str:
        if mode := self.omarchy_install.get("mode"):
            return mode
        cfg_type = self.user_configuration.get("disk_config", {}).get("config_type")
        return "protected" if cfg_type == "pre_mounted_config" else "full_disk"

    @property
    def is_protected(self) -> bool:
        return self.mode == "protected"


def _inject_oem_encryption_password(arch_configuration: dict, user_credentials: dict) -> None:
    """Ensure an encrypted OEM install has a LUKS passphrase archinstall can
    use. Reuse one the input already carries (an autoinstall rig may supply
    its own); otherwise generate a throwaway. Mutates the disk_encryption
    block in place — arch_configuration shares it with user_configuration, so
    the stage_oem_state phase can read the effective passphrase back."""
    disk_encryption = (arch_configuration.get("disk_config") or {}).get("disk_encryption")
    if disk_encryption is None:
        return

    password = disk_encryption.get("encryption_password") or user_credentials.get("encryption_password")
    if not password:
        password = secrets.token_urlsafe(24)

    disk_encryption["encryption_password"] = password
    user_credentials["encryption_password"] = password


def _default_omarchy_install(user_configuration: dict) -> dict[str, Any]:
    disk_config = user_configuration.get("disk_config", {})
    mode = "protected" if disk_config.get("config_type") == "pre_mounted_config" else "full_disk"
    return {
        "mode": mode,
        "target_mount": disk_config.get("mountpoint") or "/mnt",
        "boot": {
            "esp_mount": "/boot",
            "esp_path": "/EFI/limine",
            "efi_binary": "limine_x64.efi",
            "enable_fallback": mode == "full_disk",
        },
        "storage": {},
    }


def _optional_path(path: str | None) -> Path | None:
    """A path the caller always passes but that need not exist. The live ISO
    passes --authorized-keys-file on every install; only autoinstall drives put
    a file there."""
    if not path:
        return None
    p = Path(path)
    return p if p.exists() else None


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text().strip()
