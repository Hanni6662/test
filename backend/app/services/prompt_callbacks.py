from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.schemas.prompt_workflow import CallbackAcceptance, PromptCallback, PromptPhase
from app.services.prompt_workflows import (
    PromptWorkflowConflict,
    PromptWorkflowUnavailable,
    accept_prompt_callback,
)


class PromptCallbackService:
    """Own a short PG transaction for an authenticated internal callback."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def accept(
        self,
        callback: PromptCallback,
        *,
        phase: PromptPhase,
    ) -> CallbackAcceptance:
        try:
            with self._sessions() as db, db.begin():
                return accept_prompt_callback(db, callback, expected_phase=phase)
        except PromptWorkflowConflict:
            raise DomainError(
                409, "PROMPT_CALLBACK_CONFLICT", "提示词回传与已保存结果冲突"
            ) from None
        except PromptWorkflowUnavailable:
            raise DomainError(409, "PROMPT_CALLBACK_STALE", "提示词回传已过期或不可用") from None
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "提示词回传暂时无法保存") from None
