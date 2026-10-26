#!/usr/bin/env python3
"""Optional user-only pairing service lifecycle; never changes BlueZ policy.

Transactions keep an undo journal until files, service, and ownership state
commit. A later enable/disable recovers interrupted work first. Edited owned
files block removal and recovery, preserving both edits and the journal.
Default-agent contention requires disabling other persistent pairing agents;
an active legacy bt-autoagent is explicitly refused. No service is installed
or started merely by importing this module or requesting status.
"""
import base64
import json
import os
from pathlib import Path
import sys
import stat

try:
    from . import airpods as core
except ImportError:
    import airpods as core

UNIT = 'omarchy-airpods-pairing.service'
ABSENT = {'kind': 'absent'}


def quote(value):
    """systemd ExecStart token: quote whitespace and escape expansion syntax."""
    value = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise core.Error('Control characters in service path refused')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


class System(core.System):
    def dropins(self):
        return core.run(['systemctl', '--user', 'show', UNIT, '--property=DropInPaths', '--value']).strip()

    def unit_paths(self):
        return [Path(line) for line in core.run(['systemd-analyze', '--user', 'unit-paths']).splitlines() if line]

    def check_agent(self, script, devices):
        # The agent's check mode never connects to D-Bus.
        core.run(['/usr/bin/python3', '-I', str(script), '--devices-file', str(devices), '--check'])


