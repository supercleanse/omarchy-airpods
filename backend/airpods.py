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
    def devices(self):
        raw = json.loads(run(['busctl', '--system', '--json=short', '--timeout=15', 'call',
                              'org.bluez', '/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects']))
        objects = unwrap(raw)
        if isinstance(objects, list) and len(objects) == 1:
            objects = objects[0]
        if not isinstance(objects, dict):
            raise Error('Unexpected BlueZ response')
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

    def select(self, address=None):
        if address is not None:
            address = validate_mac(address)
        devices = self.system.devices()
        selected = [d for d in devices if d['paired'] and (d['address'] == address and d['eligible'] if address else d['airpods'])]
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

    def apply(self, address=None):
        self.recover()
        selected = self.select(address)
        previous = self.read('state.json')
        if previous:
            self.check(previous['entries'])
        desired = {'config': file_value(fragment([d['address'] for d in selected]))}
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
    try:
        parser = Parser(add_help=False)
        parser.add_argument('command', choices=['status', 'apply', 'restore', 'reconnect'])
        parser.add_argument('--device')
        args = parser.parse_args(argv)
        if args.device:
            validate_mac(args.device)
        if args.device and args.command not in ('apply', 'reconnect'):
            raise Error('--device only applies to apply/reconnect')
        manager = Manager()
        with lock(manager.state):
            if args.command == 'status':
                result = manager.status()
            else:
                message = getattr(manager, args.command)(args.device) if args.command in ('apply', 'reconnect') else manager.restore()
                # Do not make successful mutation depend on a second BlueZ query.
                result = dict(configured=manager.read('state.json') is not None, devices=[], issues=[], message=message)
        print(json.dumps(result))
        return 0
    except Exception as e:
        print(json.dumps(dict(configured=False, devices=[], issues=[str(e)], message='Operation failed')))
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


if __name__ == '__main__':
    sys.exit(main())
