"""Generate immutable local-ID override from the exported image-ID list."""
import json
import re
from pathlib import Path

root = Path('/delivery/images')
lines = (root / 'image-ids.txt').read_text(encoding='utf-8-sig').splitlines()
assert len(lines) == 4
ids = [line.partition(' ')[0] for line in lines]
assert all(re.fullmatch(r'sha256:[a-f0-9]{64}', value) for value in ids)
app, python, postgres, rabbit = ids
services = {name: {'image': app} for name in ('migrate', 'storage-init', 'api', 'worker', 'dispatcher', 'scheduler', 'tools')}
services.update({'db': {'image': postgres}, 'rabbitmq': {'image': rabbit}})
(root / 'compose.offline.yaml').write_text(json.dumps({'services': services}, indent=2) + '\n')
(root / 'python-image-id.txt').write_text(python + '\n')
(root / 'image-lock.json').write_text(json.dumps(dict(zip(('app', 'python', 'postgres', 'rabbitmq'), ids)), indent=2))
print('Generated offline Compose override using immutable local image IDs.')
