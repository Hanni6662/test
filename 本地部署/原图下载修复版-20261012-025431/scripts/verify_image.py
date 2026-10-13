import hashlib
import json
from pathlib import Path

manifest = json.loads(Path('/delivery/source-release.json').read_text())
for name, expected in manifest.items():
    path = Path('/app') / name
    if name == 'requirements.lock':
        path = Path('/tmp/release-requirements.lock')
    elif name == 'requirements-dev.lock':
        path = Path('/tmp/release-requirements-dev.lock')
    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'Release image mismatch: ' + name
print('Current release source and dependency-lock hashes match the image.')
