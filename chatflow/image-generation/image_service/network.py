import asyncio
import io
import ipaddress
import socket
import warnings
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps

from .errors import ServiceError

MAX_BYTES = 20 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 40_000_000


def decode(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            if image.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('format')
            image.load()
            if image.width < 8 or image.height < 8:
                raise ValueError('dimensions')
            return ImageOps.exif_transpose(image).convert('RGB')
    except Exception:
        raise ServiceError('invalid_or_oversized_image') from None


def origin(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('url')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        host = parsed.hostname.lower()
        return f'{parsed.scheme}://{host}:{port}'
    except ValueError:
        raise ServiceError('reference_origin_not_allowed') from None


class Network:
    def __init__(
        self,
        allowed_origins,
        fake_ip_hosts=(),
        reference_backend_origin='',
        reference_backend_token='',
        client=None,
    ):
        self.allowed = {origin(value) for value in allowed_origins}
        self.fake_ip_hosts = {host.lower() for host in fake_ip_hosts}
        self.reference_backend_origin = self._fixed_origin(reference_backend_origin)
        self.reference_backend_token = reference_backend_token
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(40, connect=10),
            follow_redirects=False,
            trust_env=False,
        )

    @staticmethod
    def _fixed_origin(value):
        if not value:
            return ''
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ('http', 'https')
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ('', '/')
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError('origin')
            return f'{parsed.scheme}://{parsed.netloc}'
        except ValueError:
            raise ValueError('IMAGE_SERVICE_REFERENCE_BACKEND_ORIGIN must be a fixed HTTP origin') from None

    async def backend_reference(self, generation_request_id, task_id, reference):
        """Fetch one H4-pinned asset from the configured backend origin only."""
        if not self.reference_backend_origin or not self.reference_backend_token:
            raise ServiceError('reference_source_not_configured', 503)
        position = reference.get('order')
        if not isinstance(position, int) or isinstance(position, bool) or not 1 <= position <= 4:
            raise ServiceError('invalid_references')
        asset_id = reference.get('asset_id')
        ref_id = reference.get('ref_id')
        expected_digest = reference.get('content_sha256')
        if (
            not isinstance(asset_id, str)
            or not isinstance(ref_id, str)
            or not isinstance(expected_digest, str)
        ):
            raise ServiceError('invalid_references')
        path = (
            f'/internal/v1/image-generation/generation-requests/{generation_request_id}'
            f'/references/{position}/content'
        )
        headers = {
            'Authorization': f'Bearer {self.reference_backend_token}',
            'X-Expected-Task-ID': task_id,
            'X-Expected-Asset-ID': asset_id,
            'X-Expected-Ref-ID': ref_id,
            'X-Expected-SHA256': expected_digest,
        }
        try:
            async with self.client.stream(
                'GET', self.reference_backend_origin + path, headers=headers
            ) as reply:
                if reply.status_code != 200:
                    raise ServiceError('reference_download_failed', 502)
                expected_headers = {
                    'x-generation-request-id': generation_request_id,
                    'x-task-id': task_id,
                    'x-asset-id': asset_id,
                    'x-reference-position': str(position),
                    'x-reference-id': ref_id,
                    'x-content-sha256': expected_digest,
                }
                if any(reply.headers.get(name) != value for name, value in expected_headers.items()):
                    raise ServiceError('reference_mapping_mismatch', 409)
                mime_type = reply.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                if mime_type not in ('image/jpeg', 'image/png', 'image/webp'):
                    raise ServiceError('invalid_or_oversized_image')
                try:
                    content_length = int(reply.headers.get('content-length', '0'))
                except ValueError:
                    raise ServiceError('reference_download_failed', 502) from None
                if content_length > MAX_BYTES:
                    raise ServiceError('image_too_large')
                data = bytearray()
                async for chunk in reply.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ServiceError('image_too_large')
                if content_length and len(data) != content_length:
                    raise ServiceError('reference_download_failed', 502)
        except ServiceError:
            raise
        except httpx.HTTPError:
            raise ServiceError('reference_download_failed', 502) from None
        decode(bytes(data))
        return bytes(data)

    async def image(self, url, reference=False):
        normalized = origin(url)
        headers, extensions = {}, {}
        actual_url = url
        if reference:
            if normalized not in self.allowed:
                raise ServiceError('reference_origin_not_allowed')
        else:
            parsed = urlsplit(url)
            if parsed.scheme != 'https':
                raise ServiceError('result_origin_not_allowed')
            try:
                resolved = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
                ips = {entry[4][0] for entry in resolved}
                def permitted(ip):
                    address = ipaddress.ip_address(ip)
                    # Explicit local proxy compatibility, never a general private-IP bypass.
                    return address.is_global or (parsed.hostname.lower() in self.fake_ip_hosts
                        and address in ipaddress.ip_network('198.18.0.0/15'))
                if not ips or not all(permitted(ip) for ip in ips):
                    raise ValueError('private')
                # Connect to the validated IP, preventing DNS rebinding between check and use.
                ip = sorted(ips)[0]
                ip_host = f'[{ip}]' if ':' in ip else ip
                authority = ip_host + (f':{parsed.port}' if parsed.port else '')
                actual_url = parsed._replace(netloc=authority).geturl()
                headers['Host'] = parsed.netloc
                extensions['sni_hostname'] = parsed.hostname
            except (OSError, ValueError):
                raise ServiceError('result_origin_not_allowed') from None
        try:
            async with self.client.stream('GET', actual_url, headers=headers, extensions=extensions) as reply:
                if reply.status_code != 200:
                    raise ServiceError('reference_download_failed' if reference else 'result_download_failed')
                if int(reply.headers.get('content-length', '0')) > MAX_BYTES:
                    raise ServiceError('image_too_large')
                data = bytearray()
                async for chunk in reply.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ServiceError('image_too_large')
        except (httpx.HTTPError, ValueError):
            raise ServiceError('reference_download_failed' if reference else 'result_download_failed') from None
        decode(bytes(data))
        return bytes(data)

    async def close(self):
        await self.client.aclose()
