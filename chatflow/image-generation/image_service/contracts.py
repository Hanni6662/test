import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import UTC, datetime

from .errors import ServiceError

TYPES = ['品牌导购主视觉', '核心卖点信息图', '生活方式海报', '多场景展示图',
         '场景化卖点图', '使用体验图', 'SKU展示图', '细节展示图']
CANVASES = {'1:1': ('1:1', None), '3:4': ('3:4', None),
            '800 x 800 px': ('1:1', (800, 800)),
            '1464 x 1800 px': ('4:5', (1464, 1800)),
            '1464 x 600 px': ('21:9', (1464, 600))}


def canonical(value):
    # Preserve the v3.3 document encoding, including its existing retry-time sentinels.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def canonical_json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')


CHATFLOW_IMAGE_CONTRACT = 'chatflow-image-generation-v2'
CHATFLOW_IMAGE_CONTRACT_V3 = 'chatflow-image-generation-v3'
CHATFLOW_IMAGE_CONTRACTS = frozenset(
    {CHATFLOW_IMAGE_CONTRACT, CHATFLOW_IMAGE_CONTRACT_V3}
)
CHATFLOW_PERMIT_CONTRACT = 'chatflow-generation-permit-v2'
CHATFLOW_PERMIT_VERSION = 2
_CHATFLOW_REFERENCE_IDS = ('PRIMARY', 'AUX01', 'AUX02', 'AUX03')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def is_chatflow_envelope(raw):
    """Recognize new-contract markers even when its permit was omitted."""
    if not isinstance(raw, dict):
        return False
    markers = {
        'contract_version', 'generation_request_id', 'task_id', 'dify_run_id',
        'package_digest', 'slot_mapping_digest', 'reference_manifest_digest',
        'permit_version', 'permit', 'slots',
    }
    if markers.intersection(raw):
        return True
    references = raw.get('references')
    return isinstance(references, list) and any(
        isinstance(reference, dict)
        and {'asset_id', 'content_sha256', 'order'}.intersection(reference)
        for reference in references
    )


def is_supported_chatflow_contract(value):
    return value in CHATFLOW_IMAGE_CONTRACTS


def _canonical_uuid(value, code):
    require(isinstance(value, str), code)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ServiceError(code) from None
    normalized = str(parsed)
    require(value == normalized, code)
    return normalized


def chatflow_slot_mapping(slots):
    require(isinstance(slots, list) and 1 <= len(slots) <= 8, 'invalid_slot_mapping')
    normalized = []
    task_items, attempts = set(), set()
    for position, slot in enumerate(slots, 1):
        require(isinstance(slot, dict), 'invalid_slot_mapping')
        require(set(slot) == {'slot_id', 'position', 'image_type', 'task_item_id', 'attempt_id'},
                'invalid_slot_mapping')
        require(slot.get('slot_id') == f'slot_{position:02d}'
                and isinstance(slot.get('position'), int)
                and not isinstance(slot.get('position'), bool)
                and slot['position'] == position
                and isinstance(slot.get('image_type'), str)
                and slot['image_type'] in TYPES, 'invalid_slot_mapping')
        task_item_id = _canonical_uuid(slot.get('task_item_id'), 'invalid_slot_mapping')
        attempt_id = _canonical_uuid(slot.get('attempt_id'), 'invalid_slot_mapping')
        require(
            task_item_id not in task_items and attempt_id not in attempts,
            'invalid_slot_mapping',
        )
        task_items.add(task_item_id)
        attempts.add(attempt_id)
        normalized.append({
            'slot_id': slot['slot_id'], 'position': position, 'image_type': slot['image_type'],
            'task_item_id': task_item_id, 'attempt_id': attempt_id,
        })
    return normalized


