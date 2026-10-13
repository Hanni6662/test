from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from PIL import Image

ORIGIN = "http://127.0.0.1:18001"
COOKIE_NAME = "guest_session_dev"


def _image_bytes(color: tuple[int, int, int], image_format: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format=image_format)
    return output.getvalue()


def _check_upload_error(status: int, headers, body: bytes) -> None:
    if status != 413:
        raise RuntimeError(f"request limit did not return 413: {status}")
    body_request_id = json.loads(body).get("request_id")
    if body_request_id != headers.get("x-request-id"):
        raise RuntimeError("413 body/header request ID mismatch")
    UUID(str(body_request_id))


async def _verify_actual_request_limits(base_url: str) -> None:
    headers = {"Content-Type": "multipart/form-data; boundary=limit-check"}
    async with httpx.AsyncClient(base_url=base_url, timeout=120) as client:
        chunk = b"x" * (30 * 1024 * 1024)

        async def body_chunks():
            yield chunk
            yield chunk

        streamed = await client.post(
            "/api/v1/reference-sets",
            headers=headers,
            content=body_chunks(),
        )
        if "content-length" in streamed.request.headers:
            raise RuntimeError("stream test unexpectedly sent Content-Length")
        _check_upload_error(streamed.status_code, streamed.headers, streamed.content)


def _wait_ready(base_url: str) -> None:
    deadline = time.monotonic() + 60
    last_status: int | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/health/ready", timeout=2)
            last_status = response.status_code
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"test API did not become ready; last status={last_status}")


def verify(base_url: str, *, external_restart: bool) -> dict[str, str | int]:
    parsed = urlsplit(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    _wait_ready(base_url)
    asyncio.run(_verify_actual_request_limits(base_url))
    primary = _image_bytes((225, 15, 55), "PNG")
    auxiliary = _image_bytes((15, 215, 85), "JPEG")

    with httpx.Client(base_url=base_url, timeout=30) as owner:
        entered = owner.post("/api/v1/auth/guest", headers={"Origin": origin})
        entered.raise_for_status()
        user_id = UUID(entered.json()["user_id"])
        if COOKIE_NAME not in owner.cookies or COOKIE_NAME in entered.text:
            raise RuntimeError("guest credential was not kept in the HTTP cookie jar")

        uploaded = owner.post(
            "/api/v1/reference-sets",
            headers={"Origin": origin},
            files=[
                ("product_category", (None, "验收测试商品")),
                ("primary_product_image", ("primary.png", primary, "image/png")),
                ("supplementary_product_images", ("detail.jpg", auxiliary, "image/jpeg")),
            ],
        )
        uploaded.raise_for_status()
        reference_set_id = UUID(uploaded.json()["reference_set_id"])
        if uploaded.json()["status"] != "ready":
            raise RuntimeError("HTTP upload did not return a ready reference set")
        if owner.get(f"/api/v1/reference-sets/{reference_set_id}").status_code != 200:
            raise RuntimeError("owner could not read the uploaded reference set")

        recommendation_created = owner.post(
            "/api/v1/recommendations",
            headers={"Origin": origin},
            json={"reference_set_id": str(reference_set_id)},
        )
        if recommendation_created.status_code != 202:
            raise RuntimeError(
                f"recommendation request did not return 202: {recommendation_created.status_code}"
            )
        recommendation_id = UUID(recommendation_created.json()["recommendation_id"])
        deadline = time.monotonic() + 60
        ready_recommendation = None
        while time.monotonic() < deadline:
            recommendation_response = owner.get(
                f"/api/v1/recommendations/{recommendation_id}"
            )
            if recommendation_response.status_code != 200:
                raise RuntimeError("owner could not read the new recommendation")
            ready_recommendation = recommendation_response.json()
            if ready_recommendation.get("status") != "analyzing":
                break
            time.sleep(0.25)
        if ready_recommendation is None or ready_recommendation.get("status") != "ready":
            raise RuntimeError("recommendation Worker did not return ready before timeout")
        public_keys = {
            "recommendation_id",
            "reference_set_id",
            "status",
            "revision",
            "options",
            "image_types",
            "valid_until",
            "error",
        }
        if set(ready_recommendation) != public_keys:
            raise RuntimeError("recommendation response did not match the public DTO")
        language_codes = [
            item["code"] for item in ready_recommendation["options"]["language"]["choices"]
        ]
        if language_codes != ["A", "B", "C"]:
            raise RuntimeError("ready recommendation did not contain A/B/C options")
        if ready_recommendation["revision"] != 1:
            raise RuntimeError("new recommendation revision was not 1")

        with httpx.Client(base_url=base_url, timeout=20) as other:
            if other.get(f"/api/v1/reference-sets/{reference_set_id}").status_code != 401:
                raise RuntimeError("missing cookie was not rejected")
            other_identity = other.post("/api/v1/auth/guest", headers={"Origin": origin})
            other_identity.raise_for_status()
            if other.get(f"/api/v1/reference-sets/{reference_set_id}").status_code != 404:
                raise RuntimeError("another identity could read the reference set")
            if other.get(
                f"/api/v1/recommendations/{recommendation_id}"
            ).status_code != 404:
                raise RuntimeError("another identity could read the recommendation")

        if not external_restart:
            raise RuntimeError("run with --external-restart and restart test-api from the caller")
        print("READY_FOR_EXTERNAL_TEST_API_RESTART", flush=True)
        if not sys.stdin.readline():
            raise RuntimeError("external restart was not acknowledged")

        _wait_ready(base_url)
        restored_identity = owner.get("/api/v1/auth/me")
        restored_group = owner.get(f"/api/v1/reference-sets/{reference_set_id}")
        restored_recommendation = owner.get(
            f"/api/v1/recommendations/{recommendation_id}"
        )
        if restored_identity.status_code != 200:
            raise RuntimeError("guest identity did not survive API container restart")
        if UUID(restored_identity.json()["user_id"]) != user_id:
            raise RuntimeError("API restart changed the stored guest identity")
        if restored_group.status_code != 200:
            raise RuntimeError("reference group did not survive API container restart")
        if UUID(restored_group.json()["reference_set_id"]) != reference_set_id:
            raise RuntimeError("API restart changed the stored reference group")
        if restored_recommendation.status_code != 200:
            raise RuntimeError("recommendation did not survive API container restart")
        restored_public = restored_recommendation.json()
        if (
            restored_public["status"] != "ready"
            or restored_public["revision"] != 1
            or restored_public["options"] != ready_recommendation["options"]
        ):
            raise RuntimeError("API restart changed the persisted recommendation snapshot")

    return {
        "owner_user_id": str(user_id),
        "reference_set_id": str(reference_set_id),
        "recommendation_id": str(recommendation_id),
        "recommendation_status_after_restart": restored_public["status"],
        "status_after_restart": restored_group.status_code,
        "chunked_request_limit_checks": 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run private upload HTTP and API-container restart checks "
            "against image-backend-test."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("IMAGE_BACKEND_TEST_API_URL", ORIGIN),
        help="loopback URL of the test-api Compose service",
    )
    parser.add_argument(
        "--external-restart",
        action="store_true",
        help="pause while the caller restarts test-api, keeping the cookie jar in memory",
    )
    arguments = parser.parse_args()
    if not arguments.external_restart:
        parser.error("--external-restart is required; restart the named test-api Compose service")
    result = verify(arguments.base_url.rstrip("/"), external_restart=arguments.external_restart)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
