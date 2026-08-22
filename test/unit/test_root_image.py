"""Unit tests for the root image install path in the orchestrator.

Covers the pre-flight checks prepare_install_target runs before the disk is
touched (stream present and matching its checksum, a disk layout the image
can land on) and the destructive subvolume dance in _install_root_image,
asserted on the subprocess calls against a temp target.
"""

import hashlib
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs/airootfs/usr/share/omarchy-iso"))

sys.modules.setdefault(
    "orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter")
)

from orchestrator import phases_impl  # noqa: E402


def btrfs_root_layout(name="@", mountpoint="/"):
    return {
        "config_type": "default_layout",
        "device_modifications": [
            {
                "device": "/dev/vda",
                "wipe": True,
                "partitions": [
                    {"fs_type": "fat32", "mountpoint": "/boot", "btrfs": []},
                    {
                        "fs_type": "btrfs",
                        "mountpoint": None,
                        "btrfs": [
                            {"name": name, "mountpoint": mountpoint},
                            {"name": "@home", "mountpoint": "/home"},
                        ],
                    },
                ],
            }
        ],
    }


class VerifyRootImageLayoutTest(unittest.TestCase):
    def test_btrfs_root_subvolume_passes(self):
        phases_impl.verify_root_image_layout(btrfs_root_layout())
        phases_impl.verify_root_image_layout(btrfs_root_layout(name="/@"))

    def test_lvm_rejected(self):
        layout = btrfs_root_layout()
        layout["lvm_config"] = {"config_type": "default", "vol_groups": []}
        with self.assertRaisesRegex(RuntimeError, "LVM"):
            phases_impl.verify_root_image_layout(layout)

    def test_root_not_on_at_subvolume_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "@ subvolume"):
            phases_impl.verify_root_image_layout(btrfs_root_layout(name="root"))

    def test_ext4_root_rejected(self):
        layout = {
            "device_modifications": [
                {"partitions": [{"fs_type": "ext4", "mountpoint": "/", "btrfs": []}]}
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "@ subvolume"):
            phases_impl.verify_root_image_layout(layout)

    def test_empty_config_rejected(self):
        with self.assertRaises(RuntimeError):
            phases_impl.verify_root_image_layout({})


class VerifyRootImageStreamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.stream = self.dir / "omarchy-root.btrfs"
        self.checksum = self.dir / "omarchy-root.btrfs.sha256"
        self.ctx = types.SimpleNamespace(state_dir=self.dir / "state")

        patch = mock.patch.object(phases_impl, "ROOT_IMAGE_STREAM_CANDIDATES", (self.stream,))
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("info", "_write_phase_progress"):
            patch = mock.patch.object(phases_impl, name)
            patch.start()
            self.addCleanup(patch.stop)

    def write_stream(self, data=b"btrfs-stream" * 1000):
        self.stream.write_bytes(data)
        self.checksum.write_text(f"{hashlib.sha256(data).hexdigest()}  {self.stream.name}\n")

    def test_matching_checksum_passes(self):
        self.write_stream()
        phases_impl.verify_root_image_stream(self.ctx)

    def test_missing_stream(self):
        with self.assertRaisesRegex(RuntimeError, "stream missing"):
            phases_impl.verify_root_image_stream(self.ctx)

    def test_missing_checksum(self):
        self.stream.write_bytes(b"x")
        with self.assertRaisesRegex(RuntimeError, "checksum missing"):
            phases_impl.verify_root_image_stream(self.ctx)

    def test_unreadable_checksum(self):
        self.stream.write_bytes(b"x")
        self.checksum.write_text("not a digest\n")
        with self.assertRaisesRegex(RuntimeError, "checksum unreadable"):
            phases_impl.verify_root_image_stream(self.ctx)

    def test_truncated_stream_rejected(self):
        self.write_stream()
        data = self.stream.read_bytes()
        self.stream.write_bytes(data[: len(data) // 2])
        with self.assertRaisesRegex(RuntimeError, "corrupt"):
            phases_impl.verify_root_image_stream(self.ctx)


class RootImageStreamLookupTest(unittest.TestCase):
    def test_first_existing_candidate_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "bootmnt/omarchy-root.btrfs"
            sfs = Path(tmp) / "rootfs/omarchy-root.btrfs"
            sfs.parent.mkdir()
            sfs.write_bytes(b"old")
            with mock.patch.object(phases_impl, "ROOT_IMAGE_STREAM_CANDIDATES", (iso, sfs)):
                self.assertEqual(phases_impl._root_image_stream(), sfs)
                iso.parent.mkdir()
                iso.write_bytes(b"new")
                self.assertEqual(phases_impl._root_image_stream(), iso)

    def test_no_candidate_lists_every_path(self):
        with mock.patch.object(phases_impl, "ROOT_IMAGE_STREAM_CANDIDATES",
                               (Path("/nonexistent/a"), Path("/nonexistent/b"))):
            with self.assertRaisesRegex(RuntimeError, "stream missing.*/nonexistent/a.*/nonexistent/b"):
                phases_impl._root_image_stream()


class PrepareInstallTargetTest(unittest.TestCase):
    """prepare_install_target wires the checks together per install mode."""

    def setUp(self):
        self.calls = []
        for name in ("verify_protected_mounts", "verify_root_image_stream",
                     "verify_root_image_layout", "_root_image_target_mounts"):
            patch = mock.patch.object(
                phases_impl, name, side_effect=lambda *a, _n=name, **k: self.calls.append(_n)
            )
            patch.start()
            self.addCleanup(patch.stop)

    def test_full_disk_checks_json_layout_then_stream(self):
        ctx = types.SimpleNamespace(
            is_protected=False, target=Path("/mnt"),
            user_configuration={"disk_config": btrfs_root_layout()},
        )
        phases_impl.prepare_install_target(ctx)
        self.assertEqual(self.calls, ["verify_root_image_layout", "verify_root_image_stream"])

    def test_protected_checks_real_mounts_then_stream(self):
        ctx = types.SimpleNamespace(
            is_protected=True, target=Path("/mnt"),
            user_configuration={"disk_config": {"config_type": "pre_mounted_config"}},
        )
        phases_impl.prepare_install_target(ctx)
        self.assertEqual(
            self.calls,
            ["verify_protected_mounts", "_root_image_target_mounts", "verify_root_image_stream"],
        )


class InstallRootImageTest(unittest.TestCase):
    """The subvolume swap: receive at the top level, snapshot writable, drop
    the empty @, rename the snapshot in, replay the mounts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "mnt"
        self.target.mkdir()
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir()
        self.top = self.state_dir / "image-top"
        self.stream = Path(self.tmp.name) / "omarchy-root.btrfs"
        self.stream.write_bytes(b"stream")
        self.ctx = types.SimpleNamespace(target=self.target, state_dir=self.state_dir)

        self.mounts = [
            {"target": str(self.target), "source": "/dev/mapper/omarchy_root[/@]",
             "fstype": "btrfs", "options": "rw,noatime,compress=zstd:3,subvolid=256,subvol=/@"},
            {"target": str(self.target / "boot"), "source": "/dev/vda1",
             "fstype": "vfat", "options": "rw,relatime"},
            {"target": str(self.target / "home"), "source": "/dev/mapper/omarchy_root[/@home]",
             "fstype": "btrfs", "options": "rw,noatime,compress=zstd:3,subvolid=257,subvol=/@home"},
            {"target": str(self.target / "var/log"), "source": "/dev/mapper/omarchy_root[/@log]",
             "fstype": "btrfs", "options": "rw,noatime,compress=zstd:3,subvolid=258,subvol=/@log"},
        ]
        self.calls = []
        self.received_packages = {"limine", "omarchy-keyring", "omarchy", "omarchy-settings", "omarchy-nvim"}

        def fake_run(cmd, **kwargs):
            self.calls.append(cmd)
            if cmd[0] == "mount" and cmd[1:3] == ["-o", "subvolid=5"]:
                # The top level as archinstall left it: an empty @ and the
                # other subvolumes of the layout.
                top = Path(cmd[-1])
                for name in ("@", "@home", "@log"):
                    (top / name).mkdir(parents=True, exist_ok=True)
            elif cmd[:3] == ["btrfs", "subvolume", "snapshot"]:
                shutil.copytree(cmd[3], cmd[4])
            elif cmd[:3] == ["btrfs", "subvolume", "delete"]:
                shutil.rmtree(cmd[3])
            return CompletedProcess(cmd, 0, stdout="", stderr="")

        def fake_receive(ctx, top, stream_path):
            self.assertEqual(stream_path, self.stream)
            self.calls.append(["btrfs", "receive", str(top)])
            received = top / phases_impl.ROOT_IMAGE_SUBVOLUME
            (received / "var/log").mkdir(parents=True)
            (received / "var/log/pacman.log").write_text("[image] installed base\n")
            (received / "etc").mkdir()

        patches = [
            mock.patch.object(phases_impl.subprocess, "run", side_effect=fake_run),
            mock.patch.object(phases_impl, "_findmnt_mounts", return_value=self.mounts),
            mock.patch.object(phases_impl, "_receive_root_image", side_effect=fake_receive),
            mock.patch.object(phases_impl, "_umount_tree",
                              side_effect=lambda root: self.calls.append(["umount", "-R", str(root)])),
            mock.patch.object(phases_impl, "_root_image_required_packages",
                              return_value=["limine", "omarchy-keyring", "omarchy"]),
            mock.patch.object(phases_impl.arch, "target_has_package", create=True,
                              side_effect=lambda target, pkg: pkg in self.received_packages),
            mock.patch.object(phases_impl, "ROOT_IMAGE_STREAM_CANDIDATES", (self.stream,)),
            mock.patch.object(phases_impl, "info"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def btrfs_calls(self):
        return [cmd for cmd in self.calls if cmd[0] == "btrfs"]

    def test_swaps_received_image_in_for_the_empty_root(self):
        phases_impl._install_root_image(self.ctx)

        top = str(self.top)
        received = f"{top}/{phases_impl.ROOT_IMAGE_SUBVOLUME}"
        self.assertEqual(self.calls[0], ["mount", "-o", "subvolid=5", "/dev/mapper/omarchy_root", top])
        self.assertEqual(
            self.btrfs_calls(),
            [
                ["btrfs", "receive", top],
                ["btrfs", "subvolume", "snapshot", received, f"{top}/@.image"],
                ["btrfs", "subvolume", "delete", received],
                ["btrfs", "subvolume", "delete", f"{top}/@"],
            ],
        )
        # The old @ is deleted only once the layout is unmounted, and the
        # snapshot takes its name.
        unmount = self.calls.index(["umount", "-R", str(self.target)])
        delete_root = self.calls.index(["btrfs", "subvolume", "delete", f"{top}/@"])
        self.assertLess(unmount, delete_root)
        self.assertTrue((self.top / "@" / "etc").is_dir())
        self.assertFalse((self.top / "@.image").exists())
        self.assertFalse((self.top / phases_impl.ROOT_IMAGE_SUBVOLUME).exists())

    def test_replays_mounts_without_subvolid(self):
        phases_impl._install_root_image(self.ctx)

        delete_root = self.calls.index(["btrfs", "subvolume", "delete", f"{self.top}/@"])
        remounts = [cmd for cmd in self.calls[delete_root:] if cmd[0] == "mount"]
        self.assertEqual(
            remounts,
            [
                ["mount", "-t", "btrfs", "-o", "rw,noatime,compress=zstd:3,subvol=/@",
                 "/dev/mapper/omarchy_root", str(self.target)],
                ["mount", "-t", "vfat", "-o", "rw,relatime", "/dev/vda1", str(self.target / "boot")],
                ["mount", "-t", "btrfs", "-o", "rw,noatime,compress=zstd:3,subvol=/@home",
                 "/dev/mapper/omarchy_root", str(self.target / "home")],
                ["mount", "-t", "btrfs", "-o", "rw,noatime,compress=zstd:3,subvol=/@log",
                 "/dev/mapper/omarchy_root", str(self.target / "var/log")],
            ],
        )
        # The top level is released last, then per-machine identity is set.
        self.assertEqual(self.calls[-2], ["umount", str(self.top)])
        self.assertEqual(self.calls[-1], ["systemd-machine-id-setup", f"--root={self.target}"])

    def test_carries_image_pacman_log_into_log_subvolume(self):
        phases_impl._install_root_image(self.ctx)
        self.assertEqual((self.top / "@log/pacman.log").read_text(), "[image] installed base\n")

    def test_stale_subvolumes_from_a_previous_attempt_are_removed_first(self):
        original_run = phases_impl.subprocess.run.side_effect

        def run_with_leftovers(cmd, **kwargs):
            result = original_run(cmd, **kwargs)
            if cmd[0] == "mount" and cmd[1:3] == ["-o", "subvolid=5"]:
                top = Path(cmd[-1])
                (top / phases_impl.ROOT_IMAGE_SUBVOLUME).mkdir()
                (top / "@.image").mkdir()
            return result

        phases_impl.subprocess.run.side_effect = run_with_leftovers
        phases_impl._install_root_image(self.ctx)

        top = str(self.top)
        received = f"{top}/{phases_impl.ROOT_IMAGE_SUBVOLUME}"
        self.assertEqual(
            self.btrfs_calls()[:4],
            [
                ["btrfs", "subvolume", "delete", received],
                ["btrfs", "receive", top],
                ["btrfs", "subvolume", "delete", f"{top}/@.image"],
                ["btrfs", "subvolume", "snapshot", received, f"{top}/@.image"],
            ],
        )

    def test_missing_required_package_fails_after_the_swap(self):
        self.received_packages.discard("omarchy")
        with self.assertRaisesRegex(RuntimeError, "lacks required packages: omarchy"):
            phases_impl._install_root_image(self.ctx)
        # The layout is back in place and the top level released either way.
        self.assertIn(["umount", str(self.top)], self.calls)
        self.assertTrue((self.top / "@" / "etc").is_dir())

    def test_receive_failure_releases_top_level_and_keeps_layout_mounted(self):
        phases_impl._receive_root_image.side_effect = RuntimeError("btrfs receive failed")
        with self.assertRaisesRegex(RuntimeError, "btrfs receive failed"):
            phases_impl._install_root_image(self.ctx)
        self.assertEqual(self.calls[-1], ["umount", str(self.top)])
        self.assertNotIn(["umount", "-R", str(self.target)], self.calls)
        self.assertTrue((self.top / "@").is_dir())


class FakeUnitProc:
    """What Popen(systemd-run --wait --pipe ...) hands back."""

    def __init__(self, returncode=0, output=""):
        self.returncode = returncode
        self.output = output
        self.joined = 0

    def communicate(self):
        self.joined += 1
        return self.output, None


class TargetKeyringUnitTest(unittest.TestCase):
    """The per-machine keyring the image deliberately ships without, run as
    a transient unit and joined before the factory snapshot."""

    def setUp(self):
        self.target = Path("/mnt")
        self.ctx = types.SimpleNamespace(target=self.target, state={})
        self.runs = []
        self.popens = []

        def fake_run(cmd, **kwargs):
            self.runs.append(cmd)
            return CompletedProcess(cmd, 0, stdout="", stderr="")

        def fake_popen(cmd, **kwargs):
            self.popens.append((cmd, kwargs))
            return FakeUnitProc()

        for patch in (
            mock.patch.object(phases_impl.subprocess, "run", side_effect=fake_run),
            mock.patch.object(phases_impl.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(phases_impl, "info"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_start_runs_init_then_populate_in_a_waited_unit(self):
        phases_impl._start_target_keyring_init(self.ctx)

        self.assertEqual(self.runs, [["systemctl", "reset-failed", "omarchy-target-keyring"]])
        (cmd, kwargs), = self.popens
        self.assertEqual(
            cmd[:6],
            ["systemd-run", "--wait", "--pipe", "--collect", "--quiet", "--unit=omarchy-target-keyring"],
        )
        self.assertEqual(cmd[6:8], ["sh", "-c"])
        self.assertEqual(
            cmd[8],
            "pacman-key --gpgdir /mnt/etc/pacman.d/gnupg --init && "
            "pacman-key --gpgdir /mnt/etc/pacman.d/gnupg "
            "--populate-from /mnt/usr/share/pacman/keyrings --populate archlinux omarchy",
        )
        self.assertNotIn("arch-chroot", cmd[8])
        self.assertIs(kwargs["stderr"], phases_impl.subprocess.STDOUT)
        self.assertIsInstance(self.ctx.state["target_keyring_proc"], FakeUnitProc)

    def test_join_waits_once_and_clears_state(self):
        proc = FakeUnitProc()
        self.ctx.state["target_keyring_proc"] = proc
        phases_impl._join_target_keyring_init(self.ctx)
        phases_impl._join_target_keyring_init(self.ctx)  # no-op: nothing pending
        self.assertEqual(proc.joined, 1)
        self.assertNotIn("target_keyring_proc", self.ctx.state)

    def test_join_raises_with_the_unit_output(self):
        self.ctx.state["target_keyring_proc"] = FakeUnitProc(returncode=1, output="gpg: boom\n")
        with self.assertRaisesRegex(RuntimeError, r"(?s)keyring init failed \(exit 1\).*gpg: boom"):
            phases_impl._join_target_keyring_init(self.ctx)
        self.assertNotIn("target_keyring_proc", self.ctx.state)

    def test_stop_ends_the_unit_and_never_raises(self):
        proc = FakeUnitProc(returncode=1, output="killed")
        self.ctx.state["target_keyring_proc"] = proc
        phases_impl.stop_target_keyring_init(self.ctx)
        self.assertEqual(self.runs, [["systemctl", "stop", "omarchy-target-keyring"]])
        self.assertEqual(proc.joined, 1)
        self.assertNotIn("target_keyring_proc", self.ctx.state)

    def test_stop_without_a_unit_touches_nothing(self):
        phases_impl.stop_target_keyring_init(self.ctx)
        self.assertEqual(self.runs, [])

    def test_factory_snapshot_joins_the_unit_before_anything_else(self):
        # Even when the snapshot itself is skipped, the join runs: a failed
        # keyring must fail the install.
        self.ctx.state["target_keyring_proc"] = FakeUnitProc(returncode=1, output="gpg: boom")
        with mock.patch.object(phases_impl, "_findmnt_value", return_value="ext4"):
            with self.assertRaisesRegex(RuntimeError, "keyring init failed"):
                phases_impl.create_factory_snapshot(self.ctx)
        self.assertEqual(self.runs, [])


if __name__ == "__main__":
    unittest.main()
