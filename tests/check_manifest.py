"""Offline packaging checks; runtime validation is also required on Omarchy."""
import json
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'manifest.json').read_text())
assert type(manifest['schemaVersion']) is int and manifest['schemaVersion'] == 1
assert re.fullmatch(r'[a-z0-9][a-z0-9._-]*', manifest['id'])
assert not manifest['id'].startswith('omarchy.') and '..' not in manifest['id']
for field in ('name', 'version', 'author', 'description', 'license'):
    assert isinstance(manifest[field], str) and manifest[field].strip(), field
assert manifest['kinds'] == ['bar-widget']
assert manifest['omarchy']['clonedFrom'] == 'omarchy.audio'
assert manifest['barWidget']['defaultSection'] in ('left', 'center', 'right')
assert manifest['entryPoints']['barWidget']
for entry in manifest['entryPoints'].values():
    path = Path(entry)
    assert not path.is_absolute() and '..' not in entry and '\\' not in entry
    assert (root / path).is_file()
for path in root.rglob('*'):
    if '.git' not in path.parts:
        assert not path.is_symlink(), str(path)
for name in ('README.md', 'LICENSE', 'docs/PAIRING.md', 'docs/VALIDATION.md', 'NOTICE.md', 'licenses/OMARCHY-MIT.txt'):
    assert (root / name).is_file(), name
print('Package metadata, entry points, docs, and symlink checks passed')
