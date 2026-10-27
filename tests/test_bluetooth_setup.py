import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bluetooth_setup', Path(__file__).resolve().parents[1] / 'backend/bluetooth_setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'repo'
        self.bundle = self.source / 'companions/bluetooth'
        self.bundle.mkdir(parents=True)
        self.manifest = {'schemaVersion': 1, 'id': setup.PLUGIN, 'name': 'Bluetooth + AirPods',
                         'version': '0.3.0', 'kinds': ['bar-widget'],
                         'entryPoints': {'barWidget': 'Panel.qml'},
                         'omarchy': {'clonedFrom': setup.STOCK}}
        (self.bundle / 'manifest.json').write_text(json.dumps(self.manifest))
        (self.bundle / 'Panel.qml').write_text('import QtQuick\nItem {}\n')
        (self.source / 'manifest.json').write_text('{"id":"supercleanse.airpods"}')
        (self.source / 'backend').mkdir()
        (self.source / 'backend/airpods.py').write_text('# backend version one\n')
        (self.source / 'licenses').mkdir()
        (self.source / 'licenses/OMARCHY-MIT.txt').write_text('upstream license')
        for name in ('LICENSE', 'NOTICE.md'):
            (self.source / name).write_text(name)
        self.layout = {'left': [{'id': 'omarchy.menu'}], 'right': [
            {'id': setup.STOCK, 'custom': 42}, {'id': 'supercleanse.airpods', 'custom': 7}]}
        self.catalog = [{'id': setup.PLUGIN, 'clonedFrom': setup.STOCK, 'enabled': False}]
        self.calls = []
        self.fail = None
        self.manager = setup.Manager(self.source, self.root / 'plugins', self.command)

    def command(self, args):
        self.calls.append(args)
        if self.fail and self.fail(args):
            raise setup.Error('injected command failure')
        if args[0] == 'git':
            return str(self.source) if args[-1] == '--show-toplevel' else 'a' * 40
        if args[-1] == 'listShellConfig':
            return json.dumps({'bar': {'layout': self.layout}})
        if args[-1] == 'listPlugins':
            return json.dumps(self.catalog)
        if args[:3] == ['omarchy', 'plugin', 'validate']:
            # Exercise the real installed manifest validator without loading QML.
            if Path('/usr/share/omarchy/bin/omarchy-plugin-validate').exists():
                setup.run(['/usr/share/omarchy/bin/omarchy-plugin-validate', args[-1]])
        if args[:3] in (['omarchy', 'plugin', 'enable'], ['omarchy', 'plugin', 'disable']):
            before, after = ((setup.STOCK, setup.PLUGIN) if args[2] == 'enable'
                             else (setup.PLUGIN, setup.STOCK))
            for section in self.layout.values():
                for entry in section:
                    if entry['id'] == before:
                        entry['id'] = after
        return ''

    def test_install_reinstall_and_update_copy_canonical_backend(self):
        baseline = json.loads(json.dumps(self.layout))
        self.manager.install()
        self.assertEqual(self.layout, baseline)
        self.assertFalse(any(c[:3] == ['omarchy', 'plugin', 'enable'] for c in self.calls))
        self.assertEqual((self.manager.target / 'backend/airpods.py').read_bytes(),
                         (self.source / 'backend/airpods.py').read_bytes())
        self.assertFalse((self.manager.target / '.git').exists())
        self.assertFalse(any(p.is_symlink() for p in self.manager.target.rglob('*')))
        self.assertFalse(self.manager.install()['changed'])
        (self.source / 'backend/airpods.py').write_text('# updated\n')
        self.manager.install(update=True)
        self.assertEqual((self.manager.target / 'backend/airpods.py').read_text(), '# updated\n')
        self.assertEqual((self.manager.backup / 'backend/airpods.py').read_text(), '# backend version one\n')
        self.assertEqual(self.layout, baseline)

    def test_enable_preserves_position_settings_and_audio_then_remove_restores(self):
        baseline = json.loads(json.dumps(self.layout))
        self.manager.install()
        self.manager.enable()
        expected = json.loads(json.dumps(baseline))
        expected['right'][0]['id'] = setup.PLUGIN
        self.assertEqual(self.layout, expected)
        self.assertFalse(self.manager.enable()['changed'])
        self.manager.remove()
        self.assertEqual(self.layout, baseline)
        self.assertFalse(self.manager.target.exists())
        self.assertTrue(self.manager.backup.exists())
        self.assertFalse(self.manager.remove()['changed'])
        self.manager.install()
        self.assertTrue(self.manager.status()['installed'])

    def test_absent_duplicate_or_conflicting_bluetooth_slot_refuses_activation(self):
        self.manager.install()
        for entries, catalog in (([], self.catalog),
                                 ([{'id': setup.STOCK}, {'id': setup.STOCK}], self.catalog),
                                 ([{'id': setup.STOCK}], self.catalog + [
                                     {'id': 'other.bluetooth', 'clonedFrom': setup.STOCK, 'enabled': True}])):
            self.layout['right'] = entries
            self.catalog = catalog
            self.calls.clear()
            before = json.loads(json.dumps(self.layout))
            with self.assertRaises(setup.Error):
                self.manager.enable()
            self.assertEqual(self.layout, before)
            self.assertFalse(any(c[:3] == ['omarchy', 'plugin', 'enable'] for c in self.calls))

    def test_unowned_folder_is_not_replaced_or_removed(self):
        self.manager.target.mkdir(parents=True)
        local = self.manager.target / 'local.txt'
        local.write_text('keep')
        for action in (self.manager.install, self.manager.remove, self.manager.enable):
            with self.assertRaises(setup.Error):
                action()
            self.assertEqual(local.read_text(), 'keep')

    def test_edited_and_added_files_block_update_and_remove(self):
        self.manager.install()
        path = self.manager.target / 'Panel.qml'
        original = path.read_bytes()
        for edit in ('modified', 'added', 'mode'):
            with self.subTest(edit=edit):
                if edit == 'modified':
                    path.write_text('my edit')
                elif edit == 'added':
                    (self.manager.target / 'mine.txt').write_text('mine')
                else:
                    path.chmod(0o600)
                for action in (self.manager.install, self.manager.remove):
                    with self.assertRaises(setup.Error):
                        action()
                path.write_bytes(original)
                path.chmod(0o644)
                (self.manager.target / 'mine.txt').unlink(missing_ok=True)

    def test_symlinks_source_destination_receipt_and_backend_refused(self):
        destination = self.root / 'external'
        destination.write_text('unchanged')
        for path in (self.bundle / 'evil.qml', self.source / 'backend/evil.py', self.manager.target):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(destination)
            with self.assertRaises(setup.Error):
                self.manager.install()
            path.unlink()
        self.manager.install()
        receipt = self.manager.target / setup.RECEIPT
        receipt.unlink()
        receipt.symlink_to(destination)
        with self.assertRaises(setup.Error):
            self.manager.remove()
        self.assertEqual(destination.read_text(), 'unchanged')

    def test_writable_package_ancestor_is_refused(self):
        self.manager.plugins.mkdir()
        self.manager.plugins.chmod(0o777)
        with self.assertRaises(setup.Error):
            self.manager.install()
        self.assertFalse(self.manager.target.exists())

    def test_failed_validation_preserves_current_package(self):
        self.manager.install()
        before = setup.inventory(self.manager.target)
        self.fail = lambda args: args[:3] == ['omarchy', 'plugin', 'validate']
        with self.assertRaises(setup.Error):
            self.manager.install(update=True)
        self.assertEqual(setup.inventory(self.manager.target), before)
        self.assertFalse(self.manager.backup.exists())

    def test_failed_rescan_rolls_back_updated_files(self):
        self.manager.install()
        before = setup.inventory(self.manager.target)
        (self.bundle / 'Panel.qml').write_text('import QtQuick\nItem { visible: false }\n')
        self.fail = lambda args: args[-1] == 'rescanPlugins'
        with self.assertRaises(setup.Error):
            self.manager.install(update=True)
        self.assertEqual(setup.inventory(self.manager.target), before)

    def test_failed_disable_keeps_package_and_layout(self):
        self.manager.install()
        self.manager.enable()
        before = setup.inventory(self.manager.target)
        self.fail = lambda args: args[:3] == ['omarchy', 'plugin', 'disable']
        with self.assertRaises(setup.Error):
            self.manager.remove()
        self.assertEqual(setup.inventory(self.manager.target), before)
        self.assertEqual(self.layout['right'][0]['id'], setup.PLUGIN)

    def test_edited_backup_never_discarded(self):
        self.manager.install()
        (self.bundle / 'Panel.qml').write_text('import QtQuick\nItem { visible: false }\n')
        self.manager.install(update=True)
        (self.manager.backup / 'mine').write_text('keep')
        with self.assertRaises(setup.Error):
            self.manager.remove()
        self.assertEqual((self.manager.backup / 'mine').read_text(), 'keep')

    def test_status_is_read_only_and_cli_refuses_root(self):
        self.assertFalse(self.manager.status()['installed'])
        self.assertFalse(self.manager.plugins.exists())
        with patch.object(setup.os, 'geteuid', return_value=0), patch('builtins.print'):
            self.assertEqual(setup.main(['install']), 1)


if __name__ == '__main__':
    unittest.main()
