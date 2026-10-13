import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageOps

from .acceptance import (
    ACCEPTANCE_CONTRACT,
    initialize_acceptance_storage,
    insert_rejection,
    make_rejection_receipt,
    not_observed,
    read_rejection_row,
    rejection_binding_matches,
    rejection_row_to_dto,
    valid_sha256,
    valid_utc_timestamp,
    valid_uuid,
)
from .contracts import (
    CANVASES,
    CHATFLOW_IMAGE_CONTRACT,
    CHATFLOW_IMAGE_CONTRACT_V3,
    canonical,
    canonical_json_bytes,
    is_supported_chatflow_contract,
    prepare,
    prepare_chatflow,
    verify_chatflow_permit,
)
from .errors import ServiceError
from .network import MAX_BYTES, Network, decode
from .provider import Provider, ProviderError

TERMINAL = {'completed', 'failed', 'cancelled'}
ACTIVE = {'submitting', 'submission_unknown', 'pending', 'processing', 'persisting', 'protocol_error'}


class _PermitExpiredAtAcceptance(Exception):
    """Internal signal to rollback a tentative batch before persisting a receipt."""


@dataclass
class Settings:
    data_dir: Path
    token: str
    api_key: str
    public_url: str
    allowed_origins: tuple
    poll_seconds: float = 5
    max_submit_attempts: int = 3
    provider_url: str = 'https://api.apimart.ai'
    fake_ip_hosts: tuple = ()
    max_active_slots: int = 2
    generation_permit_secret: str = ''
    reference_backend_origin: str = ''
    reference_backend_token: str = ''

    def __post_init__(self):
        if (
            isinstance(self.max_active_slots, bool)
            or not isinstance(self.max_active_slots, int)
            or self.max_active_slots < 1
        ):
            raise ValueError('IMAGE_SERVICE_MAX_ACTIVE_SLOTS must be a positive integer')

    @staticmethod
    def _positive_int(value, name):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise RuntimeError(f'{name} must be a positive integer') from None
        if parsed < 1:
            raise RuntimeError(f'{name} must be a positive integer')
        return parsed

    @classmethod
    def from_env(cls):
        token = os.environ.get('IMAGE_SERVICE_TOKEN', '')
        key = os.environ.get('APIMART_API_KEY', '')
        if not token or not key:
            raise RuntimeError('IMAGE_SERVICE_TOKEN and APIMART_API_KEY must be configured')
        max_active_slots = cls._positive_int(
            os.environ.get('IMAGE_SERVICE_MAX_ACTIVE_SLOTS', '2'),
            'IMAGE_SERVICE_MAX_ACTIVE_SLOTS',
        )
        permit_secret = os.environ.get('IMAGE_SERVICE_GENERATION_PERMIT_SECRET', '')
        if permit_secret and (
            len(permit_secret) < 32
            or not permit_secret.isascii()
            or any(char.isspace() for char in permit_secret)
        ):
            raise RuntimeError(
                'IMAGE_SERVICE_GENERATION_PERMIT_SECRET must contain at least 32 '
                'non-whitespace ASCII characters'
            )
        reference_backend_origin = os.environ.get('IMAGE_SERVICE_REFERENCE_BACKEND_ORIGIN', '')
        reference_backend_token = os.environ.get('IMAGE_SERVICE_REFERENCE_BACKEND_TOKEN', '')
        if bool(reference_backend_origin) != bool(reference_backend_token):
            raise RuntimeError(
                'IMAGE_SERVICE_REFERENCE_BACKEND_ORIGIN and '
                'IMAGE_SERVICE_REFERENCE_BACKEND_TOKEN must be configured together'
            )
        if reference_backend_token and (
            len(reference_backend_token) < 16
            or not reference_backend_token.isascii()
            or any(char.isspace() for char in reference_backend_token)
        ):
            raise RuntimeError(
                'IMAGE_SERVICE_REFERENCE_BACKEND_TOKEN must contain at least 16 '
                'non-whitespace ASCII characters'
            )
        allowed_origins = tuple(
            value.strip()
            for value in os.environ.get('IMAGE_SERVICE_ALLOWED_REFERENCE_ORIGINS', '').split(',')
            if value.strip()
        )
        fake_ip_hosts = tuple(
            value.strip()
            for value in os.environ.get('IMAGE_SERVICE_RESULT_FAKE_IP_HOSTS', '').split(',')
            if value.strip()
        )
        return cls(
            data_dir=Path(os.environ.get('IMAGE_SERVICE_DATA_DIR', './data')),
            token=token,
            api_key=key,
            public_url=os.environ.get('IMAGE_SERVICE_PUBLIC_URL', 'http://localhost:8091').rstrip('/'),
            allowed_origins=allowed_origins,
            poll_seconds=max(1, float(os.environ.get('IMAGE_SERVICE_POLL_SECONDS', '5'))),
            max_submit_attempts=min(
                3, max(1, int(os.environ.get('IMAGE_SERVICE_MAX_SUBMIT_ATTEMPTS', '3')))
            ),
            provider_url=os.environ.get('APIMART_BASE_URL', 'https://api.apimart.ai').rstrip('/'),
            fake_ip_hosts=fake_ip_hosts,
            max_active_slots=max_active_slots,
            generation_permit_secret=permit_secret,
            reference_backend_origin=reference_backend_origin,
            reference_backend_token=reference_backend_token,
        )