def chatflow_reference_manifest(references, contract_version=CHATFLOW_IMAGE_CONTRACT):
    require(isinstance(references, list) and 1 <= len(references) <= 4, 'invalid_references')
    normalized = []
    aliases = []
    assets = set()
    for position, ref in enumerate(references, 1):
        require(isinstance(ref, dict), 'invalid_references')
        expected_keys = {'asset_id', 'ref_id', 'order', 'content_sha256'}
        if contract_version == CHATFLOW_IMAGE_CONTRACT:
            expected_keys.add('url')
        require(set(ref) == expected_keys, 'invalid_references')
        require(ref.get('ref_id') == _CHATFLOW_REFERENCE_IDS[position - 1]
                and isinstance(ref.get('order'), int)
                and not isinstance(ref.get('order'), bool)
                and ref['order'] == position
                and isinstance(ref.get('content_sha256'), str)
                and _SHA256.fullmatch(ref['content_sha256']) is not None
                and (
                    contract_version == CHATFLOW_IMAGE_CONTRACT_V3
                    or (
                        isinstance(ref.get('url'), str)
                        and bool(ref['url'].strip())
                        and len(ref['url']) <= 8000
                    )
                ),
                'invalid_references')
        asset_id = _canonical_uuid(ref.get('asset_id'), 'invalid_references')
        require(asset_id not in assets, 'invalid_references')
        aliases.append(ref['ref_id'])
        assets.add(asset_id)
        normalized.append({
            'asset_id': asset_id, 'position': position,
            'content_sha256': ref['content_sha256'], 'ref_id': ref['ref_id'],
        })
    return normalized


def chatflow_digests(raw, contract_version=None):
    """Return H4-compatible mapping/reference SHA-256 values, excluding URLs."""
    if contract_version is None:
        contract_version = (
            raw.get('contract_version', CHATFLOW_IMAGE_CONTRACT)
            if isinstance(raw, dict)
            else CHATFLOW_IMAGE_CONTRACT
        )
    slots = chatflow_slot_mapping(raw.get('slots'))
    references = chatflow_reference_manifest(raw.get('references'), contract_version)
    return (
        hashlib.sha256(canonical_json_bytes(slots)).hexdigest(),
        hashlib.sha256(canonical_json_bytes(references)).hexdigest(),
        slots,
        references,
    )


