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

    def power_on(self, d=None):
        self.calls.append(('power-on', d['address'] if d else None))

    def discover(self):
        self.power_on()
        self.calls.append(('scan-bredr', None))
        return self.items

    def trust(self, d):
        self.calls.append(('trust', d['address']))
        d['trusted'] = True

    def pair(self, d):
        self.calls.append(('pair', d['address']))
        d['paired'] = True

    def connect(self, d):
        self.calls.append(('connect', d['address']))
        d['connected'] = True

    def audio_ready(self, d):
        return False


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

    def test_discover_none_and_multiple_only_returns_choices(self):
        self.system.items = []
        result = self.m.discover()
        self.assertEqual(result['outcome'], 'not_found')
        self.assertEqual(result['devices'], [])
        self.system.items = [device(paired=False), device(OTHER, paired=False)]
        result = self.m.discover()
        self.assertEqual(result['outcome'], 'choice_required')
        self.assertEqual(len(result['devices']), 2)
        self.assertFalse(any(call[0] in ('pair', 'trust', 'connect') for call in self.system.calls))

    def test_discover_excludes_unrelated_apple_but_includes_known_renamed(self):
        self.m.apply()
        self.system.items = [device(airpods=False, eligible=False, name='Renamed'),
                             device(OTHER, airpods=False, eligible=True, name='Other Apple headphones')]
        result = self.m.discover()
        self.assertEqual(result['outcome'], 'candidate_found')
        self.assertEqual([d['address'] for d in result['devices']], [MAC])
        self.system.calls.clear()
        with self.assertRaises(a.RecoveryError) as error:
            self.m.recover_device(OTHER)
        self.assertEqual(error.exception.outcome, 'not_found')
        self.assertEqual(self.system.calls, [])

    def test_recover_existing_bond_never_pairs_or_disconnects(self):
        self.m.apply()
        self.system.calls.clear()
        self.system.items[0]['connected'] = False
        result = self.m.recover_device(MAC)
        self.assertEqual(result['outcome'], 'recovered')
        self.assertEqual(self.system.calls, [('power-on', MAC), ('connect', MAC)])
        self.system.calls.clear()
        self.m.recover_device(MAC)
        self.assertEqual(self.system.calls, [('power-on', MAC), ('connect', MAC)])

    def test_recover_unpaired_trusts_before_pair_and_preserves_baseline(self):
        a.atomic_write(self.m.paths['config'], b'foreign config', 0o640)
        self.system.items = [device(paired=False, trusted=False, connected=False)]
        result = self.m.recover_device(MAC)
        self.assertEqual(result['outcome'], 'recovered')
        self.assertLess(self.system.calls.index(('trust', MAC)), self.system.calls.index(('pair', MAC)))
        self.assertLess(self.system.calls.index(('pair', MAC)), self.system.calls.index(('connect', MAC)))
        self.assertFalse(result['configuration_pending'])
        self.m.restore()
        self.assertEqual(self.m.paths['config'].read_bytes(), b'foreign config')
        self.assertTrue(self.system.items[0]['paired'])

    def test_recover_does_not_repair_bond_completed_during_trust(self):
        self.system.items = [device(paired=False, trusted=False, connected=False)]
        def trust(d):
            d.update(trusted=True, paired=True)
        with patch.object(self.system, 'trust', side_effect=trust), patch.object(self.system, 'pair') as pair:
            self.m.recover_device(MAC)
        pair.assert_not_called()

    def test_recover_preserves_other_device_rules(self):
        self.m.apply()
        self.system.items = [device(OTHER, connected=False)]
        self.m.recover_device(OTHER)
        self.assertEqual(self.m.known_addresses(), {MAC, OTHER})
        self.assertEqual(self.m.paths['config'].read_bytes(), a.fragment([MAC, OTHER]))

    def test_recover_defers_restart_when_another_device_connected(self):
        self.m.apply()
        old = self.m.paths['config'].read_bytes()
        self.system.items.append(device(OTHER, paired=False, trusted=False, connected=False))
        self.system.calls.clear()
        result = self.m.recover_device(OTHER)
        self.assertTrue(result['configuration_pending'])
        self.assertEqual(self.m.paths['config'].read_bytes(), old)
        self.assertNotIn(('try-restart', 'wireplumber.service'), self.system.calls)
        self.assertTrue(self.system.items[0]['connected'])
        self.assertTrue(self.system.items[1]['connected'])

    def test_pending_setup_cli_preserves_known_rules_and_restore_baseline(self):
        a.atomic_write(self.m.paths['config'], b'# original settings\n', 0o640)
        baseline = {k: a.snapshot(p) for k, p in self.m.paths.items()}
        self.m.apply(MAC)
        self.system.items.append(device(OTHER, connected=False))
        result = self.m.recover_device(OTHER)
        self.assertTrue(result['configuration_pending'])
        self.assertEqual(self.m.known_addresses(), {MAC})
        # A may be absent when the user later approves setup for B.
        self.system.items = [device(OTHER)]
        out = io.StringIO()
        with patch.object(a, 'Manager', return_value=self.m), contextlib.redirect_stdout(out):
            self.assertEqual(a.main(['apply', '--device', OTHER, '--preserve-known']), 0)
        self.assertEqual(json.loads(out.getvalue())['issues'], [])
        self.assertEqual(self.m.known_addresses(), {MAC, OTHER})
        self.assertEqual(self.m.paths['config'].read_bytes(), a.fragment([MAC, OTHER]))
        calls = list(self.system.calls)
        with patch.object(a, 'Manager', return_value=self.m), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(a.main(['apply', '--device', OTHER, '--preserve-known']), 0)
        self.assertEqual(self.system.calls, calls)
        self.m.restore()
        self.assertEqual({k: a.snapshot(p) for k, p in self.m.paths.items()}, baseline)

    def test_preserve_known_flag_rejected_for_other_commands_before_actions(self):
        for command in ('status', 'restore', 'reconnect', 'discover', 'recover'):
            with self.subTest(command=command), patch.object(a, 'Manager') as manager, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(a.main([command, '--preserve-known']), 1)
                self.assertTrue(json.loads(out.getvalue())['issues'])
                manager.assert_not_called()

    def test_recover_failure_is_actionable_sanitized_and_keeps_bond(self):
        self.system.items = [device(paired=False, trusted=False, connected=False)]
        with patch.object(self.system, 'pair', side_effect=a.Error('timeout SECRET')):
            with self.assertRaises(a.RecoveryError) as error:
                self.m.recover_device(MAC)
        self.assertEqual(error.exception.outcome, 'pair_failed')
        self.assertNotIn('SECRET', str(error.exception))
        self.assertTrue(self.system.items[0]['trusted'])
        self.assertFalse(any(call[0] == 'connect' for call in self.system.calls))
        self.assertIsNone(self.m.read('state.json'))

    def test_recover_requires_explicit_valid_unique_device(self):
        for address in (None, MAC + ';remove', MAC + '\n'):
            with self.assertRaises(a.Error):
                self.m.recover_device(address)
        self.system.items.append(device())
        with self.assertRaises(a.RecoveryError) as error:
            self.m.recover_device(MAC)
        self.assertEqual(error.exception.outcome, 'adapter_choice_required')
        self.assertEqual(self.system.calls, [])

    def test_recover_absent_scans_once_and_pending_config_blocks_actions(self):
        self.system.items = []
        with self.assertRaises(a.RecoveryError) as error:
            self.m.recover_device(MAC)
        self.assertEqual(error.exception.outcome, 'not_found')
        self.assertEqual(self.system.calls, [('power-on', None), ('scan-bredr', None)])
        self.system.calls.clear()
        self.m.save('pending.json', {'version': 1})
        with self.assertRaises(a.RecoveryError) as error:
            self.m.recover_device(MAC)
        self.assertEqual(error.exception.outcome, 'restore_required')
        self.assertEqual(self.system.calls, [])

    def test_system_classic_discovery_and_power_are_bounded(self):
        system = a.System()
        objects = {'/org/bluez/hci0': {'org.bluez.Adapter1': {'Powered': False}}}
        with patch.object(system, 'objects', return_value=objects), patch.object(system, 'devices', return_value=[]), patch.object(a.shutil, 'which', return_value=None), patch.object(a, 'run', return_value='Discovery started\n') as run:
            self.assertEqual(system.discover(), [])
        calls = run.call_args_list
        self.assertEqual(calls[0].args[0][-4:], ['org.bluez.Adapter1', 'Powered', 'b', 'true'])
        self.assertEqual(calls[1].args[0], ['bluetoothctl', '--timeout', '10', 'scan', 'bredr'])
        self.assertEqual(calls[1].kwargs['timeout'], 15)
        objects['/org/bluez/hci0']['org.bluez.Adapter1']['Powered'] = True
        with patch.object(system, 'objects', return_value=objects), patch.object(a, 'run') as run:
            system.power_on()
        run.assert_not_called()

    def test_power_on_uses_omarchy_helper_for_softblocked_adapter(self):
        system = a.System()
        off = {'/org/bluez/hci0': {'org.bluez.Adapter1': {'Powered': False}}}
        on = {'/org/bluez/hci0': {'org.bluez.Adapter1': {'Powered': True}}}
        with patch.object(system, 'objects', side_effect=[off, on]), patch.object(a.shutil, 'which', return_value='/usr/bin/omarchy-bluetooth-power'), patch.object(a, 'run') as run:
            system.power_on()
        run.assert_called_once_with(['/usr/bin/omarchy-bluetooth-power', 'on'], timeout=15)
        with patch.object(system, 'objects', return_value=on), patch.object(a.shutil, 'which') as which, patch.object(a, 'run') as run:
            system.power_on()
        which.assert_not_called()
        run.assert_not_called()

    def test_power_on_targets_adapter_if_helper_only_powered_another(self):
        system = a.System()
        objects = {'/org/bluez/hci0': {'org.bluez.Adapter1': {'Powered': False}},
                   '/org/bluez/hci1': {'org.bluez.Adapter1': {'Powered': True}}}
        with patch.object(system, 'objects', return_value=objects), patch.object(a.shutil, 'which', return_value='/usr/bin/omarchy-bluetooth-power'), patch.object(a, 'run') as run:
            system.power_on(device())
        self.assertEqual(run.call_args_list[0].args[0], ['/usr/bin/omarchy-bluetooth-power', 'on'])
        self.assertEqual(run.call_args_list[1].args[0][-5:], ['/org/bluez/hci0', 'org.bluez.Adapter1', 'Powered', 'b', 'true'])

    def test_power_failure_reports_unblock_without_raw_error(self):
        system = a.System()
        off = {'/org/bluez/hci0': {'org.bluez.Adapter1': {'Powered': False}}}
        for helper in (None, '/usr/bin/omarchy-bluetooth-power'):
            with patch.object(system, 'objects', return_value=off), patch.object(a.shutil, 'which', return_value=helper), patch.object(a, 'run', side_effect=a.Error('blocked SECRET')):
                with self.assertRaises(a.RecoveryError) as error:
                    system.power_on()
            self.assertEqual(error.exception.outcome, 'power_failed')
            self.assertIn('Unblock', str(error.exception))
            self.assertNotIn('SECRET', str(error.exception))

    def test_discovery_zero_exit_failure_is_not_a_successful_scan(self):
        system = a.System()
        for output in ('Failed to start discovery: org.bluez.Error.NotReady\n',
                       'SetDiscoveryFilter success\n', '',
                       'Failed to set discovery filter\nDiscovery started\n',
                       'SetDiscoveryFilter failed: org.bluez.Error.Failed\nDiscovery started\n'):
            with self.subTest(output=output), patch.object(system, 'power_on'), patch.object(a, 'run', return_value=output), patch.object(system, 'devices') as devices:
                with self.assertRaises(a.RecoveryError) as error:
                    system.discover()
                self.assertEqual(error.exception.outcome, 'discovery_failed')
                devices.assert_not_called()

    def test_discovery_no_or_multiple_adapters_does_not_mutate(self):
        system = a.System()
        for objects, outcome in (({}, 'no_adapter'),
                                 ({'/org/bluez/hci0': {'org.bluez.Adapter1': {'Powered': True}},
                                   '/org/bluez/hci1': {'org.bluez.Adapter1': {'Powered': False}}}, 'adapter_choice_required')):
            with patch.object(system, 'objects', return_value=objects), patch.object(a, 'run') as run:
                with self.assertRaises(a.RecoveryError) as error:
                    system.discover()
            self.assertEqual(error.exception.outcome, outcome)
            run.assert_not_called()

    def test_recover_cli_contract_and_rejection_before_actions(self):
        for arguments, outcome in ((['discover'], 'candidate_found'),
                                   (['recover'], 'device_required'),
                                   (['recover', '--device', 'invalid'], 'operation_failed')):
            out = io.StringIO()
            with patch.object(a, 'Manager', return_value=self.m), contextlib.redirect_stdout(out):
                code = a.main(arguments)
            result = json.loads(out.getvalue())
            self.assertEqual(result['outcome'], outcome)
            self.assertEqual(code, 0 if outcome == 'candidate_found' else 1)
            self.assertIn('configuration_pending', result)
        self.assertFalse(any(call[0] in ('trust', 'pair', 'connect') for call in self.system.calls))

    def test_system_pair_uses_device_method_not_bluetoothctl(self):
        with patch.object(a, 'run') as run:
            a.System().pair(device())
        self.assertEqual(run.call_args.args[0][-2:], ['org.bluez.Device1', 'Pair'])
        self.assertEqual(run.call_args.kwargs['timeout'], 35)
        self.assertEqual(run.call_args.args[0][0], 'busctl')

    def test_recover_connects_profiles_after_pair_sets_connected(self):
        self.system.items = [device(paired=False, trusted=False, connected=False)]
        def pair(d):
            self.system.calls.append(('pair', d['address']))
            d.update(paired=True, connected=True)
        with patch.object(self.system, 'pair', side_effect=pair):
            result = self.m.recover_device(MAC)
        self.assertEqual(result['outcome'], 'recovered')
        self.assertIn(('connect', MAC), self.system.calls)
        self.assertTrue(result['configuration_pending'])

    def test_system_connect_only_tolerates_already_connected(self):
        for message in ('org.bluez.Error.AlreadyConnected', 'Already Connected'):
            with patch.object(a, 'run', side_effect=a.Error(message)) as run:
                a.System().connect(device())
            self.assertEqual(run.call_args.args[0][-1], 'Connect')
        for message in ('AuthenticationFailed', 'timeout', 'InProgress'):
            with patch.object(a, 'run', side_effect=a.Error(message)):
                with self.assertRaises(a.Error):
                    a.System().connect(device())

    def test_recover_skips_connect_for_established_audio_profile(self):
        self.m.apply()
        self.system.calls.clear()
        with patch.object(self.system, 'audio_ready', return_value=True), patch.object(self.system, 'connect') as connect:
            result = self.m.recover_device(MAC)
        self.assertEqual(result['outcome'], 'recovered')
        connect.assert_not_called()
        self.assertEqual(self.system.calls, [('power-on', MAC)])

    def test_recover_connect_timeout_accepts_only_fresh_working_audio(self):
        self.m.apply()
        for ready in (True, False):
            with self.subTest(ready=ready), patch.object(self.system, 'audio_ready', side_effect=[False, ready]) as audio, patch.object(self.system, 'connect', side_effect=a.Error('timeout SECRET')):
                if ready:
                    self.assertEqual(self.m.recover_device(MAC)['outcome'], 'recovered')
                else:
                    with self.assertRaises(a.RecoveryError) as error:
                        self.m.recover_device(MAC)
                    self.assertEqual(error.exception.outcome, 'connect_failed')
                    self.assertNotIn('SECRET', str(error.exception))
                self.assertEqual(audio.call_count, 2)

    def test_audio_ready_requires_selected_card_and_real_profile_sink(self):
        card = {'id': 50, 'type': 'PipeWire:Interface:Device', 'info': {'props': {
            'device.api': 'bluez5', 'api.bluez5.path': device()['path'],
            'api.bluez5.address': MAC, 'api.bluez5.connection': 'connected'}}}
        sink = {'id': 51, 'type': 'PipeWire:Interface:Node', 'info': {
            'state': 'running', 'props': {'device.api': 'bluez5', 'media.class': 'Audio/Sink',
                                        'device.id': 50, 'api.bluez5.profile': 'a2dp-sink'}}}
        def ready(objects, connected=True):
            with patch.object(a, 'run', return_value=json.dumps(objects)) as run:
                result = a.System().audio_ready(device(connected=connected))
            if connected:
                run.assert_called_once_with(['pw-dump'], timeout=5)
            else:
                run.assert_not_called()
            return result
        for state in ('running', 'idle', 'suspended'):
            sink['info']['state'] = state
            self.assertTrue(ready([card, sink]))
        self.assertFalse(ready([card, sink], connected=False))
        self.assertFalse(ready([card]))
        self.assertFalse(ready([sink]))
        for key, value in (('device.id', 999), ('media.class', 'Audio/Source'),
                           ('device.api', 'alsa'), ('api.bluez5.profile', '')):
            with patch.dict(sink['info']['props'], {key: value}):
                self.assertFalse(ready([card, sink]))
        for key, value in (('api.bluez5.path', '/org/bluez/hci1/other'),
                           ('api.bluez5.address', OTHER), ('api.bluez5.connection', 'disconnected')):
            with patch.dict(card['info']['props'], {key: value}):
                self.assertFalse(ready([card, sink]))
        sink['info']['state'] = 'error'
        self.assertFalse(ready([card, sink]))
        for output in ('invalid', 'null', '{}', '[null]'):
            with patch.object(a, 'run', return_value=output):
                self.assertFalse(a.System().audio_ready(device()))
        with patch.object(a, 'run', side_effect=a.Error('pw-dump timeout')):
            self.assertFalse(a.System().audio_ready(device()))

    def test_discovery_timeout_returns_sanitized_json(self):
        out = io.StringIO()
        with patch.object(self.system, 'discover', side_effect=a.Error('timeout SECRET')), patch.object(a, 'Manager', return_value=self.m), contextlib.redirect_stdout(out):
            self.assertEqual(a.main(['discover']), 1)
        result = json.loads(out.getvalue())
        self.assertEqual(result['outcome'], 'discovery_failed')
        self.assertNotIn('SECRET', out.getvalue())
        self.assertTrue(result['issues'])


if __name__ == '__main__':
    unittest.main()
