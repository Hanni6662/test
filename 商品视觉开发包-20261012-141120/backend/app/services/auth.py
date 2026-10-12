from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.db.models import GuestSession, SessionPolicy, User

PRODUCTION_COOKIE_NAME: Final = "__Host-guest_session"
DEVELOPMENT_COOKIE_NAME: Final = "guest_session_dev"


def _canonical_origin(value: str, *, allow_referer_path: bool) -> str | None:
    from urllib.parse import urlsplit

    if not value or value == "null" or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            return None
        if not allow_referer_path and (parsed.path or parsed.query):
            return None
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 80 if scheme == "http" else 443
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{hostname}{port_suffix}"


@dataclass(frozen=True, slots=True)
class AuthOptions:
    trusted_origins: frozenset[str]
    production: bool = True
    use_dev_cookie: bool = False

    def __post_init__(self) -> None:
        origins = frozenset(self.trusted_origins)
        if not origins or any(
            _canonical_origin(origin, allow_referer_path=False) != origin for origin in origins
        ):
            raise ValueError("trusted_origins must contain canonical HTTP(S) origins")
        if self.production and any(not origin.startswith("https://") for origin in origins):
            raise ValueError("production trusted_origins must use HTTPS")
        if self.production and self.use_dev_cookie:
            raise ValueError("development cookies are forbidden in production")
        object.__setattr__(self, "trusted_origins", origins)

    @property
    def cookie_name(self) -> str:
        return DEVELOPMENT_COOKIE_NAME if self.use_dev_cookie else PRODUCTION_COOKIE_NAME

    @property
    def cookie_secure(self) -> bool:
        return not self.use_dev_cookie


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    user_id: UUID
    identity_kind: str
    session_id: UUID


@dataclass(frozen=True, slots=True)
class GuestSessionResult:
    identity: AuthenticatedIdentity
    credential: str | None
    database_now: datetime
    expires_at: datetime


def _credential_hash(credential: str | None) -> bytes | None:
    if not credential or len(credential) > 512 or not credential.isascii():
        return None
    return hashlib.sha256(credential.encode("ascii")).digest()