def verify_chatflow_permit(token, secret, *, allow_expired=False):
    require(
        isinstance(secret, str)
        and len(secret) >= 32
        and secret.isascii()
        and not any(char.isspace() for char in secret),
        'permit_verifier_not_configured',
        503,
    )
    require(
        isinstance(token, str) and 1 <= len(token) <= 4096,
        'invalid_generation_permit',
        403,
    )
    parts = token.split('.')
    require(len(parts) == 3 and parts[0] == 'h4p2', 'invalid_generation_permit', 403)
    _, encoded, signature = parts
    try:
        encoded_bytes = encoded.encode('ascii')
        signature_bytes = base64.b64decode(
            signature.encode('ascii') + b'=' * (-len(signature) % 4),
            altchars=b'-_',
            validate=True,
        )
        payload_bytes = base64.b64decode(
            encoded_bytes + b'=' * (-len(encoded_bytes) % 4),
            altchars=b'-_',
            validate=True,
        )
    except (ValueError, UnicodeEncodeError):
        raise ServiceError('invalid_generation_permit', 403) from None
    expected = hmac.digest(
        secret.encode('ascii'),
        b'chatflow-generation-permit-v2.' + encoded_bytes,
        'sha256',
    )
    require(
        hmac.compare_digest(signature_bytes, expected), 'invalid_generation_permit', 403
    )
    try:
        claims = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError):
        raise ServiceError('invalid_generation_permit', 403) from None
    required = {
        'contract_version',
        'expires_at',
        'generation_request_id',
        'package_digest',
        'permit_id',
        'reference_manifest_digest',
        'slot_mapping_digest',
        'task_id',
        'token_version',
    }
    legacy_claims = required
    authority_claims = required | {'service_authority_id'}
    require(
        isinstance(claims, dict) and set(claims) in (legacy_claims, authority_claims),
        'invalid_generation_permit',
        403,
    )
    try:
        canonical_payload = json.dumps(
            claims,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode('ascii')
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ServiceError('invalid_generation_permit', 403) from None
    require(canonical_payload == payload_bytes, 'invalid_generation_permit', 403)
    require(
        claims.get('contract_version') == CHATFLOW_PERMIT_CONTRACT
        and claims.get('token_version') == CHATFLOW_PERMIT_VERSION,
        'invalid_generation_permit',
        403,
    )
    for key in ('generation_request_id', 'task_id', 'permit_id'):
        claims[key] = _canonical_uuid(claims.get(key), 'invalid_generation_permit')
    if 'service_authority_id' in claims:
        claims['service_authority_id'] = _canonical_uuid(
            claims.get('service_authority_id'), 'invalid_generation_permit'
        )
    for key in ('package_digest', 'slot_mapping_digest', 'reference_manifest_digest'):
        require(
            isinstance(claims.get(key), str)
            and _SHA256.fullmatch(claims[key]) is not None,
            'invalid_generation_permit',
            403,
        )
    try:
        expiry = datetime.fromisoformat(claims['expires_at'].replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ServiceError('invalid_generation_permit', 403) from None
    require(
        expiry.tzinfo is not None and expiry.utcoffset() is not None,
        'invalid_generation_permit',
        403,
    )
    expiry = expiry.astimezone(UTC)
    if expiry.timestamp() <= time.time() and not allow_expired:
        raise ServiceError('generation_permit_expired', 410)
    claims['expires_at'] = expiry.isoformat()
    return claims


def prepare_chatflow(raw):
    version = raw.get('contract_version') if isinstance(raw, dict) else None
    if version not in {CHATFLOW_IMAGE_CONTRACT, CHATFLOW_IMAGE_CONTRACT_V3}:
        raise ServiceError('unsupported_chatflow_contract', 409)
    return _prepare_chatflow(raw, version)


def _prepare_chatflow(raw, version):
    require(isinstance(raw, dict), 'invalid_request')
    required = {
        'contract_version',
        'generation_request_id',
        'task_id',
        'dify_run_id',
        'permit_version',
        'permit_contract_version',
        'package_digest',
        'slot_mapping_digest',
        'reference_manifest_digest',
        'permit',
        'prompt_package',
        'references',
        'slots',
    }
    allowed = required | {'options'}
    require(
        required.issubset(raw) and set(raw).issubset(allowed),
        'invalid_chatflow_request',
    )
    require(
        raw['contract_version'] == version
        and raw['permit_version'] == CHATFLOW_PERMIT_VERSION
        and raw['permit_contract_version'] == CHATFLOW_PERMIT_CONTRACT,
        'unsupported_chatflow_contract',
    )
    request_id = _canonical_uuid(
        raw['generation_request_id'], 'invalid_generation_request_id'
    )
    task_id = _canonical_uuid(raw['task_id'], 'invalid_task_id')
    require(
        isinstance(raw['dify_run_id'], str) and 1 <= len(raw['dify_run_id']) <= 320,
        'invalid_dify_run_id',
    )
    require(
        isinstance(raw['package_digest'], str)
        and _SHA256.fullmatch(raw['package_digest']) is not None,
        'invalid_package_digest',
    )
    require(isinstance(raw['slot_mapping_digest'], str)
            and _SHA256.fullmatch(raw['slot_mapping_digest']) is not None,
            'invalid_slot_mapping_digest')
    require(isinstance(raw['reference_manifest_digest'], str)
            and _SHA256.fullmatch(raw['reference_manifest_digest']) is not None,
            'invalid_reference_manifest_digest')
    package = raw['prompt_package']
    require(isinstance(package, dict), 'invalid_prompt_package')
    try:
        package_digest = hashlib.sha256(canonical_json_bytes(package)).hexdigest()
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ServiceError('invalid_prompt_package') from None
    require(
        hmac.compare_digest(package_digest, raw['package_digest']),
        'invalid_package_digest',
        409,
    )
    slots_digest, references_digest, slots, manifest = chatflow_digests(raw, version)
    require(hmac.compare_digest(slots_digest, raw['slot_mapping_digest']),
            'invalid_slot_mapping_digest', 409)
    require(hmac.compare_digest(references_digest, raw['reference_manifest_digest']),
            'invalid_reference_manifest_digest', 409)
    images = package.get('images')
    require(isinstance(images, list) and len(images) == len(slots), 'invalid_slot_mapping')
    require([image.get('type') if isinstance(image, dict) else None for image in images]
            == [slot['image_type'] for slot in slots], 'invalid_slot_mapping')
    options = raw.get('options', {})
    require(isinstance(options, dict), 'invalid_options')
    options = {
        'version': options.get('version', 'flare'),
        'resolution': options.get('resolution', '2K'),
    }
    require(options['version'] in ('flare', 'sunburst')
            and options['resolution'] in ('1K', '2K', '4K'), 'invalid_options')
    legacy_raw = {
        'request_id': request_id,
        'prompt_package': package,
        'references': (
            [{'ref_id': ref['ref_id'], 'url': source['url']}
             for ref, source in zip(manifest, raw['references'], strict=True)]
            if version == CHATFLOW_IMAGE_CONTRACT
            else []
        ),
        'options': options,
    }
    legacy_prepared = prepare(
        legacy_raw,
        reference_ids_override=(
            [reference['ref_id'] for reference in manifest]
            if version == CHATFLOW_IMAGE_CONTRACT_V3
            else None
        ),
    )
    require([slot['type'] for slot in legacy_prepared['slots']]
            == [slot['image_type'] for slot in slots], 'invalid_slot_mapping')
    permit = raw['permit']
    return {
        'contract_version': version,
        'generation_request_id': request_id,
        'request_id': request_id,
        'task_id': task_id,
        'dify_run_id': raw['dify_run_id'],
        'permit': permit,
        'permit_version': CHATFLOW_PERMIT_VERSION,
        'permit_contract_version': CHATFLOW_PERMIT_CONTRACT,
        'package_digest': package_digest,
        'slot_mapping_digest': slots_digest,
        'reference_manifest_digest': references_digest,
        'package': legacy_prepared['package'],
        'references': raw['references'],
        'reference_manifest': manifest,
        'options': options,
        'slots': [
            {**legacy_slot, **mapping, 'image_type': mapping['image_type']}
            for legacy_slot, mapping in zip(legacy_prepared['slots'], slots, strict=True)
        ],
    }


def require(condition, code='invalid_prompt_package', status=422):
    if not condition:
        raise ServiceError(code, status)


def nonempty(value, limit=20000):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def prepare(raw, *, reference_ids_override=None):
    require(isinstance(raw, dict), 'invalid_request')
    require(nonempty(raw.get('request_id'), 200), 'invalid_request_id')
    package = raw.get('prompt_package')
    if isinstance(package, str):
        try:
            package = json.loads(package)
        except (ValueError, TypeError):
            raise ServiceError('invalid_prompt_package') from None
    require(isinstance(package, dict))
    require(len(canonical(package).encode()) <= 256000, 'prompt_package_too_large')
    basis, images = package.get('basis'), package.get('images')
    require(isinstance(basis, dict) and isinstance(images, list) and 1 <= len(images) <= 8)
    selected = basis.get('image_types')
    require(isinstance(selected, list) and selected == [t for t in TYPES if t in selected])
    require(len(selected) == len(images))
    require(nonempty(basis.get('identity_prompt')))
    require(isinstance(basis.get('identity_constraints'), (list, dict, str)))
    require(isinstance(basis.get('unknowns'), (list, dict, str)))
    require(isinstance(basis.get('preference_form'), dict))
    profile = basis.get('platform_profile')
    require(isinstance(profile, dict) and nonempty(profile.get('platform'), 100) and nonempty(profile.get('rule')))
    canvases = profile.get('canvases')
    require(isinstance(canvases, list) and len(canvases) == len(images))
    refs = raw.get('references')
    if reference_ids_override is None:
        require(isinstance(refs, list) and 1 <= len(refs) <= 16, 'invalid_references')
        ref_ids = []
        for ref in refs:
            require(isinstance(ref, dict) and nonempty(ref.get('ref_id'), 100)
                    and re.fullmatch(r'[A-Za-z0-9_-]+', ref['ref_id']) is not None
                    and nonempty(ref.get('url'), 8000), 'invalid_references')
            ref_ids.append(ref['ref_id'])
    else:
        require(
            isinstance(reference_ids_override, list)
            and 1 <= len(reference_ids_override) <= 4
            and reference_ids_override == list(_CHATFLOW_REFERENCE_IDS[:len(reference_ids_override)]),
            'invalid_references',
        )
        ref_ids = list(reference_ids_override)
        refs = []
    require(len(set(ref_ids)) == len(ref_ids), 'duplicate_reference_id')
    assets = basis.get('asset_ids')
    if assets is not None:
        require(len(ref_ids) <= 4, 'invalid_references')
        require(isinstance(assets, dict) and assets.get('primary') == 'PRIMARY', 'invalid_reference_plan')
        auxiliary_ids = assets.get('auxiliary')
        require(
            isinstance(auxiliary_ids, list)
            and auxiliary_ids == [f'AUX{i:02d}' for i in range(1, len(ref_ids))],
            'invalid_reference_plan',
        )
        require(ref_ids == ['PRIMARY'] + auxiliary_ids, 'invalid_reference_plan')
    options = raw.get('options', {})
    require(isinstance(options, dict), 'invalid_options')
    options = {'version': options.get('version', 'flare'), 'resolution': options.get('resolution', '2K')}
    require(options['version'] in ('flare', 'sunburst') and options['resolution'] in ('1K', '2K', '4K'), 'invalid_options')
    slots = []
    for i, (item, name, expected) in enumerate(zip(images, selected, canvases), 1):
        require(isinstance(item, dict) and isinstance(expected, dict))
        require(item.get('type') == name == expected.get('type'))
        canvas = item.get('canvas')
        require(isinstance(canvas, str) and canvas in CANVASES and canvas == expected.get('canvas'), 'invalid_canvas')
        require(nonempty(item.get('identity_prompt')) and nonempty(item.get('design_prompt'), 16000))
        require(item['identity_prompt'] == basis['identity_prompt'], 'identity_prompt_mismatch')
        resources = item.get('resources')
        require(isinstance(resources, list) and resources)
        fields = set()
        for resource in resources:
            require(isinstance(resource, dict) and str(resource.get('field')) in basis['preference_form'] and nonempty(resource.get('use'), 1200))
            fields.add(str(resource['field']))
        require({'1', '2'} <= fields)
        ratio = CANVASES[canvas][0]
        plan = item.get('reference_plan')
        if plan is not None:
            require(isinstance(plan, dict), 'invalid_reference_plan')
            anchor = plan.get('identity_anchor_id')
            require(anchor in ref_ids, 'invalid_reference_plan')
            if assets is not None:
                require(anchor == assets['primary'], 'invalid_reference_plan')
            for key in ('view_reference_ids', 'excluded_reference_ids'):
                require(isinstance(plan.get(key), list) and all(r in ref_ids for r in plan[key]), 'invalid_reference_plan')
            require(anchor not in plan['excluded_reference_ids'], 'invalid_reference_plan')
            used, excluded = plan['view_reference_ids'], plan['excluded_reference_ids']
            require(len(set(used)) == len(used) and len(set(excluded)) == len(excluded)
                    and set(used).isdisjoint(excluded), 'invalid_reference_plan')
            if assets is not None:
                require(set(used) | set(excluded) == set(assets['auxiliary']), 'invalid_reference_plan')
            require(nonempty(plan.get('target_view'), 200), 'invalid_reference_plan')
        reference_context = {
            key: basis[key]
            for key in ('primary_view', 'physical_profile', 'auxiliary_relations', 'asset_ids')
            if key in basis
        }
        auxiliary_relations = reference_context.get('auxiliary_relations')
        if isinstance(auxiliary_relations, list):
            reference_context['auxiliary_relations'] = [
                {key: value for key, value in relation.items() if key != 'relation'}
                if isinstance(relation, dict) else relation
                for relation in auxiliary_relations
            ]
        prompt = '\n'.join([
            f'任务：生成当前图位的一张成图。当前图位：{name}。',
            (
                '参考图约定：本图需综合全部输入参考图作为同一商品的外观证据，'
                '按输入顺序对应编号：' + canonical(ref_ids) + '。不可自动拼贴全部参考图。'
            ),
            '商品保真：' + item['identity_prompt'],
            '保真约束：' + canonical(basis['identity_constraints']),
            '事实边界：以下未知内容不可作为已证实事实呈现：' + canonical(basis['unknowns']),
            '平台与文字规则：' + profile['rule'],
            'SKU展示图不得新增文字。' if name == 'SKU展示图' else '',
            '商品身份及视角补充：' + canonical(reference_context),
            '本图设计：' + item['design_prompt'],
            f'画布适配：按 {ratio} 生成，最终交付 {canvas}；必要时等比缩放补边，不裁切、不拉伸。商品与文字须完整，留安全边距。',
        ])
        slots.append({'slot_id': f'slot_{i:02d}', 'index': i, 'type': name, 'target_canvas': canvas,
                      'provider_size': ratio, 'prompt': prompt})
    return {'request_id': raw['request_id'], 'package': package, 'references': refs, 'options': options, 'slots': slots}
