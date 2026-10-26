import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "system_policy", Path(__file__).resolve().parents[1] / "backend/system_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "etc/bluetooth").mkdir(parents=True)
        (root / "var/lib").mkdir(parents=True)
        self.config = root / "etc/bluetooth/main.conf"
        self.original = b"# preserve me\n[General]\nJustWorksRepairing=always\n\n[Policy]\nAutoEnable=true\n"
        self.config.write_bytes(self.original)
        self.config.chmod(0o640)
        self.helper = policy.SystemPolicy(self.config, root / "var/lib/omarchy-airpods")

    def test_existing_values_enable_reapply_restore(self):
        for value in (b"always", b"never", b"confirm"):
            with self.subTest(value=value):
                original = self.original.replace(b"always", value)
                self.config.write_bytes(original)
                self.assertEqual(self.helper.enable()["policy"], "confirm")
                self.assertEqual(self.helper.backup.read_bytes(), original)
                state = self.helper.state.read_bytes()
                self.helper.enable()
                self.assertEqual(self.helper.state.read_bytes(), state)
                self.assertEqual(self.helper.backup.read_bytes(), original)
                self.assertEqual(stat.S_IMODE(self.helper.directory.stat().st_mode), 0o700)
                for file in (self.helper.state, self.helper.backup, self.helper.lock):
                    self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
                self.helper.restore()
                self.assertEqual(self.config.read_bytes(), original)
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
                self.assertFalse(self.helper.state.exists())
                self.assertFalse(self.helper.backup.exists())
                self.helper.restore()

    def test_missing_key_and_general(self):
        for original in (b"", b"# comment", b"[Policy]\nAutoEnable=true\n",
                         b"[General]\n# comment\n[Policy]\nAutoEnable=true",
                         b"[General]\n# end"):
            with self.subTest(original=original):
                self.config.write_bytes(original)
                result = self.helper.status()
                self.assertIsNone(result["policy"])
                self.assertFalse(result["configured"])
                self.helper.enable()
                self.assertEqual(self.helper.status()["policy"], "confirm")
                self.helper.restore()
                self.assertEqual(self.config.read_bytes(), original)

    def test_preserves_comments_whitespace_crlf_and_other_sections(self):
        original = (b"; hi\r\n[General]\r\n  JustWorksRepairing = always  # reason\r\n"
                    b"Name = device\r\n\r\n[Other]\r\nJustWorksRepairing=never\r\n")
        self.config.write_bytes(original)
        self.helper.enable()
        self.assertEqual(self.config.read_bytes(), original.replace(b"always", b"confirm"))
        self.helper.restore()
        self.assertEqual(self.config.read_bytes(), original)

    def test_malformed_and_ambiguous_refused_without_backup(self):
        cases = (b"[General]\nJustWorksRepairing=always\nJustWorksRepairing=never\n",
                 b"[General]\n[General]\n", b"JustWorksRepairing=always\n",
                 b"[General]\nbad line\n", b"[General]\nJustWorksRepairing=maybe\n",
                 b"[general]\n", b"[General]\njustworksrepairing=always\n",
                 b"[General]\nName=a\nName=b\n", b"\xff", b"\0")
        for original in cases:
            with self.subTest(original=original):
                self.config.write_bytes(original)
                with self.assertRaises(policy.PolicyError):
                    self.helper.enable()
                self.assertEqual(self.config.read_bytes(), original)
                self.assertFalse(self.helper.state.exists())
                self.assertFalse(self.helper.backup.exists())

    def test_post_enable_content_or_mode_edit_refuses_restore_and_reapply(self):
        self.helper.enable()
        installed = self.config.read_bytes()
        for change in ("content", "mode"):
            with self.subTest(change=change):
                self.config.write_bytes(installed)
                self.config.chmod(0o640)
                if change == "content":
                    self.config.write_bytes(installed + b"# user edit\n")
                else:
                    self.config.chmod(0o600)
                edited = self.config.read_bytes()
                self.assertTrue(self.helper.status()["conflict"])
                for action in (self.helper.restore, self.helper.enable):
                    with self.assertRaises(policy.PolicyError):
                        action()
                    self.assertEqual(self.config.read_bytes(), edited)
                self.assertEqual(self.helper.backup.read_bytes(), self.original)

    def test_status_has_no_side_effects(self):
        result = self.helper.status()
        self.assertEqual(result["policy"], "always")
        self.assertFalse(self.helper.directory.exists())
        self.assertIsNone(result["restart_required"])
        self.assertIn("disconnects all devices", result["note"])
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_status_reads_policy_without_access_to_root_state(self):
        with patch.object(self.helper, "_load", side_effect=PermissionError("root-only")):
            result = self.helper.status()
        self.assertEqual(result["policy"], "always")
        self.assertIsNone(result["managed"])
        self.assertIsNone(result["conflict"])
        self.assertFalse(result["state_readable"])
        self.assertFalse(self.helper.directory.exists())

    def test_failure_before_atomic_replace_preserves_config(self):
        replace = os.replace
        def failing_replace(source, destination):
            if destination == self.config:
                raise OSError("write failure")
            return replace(source, destination)
        with patch.object(os, "replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                self.helper.enable()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.helper.backup.read_bytes(), self.original)
        self.helper.enable()
        self.helper.restore()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_missing_config_refused(self):
        self.config.unlink()
        for action in (self.helper.status, self.helper.enable, self.helper.restore):
            with self.assertRaises(policy.PolicyError):
                action()
        self.assertFalse(self.config.exists())

    def test_symlinks_refused(self):
        for attribute in ("config", "state", "backup", "lock", "directory"):
            with self.subTest(attribute=attribute):
                path = getattr(self.helper, attribute)
                if attribute not in ("config", "directory"):
                    self.helper.directory.mkdir(mode=0o700, exist_ok=True)
                if path.exists():
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()
                path.symlink_to(path.parent / "nonexistent")
                with self.assertRaises(policy.PolicyError):
                    self.helper.enable()
                with self.assertRaises(policy.PolicyError):
                    self.helper.status()
                path.unlink()
                self.config.write_bytes(self.original)

    def test_corrupt_backup_and_state_refused(self):
        self.helper.enable()
        good = self.helper.state.read_bytes()
        self.helper.state.write_text("{}")
        with self.assertRaises(policy.PolicyError):
            self.helper.restore()
        self.helper.state.write_bytes(good)
        self.helper.backup.write_bytes(b"bad")
        with self.assertRaises(policy.PolicyError):
            self.helper.restore()
        self.assertEqual(self.config.read_bytes(), self.original.replace(b"always", b"confirm"))

    def test_enable_recovery_after_each_durable_write(self):
        # Metadata -> backup -> config -> enabled metadata. Simulate process death.
        for crash_at in range(1, 5):
            with self.subTest(crash_at=crash_at):
                count = 0
                real_write = self.helper._write
                def crashing_write(*args):
                    nonlocal count
                    real_write(*args)
                    count += 1
                    if count == crash_at:
                        raise OSError("simulated crash")
                with patch.object(self.helper, "_write", side_effect=crashing_write):
                    with self.assertRaises(OSError):
                        self.helper.enable()
                self.helper.enable()
                self.assertEqual(self.helper.status()["policy"], "confirm")
                self.helper.restore()
                self.assertEqual(self.config.read_bytes(), self.original)

    def test_journal_parent_is_synced_before_transaction_writes(self):
        events = []
        real_sync = self.helper._sync
        real_write = self.helper._write

        def sync(directory):
            self.assertTrue(self.helper.directory.is_dir())
            real_sync(directory)
            events.append(("synced", directory))

        def write(path, *args):
            self.assertIn(("synced", self.helper.directory.parent), events)
            events.append(("write", path))
            real_write(path, *args)

        with patch.object(self.helper, "_sync", side_effect=sync), \
                patch.object(self.helper, "_write", side_effect=write):
            self.helper.enable()
        self.assertEqual(events[0], ("synced", self.helper.directory.parent))
        self.assertIn(("write", self.config), events)

    def test_parent_sync_failure_blocks_writes_including_on_retry(self):
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with patch.object(self.helper, "_sync", side_effect=OSError("fsync failed")) as sync:
                    with self.assertRaises(OSError):
                        self.helper.enable()
                sync.assert_called_once_with(self.helper.directory.parent)
                self.assertTrue(self.helper.directory.is_dir())
                self.assertFalse(self.helper.state.exists())
                self.assertFalse(self.helper.backup.exists())
                self.assertEqual(self.config.read_bytes(), self.original)
        self.helper.enable()
        self.helper.restore()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_restore_recovery_after_durable_writes_and_cleanup(self):
        for crash_at in (1, 2, 3):
            with self.subTest(crash_at=crash_at):
                self.helper.enable()
                real_write = self.helper._write
                count = 0
                def crashing_write(*args):
                    nonlocal count
                    real_write(*args)
                    count += 1
                    if count == crash_at:
                        raise OSError("simulated crash")
                if crash_at < 3:
                    with patch.object(self.helper, "_write", side_effect=crashing_write):
                        with self.assertRaises(OSError):
                            self.helper.restore()
                else:
                    def interrupted_cleanup():
                        self.helper.backup.unlink()
                        raise OSError("crash during cleanup")
                    with patch.object(self.helper, "_cleanup", side_effect=interrupted_cleanup):
                        with self.assertRaises(OSError):
                            self.helper.restore()
                self.helper.restore()
                self.assertEqual(self.config.read_bytes(), self.original)
                self.assertFalse(self.helper.state.exists())

    def test_pending_transaction_wont_overwrite_edit(self):
        real_write = self.helper._write
        def crash_after_state(path, *args):
            real_write(path, *args)
            raise OSError("crash")
        with patch.object(self.helper, "_write", side_effect=crash_after_state):
            with self.assertRaises(OSError):
                self.helper.enable()
        edited = self.original + b"# external edit\n"
        self.config.write_bytes(edited)
        with self.assertRaises(policy.PolicyError):
            self.helper.enable()
        self.assertEqual(self.config.read_bytes(), edited)

    def test_cli_rejects_unprivileged_mutations_and_path_options(self):
        with patch.object(os, "geteuid", return_value=1000), patch("builtins.print") as output:
            self.assertEqual(policy.main(["enable"]), 1)
            self.assertIn("pkexec /usr/bin/python3 -I backend/system_policy.py enable",
                          json.loads(output.call_args.args[0])["error"])
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            policy.main(["enable", "--config", str(self.config)])


if __name__ == "__main__":
    unittest.main()