class AuthService:
    """Database-backed guest session service; every method owns its Session."""

    def __init__(self, engine: Engine, options: AuthOptions) -> None:
        self.options = options
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @property
    def cookie_name(self) -> str:
        return self.options.cookie_name

    def authenticate(self, credential: str | None) -> AuthenticatedIdentity | None:
        token_hash = _credential_hash(credential)
        if token_hash is None:
            return None
        with self._sessions() as db, db.begin():
            row = db.execute(
                select(
                    GuestSession.id,
                    GuestSession.user_id,
                    User.identity_kind,
                    User.status,
                    GuestSession.expires_at,
                    GuestSession.revoked_at,
                    func.clock_timestamp(),
                )
                .join(User, User.id == GuestSession.user_id)
                .where(GuestSession.token_hash == token_hash)
            ).one_or_none()
            if row is None:
                return None
            session_id, user_id, kind, user_status, expires_at, revoked_at, database_now = row
            if user_status != "active" or revoked_at is not None or expires_at <= database_now:
                return None
            return AuthenticatedIdentity(
                user_id=user_id,
                identity_kind=kind,
                session_id=session_id,
            )

    def enter_guest(self, credential: str | None) -> GuestSessionResult:
        token_hash = _credential_hash(credential)
        with self._sessions() as db, db.begin():
            policy = self._lock_policy(db)
            current = self._database_now(db)
            old_session, old_user = self._lock_identity(db, token_hash)
            if self._is_valid(old_session, old_user, current):
                assert old_session is not None and old_user is not None
                identity = AuthenticatedIdentity(
                    old_user.id, old_user.identity_kind, old_session.id
                )
                if old_user.identity_kind == "member":
                    return GuestSessionResult(identity, None, current, old_session.expires_at)
                return self._renew_guest(old_session, identity, policy, current, credential or "")
            return self._create_guest(db, policy, current)

    def refresh_guest(self, credential: str | None) -> GuestSessionResult:
        token_hash = _credential_hash(credential)
        if token_hash is None:
            raise DomainError(401, "AUTH_REQUIRED", "需要有效身份")
        with self._sessions() as db, db.begin():
            policy = self._lock_policy(db)
            current = self._database_now(db)
            guest_session, user = self._lock_identity(db, token_hash)
            if not self._is_valid(guest_session, user, current):
                raise DomainError(401, "AUTH_REQUIRED", "需要有效身份")
            if user is None or guest_session is None or user.identity_kind != "guest":
                raise DomainError(403, "GUEST_REQUIRED", "此操作仅适用于访客身份")
            identity = AuthenticatedIdentity(user.id, user.identity_kind, guest_session.id)
            return self._renew_guest(guest_session, identity, policy, current, credential or "")

    def reset_guest(self, credential: str | None) -> GuestSessionResult:
        token_hash = _credential_hash(credential)
        if token_hash is None:
            raise DomainError(401, "AUTH_REQUIRED", "需要有效身份")
        new_credential = secrets.token_urlsafe(32)
        new_hash = hashlib.sha256(new_credential.encode("ascii")).digest()
        with self._sessions() as db, db.begin():
            policy = self._lock_policy(db)
            current = self._database_now(db)
            old_session, old_user = self._lock_identity(db, token_hash)
            if not self._is_valid(old_session, old_user, current):
                raise DomainError(401, "AUTH_REQUIRED", "需要有效身份")
            if old_user is None or old_session is None or old_user.identity_kind != "guest":
                raise DomainError(403, "GUEST_REQUIRED", "此操作仅适用于访客身份")
            old_session.revoked_at = current
            db.flush()
            return self._insert_guest(db, policy, current, new_credential, new_hash)

    def _lock_policy(self, db: Session) -> SessionPolicy:
        policy = db.execute(
            select(SessionPolicy).where(SessionPolicy.id == 1).with_for_update()
        ).scalar_one_or_none()
        if policy is None:
            raise DomainError(503, "SESSION_POLICY_UNAVAILABLE", "会话策略暂不可用")
        return policy

    @staticmethod
    def _database_now(db: Session) -> datetime:
        return db.execute(select(func.clock_timestamp())).scalar_one()

    @staticmethod
    def _lock_identity(
        db: Session, token_hash: bytes | None
    ) -> tuple[GuestSession | None, User | None]:
        if token_hash is None:
            return None, None
        guest_session = db.execute(
            select(GuestSession).where(GuestSession.token_hash == token_hash).with_for_update()
        ).scalar_one_or_none()
        if guest_session is None:
            return None, None
        user = db.execute(
            select(User).where(User.id == guest_session.user_id).with_for_update()
        ).scalar_one_or_none()
        return guest_session, user

    @staticmethod
    def _is_valid(
        guest_session: GuestSession | None,
        user: User | None,
        database_now: datetime,
    ) -> bool:
        return bool(
            guest_session is not None
            and user is not None
            and user.status == "active"
            and guest_session.revoked_at is None
            and guest_session.expires_at > database_now
        )

    def _renew_guest(
        self,
        guest_session: GuestSession,
        identity: AuthenticatedIdentity,
        policy: SessionPolicy,
        database_now: datetime,
        credential: str,
    ) -> GuestSessionResult:
        new_expiration = database_now + timedelta(days=policy.ttl_days)
        if guest_session.policy_revision == policy.revision:
            new_expiration = max(guest_session.expires_at, new_expiration)
        guest_session.last_renewed_at = max(guest_session.last_renewed_at, database_now)
        guest_session.expires_at = new_expiration
        guest_session.ttl_days = policy.ttl_days
        guest_session.policy_revision = policy.revision
        return GuestSessionResult(identity, credential, database_now, new_expiration)

    def _create_guest(
        self, db: Session, policy: SessionPolicy, database_now: datetime
    ) -> GuestSessionResult:
        credential = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(credential.encode("ascii")).digest()
        return self._insert_guest(db, policy, database_now, credential, token_hash)

    @staticmethod
    def _insert_guest(
        db: Session,
        policy: SessionPolicy,
        database_now: datetime,
        credential: str,
        token_hash: bytes,
    ) -> GuestSessionResult:
        user_id = uuid4()
        session_id = uuid4()
        expiration = database_now + timedelta(days=policy.ttl_days)
        user = User(id=user_id, identity_kind="guest", status="active")
        guest_session = GuestSession(
            id=session_id,
            user_id=user_id,
            token_hash=token_hash,
            created_at=database_now,
            last_renewed_at=database_now,
            expires_at=expiration,
            ttl_days=policy.ttl_days,
            policy_revision=policy.revision,
        )
        db.add(user)
        db.flush([user])
        db.add(guest_session)
        db.flush([guest_session])
        return GuestSessionResult(
            AuthenticatedIdentity(user_id, "guest", session_id),
            credential,
            database_now,
            expiration,
        )
