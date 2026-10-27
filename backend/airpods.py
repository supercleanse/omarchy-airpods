#!/usr/bin/env python3
"""User-scoped AirPods configuration. No live effects on import.

State embeds private, lossless file backups. pending.json is a write-ahead undo
journal: after interruption, restore retries recovery before uninstalling.
Enablement links are never changed; masking overlays the unit definition only.
"""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

TIMEOUT = 15
MAX_FILE = 4 * 1024 * 1024
FRAGMENT = 'zz-omarchy-airpods.conf'
UNITS = ('mpris-proxy.service', 'bt-media-watch.service')


class Error(Exception):
    pass


class RecoveryError(Error):
    def __init__(self, outcome, message):
        super().__init__(message)
        self.outcome = outcome


def public_devices(devices):
    return [{k: d[k] for k in ('address', 'name', 'connected', 'paired', 'trusted')}
            for d in devices]


def validate_mac(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', value):
        raise Error('Invalid Bluetooth address')
    return value.upper()


def run(argv, timeout=TIMEOUT):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env={**os.environ, 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f'{argv[0]} unavailable or timed out') from e
    if p.returncode:
        raise Error(f'{argv[0]} {argv[1]} failed (exit {p.returncode}): {p.stderr.strip()[:400]}')
    return p.stdout


def safe_parent(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Error('Paths must be absolute without parent traversal')
    for parent in reversed(path.parents):
        if parent.is_symlink():
            raise Error('Symlink directory refused: ' + str(parent))


def mkdir(path, private=False):
    safe_parent(path / 'entry')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if private:
        if path.stat().st_uid != os.getuid():
            raise Error('State directory is not owned by current user')
        path.chmod(0o700)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def snapshot(path):
    safe_parent(path)
    try:
        s = path.lstat()
    except FileNotFoundError:
        return {'kind': 'absent'}
    if stat.S_ISLNK(s.st_mode):
        return {'kind': 'link', 'target': os.readlink(path)}
    if not stat.S_ISREG(s.st_mode) or s.st_size > MAX_FILE:
        raise Error('Refusing nonregular or oversized file: ' + str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_size > MAX_FILE:
            raise Error('File changed type or exceeded size limit')
        data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise Error('File too large')
    return {'kind': 'file', 'data': base64.b64encode(data).decode(), 'mode': stat.S_IMODE(s.st_mode)}


def file_value(data, mode=0o600):
    return {'kind': 'file', 'data': base64.b64encode(data).decode(), 'mode': mode}


def atomic_write(path, data, mode=0o600):
    put(path, file_value(data, mode))


def put(path, value):
    mkdir(path.parent)
    safe_parent(path)
    if value['kind'] == 'absent':
        path.unlink(missing_ok=True)
    else:
        fd, tmp = tempfile.mkstemp(prefix='.airpods-', dir=path.parent)
        try:
            if value['kind'] == 'file':
                with os.fdopen(fd, 'wb') as f:
                    os.fchmod(f.fileno(), value['mode'])
                    f.write(base64.b64decode(value['data'], validate=True))
                    f.flush()
                    os.fsync(f.fileno())
            elif value['kind'] == 'link':
                os.close(fd)
                os.unlink(tmp)
                os.symlink(value['target'], tmp)
            else:
                os.close(fd)
                raise Error('Invalid backup kind')
            os.replace(tmp, path)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
    sync_dir(path.parent)


@contextmanager
def lock(directory, timeout=5):
    mkdir(directory, private=True)
    fd = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Error('Lock must be a regular file')
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Error('Another AirPods operation holds the lock')
                time.sleep(.05)
        yield
    finally:
        os.close(fd)


def unwrap(value):
    if isinstance(value, dict):
        if set(value) == {'type', 'data'}:
            return unwrap(value['data'])
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value


class System:
    def objects(self):
        raw = json.loads(run(['busctl', '--system', '--json=short', '--timeout=15', 'call',
                              'org.bluez', '/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects']))
        objects = unwrap(raw)
        if isinstance(objects, list) and len(objects) == 1:
            objects = objects[0]
        if not isinstance(objects, dict):
            raise Error('Unexpected BlueZ response')
        return objects

    def devices(self):
        objects = self.objects()
        result = []
        for path, interfaces in objects.items():
            props = interfaces.get('org.bluez.Device1')
            if not props:
                continue
            address = validate_mac(props['Address'])
            name = str(props.get('Alias', props.get('Name', 'Bluetooth headphones')))
            known = 'airpods' in (name + ' ' + str(props.get('Name', ''))).lower()
            # Explicit selection supports renamed Apple audio devices; manufacturer
            # data alone is insufficient without an audio UUID or audio icon.
            apple = '76' in props.get('ManufacturerData', {}) or bool(re.search(r'(?:bluetooth:v004C|usb:v05AC)', str(props.get('Modalias', '')), re.I))
            audio = props.get('Icon') in ('audio-headset', 'audio-headphones') or any(str(u).lower().startswith(('0000110b-', '0000111e-')) for u in props.get('UUIDs', []))
            result.append(dict(address=address, name=name, connected=bool(props.get('Connected')),
                               paired=bool(props.get('Paired')), trusted=bool(props.get('Trusted')),
                               eligible=known or (apple and audio), airpods=known, path=path))
        return result

    def power_on(self, device=None):
        adapters = {path: interfaces['org.bluez.Adapter1']
                    for path, interfaces in self.objects().items()
                    if 'org.bluez.Adapter1' in interfaces}
        if device:
            path = device['path'].rsplit('/', 1)[0]
            adapters = {path: adapters[path]} if path in adapters else {}
        if not adapters:
            raise RecoveryError('no_adapter', 'No Bluetooth adapter available. Check Bluetooth hardware and service.')
        if len(adapters) != 1:
            raise RecoveryError('adapter_choice_required', 'Multiple Bluetooth adapters found. Use the Bluetooth panel to search on the intended adapter.')
        path, props = next(iter(adapters.items()))
        if not props.get('Powered', False):
            try:
                # Omarchy off is an rfkill soft block, so Powered=true alone
                # cannot undo it. Only request ON; never toggle or cycle radios.
                helper = shutil.which('omarchy-bluetooth-power')
                if helper:
                    run([helper, 'on'], timeout=15)
                    refreshed = self.objects().get(path, {}).get('org.bluez.Adapter1', {})
                    if refreshed.get('Powered', False):
                        return
                # The helper may see another powered adapter and finish before
                # this one is on. Also supports systems without the helper.
                run(['busctl', '--system', '--timeout=15', 'set-property', 'org.bluez',
                     path, 'org.bluez.Adapter1', 'Powered', 'b', 'true'])
            except Exception as exc:
                raise RecoveryError('power_failed', 'Bluetooth could not be enabled. Unblock Bluetooth in the system controls or hardware switch, then try again.') from exc

    def discover(self):
        self.power_on()
        # One persistent bluetoothctl connection owns the discovery session and
        # BR/EDR filter. Exiting releases only that session, not other scanners.
        output = run(['bluetoothctl', '--timeout', '10', 'scan', 'bredr'], timeout=15)
        # bluetoothctl's timed noninteractive mode may exit zero even when
        # StartDiscovery fails. Never present cached objects as a successful scan.
        if ('Discovery started' not in output or any(message in output for message in (
                'Failed to start discovery', 'Failed to set discovery filter',
                'SetDiscoveryFilter failed', 'No default controller available'))):
            raise RecoveryError('discovery_failed', 'Classic Bluetooth discovery did not start. Check adapter power and Bluetooth permissions, then try again.')
        return self.devices()

    def trust(self, device):
        run(['busctl', '--system', '--timeout=15', 'set-property', 'org.bluez',
             device['path'], 'org.bluez.Device1', 'Trusted', 'b', 'true'])

    def pair(self, device):
        # Never use bluetoothctl pair: it may remove an existing bond first.
        run(['busctl', '--system', '--timeout=30', 'call', 'org.bluez',
             device['path'], 'org.bluez.Device1', 'Pair'], timeout=35)

    def connect(self, device):
        try:
            run(['busctl', '--system', '--timeout=15', 'call', 'org.bluez',
                 device['path'], 'org.bluez.Device1', 'Connect'])
        except Error as exc:
            if not re.search(r'\bAlreadyConnected\b|\balready connected\b', str(exc), re.I):
                raise

    def audio_ready(self, device):
        """Require a real profile sink attached to this exact BlueZ device.

        Connected alone can be an ACL-only link. Virtual microphone nodes and
        unrelated/default audio sinks are not proof of an established profile.
        A suspended real sink still represents a usable, idle audio profile.
        """
        if not device['connected']:
            return False
        try:
            objects = json.loads(run(['pw-dump'], timeout=5))
            cards = set()
            for obj in objects:
                props = obj.get('info', {}).get('props', {})
                if (obj.get('type') == 'PipeWire:Interface:Device' and
                        props.get('device.api') == 'bluez5' and
                        props.get('api.bluez5.path') == device['path'] and
                        str(props.get('api.bluez5.address', '')).upper() == device['address'] and
                        props.get('api.bluez5.connection') == 'connected'):
                    cards.add(str(obj['id']))
            for obj in objects:
                info = obj.get('info', {})
                props = info.get('props', {})
                if (obj.get('type') == 'PipeWire:Interface:Node' and
                        info.get('state') in ('running', 'idle', 'suspended') and
                        props.get('media.class') == 'Audio/Sink' and
                        props.get('device.api') == 'bluez5' and
                        props.get('api.bluez5.profile') and
                        str(props.get('device.id')) in cards):
                    return True
        except (Error, ValueError, TypeError, KeyError, AttributeError):
            # Missing/unavailable PipeWire cannot prove that Connect is redundant.
            pass
        return False

    def unit(self, name):
        out = run(['systemctl', '--user', 'show', name, '--property=LoadState,ActiveState,UnitFileState'])
        values = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        if not {'LoadState', 'ActiveState', 'UnitFileState'} <= values.keys():
            raise Error('Incomplete systemd unit response')
        if values['ActiveState'] not in ('active', 'inactive', 'failed'):
            raise Error('Unit is transitioning; retry later')
        return dict(exists=values['LoadState'] != 'not-found', active=values['ActiveState'] == 'active',
                    enabled=values['UnitFileState'])

    def control(self, action, name=None):
        run(['systemctl', '--user', action] + ([name] if name else []))

    def reconnect(self, device):
        argv = ['busctl', '--system', '--timeout=15', 'call', 'org.bluez',
                device['path'], 'org.bluez.Device1']
        run(argv + ['Disconnect'])
        time.sleep(3)
        deadline = time.monotonic() + 30
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Error('Reconnect timed out')
            try:
                run(argv + ['Connect'], timeout=min(TIMEOUT, remaining))
                return
            except Error as e:
                transient = re.search(r'\b(InProgress|NotReady)\b|operation already in progress|resource not ready', str(e), re.I)
                if not transient or attempt == 2:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Error('Reconnect timed out') from e
                time.sleep(min(1, remaining))


def fragment(addresses):
    lines = ['# Managed by omarchy-airpods', 'monitor.bluez.properties = { bluez5.dummy-avrcp-player = true }',
             'monitor.bluez.rules = [']
    for address in sorted(addresses):
        address = validate_mac(address)
        mac = address.replace(':', '_')
        # Virtual sources may use colons and no suffix; sinks commonly use
        # underscores and a profile suffix. Anchor the complete device address.
        node_address = '(' + address + '|' + mac + ')([.].+)?$'
        lines += ['  { matches = [ { device.name = "bluez_card.' + mac + '" } ]',
                  '    actions = { update-props = { bluez5.auto-connect = [ hfp_hf hfp_ag a2dp_sink a2dp_source ] } } }',
                  '  { matches = [ { node.name = "~^bluez_input[.]' + node_address + '" }',
                  '                  { node.name = "~^bluez_output[.]' + node_address + '" } ]',
                  '    actions = { update-props = { session.suspend-timeout-seconds = 0 } } }']
    return ('\n'.join(lines + [']', ''])).encode()


class Manager:
    def __init__(self, system=None, config=None, state=None):
        home = Path.home()
        self.config = Path(config or os.environ.get('XDG_CONFIG_HOME', home / '.config'))
        self.state = Path(state or os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'omarchy-airpods'
        self.system = system or System()
        self.paths = {'config': self.config / 'wireplumber/wireplumber.conf.d' / FRAGMENT,
                      **{u: self.config / 'systemd/user' / u for u in UNITS}}

    def read(self, name):
        value = snapshot(self.state / name)
        if value['kind'] == 'absent':
            return None
        if value['kind'] != 'file':
            raise Error('Symlink state file refused')
        obj = json.loads(base64.b64decode(value['data']))
        if obj.get('version') != 1:
            raise Error('Unsupported state version')
        return obj

    def save(self, name, value):
        atomic_write(self.state / name, json.dumps(value, sort_keys=True).encode())

    def known_addresses(self):
        state = self.read('state.json')
        if not state:
            return set()
        # Read the plugin's recorded output, never a foreign configuration or
        # nearby Apple's manufacturer data. Also supports pre-recovery installs.
        value = state['entries']['config']['after']
        if value['kind'] != 'file':
            return set()
        text = base64.b64decode(value['data']).decode('utf-8')
        return {validate_mac(mac.replace('_', ':')) for mac in re.findall(
            r'device[.]name = "bluez_card[.]((?:[0-9A-F]{2}_){5}[0-9A-F]{2})"', text)}

    def select(self, address=None):
        if address is not None:
            address = validate_mac(address)
        devices = self.system.devices()
        known = self.known_addresses() if address else set()
        selected = [d for d in devices if d['paired'] and (d['address'] == address and (d['eligible'] or address in known) if address else d['airpods'])]
        if not selected:
            raise Error('No matching paired AirPods/Apple headphones; explicit selection requires Apple audio identification')
        if address and len(selected) != 1:
            raise Error('Address appears on multiple adapters; selection is ambiguous')
        return selected

    def check(self, entries, restoring=False):
        for key, item in entries.items():
            current = snapshot(self.paths[key])
            allowed = [item['after'], item['before']] if restoring else [item['after']]
            if current not in allowed:
                raise Error('Manual modification conflict: ' + str(self.paths[key]))

    def recover(self):
        pending = self.read('pending.json')
        if pending is None:
            return
        self.check(pending['entries'], restoring=True)
        for key, item in pending['entries'].items():
            if snapshot(self.paths[key]) != item['before']:
                put(self.paths[key], item['before'])
        self.system.control('daemon-reload')
        for unit, info in pending['services'].items():
            if self.system.unit(unit)['active'] != info['active']:
                self.system.control('start' if info['active'] else 'stop', unit)
        if pending['restart']:
            self.system.control('try-restart', 'wireplumber.service')
        if pending['previous'] is None:
            put(self.state / 'state.json', {'kind': 'absent'})
        else:
            self.save('state.json', pending['previous'])
        put(self.state / 'pending.json', {'kind': 'absent'})

    def apply(self, address=None, preserve_known=False):
        self.recover()
        selected = self.select(address)
        previous = self.read('state.json')
        if previous:
            self.check(previous['entries'])
        addresses = {d['address'] for d in selected}
        if preserve_known:
            addresses.update(self.known_addresses())
        desired = {'config': file_value(fragment(addresses))}
        services = {}
        for unit in UNITS:
            info = self.system.unit(unit)
            if unit == UNITS[0] or info['exists'] or snapshot(self.paths[unit])['kind'] != 'absent':
                if info['active'] and info['enabled'].startswith('masked'):
                    raise Error('Active masked service cannot be safely restored; stop it first')
                services[unit] = info
                desired[unit] = {'kind': 'link', 'target': '/dev/null'}
        # Keep previously owned units in the baseline across selection changes.
        entries = dict(previous['entries']) if previous else {}
        changes = {}
        for key, after in desired.items():
            before = snapshot(self.paths[key])
            entries[key] = {'before': entries.get(key, {}).get('before', before), 'after': after}
            if before != after:
                changes[key] = {'before': before, 'after': after}
        active = any(s['active'] for s in services.values())
        if not changes and not active:
            if previous is None:
                self.save('state.json', dict(version=1, entries=entries, services=services))
            return 'Already configured'
        restart = 'config' in changes
        pending = dict(version=1, entries=changes, services=services, restart=restart, previous=previous)
        self.save('pending.json', pending)
        try:
            for unit, info in services.items():
                if info['active']:
                    self.system.control('stop', unit)
            for key, change in changes.items():
                put(self.paths[key], change['after'])
            self.system.control('daemon-reload')
            if restart:
                self.system.control('try-restart', 'wireplumber.service')
            self.save('state.json', dict(version=1, entries=entries, services={**services, **(previous['services'] if previous else {})}))
            put(self.state / 'pending.json', {'kind': 'absent'})
        except Exception as e:
            try:
                self.recover()
            except Exception as rollback:
                raise Error(f'Apply failed: {e}; recovery incomplete: {rollback}; retry restore') from e
            raise Error(f'Apply failed and rolled back: {e}') from e
        return 'Configuration applied'

    def restore(self):
        self.recover()
        previous = self.read('state.json')
        if previous is None:
            return 'Nothing to restore'
        self.check(previous['entries'])
        for unit in previous['services']:
            if self.system.unit(unit)['active']:
                raise Error('Service manually started after apply: ' + unit)
        # Use the same durable undo mechanism, with the installation baseline as
        # the recovery destination. It can be retried after any partial failure.
        self.save('pending.json', dict(version=1, entries=previous['entries'], services=previous['services'],
                                      restart=previous['entries']['config']['before'] != previous['entries']['config']['after'], previous=None))
        self.recover()
        return 'Original configuration restored'

    def reconnect(self, address):
        if not address:
            raise Error('reconnect requires --device')
        device = self.select(address)[0]
        if not device['connected']:
            raise Error('Reconnect requires a currently connected paired device')
        self.system.reconnect(device)
        return 'Selected device reconnected'

    def recovery_result(self, outcome, devices, message, configuration_pending=False):
        return dict(configured=self.read('state.json') is not None,
                    devices=public_devices(devices), issues=[], message=message,
                    outcome=outcome, configuration_pending=configuration_pending)

    def candidates(self, devices):
        known = self.known_addresses()
        return [d for d in devices if d['airpods'] or d['address'] in known]

    def discover(self):
        try:
            candidates = self.candidates(self.system.discover())
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError('discovery_failed', 'Bluetooth search failed or timed out. Check the adapter, open the case in pairing mode, and try again.') from exc
        count = len(candidates)
        outcome = 'not_found' if not count else 'choice_required' if count > 1 else 'candidate_found'
        message = ('No AirPods found. Open the case and enable pairing mode, then search again.' if not count else
                   'Choose the AirPods to recover.' if count > 1 else 'Select Recover to connect these AirPods.')
        return self.recovery_result(outcome, candidates, message)

    def recover_device(self, address):
        if not address:
            raise RecoveryError('device_required', 'Choose one device, then use recover --device MAC.')
        address = validate_mac(address)
        if self.read('pending.json'):
            raise RecoveryError('restore_required', 'An interrupted configuration operation needs restore before Bluetooth recovery.')
        state = self.read('state.json')
        if state:
            self.check(state['entries'])

        def current():
            matches = [d for d in self.candidates(self.system.devices()) if d['address'] == address]
            if not matches:
                raise RecoveryError('not_found', 'Selected AirPods are unavailable. Open the case in pairing mode and search again.')
            if len(matches) != 1:
                raise RecoveryError('adapter_choice_required', 'This address appears on multiple adapters. Select it in the Bluetooth panel.')
            return matches[0]

        stage = 'discovery'
        try:
            # A missing object needs discovery; a cached unrelated Apple device
            # is never made eligible just because its address was supplied.
            devices = self.system.devices()
            if not any(d['address'] == address for d in devices):
                self.system.discover()
            device = current()
            stage = 'power'
            self.system.power_on(device)
            stage = 'trust'
            if not device['trusted']:
                self.system.trust(device)
            device = current()
            if not device['trusted']:
                raise RecoveryError('trust_failed', 'BlueZ did not confirm trust. Check Bluetooth permissions and try again.')
            stage = 'pair'
            if not device['paired']:
                self.system.pair(device)
                device = current()
                if not device['paired']:
                    raise RecoveryError('pair_failed', 'Pairing was not confirmed. Open pairing mode and try again; no bond was removed.')
            stage = 'configuration'
            addresses = self.known_addresses() | {address}
            desired = file_value(fragment(addresses))
            # Keep all current audio connections intact. A new/different fragment
            # requires a WP restart; leave that explicit apply step to the user.
            configuration_pending = (snapshot(self.paths['config']) != desired and
                                     any(d['connected'] for d in self.system.devices()))
            if not configuration_pending:
                self.apply(address, preserve_known=True)
            stage = 'connect'
            device = current()
            # Do not perturb a working profile. Pair can establish only an ACL,
            # so Connected=true alone must still lead to an explicit Connect.
            if not self.system.audio_ready(device):
                try:
                    self.system.connect(device)
                except Error:
                    # BlueZ may time out while audio remains/becomes healthy.
                    # Re-read both layers; never swallow failure for an ACL alone.
                    device = current()
                    if not self.system.audio_ready(device):
                        raise
            device = current()
            if not device['connected']:
                raise RecoveryError('connect_failed', 'Connection was not confirmed. Keep the AirPods nearby and try Recover again.')
            message = ('AirPods connected. Audio configuration is pending; choose Setup (apply) when audio can be interrupted.'
                       if configuration_pending else 'AirPods connected and configured.')
            return self.recovery_result('recovered', [device], message, configuration_pending)
        except RecoveryError:
            raise
        except Exception as exc:
            # Pairing errors may carry identifiers or authentication data. Expose
            # the failed stage and next action, never raw command diagnostics.
            raise RecoveryError(stage + '_failed', 'Recovery failed or timed out during ' + stage +
                                '. Check Bluetooth, open pairing mode if unpaired, and try again. Existing bonds were not removed; trust or a completed pairing may remain.') from exc

    def status(self):
        devices = self.system.devices()
        state = self.read('state.json')
        issues = []
        if self.read('pending.json'):
            issues.append('Interrupted operation; run restore to recover')
        if state:
            try:
                self.check(state['entries'])
            except Error as e:
                issues.append(str(e))
            for unit in state['services']:
                info = self.system.unit(unit)
                if info['active'] or info['enabled'] != 'masked':
                    issues.append(unit + ' is not persistently masked and stopped')
        else:
            issues.append('Not configured by omarchy-airpods')
        public = [{k: d[k] for k in ('address', 'name', 'connected', 'paired', 'trusted')} for d in devices if d['paired'] and d['eligible']]
        if not public:
            issues.append('No paired AirPods found')
        return dict(configured=bool(state) and not issues, devices=public, issues=issues,
                    message='Configured' if state and not issues else 'Attention needed')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Error(message)


def main(argv=None):
    # Bound file I/O too (for example a stalled network-backed XDG home).
    # The durable journal remains available if this deadline interrupts recovery.
    def expired(signum, frame):
        raise Error('Operation timed out; retry restore if a transaction was interrupted')

    old_handler = signal.signal(signal.SIGALRM, expired)
    signal.alarm(90)
    command = None
    try:
        parser = Parser(add_help=False)
        parser.add_argument('command', choices=['status', 'apply', 'restore', 'reconnect', 'discover', 'recover'])
        parser.add_argument('--device')
        parser.add_argument('--preserve-known', action='store_true')
        args = parser.parse_args(argv)
        command = args.command
        if args.preserve_known and args.command != 'apply':
            raise Error('--preserve-known is only valid with apply')
        if args.device:
            validate_mac(args.device)
        if args.device and args.command not in ('apply', 'reconnect', 'recover'):
            raise Error('--device only applies to apply/reconnect/recover')
        manager = Manager()
        with lock(manager.state):
            if args.command == 'status':
                result = manager.status()
            elif args.command == 'discover':
                result = manager.discover()
            elif args.command == 'recover':
                result = manager.recover_device(args.device)
            else:
                if args.command == 'apply':
                    message = manager.apply(args.device, preserve_known=args.preserve_known)
                else:
                    message = manager.reconnect(args.device) if args.command == 'reconnect' else manager.restore()
                # Do not make successful mutation depend on a second BlueZ query.
                result = dict(configured=manager.read('state.json') is not None, devices=[], issues=[], message=message)
        print(json.dumps(result))
        return 0
    except RecoveryError as e:
        print(json.dumps(dict(configured=False, devices=[], issues=[str(e)], message=str(e),
                              outcome=e.outcome, configuration_pending=False)))
        return 1
    except Exception as e:
        if command in ('discover', 'recover'):
            message = 'Recovery request could not complete. Check the selected address, configuration conflicts, or an operation already in progress.'
            print(json.dumps(dict(configured=False, devices=[], issues=[message], message=message,
                                  outcome='operation_failed', configuration_pending=False)))
        else:
            print(json.dumps(dict(configured=False, devices=[], issues=[str(e)], message='Operation failed')))
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


if __name__ == '__main__':
    sys.exit(main())
