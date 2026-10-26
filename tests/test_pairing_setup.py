"""Installer tests confined to temporary XDG roots and a fake service manager."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import pairing_setup as setup

MAC = '02:00:00:00:00:01'
OTHER = '02:00:00:00:00:02'


class FakeSystem:
    def __init__(self):
        self.manager = None
        self.active = False
        self.legacy = False
        self.foreign = False
        self.fail = None
        self.effective_dropins = ''
        self.search_paths = []
        self.calls = []
        self.found = [dict(address=MAC, paired=True, trusted=True, eligible=True, airpods=True)]

    def dropins(self):
        return self.effective_dropins

    def unit_paths(self):
        return self.search_paths

    def devices(self):
        return self.found

    def unit(self, name):
        if name == 'bt-autoagent.service':
            return dict(exists=self.legacy, active=self.legacy, enabled='enabled' if self.legacy else 'not-found')
        exists = self.foreign or self.manager.paths['unit'].exists()
        return dict(exists=exists, active=self.active,
                    enabled='enabled' if self.manager.paths['link'].is_symlink() else 'disabled' if exists else 'not-found')

    def control(self, action, name=None):
        self.calls.append((action, name))
        if self.fail == action:
            self.fail = None
            raise setup.core.Error('simulated failure')
        if action in ('start', 'restart'):
            self.active = True
            if self.fail == 'interrupt':
                self.fail = None
                raise KeyboardInterrupt
        if action == 'stop':
            self.active = False

    def check_agent(self, script, devices):
        self.calls.append(('check', None))
        if self.fail == 'check':
            self.fail = None
            raise setup.core.Error('dependency unavailable')
        assert json.loads(devices.read_text())
        assert script.read_bytes()



class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pairing space % $ ')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.system = FakeSystem()
        self.manager = setup.Manager(system=self.system, config=root / 'config', state=root / 'state', data=root / 'data')
        self.system.manager = self.manager

    def assert_clean(self):
        for path in self.manager.paths.values():
            self.assertEqual(setup.core.snapshot(path), setup.ABSENT)
        self.assertFalse((self.manager.state / 'state.json').exists())
        self.assertFalse((self.manager.state / 'pending.json').exists())
        self.assertFalse(self.system.active)

    def test_enable_disable_repeated_preserves_unrelated_files(self):
        unrelated = self.manager.data / 'unrelated'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('keep')
        self.assertTrue(self.manager.enable(MAC)['enabled'])
        self.assertEqual(json.loads(self.manager.paths['devices'].read_text()), [MAC])
        self.assertEqual(self.manager.paths['devices'].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.state.stat().st_mode & 0o777, 0o700)
        self.assertTrue(self.manager.status()['managed'])
        self.assertTrue(self.manager.enable(MAC)['enabled'])
        self.assertFalse(self.manager.disable()['enabled'])
        self.assertFalse(self.manager.disable()['managed'])
        self.assert_clean()
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertFalse(any(action == 'disable' for action, _ in self.system.calls))
        self.manager.enable(MAC)
        self.manager.disable()
        self.assert_clean()

    def test_refuse_legacy_and_untrusted_or_unpaired(self):
        self.system.legacy = True
        with self.assertRaises(setup.core.Error):
            self.manager.enable(MAC)
        self.system.legacy = False
        for field in ('paired', 'trusted', 'eligible'):
            self.system.found[0][field] = False
            with self.assertRaises(setup.core.Error):
                self.manager.enable(MAC)
            self.system.found[0][field] = True
        self.assertEqual(self.system.calls, [])
        self.assert_clean()

    def test_preexisting_targets_and_service_refused(self):
        for path in self.manager.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('foreign')
            with self.assertRaises(setup.core.Error):
                self.manager.enable(MAC)
            self.assertEqual(path.read_text(), 'foreign')
            path.unlink()
        self.system.foreign = True
        with self.assertRaises(setup.core.Error):
            self.manager.enable(MAC)
        self.assertEqual(self.system.calls, [])

    def test_service_start_and_dependency_failure_roll_back(self):
        for failure in ('start', 'check', 'daemon-reload'):
            with self.subTest(failure=failure):
                self.system.fail = failure
                with self.assertRaises(setup.core.Error):
                    self.manager.enable(MAC)
                self.assert_clean()

    def test_failed_update_restores_previous_selection_and_service(self):
        self.manager.enable(MAC)
        before = {k: setup.core.snapshot(p) for k, p in self.manager.paths.items()}
        previous = (self.manager.state / 'state.json').read_bytes()
        self.system.found.append(dict(address=OTHER, paired=True, trusted=True, eligible=True, airpods=True))
        self.system.fail = 'restart'
        with self.assertRaises(setup.core.Error):
            self.manager.enable(OTHER)
        self.assertTrue(self.system.active)
        self.assertEqual((self.manager.state / 'state.json').read_bytes(), previous)
        self.assertEqual({k: setup.core.snapshot(p) for k, p in self.manager.paths.items()}, before)

    def test_edit_conflict_keeps_all_owned_files_and_service(self):
        self.manager.enable(MAC)
        path = self.manager.paths['agent']
        path.write_text('user edit')
        before = {k: setup.core.snapshot(p) for k, p in self.manager.paths.items()}
        calls = list(self.system.calls)
        with self.assertRaises(setup.core.Error):
            self.manager.disable()
        with self.assertRaises(setup.core.Error):
            self.manager.enable(MAC)
        self.assertEqual(self.system.calls, calls)
        self.assertTrue(self.system.active)
        self.assertEqual({k: setup.core.snapshot(p) for k, p in self.manager.paths.items()}, before)
        self.assertTrue(self.manager.status()['issues'])

    def test_interrupt_is_recovered_on_disable(self):
        self.system.fail = 'interrupt'
        with self.assertRaises(KeyboardInterrupt):
            self.manager.enable(MAC)
        self.assertTrue(self.manager.status()['pending'])
        self.manager.disable()
        self.assert_clean()

    def test_recovery_conflict_retains_journal_and_edit(self):
        self.system.fail = 'interrupt'
        with self.assertRaises(KeyboardInterrupt):
            self.manager.enable(MAC)
        self.manager.paths['devices'].write_text('edited')
        with self.assertRaises(setup.core.Error):
            self.manager.disable()
        self.assertEqual(self.manager.paths['devices'].read_text(), 'edited')
        self.assertTrue((self.manager.state / 'pending.json').exists())

    def test_disable_failure_keeps_recoverable_journal(self):
        self.manager.enable(MAC)
        self.system.fail = 'stop'
        with self.assertRaises(setup.core.Error):
            self.manager.disable()
        self.assertTrue((self.manager.state / 'pending.json').exists())
        self.manager.disable()
        self.assert_clean()

    def test_status_has_no_filesystem_mutations(self):
        self.assertFalse(self.manager.status()['managed'])
        self.assertFalse(self.manager.state.exists())
        self.assertEqual(self.system.calls, [])

    def test_quoted_unit_and_restart_policy(self):
        unit = self.manager.desired(self.system.found)['unit']
        text = __import__('base64').b64decode(unit['data']).decode()
        self.assertIn('Restart=on-failure', text)
        self.assertIn('%%', text)
        self.assertIn('$$', text)
        self.assertIn('ExecStart="/usr/bin/python3" "-I" "', text)
        self.assertIn('"--devices-file" "', text)
        self.assertEqual(setup.quote('a"b\\c%$'), '"a\\"b\\\\c%%$$"')
        with self.assertRaises(setup.core.Error):
            setup.quote('bad\npath')

    def test_xdg_and_cli_json(self):
        root = Path(self.tmp.name)
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(root / 'c'), 'XDG_STATE_HOME': str(root / 's'), 'XDG_DATA_HOME': str(root / 'd')}):
            manager = setup.Manager(system=self.system)
        self.assertEqual(manager.state, root / 's/omarchy-airpods-pairing')
        self.assertEqual(manager.data, root / 'd/omarchy-airpods')
        out = io.StringIO()
        with patch.object(setup, 'Manager', return_value=self.manager), contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(['status']), 0)
        self.assertFalse(json.loads(out.getvalue())['enabled'])

    def test_symlink_target_and_mode_edits_are_preserved(self):
        victim = Path(self.tmp.name) / 'unrelated'
        victim.write_text('keep')
        path = self.manager.paths['agent']
        path.parent.mkdir(parents=True)
        path.symlink_to(victim)
        with self.assertRaises(setup.core.Error):
            self.manager.enable(MAC)
        self.assertTrue(path.is_symlink())
        self.assertEqual(victim.read_text(), 'keep')
        path.unlink()
        self.manager.enable(MAC)
        self.manager.paths['devices'].chmod(0o644)
        with self.assertRaises(setup.core.Error):
            self.manager.disable()
        self.assertTrue(self.system.active)
        self.assertTrue(self.manager.paths['devices'].exists())

    def test_selection_update_and_default_discovery(self):
        self.manager.enable(MAC)
        self.system.found.append(dict(address=OTHER, paired=True, trusted=True, eligible=True, airpods=True))
        self.manager.enable(OTHER)
        self.assertEqual(json.loads(self.manager.paths['devices'].read_text()), [OTHER])
        self.manager.enable()
        self.assertEqual(json.loads(self.manager.paths['devices'].read_text()), [MAC, OTHER])
        self.manager.disable()
        self.assert_clean()

    def test_system_queries_effective_dropins_and_search_paths(self):
        with patch.object(setup.core, 'run', side_effect=['/override.conf\n', '/first\n/second\n']) as run:
            system = setup.System()
            self.assertEqual(system.dropins(), '/override.conf')
            self.assertEqual(system.unit_paths(), [Path('/first'), Path('/second')])
            self.assertEqual(run.call_args_list[0].args[0],
                             ['systemctl', '--user', 'show', setup.UNIT, '--property=DropInPaths', '--value'])
            self.assertEqual(run.call_args_list[1].args[0], ['systemd-analyze', '--user', 'unit-paths'])

    def test_orphan_and_inherited_dropins_refused_without_mutation(self):
        root = self.manager.config / 'systemd/user'
        for name in (setup.UNIT + '.d', 'service.d', 'omarchy-.service.d', 'omarchy-airpods-.service.d'):
            directory = root / name
            directory.mkdir(parents=True)
            conf = directory / 'override.conf'
            conf.write_text('[Install]\nWantedBy=other.target\nAlso=unrelated.service\n[Service]\nExecStart=\nExecStart=/bad\n')
            with self.subTest(name=name), self.assertRaises(setup.core.Error):
                self.manager.enable(MAC)
            self.assertFalse(self.manager.state.exists())
            self.assertEqual(self.system.calls, [])
            self.assertFalse(self.manager.paths['unit'].exists())
            conf.unlink()
            directory.rmdir()

    def test_effective_dropins_refused_and_query_errors_fail_closed(self):
        self.system.effective_dropins = '/runtime/service.d/override.conf'
        with self.assertRaises(setup.core.Error):
            self.manager.enable(MAC)
        self.assertFalse(self.manager.state.exists())
        self.assertEqual(self.system.calls, [])
        self.system.effective_dropins = ''
        with patch.object(self.system, 'dropins', side_effect=setup.core.Error('query failed')):
            with self.assertRaises(setup.core.Error):
                self.manager.enable(MAC)
        self.assertFalse(self.manager.state.exists())

    def test_orphan_dropins_in_additional_search_path_refused(self):
        root = Path(self.tmp.name) / 'runtime-units'
        directory = root / (setup.UNIT + '.d')
        directory.mkdir(parents=True)
        (directory / 'override.conf').write_text('[Service]\nExecStart=/bad\n')
        self.system.search_paths = [root]
        with self.assertRaises(setup.core.Error):
            self.manager.enable(MAC)
        self.assertEqual(self.system.calls, [])

    def test_reload_rechecks_dropins_before_start_and_retains_journal(self):
        original = self.system.control
        def control(action, name=None):
            original(action, name)
            if action == 'daemon-reload':
                self.system.effective_dropins = '/new/override.conf'
        with patch.object(self.system, 'control', side_effect=control):
            with self.assertRaises(setup.core.Error):
                self.manager.enable(MAC)
        self.assertNotIn(('start', setup.UNIT), self.system.calls)
        self.assertTrue((self.manager.state / 'pending.json').exists())
        self.system.effective_dropins = ''
        self.manager.disable()
        self.assert_clean()

    def test_owned_link_only_no_install_expansion_and_failure_cleanup(self):
        self.system.fail = 'start'
        with self.assertRaises(setup.core.Error):
            self.manager.enable(MAC)
        self.assert_clean()
        self.assertFalse(list(self.manager.config.rglob('*.wants/*')))
        self.manager.enable(MAC)
        links = list(self.manager.config.rglob('*.wants/*'))
        self.assertEqual(links, [self.manager.paths['link']])
        self.assertFalse(any('enable' in action for action, _ in self.system.calls))

    def test_writable_code_directory_and_ancestors_refused(self):
        for directory in (self.manager.data, self.manager.data.parent, Path(self.tmp.name)):
            directory.mkdir(parents=True, exist_ok=True)
            mode = directory.stat().st_mode & 0o777
            for unsafe in (0o777, 0o770):
                directory.chmod(unsafe)
                try:
                    with self.subTest(path=directory, mode=unsafe), self.assertRaises(setup.core.Error):
                        self.manager.enable(MAC)
                    self.assertEqual(directory.stat().st_mode & 0o777, unsafe)
                    self.assertEqual(self.system.calls, [])
                    self.assertFalse(self.manager.paths['agent'].exists())
                finally:
                    directory.chmod(mode)

    def test_foreign_owned_directory_refused(self):
        self.manager.data.mkdir(parents=True)
        original = Path.lstat
        def info(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == self.manager.data:
                return SimpleNamespace(st_mode=value.st_mode, st_uid=987654)
            return value
        with patch.object(Path, 'lstat', info), self.assertRaises(setup.core.Error):
            self.manager.enable(MAC)
        self.assertEqual(self.system.calls, [])

    def test_secure_user_directory_below_root_sticky_ancestor_allowed(self):
        # TemporaryDirectory is user-owned 0700; /tmp is root-owned sticky.
        self.manager.enable(MAC)
        self.assertTrue(self.system.active)
        self.manager.disable()
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
