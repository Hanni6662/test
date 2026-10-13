"""Quiesced local backup validation. No external services or production volumes."""
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.config import get_settings
from app.integrations.storage.local import LocalPrivateStorage
from sqlalchemy import create_engine, text

ROOT = Path('/data/private')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def files():
    output = {}
    for p in sorted(ROOT.rglob('*')):
        if p.is_symlink():
            raise RuntimeError('Symlink not supported in private storage backup')
        if p.is_file():
            output[p.relative_to(ROOT).as_posix()] = {'size': p.stat().st_size, 'sha256': digest(p)}
    return output


def snapshot():
    engine = create_engine(get_settings().database_url.get_secret_value())
    storage = LocalPrivateStorage(ROOT)
    result = {'tables': {}, 'files': files(), 'assets': []}
    try:
        with engine.connect() as conn:
            heads = ScriptDirectory.from_config(Config('/app/alembic.ini')).get_heads()
            current = list(conn.scalars(text('SELECT version_num FROM alembic_version')))
            assert len(heads) == 1 and current == heads, ('migration head mismatch', current, heads)
            result['head'] = heads[0]
            tables = list(conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")))
            for table in tables:
                assert re.fullmatch('[a-zA-Z0-9_]+', table)
                rows = list(conn.scalars(text(f'SELECT to_jsonb(t)::text FROM public."{table}" t ORDER BY to_jsonb(t)::text')))
                result['tables'][table] = {'count': len(rows), 'sha256': hashlib.sha256(('\n'.join(rows)).encode()).hexdigest()}
            for row in conn.execute(text('SELECT id, owner_user_id, asset_kind, object_key, size_bytes, content_sha256 FROM image_assets ORDER BY object_key')).mappings():
                actual = storage.stat(row['object_key'])
                assert actual and actual.size_bytes == row['size_bytes'] and actual.sha256 == row['content_sha256'], 'asset integrity mismatch'
                result['assets'].append({k: str(v) if k in ('id', 'owner_user_id') else v for k, v in row.items()})
            assert conn.scalar(text("SELECT count(*) FROM pg_constraint WHERE connamespace='public'::regnamespace AND contype='f' AND NOT convalidated")) == 0
            result['foreign_keys_validated'] = True
            result['requirements_sha256'] = digest(Path('/tmp/release-requirements.lock')) if Path('/tmp/release-requirements.lock').exists() else None
        return result
    finally:
        engine.dispose()


def require_quiescent():
    engine = create_engine(get_settings().database_url.get_secret_value())
    try:
        with engine.connect() as conn:
            checks = [
                "SELECT count(*) FROM tasks WHERE status IN ('queued','prompting','needs_input','generating','reconciling','deleting')",
                "SELECT count(*) FROM recommendations WHERE status='analyzing'",
                "SELECT count(*) FROM task_actions WHERE status IN ('scheduled','claimed')",
                "SELECT count(*) FROM outbox_events WHERE status IN ('pending','claimed')",
                "SELECT count(*) FROM outbox_receipts WHERE status IN ('pending','processing')",
            ]
            assert all(conn.scalar(text(sql)) == 0 for sql in checks), 'Backup requires settled tasks/actions/outbox. Resume with up, allow drain, then retry a NEW backup name. Queue state is intentionally not migrated.'
    finally:
        engine.dispose()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def validate(bundle):
    manifest = read_json(bundle / 'manifest.json')
    assert manifest['format'] == 1
    for name in ('database.dump', 'private.tar', 'snapshot.json'):
        assert digest(bundle / name) == manifest['sha256'][name], name + ' corrupted'
    saved = read_json(bundle / 'snapshot.json')
    heads = ScriptDirectory.from_config(Config('/app/alembic.ini')).get_heads()
    assert heads == [saved['head']], 'Restore requires matching release and migration head'
    archive_files = {}
    with tarfile.open(bundle / 'private.tar', 'r') as tar:
        for member in tar.getmembers():
            from pathlib import PurePosixPath
            p = PurePosixPath(member.name)
            assert not p.is_absolute() and '..' not in p.parts and not member.issym() and not member.islnk()
            assert member.isdir() or member.isfile(), 'Unsupported archive member'
            if member.isfile():
                assert member.name not in archive_files, 'Duplicate archive member'
                stream = tar.extractfile(member)
                h = hashlib.sha256()
                size = 0
                while chunk := stream.read(1024 * 1024):
                    h.update(chunk)
                    size += len(chunk)
                archive_files[member.name] = {'size': size, 'sha256': h.hexdigest()}
    assert archive_files == saved['files'], 'Archive content differs from snapshot'
    return saved


command, name = sys.argv[1:]
if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_-]{1,80}', name):
    raise SystemExit('Backup name must use ASCII letters, digits, hyphen or underscore')
bundle = Path('/backup') / name
if command == 'pack':
    require_quiescent()
    bundle.mkdir(exist_ok=False)
    write_json(bundle / 'snapshot.json', snapshot())
    with tarfile.open(bundle / 'private.tar', 'w') as tar:
        for p in sorted(ROOT.rglob('*')):
            tar.add(p, arcname=p.relative_to(ROOT).as_posix(), recursive=False)
elif command == 'seal':
    write_json(bundle / 'manifest.json', {'format': 1, 'sha256': {n: digest(bundle / n) for n in ('database.dump', 'private.tar', 'snapshot.json')}})
    validate(bundle)
elif command == 'validate':
    validate(bundle)
elif command == 'unpack':
    validate(bundle)
    assert not list(ROOT.iterdir()), 'Restore refuses nonempty private volume'
    with tarfile.open(bundle / 'private.tar', 'r') as tar:
        tar.extractall(ROOT, filter='data')
    import os
    for p in [ROOT, *ROOT.rglob('*')]:
        os.chown(p, 10001, 10001)
        p.chmod(0o700 if p.is_dir() else 0o600)
elif command == 'verify':
    expected = validate(bundle)
    actual = snapshot()
    assert actual == expected, 'Restored database/files differ from backup snapshot'
    write_json(bundle / 'restore-verification.json', {'passed': True, 'head': actual['head'],
        'tables': actual['tables'], 'files': actual['files'], 'assets': actual['assets'], 'foreign_keys_validated': True})
elif command == 'empty':
    engine = create_engine(get_settings().database_url.get_secret_value())
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")) == 0, 'Restore refuses a populated database'
    engine.dispose()
else:
    raise SystemExit('Unknown maintenance command')
print(command + ': passed')
