import os
import re
import secrets
import sys
import ipaddress
import hashlib
from pathlib import Path

instance, port, occupied = sys.argv[1:]
if not re.fullmatch(r'localimg-[a-z0-9][a-z0-9-]{1,40}', instance):
    raise SystemExit('Instance must match localimg-[a-z0-9-], e.g. localimg-demo01')
if not port.isdigit() or not 1024 <= int(port) <= 65535:
    raise SystemExit('API port must be between 1024 and 65535')
path = Path('/delivery/instances') / (instance + '.env')
path.parent.mkdir(parents=True, exist_ok=True)
networks = [ipaddress.ip_network(n) for n in occupied.split() if ':' not in n]
for other in path.parent.glob('*.env'):
    for line in other.read_text(encoding='utf-8-sig').splitlines():
        key, _, value = line.partition('=')
        if key in {'PRIVATE_SUBNET', 'INGRESS_SUBNET'} and value:
            networks.append(ipaddress.ip_network(value))
start = hashlib.sha256(instance.encode()).digest()[0]
subnet = next((f'10.254.{(start + i) % 256}.0/24' for i in range(256)
               if not any(ipaddress.ip_network(f'10.{pool}.{(start + i) % 256}.0/24').overlaps(n) for n in networks for pool in (253, 254))), None)
if subnet is None:
    raise SystemExit('No unused 10.254.x.0/24 Docker subnet. Choose a free subnet manually in a fresh instance env.')
values = {'LOCAL_INSTANCE': instance, 'API_PORT': port, 'PRIVATE_SUBNET': subnet,
          'INGRESS_SUBNET': subnet.replace('10.254.', '10.253.'), 'APP_IMAGE': 'image-backend:delivery-run-closure-fix-20261013'}
keys = ('ADMIN_PASSWORD', 'MIGRATOR_PASSWORD', 'RUNTIME_PASSWORD', 'BROKER_PASSWORD', 'CALLBACK_SECRET', 'MAINTENANCE_SECRET')
values.update({k: secrets.token_hex(32) for k in keys})
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit('Existing credentials preserved; init refused. Use check/up or a new instance name.')
with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
    stream.write('\n'.join(k + '=' + v for k, v in values.items()) + '\n')
print('Created private instance configuration. Keep this file securely; it is not a template.')
