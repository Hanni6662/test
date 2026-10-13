import asyncio
import contextlib
import hmac
import html
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .contracts import (
    CHATFLOW_IMAGE_CONTRACT,
    CHATFLOW_IMAGE_CONTRACT_V3,
    is_chatflow_envelope,
)
from .errors import ServiceError
from .jobs import Engine, Settings


class ProcessLock:
    """Reject accidental multi-worker deployment against one SQLite data directory."""
    def __init__(self, path):
        self.file = path.open('a+b')
        self.file.seek(0)
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Only one image-service process may use this data directory') from None

    def close(self):
        self.file.close()


def create_app(settings=None, engine=None):
    settings = settings or Settings.from_env()
    service = engine or Engine(settings)

    @asynccontextmanager
    async def lifespan(app):
        lock = ProcessLock(settings.data_dir / 'worker.lock')
        worker = asyncio.create_task(service.run())
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            await service.shutdown()
            lock.close()

    app = FastAPI(title='Studio image generation', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.engine = service

    @app.exception_handler(ServiceError)
    async def safe_error(request, exc):
        return JSONResponse(
            getattr(exc, 'payload', None) or {'error': exc.code},
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({'error': 'invalid_request'}, status_code=422)

    @app.middleware('http')
    async def private_response(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                'Cache-Control': 'no-store',
                'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff',
            }
        )
        return response

    def authorize(value):
        if not value or not hmac.compare_digest(value, 'Bearer ' + settings.token):
            raise ServiceError('unauthorized', 401)

    def authorize_asset(batch_id, token):
        batch = service.load(batch_id)
        if not token or not hmac.compare_digest(token, batch['access_token']):
            raise ServiceError('batch_not_found', 404)
        return batch

    @app.get('/healthz')
    async def health():
        return {'status': 'ok'}

    async def read_json_body(request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 512000:
                raise ServiceError('request_too_large', 413)
        try:
            return json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise ServiceError('invalid_json') from None

    @app.post('/v1/generation-batches', status_code=202)
    async def create(
        request: Request, authorization: str | None = Header(default=None)
    ):
        authorize(authorization)
        raw = await read_json_body(request)
        if is_chatflow_envelope(raw):
            raise ServiceError('versioned_contract_requires_v2', 409)
        return await service.create(raw)

    @app.post('/v2/generation-batches', status_code=202)
    async def create_chatflow(
        request: Request, authorization: str | None = Header(default=None)
    ):
        authorize(authorization)
        raw = await read_json_body(request)
        return await service.create_chatflow(
            raw, route_contract_version=CHATFLOW_IMAGE_CONTRACT
        )

    @app.post('/v3/generation-batches', status_code=202)
    async def create_chatflow_v3(
        request: Request, authorization: str | None = Header(default=None)
    ):
        authorize(authorization)
        raw = await read_json_body(request)
        return await service.create_chatflow(
            raw, route_contract_version=CHATFLOW_IMAGE_CONTRACT_V3
        )

    @app.get('/v2/generation-requests/{generation_request_id}')
    async def recover_chatflow(
        generation_request_id: str, authorization: str | None = Header(default=None)
    ):
        authorize(authorization)
        return service.get_chatflow_by_request(
            generation_request_id, expected_contract_version=CHATFLOW_IMAGE_CONTRACT
        )

    @app.get('/v3/generation-requests/{generation_request_id}')
    async def recover_chatflow_v3(
        generation_request_id: str,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        return service.get_chatflow_by_request(
            generation_request_id, expected_contract_version=CHATFLOW_IMAGE_CONTRACT_V3
        )

    @app.get('/v3/service-authority')
    async def service_authority_v3(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return service.get_service_authority()

    @app.get('/v3/generation-requests/{generation_request_id}/acceptance')
    async def generation_acceptance_v3(
        generation_request_id: str,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        return service.get_acceptance(generation_request_id)

    @app.get('/v2/generation-batches/{batch_id}')
    async def get_chatflow(
        batch_id: str,
        wait_seconds: float = Query(default=0, ge=0, le=10),
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        first = service.get_chatflow(
            batch_id, expected_contract_version=CHATFLOW_IMAGE_CONTRACT
        )
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while not first['terminal'] and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(
                min(0.25, max(0, deadline - asyncio.get_running_loop().time()))
            )
            current = service.get_chatflow(
                batch_id, expected_contract_version=CHATFLOW_IMAGE_CONTRACT
            )
            if current != first:
                return current
        return service.get_chatflow(
            batch_id, expected_contract_version=CHATFLOW_IMAGE_CONTRACT
        )

    @app.get('/v3/generation-batches/{batch_id}')
    async def get_chatflow_v3(
        batch_id: str,
        wait_seconds: float = Query(default=0, ge=0, le=10),
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        first = service.get_chatflow(
            batch_id, expected_contract_version=CHATFLOW_IMAGE_CONTRACT_V3
        )
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while not first['terminal'] and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
            current = service.get_chatflow(
                batch_id, expected_contract_version=CHATFLOW_IMAGE_CONTRACT_V3
            )
            if current != first:
                return current
        return service.get_chatflow(
            batch_id, expected_contract_version=CHATFLOW_IMAGE_CONTRACT_V3
        )

    @app.get(
        '/v2/generation-batches/{batch_id}/slots/{slot_id}/artifacts/{file_id}'
    )
    async def chatflow_artifact(
        batch_id: str,
        slot_id: str,
        file_id: str,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        content, mime_type = service.read_chatflow_artifact(
            batch_id,
            slot_id,
            file_id,
            expected_contract_version=CHATFLOW_IMAGE_CONTRACT,
        )
        return Response(
            content=content,
            media_type=mime_type,
            headers={'Content-Disposition': 'attachment'},
        )

    @app.get(
        '/v3/generation-batches/{batch_id}/slots/{slot_id}/artifacts/{file_id}'
    )
    async def chatflow_artifact_v3(
        batch_id: str,
        slot_id: str,
        file_id: str,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        content, mime_type = service.read_chatflow_artifact(
            batch_id,
            slot_id,
            file_id,
            expected_contract_version=CHATFLOW_IMAGE_CONTRACT_V3,
        )
        return Response(
            content=content,
            media_type=mime_type,
            headers={'Content-Disposition': 'attachment'},
        )

    @app.post(
        '/v2/generation-batches/{batch_id}/slots/{slot_id}/delivery-confirmations'
    )
    async def confirm_chatflow_delivery(
        batch_id: str,
        slot_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        payload = await read_json_body(request)
        async with service.create_lock:
            status = service.confirm_chatflow_delivery(
                batch_id,
                slot_id,
                payload,
                expected_contract_version=CHATFLOW_IMAGE_CONTRACT,
            )
        return {'status': status}

    @app.post(
        '/v3/generation-batches/{batch_id}/slots/{slot_id}/delivery-confirmations'
    )
    async def confirm_chatflow_delivery_v3(
        batch_id: str,
        slot_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        payload = await read_json_body(request)
        async with service.create_lock:
            status = service.confirm_chatflow_delivery(
                batch_id,
                slot_id,
                payload,
                expected_contract_version=CHATFLOW_IMAGE_CONTRACT_V3,
            )
        return {'status': status}

    @app.get('/v1/generation-requests/{request_id}')
    async def recover(request_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        row = service.db.execute(
            'SELECT id,contract_version FROM batches WHERE request_id=?', (request_id,)
        ).fetchone()
        if not row or row[1] is not None:
            raise ServiceError('batch_not_found', 404)
        return service.get(row[0])

    @app.get('/v1/generation-batches/{batch_id}')
    async def get(batch_id: str, wait_seconds: float = Query(default=0, ge=0, le=10),
                  authorization: str | None = Header(default=None)):
        authorize(authorization)
        row = service.db.execute(
            'SELECT contract_version FROM batches WHERE id=?', (batch_id,)
        ).fetchone()
        if not row or row[0] is not None:
            raise ServiceError('batch_not_found', 404)
        first = service.get(batch_id)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while not first['terminal'] and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
            current = service.get(batch_id)
            if current != first:
                return current
        return service.get(batch_id)

    @app.get('/assets/{batch_id}/{filename}')
    async def asset(batch_id: str, filename: str, token: str = ''):
        batch = authorize_asset(batch_id, token)
        allowed = {name for slot in batch['slots'] for name in slot['original_files'] + slot['final_files']}
        if filename not in allowed:
            raise ServiceError('asset_not_found', 404)
        path = settings.data_dir / batch_id / filename
        if not path.is_file():
            raise ServiceError('asset_not_found', 404)
        return FileResponse(path)

    @app.get('/results/{batch_id}', response_class=HTMLResponse)
    async def result(batch_id: str, token: str = ''):
        authorize_asset(batch_id, token)
        value = service.get(batch_id)
        parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
                 '<title>商品图生成结果</title>',
                 '<style>body{font:16px system-ui;max-width:1000px;margin:32px auto;padding:0 20px;background:#f5f7fa}section{background:white;padding:24px;margin:16px 0;border-radius:12px}img{max-width:100%;height:auto}a{color:#235cc4}</style>',
                 '<h1>商品图生成结果</h1><p>状态：' + html.escape(value['status']) + '</p>',
                 '<p>成图请复核商品一致性、文字和补边效果。</p>']
        if not value['terminal']:
            parts.append('<meta http-equiv="refresh" content="10">')
        for slot in value['slots']:
            parts.append(f'<section><h2>{slot["index"]}. {html.escape(slot["type"])}</h2><p>{html.escape(slot["status"])}</p>')
            if slot['error']:
                parts.append('<p>' + html.escape(slot['error']) + '</p>')
            for url in slot['image_urls']:
                safe = html.escape(url, quote=True)
                parts.append(f'<a href="{safe}"><img src="{safe}" alt="商品成图"></a>')
            for url in slot['original_image_urls']:
                parts.append(f'<p><a href="{html.escape(url, quote=True)}">下载原始成图</a></p>')
            parts.append('</section>')
        parts.append('</html>')
        return HTMLResponse(''.join(parts), headers={'Content-Security-Policy': "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})

    return app
