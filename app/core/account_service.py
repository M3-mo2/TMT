"""Account lifecycle service: bridges repositories, session crypto and the
client pool (PRD §13).

Every method is owner-scoped (RULES §4); raised :class:`ServiceError` messages
are Arabic and user-facing (RULES §7) — technical detail goes to logs only.
"""

from __future__ import annotations

import logging

from app.core.models import Account, AccountStatus
from app.db import repositories as repo
from app.db.database import Database
from app.db.repositories import AccountRecord
from app.security.crypto import CryptoError, SessionCrypto
from app.tg.client_pool import ClientPool
from app.tg.login import LoginResult

logger = logging.getLogger(__name__)

__all__ = ["AccountService", "ServiceError"]

#: Shared Arabic messages (kept here — bot/texts.py renders them verbatim).
MSG_ACCOUNT_NOT_FOUND = "الحساب غير موجود."
MSG_SESSION_INVALID = "جلسة الحساب غير صالحة، أعد تسجيل الدخول إلى الحساب."
MSG_ACCOUNT_BUSY = (
    "لا يمكن حذف الحساب لوجود عملية نقل جارية عليه، ألغِ العملية أولاً."
)


class ServiceError(Exception):
    """A user-facing service failure; ``message`` is Arabic."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AccountService:
    def __init__(self, db: Database, crypto: SessionCrypto, pool: ClientPool) -> None:
        self._db = db
        self._crypto = crypto
        self._pool = pool

    async def save_login(self, owner_id: int, phone: str, result: LoginResult) -> Account:
        """Persist a successful login: encrypt the session string, upsert the
        account row, drop any pooled client for the old session (the pool does
        not detect changed session strings), and audit."""
        await repo.upsert_user(self._db, owner_id)
        encrypted = self._crypto.encrypt(result.session_string)
        account_id = await repo.upsert_account(
            self._db,
            owner_id=owner_id,
            phone=phone,
            tg_user_id=result.tg_user_id,
            tg_username=result.username,
            display_name=result.display_name,
            session_encrypted=encrypted,
        )
        # After a session upsert the pooled client (if any) still carries the
        # old session; discard so the next get() builds a fresh one.
        await self._pool.discard(account_id)
        await repo.audit(
            self._db, "account_added", owner_id=owner_id, account_id=account_id
        )
        record = await repo.get_account(self._db, owner_id, account_id)
        assert record is not None  # upsert just wrote it
        return record.to_public()

    async def list(self, owner_id: int) -> list[Account]:
        records = await repo.list_accounts(self._db, owner_id)
        return [r.to_public() for r in records]

    async def get(self, owner_id: int, account_id: int) -> Account | None:
        record = await repo.get_account(self._db, owner_id, account_id)
        return record.to_public() if record else None

    async def get_session(self, owner_id: int, account_id: int) -> str:
        """Ownership-checked decryption. A decrypt failure marks the account
        unauthorized (the stored session is unusable) and raises."""
        record = await self.account_record(owner_id, account_id)
        try:
            return self._crypto.decrypt(record.session_encrypted)
        except CryptoError:
            await repo.set_account_status(
                self._db, account_id, AccountStatus.UNAUTHORIZED
            )
            logger.error(
                "session decrypt failed for account %d; marked unauthorized",
                account_id,
            )
            raise ServiceError(MSG_SESSION_INVALID) from None

    async def remove(self, owner_id: int, account_id: int) -> None:
        record = await repo.get_account(self._db, owner_id, account_id)
        if record is None:
            raise ServiceError(MSG_ACCOUNT_NOT_FOUND)
        if await repo.count_active_jobs_for_account(self._db, account_id) > 0:
            raise ServiceError(MSG_ACCOUNT_BUSY)
        await self._pool.discard(account_id)
        await repo.delete_account_with_audit(self._db, owner_id, account_id)

    async def mark_status(
        self,
        account_id: int,
        status: AccountStatus,
        limited_until: str | None = None,
    ) -> None:
        await repo.set_account_status(
            self._db, account_id, status, limited_until=limited_until
        )

    async def account_record(self, owner_id: int, account_id: int) -> AccountRecord:
        """Full row (including the encrypted session) for internal consumers;
        raises when the account does not exist or is not owned by ``owner_id``."""
        record = await repo.get_account(self._db, owner_id, account_id)
        if record is None:
            raise ServiceError(MSG_ACCOUNT_NOT_FOUND)
        return record