class Engine:
    """One process owns the durable queue and enforces the configured active-slot cap."""

    def __init__(self, settings, network=None, provider=None, clock=None):
        self.settings = settings
        self.clock = clock or time.time
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.data_dir / 'jobs.sqlite3', timeout=30)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.execute(
            'CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, request_id TEXT UNIQUE, digest TEXT, token TEXT, document TEXT)'
        )
        self._migrate_sqlite_forward()
        self.db.commit()
        self.service_authority_id = initialize_acceptance_storage(self.db)
        self.network = network or Network(
            settings.allowed_origins,
            settings.fake_ip_hosts,
            settings.reference_backend_origin,
            settings.reference_backend_token,
        )
        self.provider = provider or Provider(settings.api_key, settings.provider_url)
        self.create_lock, self.tick_lock = asyncio.Lock(), asyncio.Lock()
        self.closed = False

    def _migrate_sqlite_forward(self):
        """Additive SQLite migration; legacy rows and JSON documents are untouched."""
        existing = {
            row[1] for row in self.db.execute('PRAGMA table_info(batches)').fetchall()
        }
        columns = {
            'contract_version': 'TEXT',
            'task_id': 'TEXT',
            'dify_run_id': 'TEXT',
            'package_digest': 'TEXT',
            'permit_id': 'TEXT',
            'permit_version': 'INTEGER',
            'permit_expires_at': 'TEXT',
            'slot_mapping_digest': 'TEXT',
            'reference_manifest_digest': 'TEXT',
            'request_digest': 'TEXT',
            'service_authority_id': 'TEXT',
            'status_version': 'INTEGER NOT NULL DEFAULT 0',
        }
        for name, declaration in columns.items():
            if name not in existing:
                self.db.execute(f'ALTER TABLE batches ADD COLUMN {name} {declaration}')
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS chatflow_generation_slots (
                batch_id TEXT NOT NULL,
                slot_id TEXT NOT NULL,
                task_item_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 8),
                image_type TEXT NOT NULL,
                status TEXT NOT NULL,
                status_version INTEGER NOT NULL,
                status_digest TEXT NOT NULL,
            error_code TEXT,
            known_retry_count INTEGER,
            artifact_ref TEXT,
            source_cleanup_eligible INTEGER NOT NULL DEFAULT 0 CHECK(source_cleanup_eligible IN (0,1)),
            PRIMARY KEY(batch_id, slot_id),
                UNIQUE(batch_id, position),
                UNIQUE(task_item_id, attempt_id),
            FOREIGN KEY(batch_id) REFERENCES batches(id) ON DELETE CASCADE
            )"""
        )
        slot_columns = {
            row[1]
            for row in self.db.execute(
                'PRAGMA table_info(chatflow_generation_slots)'
            ).fetchall()
        }
        if 'source_cleanup_eligible' not in slot_columns:
            self.db.execute(
                'ALTER TABLE chatflow_generation_slots ADD COLUMN '
                'source_cleanup_eligible INTEGER NOT NULL DEFAULT 0 '
                'CHECK(source_cleanup_eligible IN (0,1))'
            )
        self.db.execute(
            '''CREATE TABLE IF NOT EXISTS chatflow_delivery_confirmations (
                generation_request_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                slot_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                task_item_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                file_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                acknowledged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(generation_request_id, slot_id),
                FOREIGN KEY(batch_id, slot_id)
                    REFERENCES chatflow_generation_slots(batch_id, slot_id) ON DELETE CASCADE
            )'''
        )

    def close(self):
        if not self.closed:
            self.db.close()
            self.closed = True

    async def shutdown(self):
        for obj in (self.network, self.provider):
            if hasattr(obj, 'close'):
                await obj.close()
        self.close()

    def load(self, batch_id):
        row = self.db.execute('SELECT document FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise ServiceError('batch_not_found', 404)
        return json.loads(row[0])

    def save(self, batch):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute(
                'SELECT status_version FROM batches WHERE id=?', (batch['batch_id'],)
            ).fetchone()
            if not row:
                raise ServiceError('batch_not_found', 404)
            batch['source_sequence'] = int(row[0]) + 1
            self.db.execute(
                'UPDATE batches SET document=?, status_version=? WHERE id=?',
                (canonical(batch), batch['source_sequence'], batch['batch_id']),
            )
            for slot in batch['slots']:
                if not slot.get('task_item_id') or not slot.get('attempt_id'):
                    continue
                identity = {
                    'status': slot['status'],
                    'task_id': slot.get('task_id'),
                    'error_code': slot.get('error'),
                    'known_retry_count': (
                        None
                        if slot['status'] in ('submitting', 'submission_unknown')
                        else slot.get('attempts')
                    ),
                    'result_urls': slot.get('result_urls', []),
                    'original_files': slot.get('original_files', []),
                    'final_files': slot.get('final_files', []),
                }
                status_digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
                current = self.db.execute(
                    '''SELECT status_version,status_digest FROM chatflow_generation_slots
                       WHERE batch_id=? AND slot_id=?''',
                    (batch['batch_id'], slot['slot_id']),
                ).fetchone()
                if current is None:
                    raise ServiceError('slot_mapping_missing', 500)
                version = int(current[0]) + (current[1] != status_digest)
                artifact_ref = self._slot_artifact_json(batch, slot)
                self.db.execute(
                    '''UPDATE chatflow_generation_slots
                       SET status=?,status_version=?,status_digest=?,error_code=?,
                           known_retry_count=?,artifact_ref=?
                       WHERE batch_id=? AND slot_id=?''',
                    (
                        slot['status'], version, status_digest, slot.get('error'),
                        identity['known_retry_count'], artifact_ref,
                        batch['batch_id'], slot['slot_id'],
                    ),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _slot_artifacts(self, batch, slot):
        folder = self.settings.data_dir / batch['batch_id']
        base = self.settings.public_url.rstrip('/')
        query = '?token=' + batch['access_token']
        artifacts = []
        for kind, names in (('preview', slot.get('final_files', [])),
                            ('original', slot.get('original_files', []))):
            for name in names:
                path = folder / name
                if not path.is_file():
                    continue
                content = path.read_bytes()
                mime = 'image/png' if path.suffix.lower() == '.png' else (
                    'image/jpeg' if path.suffix.lower() in ('.jpg', '.jpeg') else 'image/webp'
                )
                artifacts.append({
                    'kind': kind,
                    'file_id': name,
                    'url': f'{base}/assets/{batch["batch_id"]}/{name}{query}',
                    'sha256': hashlib.sha256(content).hexdigest(),
                    'mime_type': mime,
                    'size_bytes': len(content),
                    'expires_at': None,
                })
        return artifacts

    def _slot_artifact_json(self, batch, slot):
        return canonical(self._slot_artifacts(batch, slot))

    def write(self, path, data):
        temp = path.with_suffix(path.suffix + '.tmp')
        with temp.open('wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)

    async def create(self, raw):
        prepared = prepare(raw)
        # All references must be retrieved and verified before any batch becomes runnable.
        # Serializing creation also prevents concurrent duplicates from racing on request_id.
        async with self.create_lock:
            fingerprint = hashlib.sha256(canonical({'package': prepared['package'], 'options': prepared['options'],
                                                     'references': prepared['references']}).encode()).hexdigest()
            previous = self.db.execute('SELECT id,digest FROM batches WHERE request_id=?', (prepared['request_id'],)).fetchone()
            if previous and self.load(previous[0]).get('request_fingerprint') == fingerprint:
                return self.get(previous[0])
            data = []
            for ref in prepared['references']:
                content = await self.network.image(ref['url'], reference=True)
                if len(content) > MAX_BYTES:
                    raise ServiceError('image_too_large')
                decode(content)
                data.append(content)
            if sum(map(len, data)) > 48 * 1024 * 1024:
                raise ServiceError('reference_set_too_large')
            manifests = [{'ref_id': ref['ref_id'], 'sha256': hashlib.sha256(content).hexdigest()}
                         for ref, content in zip(prepared['references'], data)]
            digest = hashlib.sha256(canonical({'package': prepared['package'], 'options': prepared['options'],
                                               'references': manifests}).encode()).hexdigest()
            previous = self.db.execute('SELECT id,digest FROM batches WHERE request_id=?', (prepared['request_id'],)).fetchone()
            if previous:
                if previous[1] != digest:
                    raise ServiceError('request_id_content_conflict', 409)
                existing = self.load(previous[0])
                existing['request_fingerprint'] = fingerprint
                self.save(existing)
                return self.get(previous[0])
            batch_id = 'batch_' + uuid.uuid4().hex
            folder = self.settings.data_dir / batch_id
            folder.mkdir()
            urls = []
            for i, content in enumerate(data):
                fmt = Image.open(io.BytesIO(content)).format
                mime = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}[fmt]
                self.write(folder / f'reference_{i + 1:02d}.bin', content)
                urls.append(f'data:{mime};base64,' + base64.b64encode(content).decode('ascii'))
            now = time.time()
            for slot in prepared['slots']:
                slot.update({'status': 'queued', 'task_id': None, 'error': None,
                             'attempts': 0, 'next_action': 0, 'first_submit': None,
                             'idempotency_key': f'{batch_id}-{slot["slot_id"]}',
                             'original_files': [], 'final_files': [], 'result_urls': [],
                             'body': {'model': 'gpt-image-2.5-ext', 'prompt': slot['prompt'],
                                      'image_urls': urls, 'n': 1, 'size': slot['provider_size'], **prepared['options']}})
            batch = {'batch_id': batch_id, 'request_id': prepared['request_id'], 'created_at': now,
                     'request_fingerprint': fingerprint,
                     'access_token': secrets.token_urlsafe(32), 'references': manifests, 'slots': prepared['slots']}
            self.db.execute(
                'INSERT INTO batches (id,request_id,digest,token,document) VALUES (?,?,?,?,?)',
                            (batch_id, prepared['request_id'], digest, batch['access_token'], canonical(batch)))
            self.db.commit()
            return self.get(batch_id)

    def _permit_expired(self, permit_expiry):
        return self.clock() >= datetime.fromisoformat(permit_expiry).timestamp()

    def _replay_batch_or_conflict(
        self, row, contract_version, request_digest, binding=None
    ):
        if row[2] != contract_version:
            raise ServiceError('generation_request_id_conflict', 409)
        if not isinstance(row[1], str) or not hmac.compare_digest(
            row[1], request_digest
        ):
            raise ServiceError('generation_request_content_conflict', 409)
        if binding is not None:
            stored = self.db.execute(
                """SELECT task_id,dify_run_id,permit_id,permit_version,permit_expires_at,
                          package_digest,slot_mapping_digest,reference_manifest_digest,
                          service_authority_id
                   FROM batches WHERE id=?""",
                (row[0],),
            ).fetchone()
            expected = tuple(
                binding[key]
                for key in (
                    'task_id',
                    'dify_run_id',
                    'permit_id',
                    'permit_version',
                    'permit_expires_at',
                    'package_digest',
                    'slot_mapping_digest',
                    'reference_manifest_digest',
                    'service_authority_id',
                )
            )
            if stored != expected:
                raise ServiceError('generation_request_content_conflict', 409)
        return row[0]

    @staticmethod
    def _raise_expired_receipt(receipt):
        error = ServiceError('generation_permit_expired', 410)
        if receipt is not None:
            error.payload = receipt
        raise error

    def _raise_stored_rejection(self, row, binding):
        if not rejection_binding_matches(row, binding):
            raise ServiceError('generation_request_content_conflict', 409)
        receipt = rejection_row_to_dto(row)
        if receipt is None:
            raise ServiceError('generation_acceptance_unavailable', 503)
        self._raise_expired_receipt(receipt)

    def _persist_expired_rejection(self, binding):
        """Persist a rejection in a separate, locked transaction after rollback."""
        candidate = make_rejection_receipt(
            binding,
            receipt_id=str(uuid.uuid4()),
            rejected_at=datetime.fromtimestamp(self.clock(), UTC).isoformat(),
        )
        try:
            self.db.execute('BEGIN IMMEDIATE')
            batch = self.db.execute(
                'SELECT id,digest,contract_version FROM batches WHERE request_id=?',
                (binding['generation_request_id'],),
            ).fetchone()
            if batch:
                self.db.rollback()
                return 'batch', batch
            rejection = read_rejection_row(self.db, binding['generation_request_id'])
            if rejection:
                self.db.rollback()
                return 'rejection', rejection
            insert_rejection(self.db, binding, candidate)
            self.db.commit()
            rejection = read_rejection_row(self.db, binding['generation_request_id'])
            if rejection is None:
                raise sqlite3.DatabaseError('receipt commit was not observable')
            return 'rejection', rejection
        except sqlite3.Error:
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            # A commit error can be ambiguous.  Only report a durable outcome
            # after reading it back; otherwise the caller receives unknown/503.
            try:
                batch = self.db.execute(
                    'SELECT id,digest,contract_version FROM batches WHERE request_id=?',
                    (binding['generation_request_id'],),
                ).fetchone()
                if batch:
                    return 'batch', batch
                rejection = read_rejection_row(
                    self.db, binding['generation_request_id']
                )
                if rejection:
                    return 'rejection', rejection
            except sqlite3.Error:
                pass
            raise ServiceError('generation_acceptance_unknown', 503) from None

    def _resolve_expiry_outcome(self, binding, request_digest, contract_version):
        if (
            binding['generation_contract_version'] != CHATFLOW_IMAGE_CONTRACT_V3
            or binding['service_authority_id'] is None
        ):
            # Legacy permit claims keep their previous 410 behavior, but cannot
            # create or replay authoritative rejection evidence.
            self._raise_expired_receipt(None)
        outcome, row = self._persist_expired_rejection(binding)
        if outcome == 'batch':
            batch_id = self._replay_batch_or_conflict(
                row, contract_version, request_digest
            )
            return self.get_chatflow(
                batch_id, expected_contract_version=contract_version
            )
        self._raise_stored_rejection(row, binding)

    async def create_chatflow(
        self, raw, *, route_contract_version=CHATFLOW_IMAGE_CONTRACT_V3
    ):
        prepared = prepare_chatflow(raw)
        contract_version = prepared['contract_version']
        if contract_version != route_contract_version:
            raise ServiceError('unsupported_chatflow_contract', 409)
        claims = verify_chatflow_permit(
            prepared['permit'],
            self.settings.generation_permit_secret,
            allow_expired=True,
        )
        expected_claims = {
            'generation_request_id': prepared['generation_request_id'],
            'task_id': prepared['task_id'],
            'package_digest': prepared['package_digest'],
            'slot_mapping_digest': prepared['slot_mapping_digest'],
            'reference_manifest_digest': prepared['reference_manifest_digest'],
        }
        if any(claims.get(key) != value for key, value in expected_claims.items()):
            raise ServiceError('generation_permit_binding_mismatch', 403)
        if not isinstance(claims.get('permit_id'), str):
            raise ServiceError('invalid_generation_permit', 403)
        service_authority_id = claims.get('service_authority_id')
        if (
            service_authority_id is not None
            and service_authority_id != self.service_authority_id
        ):
            raise ServiceError('generation_service_authority_mismatch', 403)
        permit_expiry = claims['expires_at']

        canonical_content = {
            'contract_version': contract_version,
            'generation_request_id': prepared['generation_request_id'],
            'task_id': prepared['task_id'],
            'dify_run_id': prepared['dify_run_id'],
            'permit_id': claims['permit_id'],
            'permit_version': 2,
            'package_digest': prepared['package_digest'],
            'slot_mapping_digest': prepared['slot_mapping_digest'],
            'reference_manifest_digest': prepared['reference_manifest_digest'],
            'package': prepared['package'],
            'references': prepared['reference_manifest'],
            'slots': [
                {
                    key: slot[key]
                    for key in (
                        'slot_id',
                        'position',
                        'image_type',
                        'task_item_id',
                        'attempt_id',
                    )
                }
                for slot in prepared['slots']
            ],
            'options': prepared['options'],
        }
        if service_authority_id is not None:
            canonical_content['service_authority_id'] = service_authority_id
            canonical_content['permit_expires_at'] = claims['expires_at']
        try:
            request_digest = hashlib.sha256(
                canonical_json_bytes(canonical_content)
            ).hexdigest()
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise ServiceError('invalid_chatflow_request') from None

        binding = {
            'service_authority_id': service_authority_id,
            'generation_contract_version': contract_version,
            'generation_request_id': prepared['generation_request_id'],
            'task_id': prepared['task_id'],
            'dify_run_id': prepared['dify_run_id'],
            'permit_id': claims['permit_id'],
            'permit_version': 2,
            'permit_expires_at': permit_expiry,
            'package_digest': prepared['package_digest'],
            'slot_mapping_digest': prepared['slot_mapping_digest'],
            'reference_manifest_digest': prepared['reference_manifest_digest'],
            'service_request_digest': request_digest,
        }

        async with self.create_lock:
            existing = self.db.execute(
                """SELECT id,digest,contract_version FROM batches WHERE request_id=?""",
                (prepared['generation_request_id'],),
            ).fetchone()
            if existing:
                batch_id = self._replay_batch_or_conflict(
                    existing, contract_version, request_digest, binding
                )
                return self.get_chatflow(
                    batch_id, expected_contract_version=contract_version
                )

            if contract_version != CHATFLOW_IMAGE_CONTRACT_V3:
                raise ServiceError('chatflow_v2_write_disabled', 409)
            # Legacy permit shape is accepted only for exact replay. A new v3
            # write requires the SQLite-pinned authority.
            if service_authority_id is None:
                if self._permit_expired(permit_expiry):
                    raise ServiceError('generation_permit_expired', 410)
                raise ServiceError('generation_service_authority_required', 403)

            rejection = read_rejection_row(self.db, prepared['generation_request_id'])
            if rejection:
                self._raise_stored_rejection(rejection, binding)
            if self._permit_expired(permit_expiry):
                return self._resolve_expiry_outcome(
                    binding, request_digest, contract_version
                )

            # Keep the SQLite write reservation through reference retrieval and
            # the acceptance decision.  This serializes separate Engine
            # connections sharing a database file, including test instances.
            try:
                self.db.execute('BEGIN IMMEDIATE')
            except sqlite3.Error:
                if self._permit_expired(permit_expiry):
                    raise ServiceError('generation_acceptance_unknown', 503) from None
                raise
            existing = self.db.execute(
                'SELECT id,digest,contract_version FROM batches WHERE request_id=?',
                (prepared['generation_request_id'],),
            ).fetchone()
            if existing:
                self.db.rollback()
                batch_id = self._replay_batch_or_conflict(
                    existing, contract_version, request_digest, binding
                )
                return self.get_chatflow(
                    batch_id, expected_contract_version=contract_version
                )
            rejection = read_rejection_row(self.db, prepared['generation_request_id'])
            if rejection:
                self.db.rollback()
                self._raise_stored_rejection(rejection, binding)
            if self._permit_expired(permit_expiry):
                self.db.rollback()
                return self._resolve_expiry_outcome(
                    binding, request_digest, contract_version
                )

            data = []
            try:
                for reference in prepared['references']:
                    content = await self.network.backend_reference(
                        prepared['generation_request_id'],
                        prepared['task_id'],
                        reference,
                    )
                    if len(content) > MAX_BYTES:
                        raise ServiceError('image_too_large')
                    decode(content)
                    actual_digest = hashlib.sha256(content).hexdigest()
                    if not hmac.compare_digest(
                        actual_digest, reference['content_sha256']
                    ):
                        raise ServiceError('reference_digest_mismatch', 409)
                    data.append(content)
                if sum(map(len, data)) > 48 * 1024 * 1024:
                    raise ServiceError('reference_set_too_large')
            except asyncio.CancelledError:
                self.db.rollback()
                raise
            except Exception:
                self.db.rollback()
                if self._permit_expired(permit_expiry):
                    return self._resolve_expiry_outcome(
                        binding, request_digest, contract_version
                    )
                raise
            if self._permit_expired(permit_expiry):
                self.db.rollback()
                return self._resolve_expiry_outcome(
                    binding, request_digest, contract_version
                )

            manifests = [
                {
                    'asset_id': reference['asset_id'],
                    'ref_id': reference['ref_id'],
                    'position': reference['order'],
                    'sha256': hashlib.sha256(content).hexdigest(),
                }
                for reference, content in zip(prepared['references'], data, strict=True)
            ]
            batch_id = 'batch_' + uuid.uuid4().hex
            folder = self.settings.data_dir / batch_id
            urls = []
            try:
                folder.mkdir()
                for index, content in enumerate(data, 1):
                    fmt = Image.open(io.BytesIO(content)).format
                    mime = {
                        'JPEG': 'image/jpeg',
                        'PNG': 'image/png',
                        'WEBP': 'image/webp',
                    }[fmt]
                    self.write(folder / f'reference_{index:02d}.bin', content)
                    urls.append(
                        f'data:{mime};base64,'
                        + base64.b64encode(content).decode('ascii')
                    )

                now = self.clock()
                for slot in prepared['slots']:
                    slot.update(
                        {
                            'status': 'queued',
                            'task_id': None,
                            'error': None,
                            'attempts': 0,
                            'next_action': 0,
                            'first_submit': None,
                            'idempotency_key': f'{batch_id}-{slot["slot_id"]}',
                            'original_files': [],
                            'final_files': [],
                            'result_urls': [],
                            'body': {
                                'model': 'gpt-image-2.5-ext',
                                'prompt': slot['prompt'],
                                'image_urls': urls,
                                'n': 1,
                                'size': slot['provider_size'],
                                **prepared['options'],
                            },
                        }
                    )
                batch = {
                    'batch_id': batch_id,
                    'request_id': prepared['generation_request_id'],
                    'generation_request_id': prepared['generation_request_id'],
                    'contract_version': contract_version,
                    'service_authority_id': service_authority_id,
                    'task_id': prepared['task_id'],
                    'dify_run_id': prepared['dify_run_id'],
                    'permit_id': claims['permit_id'],
                    'permit_version': 2,
                    'permit_expires_at': permit_expiry,
                    'package_digest': prepared['package_digest'],
                    'slot_mapping_digest': prepared['slot_mapping_digest'],
                    'reference_manifest_digest': prepared['reference_manifest_digest'],
                    'request_digest': request_digest,
                    'created_at': now,
                    'source_sequence': 1,
                    'request_fingerprint': request_digest,
                    'access_token': secrets.token_urlsafe(32),
                    'references': manifests,
                    'slots': prepared['slots'],
                }
                existing = self.db.execute(
                    'SELECT id,digest,contract_version FROM batches WHERE request_id=?',
                    (prepared['generation_request_id'],),
                ).fetchone()
                if existing:
                    self.db.rollback()
                    shutil.rmtree(folder, ignore_errors=True)
                    existing_id = self._replay_batch_or_conflict(
                        existing, contract_version, request_digest, binding
                    )
                    return self.get_chatflow(
                        existing_id, expected_contract_version=contract_version
                    )
                rejection = read_rejection_row(
                    self.db, prepared['generation_request_id']
                )
                if rejection:
                    self.db.rollback()
                    shutil.rmtree(folder, ignore_errors=True)
                    self._raise_stored_rejection(rejection, binding)
                self.db.execute(
                    """INSERT INTO batches (
                        id,request_id,digest,token,document,contract_version,task_id,dify_run_id,
                        package_digest,permit_id,permit_version,permit_expires_at,
                        slot_mapping_digest,reference_manifest_digest,request_digest,
                        service_authority_id,status_version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        batch_id,
                        prepared['generation_request_id'],
                        request_digest,
                        batch['access_token'],
                        canonical(batch),
                        contract_version,
                        prepared['task_id'],
                        prepared['dify_run_id'],
                        prepared['package_digest'],
                        claims['permit_id'],
                        2,
                        permit_expiry,
                        prepared['slot_mapping_digest'],
                        prepared['reference_manifest_digest'],
                        request_digest,
                        service_authority_id,
                        1,
                    ),
                )
                for slot in prepared['slots']:
                    state = {
                        'status': slot['status'],
                        'task_id': slot.get('task_id'),
                        'error_code': slot.get('error'),
                        'known_retry_count': slot.get('attempts'),
                        'result_urls': slot.get('result_urls', []),
                        'original_files': slot.get('original_files', []),
                        'final_files': slot.get('final_files', []),
                    }
                    state_digest = hashlib.sha256(
                        canonical_json_bytes(state)
                    ).hexdigest()
                    self.db.execute(
                        """INSERT INTO chatflow_generation_slots (
                            batch_id,slot_id,task_item_id,attempt_id,position,image_type,status,
                            status_version,status_digest,error_code,known_retry_count,artifact_ref
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            batch_id,
                            slot['slot_id'],
                            slot['task_item_id'],
                            slot['attempt_id'],
                            slot['position'],
                            slot['image_type'],
                            slot['status'],
                            1,
                            state_digest,
                            slot.get('error'),
                            slot.get('attempts'),
                            '[]',
                        ),
                    )
                if self._permit_expired(permit_expiry):
                    raise _PermitExpiredAtAcceptance
                # The batch is accepted only when this transaction commits while
                # the permit is still valid; later queue delay does not revoke it.
                self.db.commit()
            except _PermitExpiredAtAcceptance:
                self.db.rollback()
                shutil.rmtree(folder, ignore_errors=True)
                return self._resolve_expiry_outcome(
                    binding, request_digest, contract_version
                )
            except sqlite3.IntegrityError:
                self.db.rollback()
                shutil.rmtree(folder, ignore_errors=True)
                previous = self.db.execute(
                    'SELECT id,digest,contract_version FROM batches WHERE request_id=?',
                    (prepared['generation_request_id'],),
                ).fetchone()
                if previous:
                    previous_id = self._replay_batch_or_conflict(
                        previous, contract_version, request_digest, binding
                    )
                    return self.get_chatflow(
                        previous_id, expected_contract_version=contract_version
                    )
                rejection = read_rejection_row(
                    self.db, prepared['generation_request_id']
                )
                if rejection:
                    self._raise_stored_rejection(rejection, binding)
                raise ServiceError('generation_request_id_conflict', 409) from None
            except Exception:
                self.db.rollback()
                shutil.rmtree(folder, ignore_errors=True)
                if self._permit_expired(permit_expiry):
                    return self._resolve_expiry_outcome(
                        binding, request_digest, contract_version
                    )
                raise
            return self.get_chatflow(
                batch_id, expected_contract_version=contract_version
            )

    def get_service_authority(self):
        return {'service_authority_id': self.service_authority_id}

    def get_acceptance(self, generation_request_id):
        try:
            request_id = str(uuid.UUID(generation_request_id))
        except (ValueError, TypeError, AttributeError):
            raise ServiceError('invalid_generation_request_id', 422) from None
        if generation_request_id != request_id:
            raise ServiceError('invalid_generation_request_id', 422)

        not_seen = not_observed(self.service_authority_id, request_id)
        batch = self.db.execute(
            """SELECT id,contract_version,service_authority_id,request_id,task_id,
                      dify_run_id,permit_id,permit_version,permit_expires_at,package_digest,
                      slot_mapping_digest,reference_manifest_digest,request_digest
               FROM batches WHERE request_id=?""",
            (request_id,),
        ).fetchone()
        if batch:
            (
                batch_id,
                contract_version,
                authority_id,
                stored_request_id,
                task_id,
                run_id,
                permit_id,
                permit_version,
                expires_at,
                package_digest,
                slot_digest,
                reference_digest,
                service_request_digest,
            ) = batch
            if (
                contract_version != CHATFLOW_IMAGE_CONTRACT_V3
                or authority_id != self.service_authority_id
                or not valid_uuid(authority_id)
                or stored_request_id != request_id
                or not valid_uuid(stored_request_id)
                or not valid_uuid(task_id)
                or not isinstance(run_id, str)
                or not 1 <= len(run_id) <= 320
                or run_id != run_id.strip()
                or '\n' in run_id
                or '\r' in run_id
                or not valid_uuid(permit_id)
                or isinstance(permit_version, bool)
                or permit_version != 2
                or not valid_utc_timestamp(expires_at)
                or not all(
                    valid_sha256(value)
                    for value in (
                        package_digest,
                        slot_digest,
                        reference_digest,
                        service_request_digest,
                    )
                )
            ):
                return not_seen
            return {
                'contract_version': ACCEPTANCE_CONTRACT,
                'outcome': 'accepted',
                'service_authority_id': authority_id,
                'generation_contract_version': contract_version,
                'generation_request_id': stored_request_id,
                'task_id': task_id,
                'dify_run_id': run_id,
                'permit_id': permit_id,
                'permit_version': permit_version,
                'permit_expires_at': expires_at,
                'package_digest': package_digest,
                'slot_mapping_digest': slot_digest,
                'reference_manifest_digest': reference_digest,
                'service_request_digest': service_request_digest,
                'batch_id': batch_id,
            }

        rejection = read_rejection_row(self.db, request_id)
        if rejection:
            receipt = rejection_row_to_dto(rejection)
            if (
                receipt is not None
                and receipt['service_authority_id'] == self.service_authority_id
            ):
                return receipt
        return not_seen

    def get_chatflow_by_request(
        self, generation_request_id, *, expected_contract_version=None
    ):
        row = self.db.execute(
            'SELECT id,contract_version FROM batches WHERE request_id=?',
            (generation_request_id,),
        ).fetchone()
        if (
            not row
            or row[1] not in (CHATFLOW_IMAGE_CONTRACT, CHATFLOW_IMAGE_CONTRACT_V3)
            or (
                expected_contract_version is not None
                and row[1] != expected_contract_version
            )
        ):
            raise ServiceError('generation_request_not_found', 404)
        return self.get_chatflow(
            row[0], expected_contract_version=expected_contract_version
        )

    def get_chatflow(self, batch_id, *, expected_contract_version=None):
        batch = self.load(batch_id)
        if (
            not is_supported_chatflow_contract(batch.get('contract_version'))
            or (
                expected_contract_version is not None
                and batch.get('contract_version') != expected_contract_version
            )
        ):
            raise ServiceError('generation_batch_not_found', 404)
        rows = self.db.execute(
            '''SELECT slot_id,task_item_id,attempt_id,position,image_type,status,status_version,
                      error_code,known_retry_count,artifact_ref
               FROM chatflow_generation_slots WHERE batch_id=? ORDER BY position,slot_id''',
            (batch_id,),
        ).fetchall()
        if len(rows) != len(batch['slots']):
            raise ServiceError('generation_batch_unavailable', 503)
        states = {slot['slot_id']: slot for slot in batch['slots']}
        structured_slots = []
        for row in rows:
            (slot_id, task_item_id, attempt_id, position, image_type, status,
             status_version, error_code, retry_count, artifact_ref) = row
            document_slot = states.get(slot_id)
            if not document_slot or document_slot['position'] != position \
                    or document_slot['task_item_id'] != task_item_id \
                    or document_slot['attempt_id'] != attempt_id \
                    or document_slot['image_type'] != image_type:
                raise ServiceError('generation_batch_unavailable', 503)
            structured_slots.append({
                'slot_id': slot_id,
                'task_item_id': task_item_id,
                'attempt_id': attempt_id,
                'position': position,
                'image_type': image_type,
                'status': status,
                'status_version': status_version,
                'error_code': error_code,
                'known_retry_count': retry_count,
                'artifact_ref': json.loads(artifact_ref or '[]'),
            })
        states_list = [slot['status'] for slot in structured_slots]
        terminal = all(state in TERMINAL for state in states_list)
        status = (
            (
                'completed'
                if all(state == 'completed' for state in states_list)
                else 'partial_failed'
                if 'completed' in states_list
                else 'failed'
            )
            if terminal
            else 'running'
        )
        row = self.db.execute(
            'SELECT status_version FROM batches WHERE id=?', (batch_id,)
        ).fetchone()
        return {
            'contract_version': batch['contract_version'],
            'generation_request_id': batch['generation_request_id'],
            'task_id': batch['task_id'],
            'dify_run_id': batch['dify_run_id'],
            'batch_id': batch_id,
            'permit_version': batch['permit_version'],
            'package_digest': batch['package_digest'],
            'slot_mapping_digest': batch['slot_mapping_digest'],
            'reference_manifest_digest': batch['reference_manifest_digest'],
            'source_sequence': row[0],
            'status': status,
            'terminal': terminal,
            'slots': structured_slots,
        }

    def read_chatflow_artifact(
        self, batch_id, slot_id, file_id, *, expected_contract_version=None
    ):
        """Read one source artifact by fixed batch/slot/file identity, never by URL."""
        batch = self.load(batch_id)
        if (
            not is_supported_chatflow_contract(batch.get('contract_version'))
            or (
                expected_contract_version is not None
                and batch.get('contract_version') != expected_contract_version
            )
        ):
            raise ServiceError('generation_batch_not_found', 404)
        row = self.db.execute(
            '''SELECT artifact_ref,status FROM chatflow_generation_slots
               WHERE batch_id=? AND slot_id=?''',
            (batch_id, slot_id),
        ).fetchone()
        if row is None or row[1] != 'completed':
            raise ServiceError('artifact_not_found', 404)
        artifacts = json.loads(row[0] or '[]')
        matches = [
            item for item in artifacts
            if isinstance(item, dict) and item.get('kind') == 'original'
            and item.get('file_id') == file_id
        ]
        if len(matches) != 1:
            raise ServiceError('artifact_not_found', 404)
        artifact = matches[0]
        if (
            not isinstance(file_id, str)
            or not file_id.startswith(slot_id + '-original-')
            or '/' in file_id
            or '\\' in file_id
            or artifact.get('mime_type') not in ('image/jpeg', 'image/png', 'image/webp')
        ):
            raise ServiceError('artifact_not_found', 404)
        path = self.settings.data_dir / batch_id / file_id
        try:
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ServiceError('artifact_not_found', 404)
            content = path.read_bytes()
        except OSError:
            raise ServiceError('artifact_not_found', 404) from None
        if (
            not content
            or len(content) > MAX_BYTES
            or len(content) != artifact.get('size_bytes')
            or hashlib.sha256(content).hexdigest() != artifact.get('sha256')
        ):
            raise ServiceError('artifact_unavailable', 503)
        return content, artifact['mime_type']

    def confirm_chatflow_delivery(
        self, batch_id, slot_id, payload, *, expected_contract_version=None
    ):
        """Persist an exact delivery ACK before marking the source cleanup eligible."""
        required = {
            'generation_request_id', 'task_id', 'task_item_id', 'attempt_id',
            'file_id', 'sha256',
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise ServiceError('invalid_delivery_confirmation', 422)
        if any(not isinstance(payload[key], str) or not payload[key] for key in required):
            raise ServiceError('invalid_delivery_confirmation', 422)
        if len(payload['sha256']) != 64 or any(
            char not in '0123456789abcdef' for char in payload['sha256']
        ):
            raise ServiceError('invalid_delivery_confirmation', 422)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            batch = self.db.execute(
                '''SELECT request_id,task_id,contract_version FROM batches WHERE id=?''',
                (batch_id,),
            ).fetchone()
            row = self.db.execute(
                '''SELECT task_item_id,attempt_id,status,artifact_ref,source_cleanup_eligible
                   FROM chatflow_generation_slots WHERE batch_id=? AND slot_id=?''',
                (batch_id, slot_id),
            ).fetchone()
            if (
                batch is None or not is_supported_chatflow_contract(batch[2])
                or (
                    expected_contract_version is not None
                    and batch[2] != expected_contract_version
                )
                or row is None or row[2] != 'completed'
                or batch[0] != payload['generation_request_id']
                or batch[1] != payload['task_id']
                or row[0] != payload['task_item_id']
                or row[1] != payload['attempt_id']
            ):
                raise ServiceError('delivery_confirmation_conflict', 409)
            artifacts = json.loads(row[3] or '[]')
            originals = [
                item for item in artifacts
                if isinstance(item, dict) and item.get('kind') == 'original'
            ]
            if len(originals) != 1 or (
                originals[0].get('file_id') != payload['file_id']
                or originals[0].get('sha256') != payload['sha256']
            ):
                raise ServiceError('delivery_confirmation_conflict', 409)
            existing = self.db.execute(
                '''SELECT generation_request_id,batch_id,slot_id,task_id,task_item_id,
                          attempt_id,file_id,sha256
                   FROM chatflow_delivery_confirmations
                   WHERE generation_request_id=? AND slot_id=?''',
                (payload['generation_request_id'], slot_id),
            ).fetchone()
            expected = (
                payload['generation_request_id'], batch_id, slot_id, payload['task_id'],
                payload['task_item_id'], payload['attempt_id'], payload['file_id'],
                payload['sha256'],
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ServiceError('delivery_confirmation_conflict', 409)
                self.db.commit()
                return 'replayed'
            self.db.execute(
                '''INSERT INTO chatflow_delivery_confirmations
                   (generation_request_id,batch_id,slot_id,task_id,task_item_id,attempt_id,
                    file_id,sha256) VALUES (?,?,?,?,?,?,?,?)''',
                expected,
            )
            self.db.execute(
                '''UPDATE chatflow_generation_slots SET source_cleanup_eligible=1
                   WHERE batch_id=? AND slot_id=?''',
                (batch_id, slot_id),
            )
            self.db.commit()
            return 'confirmed'
        except Exception:
            self.db.rollback()
            raise

    def cleanup_eligible_chatflow_artifacts(self, batch_id=None):
        """Return only source files with a committed delivery ACK; no cleaner runs here."""
        query = (
            '''SELECT batch_id,slot_id,artifact_ref FROM chatflow_generation_slots
               WHERE source_cleanup_eligible=1'''
        )
        params = ()
        if batch_id is not None:
            query += ' AND batch_id=?'
            params = (batch_id,)
        rows = self.db.execute(query, params).fetchall()
        return [
            (row[0], row[1], item['file_id'])
            for row in rows
            for item in json.loads(row[2] or '[]')
            if isinstance(item, dict) and item.get('kind') == 'original'
        ]

    def get(self, batch_id):
        batch = self.load(batch_id)
        states = [s['status'] for s in batch['slots']]
        terminal = all(s in TERMINAL for s in states)
        status = ('completed' if all(s == 'completed' for s in states) else
                  'partial_failed' if 'completed' in states else 'failed') if terminal else 'running'
        base = self.settings.public_url.rstrip('/')
        suffix = '?token=' + batch['access_token']
        slots = []
        for slot in batch['slots']:
            visible = {key: slot[key] for key in ('slot_id', 'index', 'type', 'target_canvas', 'provider_size', 'status', 'task_id', 'error')}
            visible.update({'requires_visual_review': True,
                            'image_urls': [f'{base}/assets/{batch_id}/{name}{suffix}' for name in slot['final_files']],
                            'original_image_urls': [f'{base}/assets/{batch_id}/{name}{suffix}' for name in slot['original_files']]})
            slots.append(visible)
        result_url = f'{base}/results/{batch_id}{suffix}'
        markdown = '\n\n'.join(f"### {s['index']}. {s['type']}\n" +
                               ('\n'.join(f'![{s["type"]}]({url})' for url in s['image_urls']) or f"状态：{s['status']}")
                               for s in slots) + f'\n\n[查看完整结果]({result_url})'
        return {'batch_id': batch_id, 'status': status, 'terminal': terminal, 'slots': slots,
                'result_url': result_url, 'markdown': markdown}

    async def persist(self, batch, slot):
        folder = self.settings.data_dir / batch['batch_id']
        originals, finals = [], []
        for i, url in enumerate(slot['result_urls'], 1):
            # A successfully saved original is reused after process restart or final-save failure.
            existing = list(folder.glob(f"{slot['slot_id']}-original-{i}.*"))
            existing = [p for p in existing if p.suffix != '.tmp']
            if existing:
                original = existing[0]
                content = original.read_bytes()
            else:
                content = await self.network.image(url)
                fmt = Image.open(io.BytesIO(content)).format
                extension = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp'}[fmt]
                original = folder / f"{slot['slot_id']}-original-{i}.{extension}"
                self.write(original, content)
            image = decode(content)
            target = CANVASES[slot['target_canvas']][1]
            if target:
                resized = ImageOps.contain(image, target, Image.Resampling.LANCZOS)
                final_image = Image.new('RGB', target, 'white')
                final_image.paste(resized, ((target[0] - resized.width) // 2, (target[1] - resized.height) // 2))
            else:
                final_image = image
            out = io.BytesIO()
            final_image.save(out, 'PNG')
            final_name = f"{slot['slot_id']}-final-{i}.png"
            self.write(folder / final_name, out.getvalue())
            originals.append(original.name)
            finals.append(final_name)
        slot.update(status='completed', error=None, original_files=originals, final_files=finals)
        self.save(batch)

    async def advance(self, batch, slot):
        now = time.time()
        if slot['next_action'] > now:
            return
        if slot['task_id'] and slot['status'] != 'persisting':
            try:
                status, urls = await self.provider.poll(slot['task_id'])
                slot.update(status='persisting' if status == 'completed' else status,
                            result_urls=urls, error='provider_' + status if status in ('failed', 'cancelled') else None,
                            next_action=now + self.settings.poll_seconds)
                self.save(batch)
            except ProviderError as exc:
                slot['error'] = exc.code
                if exc.provider_status is not None:
                    slot['provider_status'] = exc.provider_status[:200]
                slot['next_action'] = now + max(self.settings.poll_seconds, exc.retry_after)
                if not exc.retryable:
                    slot['status'] = 'protocol_error'
                    slot['next_action'] = float('inf')
                self.save(batch)
                return
        if slot['status'] == 'persisting':
            try:
                await self.persist(batch, slot)
            except (ServiceError, OSError, ValueError):
                slot.update(error='result_persistence_retrying', next_action=now + max(0, self.settings.poll_seconds))
                self.save(batch)
            return
        if slot['status'] == 'submitting' and not slot['task_id']:
            # A restart after the durable pre-POST marker cannot distinguish a
            # request accepted upstream from one that never left this process.
            slot.update(
                status='submission_unknown', error='submission_outcome_unknown',
                had_unknown=True, next_action=float('inf'),
            )
            self.save(batch)
            return
        if slot['status'] == 'submission_unknown' and not slot['task_id']:
            # APIMart documents lookup by task_id only. Reusing an idempotency
            # key is not evidence that another POST is safe.
            return
        if slot['status'] != 'queued' or slot['task_id']:
            return
        permit_expired = (
            is_supported_chatflow_contract(batch.get('contract_version'))
            and time.time() >= datetime.fromisoformat(batch['permit_expires_at']).timestamp()
        )
        if permit_expired and slot.get('first_submit'):
            # Queued slots with no submit marker belong to a durably accepted
            # batch and remain eligible for their first provider submission.
            # A pre-existing first-submit marker is ambiguous and is frozen.
            slot.update(status='submission_unknown', error='submission_outcome_unknown',
                        had_unknown=True, next_action=float('inf'))
            self.save(batch)
            return
        if slot['attempts'] >= self.settings.max_submit_attempts or (slot['first_submit'] and now - slot['first_submit'] > 600):
            slot.update(status='submission_unknown', error='submission_requires_reconciliation')
            self.save(batch)
            return
        slot.update(status='submitting', attempts=slot['attempts'] + 1,
                    first_submit=slot['first_submit'] or now)
        self.save(batch)  # Commit the immutable body/key and attempt before touching the network.
        try:
            task_id = await self.provider.submit(slot['body'], slot['idempotency_key'])
            slot.update(task_id=task_id, status='pending', error=None, next_action=now + self.settings.poll_seconds)
        except ProviderError as exc:
            ambiguous = exc.unknown or slot.get('had_unknown', False)
            slot.update(status='submission_unknown' if ambiguous else 'failed',
                        error=exc.code, had_unknown=ambiguous,
                        next_action=now + max(exc.retry_after, self.settings.poll_seconds * 2 ** (slot['attempts'] - 1)))
            if not exc.retryable:
                slot['attempts'] = self.settings.max_submit_attempts
        self.save(batch)

    async def tick(self):
        # create_chatflow keeps a SQLite write transaction open across awaited
        # reference retrieval.  Do not let the worker commit through the shared
        # connection until that request has committed or rolled back.
        async with self.create_lock, self.tick_lock:
            batch_ids = [
                row[0]
                for row in self.db.execute('SELECT id FROM batches ORDER BY rowid')
            ]
            # Existing tasks get priority, across every batch.
            for batch_id in batch_ids:
                batch = self.load(batch_id)
                for slot in batch['slots']:
                    if slot['status'] in ACTIVE and slot['status'] != 'protocol_error':
                        await self.advance(batch, slot)
            batches = [self.load(batch_id) for batch_id in batch_ids]
            active = sum(
                slot['status'] in ACTIVE for batch in batches for slot in batch['slots']
            )
            for batch in batches:
                for slot in batch['slots']:
                    if (
                        slot['status'] == 'queued'
                        and active < self.settings.max_active_slots
                    ):
                        await self.advance(batch, slot)
                        active += slot['status'] in ACTIVE

    async def run(self):
        while True:
            try:
                await self.tick()
            except (OSError, sqlite3.Error):
                # Persisted state remains authoritative; never resubmit with a fresh key.
                pass
            await asyncio.sleep(max(0.1, self.settings.poll_seconds))
