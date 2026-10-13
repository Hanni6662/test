from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError


class HealthStatus(BaseModel):
    status: Literal["live", "ready", "not_ready"]


router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthStatus)
def live() -> HealthStatus:
    return HealthStatus(status="live")


@router.get("/health/ready", response_model=HealthStatus)
def ready(request: Request, response: Response) -> HealthStatus:
    engine: Engine = request.app.state.database_engine
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(status="not_ready")
    return HealthStatus(status="ready")
