"""Offline tests: no system bus connections, hardware, or optional packages."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "backend" / "pairing_agent.py"
spec = importlib.util.spec_from_file_location("pairing_agent_under_test", SOURCE)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
SELECTED = "02:00:00:00:00:01"
OTHER = "02:00:00:00:00:02"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "pairing-devices.json"
        self.path.write_text(json.dumps([SELECTED]))

    def test_policy_matrix(self):
        for address in (SELECTED, OTHER, None, "invalid"):
            for paired in (True, False, None, "true", 1):
                for trusted in (True, False, None, "true", 1):
                    with self.subTest(address=address, paired=paired, trusted=trusted):
                        self.assertEqual(agent.authorized(self.path, address, paired, trusted),
                                         address == SELECTED and paired is True and trusted is True)

    def test_case_normalization_and_empty_list(self):
        self.path.write_text('["aa:bb:cc:dd:ee:ff"]')
        self.assertTrue(agent.authorized(self.path, "AA:BB:CC:DD:EE:FF", True, True))
        self.path.write_text('[]')
        self.assertEqual(agent.read_devices(self.path), frozenset())
        self.assertFalse(agent.authorized(self.path, SELECTED, True, True))

    def test_malformed_data_fails_entire_file(self):
        for content in ('', '{', '{}', 'null', 'true', '42', '"hello"',
                        '[null]', '[1]', '[true]', '[["x"]]',
                        json.dumps([SELECTED, "bad"]),
                        json.dumps([SELECTED + "\n"]),
                        json.dumps([SELECTED.replace(":", "_")])):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(agent.DeviceFileError):
                    agent.read_devices(self.path)
                self.assertFalse(agent.authorized(self.path, SELECTED, True, True))

    def test_missing_unreadable_invalid_encoding(self):
        self.path.unlink()
        self.assertFalse(agent.authorized(self.path, SELECTED, True, True))
        for error in (PermissionError("sensitive detail"), OSError("sensitive detail")):
            with patch.object(Path, "read_text", side_effect=error):
                self.assertFalse(agent.authorized(self.path, SELECTED, True, True))
        self.path.write_bytes(b'\xff')
        self.assertFalse(agent.authorized(self.path, SELECTED, True, True))

    def test_selection_is_reread(self):
        self.assertTrue(agent.authorized(self.path, SELECTED, True, True))
        self.path.write_text(json.dumps([OTHER]))
        self.assertFalse(agent.authorized(self.path, SELECTED, True, True))
        self.assertTrue(agent.authorized(self.path, OTHER, True, True))
        self.path.unlink()
        self.assertFalse(agent.authorized(self.path, OTHER, True, True))

    def test_check_never_runs_agent(self):
        with patch.object(agent, "dependencies", return_value=(Mock(), Mock())), \
             patch.object(agent, "run_agent") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.main(["--devices-file", str(self.path), "--check"]), 0)
            run.assert_not_called()

    def test_check_errors_are_sanitized(self):
        for invalid_file in (False, True):
            if invalid_file:
                self.path.write_text('invalid')
            output = io.StringIO()
            with patch.object(agent, "dependencies", side_effect=ImportError(SELECTED)), \
                 patch.object(agent, "run_agent") as run, contextlib.redirect_stderr(output):
                self.assertEqual(agent.main(["--devices-file", str(self.path), "--check"]), 1)
                run.assert_not_called()
            self.assertNotIn(SELECTED, output.getvalue())
            self.assertNotIn(str(self.path), output.getvalue())

    def test_dbus_handlers_enforce_policy_and_reject_secrets(self):
        objects = []

        class Object:
            def __init__(self, *args):
                objects.append(self)

            def remove_from_connection(self):
                pass

        manager = Mock()
        properties = Mock()
        properties.GetAll.return_value = {"Address": SELECTED, "Paired": True, "Trusted": True}
        bus = Mock()
        dbus = SimpleNamespace(
            DBusException=type("DBusException", (Exception,), {}),
            service=SimpleNamespace(Object=Object, method=lambda *a, **k: lambda f: f),
            mainloop=SimpleNamespace(glib=SimpleNamespace(DBusGMainLoop=Mock())),
            SystemBus=Mock(return_value=bus),
            Interface=lambda obj, interface: manager if interface == "org.bluez.AgentManager1" else properties)
        glib = Mock()

        def exercise():
            live = objects[0]
            manager.RegisterAgent.assert_called_once_with(agent.AGENT_PATH, "NoInputNoOutput")
            live.RequestConfirmation("device", 123456)
            live.AuthorizeService("device", "service")
            live.RequestAuthorization("device")
            for paired, trusted in ((False, True), (True, False)):
                properties.GetAll.return_value.update(Paired=paired, Trusted=trusted)
                for method, args in ((live.RequestConfirmation, ("device", 123456)),
                                     (live.AuthorizeService, ("device", "service")),
                                     (live.RequestAuthorization, ("device",))):
                    with self.assertRaises(dbus.DBusException):
                        method(*args)
            properties.GetAll.return_value.update(Paired=True, Trusted=True)
            self.path.write_text('[]')
            with self.assertRaises(dbus.DBusException):
                live.RequestConfirmation("device", 123456)
            self.path.write_text(json.dumps([SELECTED]))
            for method, args in ((live.RequestPinCode, ("device",)),
                                 (live.RequestPasskey, ("device",)),
                                 (live.DisplayPinCode, ("device", "1234")),
                                 (live.DisplayPasskey, ("device", 123456, 0))):
                with self.assertRaises(dbus.DBusException):
                    method(*args)
            properties.GetAll.side_effect = RuntimeError("secret")
            with self.assertRaises(dbus.DBusException):
                live.AuthorizeService("device", "service")
            live.Release()
            self.assertFalse(live.registered)
            glib.timeout_add_seconds.assert_called_once()
            owner_changed = bus.add_signal_receiver.call_args.args[0]
            owner_changed("org.bluez", "old", "new")
            glib.timeout_add_seconds.assert_called_once()  # No duplicate retry.

        glib.MainLoop.return_value.run.side_effect = exercise
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            agent.run_agent(self.path, dbus, glib)
        self.assertEqual(output.getvalue(), "")


class RetryTests(unittest.TestCase):
    def test_persistent_bounded_backoff_and_recovery(self):
        glib = Mock()
        register = Mock(side_effect=RuntimeError("BlueZ absent"))
        retry = agent.Registration(glib, register)
        retry.attempt()
        for _ in range(9):
            retry.attempt()  # Simulate the pending one-shot timer firing.
        delays = [call.args[0] for call in glib.timeout_add_seconds.call_args_list]
        self.assertEqual(delays, [2, 4, 8, 16, 32, 60, 60, 60, 60, 60])
        register.side_effect = None
        retry.attempt()
        self.assertIsNone(retry.timer)
        retry.schedule()  # Release after successful registration retains backoff.
        self.assertEqual(glib.timeout_add_seconds.call_args.args[0], 60)
        count = glib.timeout_add_seconds.call_count
        retry.schedule()
        self.assertEqual(glib.timeout_add_seconds.call_count, count)
        retry.stop()
        glib.source_remove.assert_called_once()
        retry.schedule()
        retry.attempt()
        self.assertEqual(glib.timeout_add_seconds.call_count, count)

    def test_import_does_not_require_optional_packages(self):
        code = '''import builtins, runpy, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('dbus', 'gi'):
        raise ImportError('optional dependency blocked')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
runpy.run_path(sys.argv[1], run_name='policy_import_test')
'''
        result = subprocess.run([sys.executable, "-c", code, str(SOURCE)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
