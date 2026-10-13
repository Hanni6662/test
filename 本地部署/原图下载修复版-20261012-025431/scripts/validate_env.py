import re
import ipaddress
import sys
from pathlib import Path

instance = sys.argv[1]
values = {}
for line in (Path('/delivery/instances') / (instance + '.env')).read_text().splitlines():
    if not line or line.startswith('#'):
        continue
    key, sep, value = line.partition('=')
    assert sep and key not in values, 'Malformed/duplicate configuration key'
    values[key] = value
keys = {'ADMIN_PASSWORD', 'MIGRATOR_PASSWORD', 'RUNTIME_PASSWORD', 'BROKER_PASSWORD', 'CALLBACK_SECRET', 'MAINTENANCE_SECRET'}
assert set(values) == keys | {'LOCAL_INSTANCE', 'API_PORT', 'APP_IMAGE', 'PRIVATE_SUBNET', 'INGRESS_SUBNET'}, 'Missing/unknown configuration key'
assert str(ipaddress.IPv4Network(values['PRIVATE_SUBNET'])) == values['PRIVATE_SUBNET']
assert str(ipaddress.IPv4Network(values['INGRESS_SUBNET'])) == values['INGRESS_SUBNET']
assert not ipaddress.IPv4Network(values['PRIVATE_SUBNET']).overlaps(ipaddress.IPv4Network(values['INGRESS_SUBNET']))
assert values['LOCAL_INSTANCE'] == instance and re.fullmatch('localimg-[a-z0-9][a-z0-9-]{1,40}', instance)
assert values['API_PORT'].isdigit() and 1024 <= int(values['API_PORT']) <= 65535
assert values['APP_IMAGE'] == 'local-private-backend:20261011-current'
assert all(re.fullmatch('[a-f0-9]{64}', values[k]) for k in keys), 'Credentials must be independent random hex64 values'
assert len({values[k] for k in keys}) == 6, 'Credentials must differ'
print('Instance configuration validated (secrets hidden).')