class Manager:
    def __init__(self, system=None, config=None, state=None, data=None, source=None):
        home = Path.home()
        self.config = Path(config or os.environ.get('XDG_CONFIG_HOME', home / '.config'))
        self.state = Path(state or os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'omarchy-airpods-pairing'
        self.data = Path(data or os.environ.get('XDG_DATA_HOME', home / '.local/share')) / 'omarchy-airpods'
        self.source = Path(source or Path(__file__).with_name('pairing_agent.py'))
        self.system = system or System()
        self.paths = {
            'unit': self.config / 'systemd/user' / UNIT,
            'agent': self.data / 'pairing_agent.py',
            'devices': self.state / 'pairing-devices.json',
            'link': self.config / 'systemd/user/default.target.wants' / UNIT,
        }
        for path in (*self.paths.values(), self.state / 'state.json'):
            core.safe_parent(path)
            quote(path)

    def secure_directories(self):
        """Refuse replaceable code/config/state; never repair existing permissions."""
        for target in self.paths.values():
            for directory in target.parents:
                try:
                    info = directory.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
                    raise core.Error('Unsafe installer directory ownership or type: ' + str(directory))
                # /tmp-style root-owned sticky ancestors protect secure user children.
                sticky_ancestor = (info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                                   and directory != target.parent)
                if info.st_mode & 0o022 and not sticky_ancestor:
                    raise core.Error('Writable installer directory refused: ' + str(directory))
                if directory == self.data and info.st_uid != os.getuid():
                    raise core.Error('Installed code directory must belong to the current user')

    def refuse_dropins(self):
        # The effective list catches inherited and runtime overrides. Unit search
        # paths also catch orphan/unloaded drop-ins before the new unit exists.
        if self.system.dropins():
            raise core.Error('Effective pairing service drop-ins refused')
        roots = {self.config / 'systemd/user', *self.system.unit_paths()}
        names = ('service.d', 'omarchy-.service.d', 'omarchy-airpods-.service.d', UNIT + '.d')
        for root in roots:
            core.safe_parent(root / 'entry')
            for name in names:
                directory = root / name
                if directory.is_symlink() or (directory.exists() and
                        (not directory.is_dir() or any(directory.glob('*.conf')))):
                    raise core.Error('Pairing service drop-in directory refused: ' + str(directory))

    def save(self, name, value):
        core.atomic_write(self.state / name, json.dumps(value, sort_keys=True).encode())

    def read(self, name):
        snap = core.snapshot(self.state / name)
        if snap == ABSENT:
            return None
        try:
            if snap['kind'] != 'file' or snap['mode'] != 0o600:
                raise ValueError
            obj = json.loads(base64.b64decode(snap['data']))
            if obj['version'] != 1 or obj['paths'] != {k: str(v) for k, v in self.paths.items()}:
                raise ValueError
            if set(obj['entries']) != set(self.paths):
                raise ValueError
            for item in obj['entries'].values():
                for side in ('before', 'after'):
                    value = item[side]
                    if value['kind'] not in ('absent', 'file', 'link'):
                        raise ValueError
                    if value['kind'] == 'file':
                        base64.b64decode(value['data'], validate=True)
                        if type(value['mode']) is not int or not 0 <= value['mode'] <= 0o777:
                            raise ValueError
                    if value['kind'] == 'link' and not isinstance(value['target'], str):
                        raise ValueError
            return obj
        except (KeyError, ValueError, TypeError):
            raise core.Error('Invalid pairing ownership state; retain for manual recovery') from None

    def record(self, entries, **extra):
        return dict(version=1, paths={k: str(v) for k, v in self.paths.items()}, entries=entries, **extra)

    def check(self, entries, recovery=False):
        for key, item in entries.items():
            allowed = (item['before'], item['after']) if recovery else (item['after'],)
            if core.snapshot(self.paths[key]) not in allowed:
                raise core.Error('Owned file changed; refusing to overwrite or remove: ' + str(self.paths[key]))

    def recover(self):
        pending = self.read('pending.json')
        if pending is None:
            return
        self.secure_directories()
        self.refuse_dropins()
        self.check(pending['entries'], recovery=True)
        # Stop before undoing code or the allowlist. Never systemctl disable:
        # it may remove enablement links that this installer did not create.
        info = self.system.unit(UNIT)
        if info['exists'] or info['active']:
            self.system.control('stop', UNIT)
        for key, item in pending['entries'].items():
            core.put(self.paths[key], item['before'])
        self.system.control('daemon-reload')
        if pending['active']:
            self.system.control('start', UNIT)
        if pending['previous'] is None:
            core.put(self.state / 'state.json', ABSENT)
        else:
            self.save('state.json', pending['previous'])
        core.put(self.state / 'pending.json', ABSENT)

    def desired(self, selected):
        command = ' '.join(quote(v) for v in ('/usr/bin/python3', '-I', self.paths['agent'], '--devices-file', self.paths['devices']))
        unit = ('[Unit]\nDescription=Selected trusted AirPods pairing agent\nAfter=dbus.socket\n\n'
                '[Service]\nType=exec\nExecStart=' + command + '\nRestart=on-failure\nRestartSec=5\n'
                '\n[Install]\nWantedBy=default.target\n')
        source = core.snapshot(self.source)
        if source['kind'] != 'file':
            raise core.Error('Agent source must be a regular file')
        return {
            'unit': core.file_value(unit.encode(), 0o644),
            'agent': core.file_value(base64.b64decode(source['data']), 0o600),
            'devices': core.file_value((json.dumps(sorted({core.validate_mac(d['address']) for d in selected})) + '\n').encode()),
            'link': {'kind': 'link', 'target': str(self.paths['unit'])},
        }

    def enable(self, address=None):
        self.secure_directories()
        self.refuse_dropins()
        with core.lock(self.state):
            self.recover()
            if self.system.unit('bt-autoagent.service')['active']:
                raise core.Error('Legacy bt-autoagent is active; disable it explicitly before enabling this agent')
            selected = core.Manager(system=self.system, config=self.config).select(address)
            if not selected or any(not d['paired'] or not d['trusted'] for d in selected):
                raise core.Error('Every selected device must already be paired and trusted')
            previous = self.read('state.json')
            info = self.system.unit(UNIT)
            if previous:
                self.check(previous['entries'])
            elif info['exists'] or info['active'] or info['enabled'] not in ('', 'disabled', 'not-found'):
                raise core.Error('Preexisting pairing service refused')
            wanted = self.desired(selected)
            entries = {k: {'before': core.snapshot(path), 'after': wanted[k]} for k, path in self.paths.items()}
            if previous is None and any(e['before'] != ABSENT for e in entries.values()):
                raise core.Error('Preexisting target file refused')
            pending = self.record(entries, active=info['active'], previous=previous)
            self.save('pending.json', pending)
            try:
                for key in ('agent', 'devices', 'unit', 'link'):
                    core.put(self.paths[key], wanted[key])
                self.secure_directories()
                self.system.check_agent(self.paths['agent'], self.paths['devices'])
                self.system.control('daemon-reload')
                self.refuse_dropins()
                # Own exactly one wants link; never expand WantedBy/Also/Alias.
                self.system.control('restart' if info['active'] else 'start', UNIT)
                self.check(entries)
                now = self.system.unit(UNIT)
                if not now['active'] or now['enabled'] != 'enabled':
                    raise core.Error('Pairing service did not become active and persistently enabled')
                owned = {k: {'before': ABSENT, 'after': v} for k, v in wanted.items()}
                self.save('state.json', self.record(owned))
                core.put(self.state / 'pending.json', ABSENT)
            except Exception as exc:
                try:
                    self.recover()
                except Exception as rollback:
                    raise core.Error('Enable failed; recovery pending. Preserve state and retry disable: ' + str(rollback)) from exc
                raise core.Error('Enable failed; previous files and service restored') from exc
            return {'enabled': True, 'managed': True, 'pending': False, 'issues': []}

    def disable(self):
        self.secure_directories()
        self.refuse_dropins()
        with core.lock(self.state):
            self.recover()
            previous = self.read('state.json')
            if previous is None:
                return {'enabled': False, 'managed': False, 'pending': False, 'issues': []}
            self.check(previous['entries'])
            self.save('pending.json', self.record(previous['entries'], active=False, previous=None))
            self.recover()
            return {'enabled': False, 'managed': False, 'pending': False, 'issues': []}

    def status(self):
        # Read-only: no lock directory creation or interrupted-work recovery.
        pending = self.read('pending.json')
        previous = self.read('state.json')
        issues = []
        if pending:
            issues.append('Interrupted operation; run disable to recover')
        if previous:
            try:
                self.check(previous['entries'])
            except core.Error as exc:
                issues.append(str(exc))
        info = self.system.unit(UNIT)
        return dict(enabled=info['active'] and info['enabled'] == 'enabled',
                    managed=previous is not None, pending=pending is not None, issues=issues)


def main(argv=None):
    try:
        parser = core.Parser(description=__doc__)
        parser.add_argument('command', choices=('enable', 'disable', 'status'))
        parser.add_argument('--device')
        args = parser.parse_args(argv)
        if args.device and args.command != 'enable':
            raise core.Error('--device is only valid with enable')
        manager = Manager()
        result = manager.enable(args.device) if args.command == 'enable' else getattr(manager, args.command)()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({'enabled': False, 'issues': [str(exc)], 'message': 'Operation failed'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
