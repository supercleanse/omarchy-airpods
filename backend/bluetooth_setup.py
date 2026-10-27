#!/usr/bin/env python3
"""Explicit user-only packaging for the Bluetooth companion; no startup hooks."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time

PLUGIN = 'supercleanse.airpods-bluetooth'
STOCK = 'omarchy.bluetooth'
RECEIPT = '.airpods-package.json'


class Error(RuntimeError):
    pass


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30,
                            stdin=subprocess.DEVNULL)
    if result.returncode:
        raise Error(f'{args[0]} failed: {result.stderr.strip()[:300]}')
    return result.stdout.strip()


def safe(path):
    """No symlinks, foreign owners, or writable package ancestors."""
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, os.getuid()):
            raise Error('Unsafe ownership or symlink: ' + str(item))
        sticky_parent = item != path and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky_parent:
            raise Error('Group/other-writable path: ' + str(item))


def inventory(root):
    safe(root)
    result = {}
    for path in sorted(root.rglob('*')):
        safe(path)
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            result[name] = {'directory': True, 'mode': stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if name != RECEIPT:
                result[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                'mode': stat.S_IMODE(info.st_mode)}
        else:
            raise Error('Non-regular package member: ' + str(path))
    return result


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Manager:
    def __init__(self, source=None, plugins=None, runner=run):
        self.source = Path(source or Path(__file__).absolute().parents[1])
        self.plugins = Path(plugins or Path.home() / '.config/omarchy/plugins')
        self.target = self.plugins / PLUGIN
        self.backup = self.plugins / ('.' + PLUGIN + '.previous')
        self.runner = runner

    @contextmanager
    def locked(self):
        safe(self.plugins)
        self.plugins.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = self.plugins / ('.' + PLUGIN + '.lock')
        safe(lock)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise Error('Invalid package lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def owned(self, root):
        safe(root)
        if not root.exists():
            return None
        if root.stat().st_uid != os.getuid():
            raise Error('Companion directory must belong to the desktop user')
        actual = inventory(root)
        receipt = root / RECEIPT
        try:
            saved = json.loads(receipt.read_text())
            if (saved['version'] != 1 or saved['id'] != PLUGIN
                    or saved['files'] != actual):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Error('Unowned or edited companion; preserving ' + str(root)) from exc
        return saved

    def rescan(self):
        self.runner(['omarchy-shell', 'shell', 'rescanPlugins'])

    def package(self, stage):
        safe(self.source)
        top = self.runner(['git', '-C', str(self.source), 'rev-parse', '--show-toplevel'])
        if Path(top) != self.source or json.loads((self.source / 'manifest.json').read_text())['id'] != 'supercleanse.airpods':
            raise Error('Run from the root Audio plugin git checkout')
        source = self.source / 'companions/bluetooth'
        inventory(source)
        if not source.is_dir():
            raise Error('Bluetooth companion source is missing')
        shutil.copytree(source, stage, dirs_exist_ok=True)
        (stage / 'backend').mkdir(exist_ok=True)
        # Copy the canonical Python modules together; never use cross-plugin links.
        for path in sorted((self.source / 'backend').glob('*.py')):
            safe(path)
            if not path.is_file() or path.stat().st_nlink != 1:
                raise Error('Invalid backend source')
            if path.name != 'bluetooth_setup.py':
                shutil.copy2(path, stage / 'backend' / path.name)
        for name in ('LICENSE', 'NOTICE.md'):
            safe(self.source / name)
            shutil.copy2(self.source / name, stage / name)
        inventory(self.source / 'licenses')
        shutil.copytree(self.source / 'licenses', stage / 'licenses', dirs_exist_ok=True)
        manifest = json.loads((stage / 'manifest.json').read_text())
        if manifest['id'] != PLUGIN or manifest.get('omarchy', {}).get('clonedFrom') != STOCK:
            raise Error('Unexpected companion identity')
        if not (stage / 'backend/airpods.py').is_file():
            raise Error('Missing companion backend')
        self.runner(['omarchy', 'plugin', 'validate', str(stage)])
        receipt = {'version': 1, 'id': PLUGIN, 'source': str(self.source),
                   'revision': self.runner(['git', '-C', str(self.source), 'rev-parse', 'HEAD']),
                   'files': inventory(stage)}
        # Inventory records actual bytes, including reviewed uncommitted development edits.
        (stage / RECEIPT).write_text(json.dumps(receipt, sort_keys=True))
        (stage / RECEIPT).chmod(0o600)
        for path in stage.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        for path in sorted((p for p in stage.rglob('*') if p.is_dir()), reverse=True):
            sync(path)
        sync(stage)

    def install(self, update=False):
        with self.locked():
            previous = self.owned(self.target)
            if update and previous is None:
                raise Error('Companion is not installed; use install')
            self.owned(self.backup)
            # A removed or interrupted package may remain in .previous; retain it.
            stage = Path(tempfile.mkdtemp(prefix='.' + PLUGIN + '.stage-', dir=self.plugins))
            try:
                self.package(stage)
                candidate = self.owned(stage)
                if previous and previous['files'] == candidate['files']:
                    return {'installed': True, 'changed': False, 'id': PLUGIN}
                if previous:
                    # Do not discard edited backups. One prior clean version is retained.
                    if self.backup.exists():
                        shutil.rmtree(self.backup)
                    self.target.rename(self.backup)
                    sync(self.plugins)
                try:
                    stage.rename(self.target)
                    sync(self.plugins)
                    self.rescan()
                except Exception:
                    if self.target.exists():
                        self.owned(self.target)
                        self.target.rename(stage)
                    if previous:
                        self.backup.rename(self.target)
                    sync(self.plugins)
                    try:
                        self.rescan()
                    except Exception:
                        pass
                    raise
                return {'installed': True, 'changed': True, 'id': PLUGIN,
                        'note': 'Run enable explicitly to replace the existing Bluetooth slot.'}
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

    def layout(self):
        config = json.loads(self.runner(['omarchy-shell', 'shell', 'listShellConfig']))
        return config.get('bar', {}).get('layout', {})

    def enable(self):
        with self.locked():
            if not self.owned(self.target):
                raise Error('Install the companion first')
            self.rescan()
            # Discovery is asynchronous; wait only for this manifest, never mutate a layout to probe.
            for attempt in range(20):
                catalog = json.loads(self.runner(['omarchy-shell', 'shell', 'listPlugins']))
                if any(p['id'] == PLUGIN for p in catalog):
                    break
                time.sleep(0.1)
            else:
                raise Error('Companion discovery timed out; retry enable')
            layout = self.layout()
            ids = [entry.get('id') if isinstance(entry, dict) else entry
                   for section in layout.values() for entry in section]
            if any(p.get('enabled') and p.get('clonedFrom') == STOCK and p['id'] != PLUGIN for p in catalog):
                raise Error('Another Bluetooth clone is active; disable it first')
            if ids.count(PLUGIN) == 1 and STOCK not in ids:
                return {'enabled': True, 'changed': False}
            if ids.count(STOCK) != 1 or PLUGIN in ids:
                raise Error('Exactly one stock Bluetooth slot is required; layout left unchanged')
            self.runner(['omarchy', 'plugin', 'enable', PLUGIN])
            expected = json.loads(json.dumps(layout))
            for section in expected.values():
                for index, entry in enumerate(section):
                    if isinstance(entry, dict) and entry.get('id') == STOCK:
                        entry['id'] = PLUGIN
                    elif entry == STOCK:
                        section[index] = {'id': PLUGIN}
            if self.layout() != expected:
                # Do not overwrite concurrent layout edits with an old config snapshot.
                raise Error('Layout changed during enable; inspect it before continuing')
            return {'enabled': True, 'changed': True}

    def remove(self):
        with self.locked():
            if not self.owned(self.target):
                return {'installed': False, 'changed': False}
            self.owned(self.backup)
            self.rescan()
            self.runner(['omarchy', 'plugin', 'disable', PLUGIN])
            # Retain a complete last-installed copy; stock Bluetooth is restored by disable.
            if self.backup.exists():
                shutil.rmtree(self.backup)
            self.target.rename(self.backup)
            sync(self.plugins)
            self.rescan()
            return {'installed': False, 'backup': str(self.backup), 'changed': True}

    def status(self):
        saved = self.owned(self.target)
        return {'installed': saved is not None, 'id': PLUGIN,
                'source': saved['source'] if saved else None,
                'revision': saved['revision'] if saved else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'update', 'enable', 'remove', 'status'))
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise Error('Run as your desktop user, never with sudo or pkexec')
        manager = Manager()
        result = manager.install(update=True) if args.action == 'update' else getattr(manager, args.action)()
        print(json.dumps(result))
        return 0
    except (Error, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
