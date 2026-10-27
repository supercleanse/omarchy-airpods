import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('airpods', ROOT / 'backend/airpods.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
# Synthetic locally administered addresses, never real hardware identifiers.
MAC = ':'.join(['02'] + ['00'] * 4 + ['01'])
OTHER = ':'.join(['02'] + ['00'] * 4 + ['02'])
SPA_JSON_DUMP = shutil.which('spa-json-dump')
# Previous release's exact rule layout, for tracked-installation upgrades.
LEGACY_FRAGMENT = ('''# Managed by omarchy-airpods
monitor.bluez.properties = { bluez5.dummy-avrcp-player = true }
monitor.bluez.rules = [
  { matches = [ { device.name = "bluez_card.MAC" } ]
    actions.update-props = { bluez5.auto-connect = [ hfp_hf hfp_ag a2dp_sink a2dp_source ] } }
  { matches = [ { node.name = "~bluez_input[.]MAC[.].*" }
                  { node.name = "~bluez_output[.]MAC[.].*" } ]
    actions.update-props = { session.suspend-timeout-seconds = 0 } }
]
'''.replace('MAC', MAC.replace(':', '_'))).encode()


def device(address=MAC, **kwargs):
    return dict(dict(address=address, name='AirPods test', paired=True, trusted=True,
                     connected=True, airpods=True, eligible=True,
                     path='/org/bluez/hci0/dev_' + address.replace(':', '_')), **kwargs)


class FakeSystem:
    def __init__(self):
        self.items = [device()]
        self.units = {a.UNITS[0]: dict(exists=True, active=True, enabled='enabled'),
                      a.UNITS[1]: dict(exists=False, active=False, enabled='')}
        self.calls = []
        self.fail = None
        self.manager = None

    def devices(self):
        return self.items

    def unit(self, name):
        info = self.units[name].copy()
        if self.manager and a.snapshot(self.manager.paths[name]) == {'kind': 'link', 'target': '/dev/null'}:
            info['enabled'] = 'masked'
        return info

    def control(self, action, name=None):
        self.calls.append((action, name))
        if self.fail == (action, name):
            self.fail = None
            raise a.Error('injected failure')
        if action in ('stop', 'start'):
            self.units[name]['active'] = action == 'start'

    def reconnect(self, d):
        self.calls.append(('reconnect', d['address']))


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.system = FakeSystem()
        self.m = a.Manager(self.system, self.root / 'config', self.root / 'state')
        self.system.manager = self.m
        a.mkdir(self.m.state, private=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_reapply_restore(self):
        self.m.apply()
        conf = self.m.paths['config'].read_text()
        self.assertIn('dummy-avrcp-player = true', conf)
        self.assertIn('hfp_hf hfp_ag a2dp_sink a2dp_source', conf)
        self.assertIn('session.suspend-timeout-seconds = 0', conf)
        self.assertTrue(self.m.status()['configured'])
        calls = list(self.system.calls)
        self.m.apply()
        self.assertEqual(calls, self.system.calls)
        self.m.restore()
        self.assertFalse(self.m.paths['config'].exists())
        self.assertTrue(self.system.units[a.UNITS[0]]['active'])
        self.assertEqual(self.system.units[a.UNITS[0]]['enabled'], 'enabled')
        self.assertEqual(self.m.restore(), 'Nothing to restore')
        self.assertFalse(any(x[0] == 'reconnect' for x in self.system.calls))

    def assert_node_matches(self, patterns, address):
        self.assertEqual(len(patterns), 2)
        for pattern in patterns:
            self.assertTrue(pattern.startswith('~^'))
            self.assertTrue(pattern.endswith('$'))
            self.assertNotIn('\\', pattern)
        def matches(name):
            return any(re.search(pattern[1:], name) for pattern in patterns)
        for direction in ('input', 'output'):
            for formatted in (address, address.replace(':', '_')):
                for suffix in ('', '.1', '.profile'):
                    name = 'bluez_' + direction + '.' + formatted + suffix
                    self.assertTrue(matches(name), name)
                for name in ('prefix_bluez_' + direction + '.' + formatted,
                             'bluez_' + direction + 'X' + formatted,
                             'bluez_' + direction + '.' + formatted + '0',
                             'bluez_' + direction + '.' + formatted + '_other',
                             'bluez_' + direction + '.' + formatted + '.'):
                    self.assertFalse(matches(name), name)
            other = OTHER if address == MAC else MAC
            for formatted in (other, other.replace(':', '_')):
                for suffix in ('', '.1'):
                    name = 'bluez_' + direction + '.' + formatted + suffix
                    self.assertFalse(matches(name), name)

    def test_fragment_node_matching_boundaries(self):
        # Extract only quoted patterns, not a substitute configuration parser.
        text = a.fragment([MAC]).decode()
        patterns = re.findall(r'node[.]name = "([^"]+)"', text)
        self.assert_node_matches(patterns, MAC)
        self.assertNotIn('actions.update-props', text)

    @unittest.skipUnless(SPA_JSON_DUMP, 'spa-json-dump is not installed')
    def test_fragment_native_spa_structure_and_matches(self):
        path = self.root / 'generated.conf'
        path.write_bytes(a.fragment([OTHER, MAC]))
        proc = subprocess.run([SPA_JSON_DUMP, str(path)], capture_output=True,
                              text=True, timeout=10, check=True)
        parsed = json.loads(proc.stdout)
        self.assertIs(parsed['monitor.bluez.properties']['bluez5.dummy-avrcp-player'], True)
        rules = parsed['monitor.bluez.rules']
        self.assertEqual(len(rules), 4)
        for index, address in enumerate(sorted([MAC, OTHER])):
            card, node = rules[index * 2:index * 2 + 2]
            for rule in (card, node):
                self.assertEqual(set(rule), {'matches', 'actions'})
                self.assertEqual(set(rule['actions']), {'update-props'})
            self.assertEqual(card['matches'], [{'device.name': 'bluez_card.' + address.replace(':', '_')}])
            self.assertEqual(card['actions']['update-props'], {
                'bluez5.auto-connect': ['hfp_hf', 'hfp_ag', 'a2dp_sink', 'a2dp_source']})
            self.assertEqual(node['actions']['update-props'], {'session.suspend-timeout-seconds': 0})
            self.assert_node_matches([match['node.name'] for match in node['matches']], address)

    def install_legacy_fragment(self):
        a.atomic_write(self.m.paths['config'], b'# Original foreign fragment\n', 0o640)
        a.put(self.m.paths[a.UNITS[0]], {'kind': 'link', 'target': '/dev/null'})
        self.system.units[a.UNITS[0]].update(active=False, enabled='masked')
        baseline = {k: a.snapshot(p) for k, p in self.m.paths.items()}
        with patch.object(a, 'fragment', return_value=LEGACY_FRAGMENT):
            self.m.apply()
        self.system.calls.clear()
        return baseline

    def test_legacy_upgrade_preserves_original_baseline(self):
        baseline = self.install_legacy_fragment()
        prior = self.m.read('state.json')
        self.m.apply()
        self.assertEqual(self.m.paths['config'].read_bytes(), a.fragment([MAC]))
        state = self.m.read('state.json')
        self.assertEqual(state['entries']['config']['before'], baseline['config'])
        self.assertEqual(state['services'], prior['services'])
        self.assertEqual(self.system.calls.count(('try-restart', 'wireplumber.service')), 1)
        calls = list(self.system.calls)
        self.m.apply()
        self.assertEqual(self.system.calls, calls)
        self.m.restore()
        self.assertEqual({k: a.snapshot(p) for k, p in self.m.paths.items()}, baseline)

    def test_failed_legacy_upgrade_restores_tracked_config_and_baseline(self):
        baseline = self.install_legacy_fragment()
        prior = self.m.read('state.json')
        self.system.fail = ('try-restart', 'wireplumber.service')
        with self.assertRaisesRegex(a.Error, 'rolled back'):
            self.m.apply()
        self.assertEqual(self.m.paths['config'].read_bytes(), LEGACY_FRAGMENT)
        self.assertEqual(self.m.read('state.json'), prior)
        self.assertIsNone(self.m.read('pending.json'))
        self.m.restore()
        self.assertEqual({k: a.snapshot(p) for k, p in self.m.paths.items()}, baseline)

    def test_initial_masks_and_unrelated_configs_survive(self):
        for unit in a.UNITS:
            a.put(self.m.paths[unit], {'kind': 'link', 'target': '/dev/null'})
            self.system.units[unit] = dict(exists=True, active=False, enabled='masked')
        others = [self.m.config / 'wireplumber/wireplumber.conf', self.m.paths['config'].with_name('51-bluez-avrcp.conf')]
        for path in others:
            a.atomic_write(path, b'original')
        self.m.apply()
        self.m.restore()
        for unit in a.UNITS:
            self.assertEqual(os.readlink(self.m.paths[unit]), '/dev/null')
        for path in others:
            self.assertEqual(path.read_bytes(), b'original')

    def test_foreign_files_and_enablement_preserved(self):
        for path in self.m.paths.values():
            a.atomic_write(path, b'foreign\x00bytes', 0o640)
        self.system.units[a.UNITS[1]] = dict(exists=True, active=True, enabled='enabled')
        wants = self.m.config / 'systemd/user/default.target.wants/mpris-proxy.service'
        a.put(wants, {'kind': 'link', 'target': '../mpris-proxy.service'})
        before = {k: a.snapshot(p) for k, p in self.m.paths.items()}
        self.m.apply()
        self.assertEqual(os.readlink(wants), '../mpris-proxy.service')
        self.m.restore()
        self.assertEqual(before, {k: a.snapshot(p) for k, p in self.m.paths.items()})
        self.assertTrue(self.system.units[a.UNITS[1]]['active'])

    def test_conflict_refuses_any_restoration(self):
        self.m.apply()
        a.atomic_write(self.m.paths['config'], b'manually changed')
        calls = list(self.system.calls)
        with self.assertRaisesRegex(a.Error, 'conflict'):
            self.m.restore()
        with self.assertRaisesRegex(a.Error, 'conflict'):
            self.m.apply()
        self.assertEqual(calls, self.system.calls)
        self.assertEqual(self.m.paths['config'].read_bytes(), b'manually changed')
        self.assertFalse(self.m.status()['configured'])

    def test_failure_rolls_back(self):
        a.atomic_write(self.m.paths['config'], b'foreign')
        self.system.fail = ('try-restart', 'wireplumber.service')
        with self.assertRaisesRegex(a.Error, 'rolled back'):
            self.m.apply()
        self.assertEqual(self.m.paths['config'].read_bytes(), b'foreign')
        self.assertFalse(self.m.paths[a.UNITS[0]].is_symlink())
        self.assertTrue(self.system.units[a.UNITS[0]]['active'])
        self.assertIsNone(self.m.read('state.json'))
        self.assertIsNone(self.m.read('pending.json'))

    def test_partial_restore_retries(self):
        self.m.apply()
        self.system.fail = ('start', a.UNITS[0])
        with self.assertRaises(a.Error):
            self.m.restore()
        self.assertIsNotNone(self.m.read('pending.json'))
        self.m.restore()
        self.assertTrue(self.system.units[a.UNITS[0]]['active'])
        self.assertIsNone(self.m.read('state.json'))

    def test_failed_reapply_recovers_previous_install(self):
        self.m.apply()
        old = self.m.paths['config'].read_bytes()
        self.system.items.append(device(OTHER))
        self.system.fail = ('try-restart', 'wireplumber.service')
        with self.assertRaises(a.Error):
            self.m.apply()
        self.assertEqual(self.m.paths['config'].read_bytes(), old)
        self.m.restore()
        self.assertFalse(self.m.paths['config'].exists())

    def test_explicit_renamed_and_paired_selection(self):
        self.system.items = [device(name='Renamed', airpods=False)]
        with self.assertRaises(a.Error):
            self.m.apply()
        self.m.apply(MAC.lower())
        self.m.restore()
        self.system.items[0]['paired'] = False
        with self.assertRaises(a.Error):
            self.m.apply(MAC)
        self.system.items = [device(eligible=False)]
        with self.assertRaises(a.Error):
            self.m.apply(MAC)

    def test_injection_rejected_before_discovery(self):
        with patch.object(self.system, 'devices', side_effect=AssertionError('must not discover')):
            for bad in ['--help', MAC + ';id', '$(id)', MAC + '\n', '../file']:
                with self.assertRaises(a.Error):
                    self.m.apply(bad)

    def test_reconnect_only_explicit_connected(self):
        with self.assertRaises(a.Error):
            self.m.reconnect(None)
        self.system.items[0]['connected'] = False
        with self.assertRaises(a.Error):
            self.m.reconnect(MAC)
        self.system.items[0]['connected'] = True
        self.m.reconnect(MAC)
        self.assertEqual(self.system.calls, [('reconnect', MAC)])

    def test_private_state_and_symlink_defenses(self):
        self.m.apply()
        self.assertEqual(self.m.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.m.state / 'state.json').stat().st_mode & 0o777, 0o600)
        self.m.restore()
        target = self.root / 'external'
        target.mkdir()
        self.m.paths['config'].parent.rmdir()
        self.m.paths['config'].parent.symlink_to(target)
        with self.assertRaisesRegex(a.Error, 'Symlink'):
            self.m.apply()
        self.assertEqual(list(target.iterdir()), [])

    def test_config_symlink_backed_up_without_reading_target(self):
        target = self.root / 'target'
        target.write_bytes(b'untouched')
        a.put(self.m.paths['config'], {'kind': 'link', 'target': str(target)})
        self.m.apply()
        self.m.restore()
        self.assertEqual(os.readlink(self.m.paths['config']), str(target))
        self.assertEqual(target.read_bytes(), b'untouched')

    def test_lock_timeout(self):
        with a.lock(self.m.state):
            with self.assertRaisesRegex(a.Error, 'lock'):
                with a.lock(self.m.state, timeout=.01):
                    pass

    def test_json_errors_and_unhealthy_status_exit_zero(self):
        for argv, expected in [(['status'], 0), (['nonsense'], 1), (['reconnect'], 1), (['--help'], 1)]:
            out = io.StringIO()
            with patch.object(a, 'Manager', return_value=self.m), contextlib.redirect_stdout(out):
                code = a.main(argv)
            self.assertEqual(code, expected)
            self.assertEqual(set(json.loads(out.getvalue())), {'configured', 'devices', 'issues', 'message'})

    def test_bluez_json_discovery(self):
        props = {'Address': {'type': 's', 'data': MAC}, 'Alias': {'type': 's', 'data': 'Renamed'},
                 'Paired': {'type': 'b', 'data': True}, 'Trusted': {'type': 'b', 'data': False},
                 'ManufacturerData': {'type': 'a{qv}', 'data': {'76': {'type': 'ay', 'data': [1]}}},
                 'Icon': {'type': 's', 'data': 'audio-headphones'}}
        payload = {'type': 'a{oa{sa{sv}}}', 'data': [{'/org/bluez/hci0/test': {'org.bluez.Device1': props}}]}
        with patch.object(a, 'run', return_value=json.dumps(payload)):
            d = a.System().devices()[0]
        self.assertTrue(d['eligible'])
        self.assertFalse(d['airpods'])
        self.assertFalse(d['trusted'])

    def test_process_timeout_and_no_shell(self):
        with patch.object(a.subprocess, 'run', side_effect=subprocess.TimeoutExpired('busctl', 15)) as call:
            with self.assertRaisesRegex(a.Error, 'timed out'):
                a.run(['busctl', '--system'])
            self.assertEqual(call.call_args.kwargs['timeout'], 15)
            self.assertNotIn('shell', call.call_args.kwargs)

    def test_initial_adoption_no_restart_and_preserves_everything(self):
        a.atomic_write(self.m.paths['config'], a.fragment([MAC]))
        a.put(self.m.paths[a.UNITS[0]], {'kind': 'link', 'target': '/dev/null'})
        self.system.units[a.UNITS[0]].update(active=False, enabled='masked')
        before = {k: a.snapshot(p) for k, p in self.m.paths.items()}
        self.m.apply()
        self.assertEqual(self.system.calls, [])
        self.assertTrue(self.m.status()['configured'])
        self.m.restore()
        self.assertEqual(before, {k: a.snapshot(p) for k, p in self.m.paths.items()})
        self.assertNotIn(('try-restart', 'wireplumber.service'), self.system.calls)

    def test_reconnect_gap_and_only_transient_retries(self):
        for reason in ['org.bluez.Error.InProgress', 'org.bluez.Error.NotReady']:
            with patch.object(a, 'run', side_effect=['', a.Error(reason), '']) as proc, patch.object(a.time, 'sleep') as sleep:
                a.System().reconnect(device())
                self.assertEqual([c.args[0][-1] for c in proc.call_args_list], ['Disconnect', 'Connect', 'Connect'])
                self.assertEqual([c.args[0] for c in sleep.call_args_list], [3, 1])
        with patch.object(a, 'run', side_effect=['', a.Error('AuthenticationFailed')]) as proc, patch.object(a.time, 'sleep'):
            with self.assertRaisesRegex(a.Error, 'AuthenticationFailed'):
                a.System().reconnect(device())
            self.assertEqual(proc.call_count, 2)
        with patch.object(a, 'run', side_effect=[''] + [a.Error('NotReady')] * 3) as proc, patch.object(a.time, 'sleep'):
            with self.assertRaises(a.Error):
                a.System().reconnect(device())
            self.assertEqual(proc.call_count, 4)

    def test_manual_service_start_conflicts(self):
        self.m.apply()
        self.system.units[a.UNITS[0]]['active'] = True
        with self.assertRaisesRegex(a.Error, 'manually started'):
            self.m.restore()
        self.assertTrue(self.system.units[a.UNITS[0]]['active'])

    def test_interrupted_apply_recovers_on_restore(self):
        original = a.put
        def crash(path, value):
            original(path, value)
            if path == self.m.paths['config']:
                raise KeyboardInterrupt()
        with patch.object(a, 'put', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.m.apply()
        self.assertIsNotNone(self.m.read('pending.json'))
        self.m.restore()
        self.assertFalse(self.m.paths['config'].exists())
        self.assertTrue(self.system.units[a.UNITS[0]]['active'])
        self.assertIsNone(self.m.read('pending.json'))

    def test_recovery_refuses_manual_edit_after_interruption(self):
        self.m.apply()
        self.system.fail = ('start', a.UNITS[0])
        with self.assertRaises(a.Error):
            self.m.restore()
        a.atomic_write(self.m.paths['config'], b'manual after partial restore')
        with self.assertRaisesRegex(a.Error, 'conflict'):
            self.m.restore()
        self.assertEqual(self.m.paths['config'].read_bytes(), b'manual after partial restore')
        self.assertIsNotNone(self.m.read('pending.json'))

    def test_no_import_time_live_effects(self):
        with patch('subprocess.run', side_effect=AssertionError('live process')), patch('pathlib.Path.home', side_effect=AssertionError('home access')):
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)


if __name__ == '__main__':
    unittest.main()
