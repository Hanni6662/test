import httpx

from app.core.domain_errors import DomainError


def ensure_creation_ready(settings) -> None:
    """Check fixed, configured origins without uploading files or starting a run."""
    if settings is None or settings.chatflow_dify_mode != "http":
        return
    checks = [
        (str(settings.dify_reference_upload_api_base_url).rstrip("/") + "/health", "Dify"),
        (str(settings.image_generation_service_url).rstrip("/") + "/healthz", "生图"),
    ]
    with httpx.Client(timeout=httpx.Timeout(3, connect=2), trust_env=False) as client:
        for url, label in checks:
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                raise DomainError(
                    503,
                    "CREATION_DEPENDENCY_UNAVAILABLE",
                    f"{label}服务暂不可用，请运行一键启动并检查服务；参考图会保留",
                ) from None
