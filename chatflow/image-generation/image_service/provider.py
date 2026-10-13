from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx


class ProviderError(Exception):
    def __init__(self, code, retryable=False, unknown=False, retry_after=0, provider_status=None):
        super().__init__(code)
        self.code, self.retryable, self.unknown, self.retry_after = code, retryable, unknown, retry_after
        self.provider_status = provider_status


class Provider:
    def __init__(self, api_key, base_url='https://api.apimart.ai', client=None):
        self.base_url = base_url.rstrip('/')
        if self.base_url.endswith('/v1'):
            self.base_url = self.base_url[:-3]
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), follow_redirects=False, trust_env=False)
        self.headers = {'Authorization': f'Bearer {api_key}', 'X-APIMart-Response-Version': '2026-07-27'}

    async def request(self, method, path, body=None, key=None):
        headers = dict(self.headers)
        if key:
            headers['Idempotency-Key'] = key
        try:
            reply = await self.client.request(method, self.base_url + path, json=body, headers=headers)
        except httpx.HTTPError:
            raise ProviderError('provider_transport_unknown' if method == 'POST' else 'provider_poll_unavailable', True, method == 'POST') from None
        if reply.status_code == 429 or reply.status_code >= 500:
            delay = 0
            try:
                delay = max(0, float(reply.headers.get('retry-after', '0')))
            except ValueError:
                try:
                    delay = max(0, (parsedate_to_datetime(reply.headers['retry-after']) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, KeyError):
                    pass
            raise ProviderError(f'provider_http_{reply.status_code}', True, method == 'POST', min(delay, 86400))
        if not 200 <= reply.status_code < 300:
            raise ProviderError(f'provider_http_{reply.status_code}')
        try:
            return reply.json()
        except ValueError:
            raise ProviderError('provider_invalid_json', method == 'POST', method == 'POST') from None

    async def submit(self, body, key):
        raw = await self.request('POST', '/v1/images/generations', body, key)
        data = raw.get('data') if isinstance(raw, dict) else None
        task = data.get('id') if isinstance(data, dict) else data[0].get('task_id') if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(task, str) or not task or len(task) > 500:
            raise ProviderError('provider_missing_task_id', True, True)
        return task

    async def poll(self, task_id):
        raw = await self.request('GET', '/v1/tasks/' + quote(task_id, safe=''))
        data = raw.get('data') if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            raise ProviderError('provider_invalid_task')
        status = data.get('status')
        if status not in ('pending', 'processing', 'completed', 'failed', 'cancelled'):
            # Preserve unknown status internally; never expose arbitrary provider text.
            raise ProviderError('provider_unknown_status', provider_status=status if isinstance(status, str) else None)
        urls = []
        if status == 'completed':
            result = data.get('result', {})
            images = result.get('images', []) if isinstance(result, dict) else []
            if not isinstance(images, list):
                raise ProviderError('provider_invalid_images')
            for item in images:
                values = item.get('url') if isinstance(item, dict) else None
                if isinstance(values, str):
                    values = [values]
                if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                    raise ProviderError('provider_invalid_images')
                urls.extend(values)
            if not urls or len(urls) > 16:
                raise ProviderError('provider_empty_or_excess_results')
        return status, urls

    async def close(self):
        await self.client.aclose()
