"""Tests for app.core.account_service: encryption at rest, owner isolation,
removal guards, unauthorized marking. Real DB + real crypto + fake pool."""

from __future__ import annotations

import pytest

from app.core.account_service import AccountService, ServiceError
from app.core.models import AccountStatus, JobStatus
from app.db import repositories as repo
from app.security.crypto import SessionCrypto
from app.tg.login import LoginResult
from tests.fakes import FakeClientPool

OWNER = 1
OTHER = 2
PLAIN_SESSION = "plain-telethon-session-string"


@pytest.fixture
def pool() -> FakeClientPool:
    return FakeClientPool()


@pytest.fixture
def service(db, crypto: SessionCrypto, pool: FakeClientPool) -> AccountService:
    return AccountService(db, crypto, pool)


def login_result(tg_user_id: int = 777, session: str = PLAIN_SESSION) -> LoginResult:
    return LoginResult(
        session_string=session, tg_user_id=tg_user_id, username="tguser",
        display_name="TG User",
    )


async def stored_session(db, account_id: int) -> str:
    row = await db.fetch_one(
        "SELECT session_encrypted FROM accounts WHERE id=?", (account_id,)
    )
    return str(row["session_encrypted"])


# ---------------------------------------------------------------- save_login


async def test_save_login_encrypts_and_audits(
    service: AccountService, db, crypto: SessionCrypto, pool: FakeClientPool
) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    assert account.id > 0
    assert account.status is AccountStatus.ACTIVE

    stored = await stored_session(db, account.id)
    assert stored != PLAIN_SESSION  # never stored in plaintext (RULES §3)
    assert crypto.decrypt(stored) == PLAIN_SESSION

    audits = await db.fetch_all(
        "SELECT event, owner_id, account_id FROM audit_log WHERE event='account_added'"
    )
    assert len(audits) == 1
    assert audits[0]["owner_id"] == OWNER
    assert audits[0]["account_id"] == account.id
    assert pool.discarded == [account.id]  # pool dropped so a new session takes effect


async def test_save_login_twice_replaces_session_and_discards_pool(
    service: AccountService, db, crypto: SessionCrypto, pool: FakeClientPool
) -> None:
    first = await service.save_login(OWNER, "+15550001111", login_result())
    second = await service.save_login(
        OWNER, "+15550001111", login_result(session="new-session-string")
    )
    assert second.id == first.id
    stored = await stored_session(db, first.id)
    assert crypto.decrypt(stored) == "new-session-string"
    assert pool.discarded == [first.id, first.id]


# ------------------------------------------------------------ list/get/isolation


async def test_list_and_get_are_owner_scoped(service: AccountService) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    await service.save_login(OTHER, "+15550002222", login_result(tg_user_id=888))

    mine = await service.list(OWNER)
    assert [a.id for a in mine] == [account.id]

    assert (await service.get(OWNER, account.id)) is not None
    assert (await service.get(OTHER, account.id)) is None


async def test_get_session_owner_isolation(service: AccountService) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    assert await service.get_session(OWNER, account.id) == PLAIN_SESSION
    with pytest.raises(ServiceError):
        await service.get_session(OTHER, account.id)


async def test_get_session_crypto_error_marks_unauthorized(
    service: AccountService, db
) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    await db.execute(
        "UPDATE accounts SET session_encrypted='garbage-not-fernet' WHERE id=?",
        (account.id,),
    )
    with pytest.raises(ServiceError):
        await service.get_session(OWNER, account.id)
    record = await service.account_record(OWNER, account.id)
    assert record.status is AccountStatus.UNAUTHORIZED


# ---------------------------------------------------------------- remove


async def test_remove_deletes_and_audits(
    service: AccountService, db, pool: FakeClientPool
) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    await service.remove(OWNER, account.id)
    assert (await service.get(OWNER, account.id)) is None
    assert pool.discarded == [account.id, account.id]  # save_login + remove
    audits = await db.fetch_all(
        "SELECT event FROM audit_log WHERE event='account_removed'"
    )
    assert len(audits) == 1


async def test_remove_keeps_finished_job_history_with_null_link(
    service: AccountService, db
) -> None:
    """Migration v2: deleting an account must not delete job history
    (PRD §20); the FK nulls jobs.account_id instead."""
    account = await service.save_login(OWNER, "+15550001111", login_result())
    job_id = await repo.insert_job(
        db, owner_id=OWNER, account_id=account.id, source_ref="@s", dest_ref="@d"
    )
    await repo.transition_job(db, job_id, (JobStatus.CREATED,), JobStatus.QUEUED)
    await repo.transition_job(
        db, job_id, (JobStatus.QUEUED,), JobStatus.COMPLETED, finished=True
    )

    await service.remove(OWNER, account.id)

    assert (await service.get(OWNER, account.id)) is None
    row = await db.fetch_one(
        "SELECT account_id, status, invited FROM jobs WHERE id=?", (job_id,)
    )
    assert row is not None
    assert row["account_id"] is None  # link nulled by ON DELETE SET NULL
    assert row["status"] == "completed"  # history survives the deletion


async def test_remove_is_owner_scoped(service: AccountService) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    with pytest.raises(ServiceError):
        await service.remove(OTHER, account.id)
    assert (await service.get(OWNER, account.id)) is not None


async def test_remove_blocked_by_active_job(
    service: AccountService, db
) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    job_id = await repo.insert_job(
        db, owner_id=OWNER, account_id=account.id, source_ref="@s", dest_ref="@d"
    )
    await repo.transition_job(
        db, job_id, (JobStatus.CREATED,), JobStatus.QUEUED
    )
    with pytest.raises(ServiceError):
        await service.remove(OWNER, account.id)
    assert (await service.get(OWNER, account.id)) is not None  # still there
    # once the job is finished, removal goes through (job history survives)
    await repo.transition_job(
        db, job_id, (JobStatus.QUEUED,), JobStatus.COMPLETED, finished=True
    )
    await service.remove(OWNER, account.id)
    assert (await service.get(OWNER, account.id)) is None


# ------------------------------------------------------- mark_status/record


async def test_mark_status_sets_limit(service: AccountService) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    until = "2099-01-01T00:00:00Z"
    await service.mark_status(account.id, AccountStatus.LIMITED, limited_until=until)
    record = await service.account_record(OWNER, account.id)
    assert record.status is AccountStatus.LIMITED
    assert record.limited_until == until


async def test_account_record_raises_when_not_owned(service: AccountService) -> None:
    account = await service.save_login(OWNER, "+15550001111", login_result())
    with pytest.raises(ServiceError):
        await service.account_record(OTHER, account.id)
    with pytest.raises(ServiceError):
        await service.account_record(OWNER, account.id + 999)
