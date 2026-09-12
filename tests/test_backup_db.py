"""Tests for the Backup System schema (Migration V11) and repository layer."""

from __future__ import annotations

from app.db import repositories as repo
from app.db.database import Database


# ---------------------------------------------------------------- migration


async def test_migration_v11_creates_backup_tables(db: Database) -> None:
    tables = {
        r["name"]
        for r in await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"app_settings", "backups"} <= tables

    cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(backups)")}
    assert {
        "id", "filename", "file_size", "status", "created_at",
        "sent_to", "error",
    } <= cols

    set_cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(app_settings)")}
    assert {"key", "value", "updated_at"} <= set_cols


async def test_migration_v11_seeds_default_settings(db: Database) -> None:
    rows = {r["key"]: r["value"] for r in await db.fetch_all("SELECT key, value FROM app_settings")}
    assert rows[repo.BACKUP_KEY_ENABLED] == "false"
    assert rows[repo.BACKUP_KEY_INTERVAL] == "24"
    assert rows[repo.BACKUP_KEY_CHAT_ID] == ""


# ---------------------------------------------------------------- settings CRUD


async def test_get_setting_missing_returns_none(db: Database) -> None:
    assert await repo.get_setting(db, "does_not_exist") is None


async def test_set_and_get_setting_roundtrip(db: Database) -> None:
    await repo.set_setting(db, repo.BACKUP_KEY_ENABLED, "true")
    assert await repo.get_setting(db, repo.BACKUP_KEY_ENABLED) == "true"
    # upsert, not insert
    await repo.set_setting(db, repo.BACKUP_KEY_ENABLED, "false")
    assert await repo.get_setting(db, repo.BACKUP_KEY_ENABLED) == "false"


async def test_get_backup_settings_returns_all_keys(db: Database) -> None:
    s = await repo.get_backup_settings(db)
    assert set(s.keys()) == {
        repo.BACKUP_KEY_ENABLED, repo.BACKUP_KEY_INTERVAL, repo.BACKUP_KEY_CHAT_ID,
    }


async def test_get_backup_settings_ignores_unrelated_keys(db: Database) -> None:
    await repo.set_setting(db, "some_other_key", "x")
    s = await repo.get_backup_settings(db)
    assert "some_other_key" not in s
    assert len(s) == 3


# ---------------------------------------------------------------- backup records


async def test_insert_and_get_backup(db: Database) -> None:
    from app.core.models import BackupStatus

    bid = await repo.insert_backup(
        db, filename="backup-test.zip", file_size=1024, status=BackupStatus.OK
    )
    rec = await repo.get_backup(db, bid)
    assert rec is not None
    assert rec.filename == "backup-test.zip"
    assert rec.file_size == 1024
    assert rec.status.value == "ok"
    assert rec.sent_to is None
    assert rec.created_at is not None


async def test_get_backup_unknown_returns_none(db: Database) -> None:
    assert await repo.get_backup(db, 9999) is None


async def test_list_backups_newest_first(db: Database) -> None:
    from app.core.models import BackupStatus

    ids = []
    for i in range(3):
        ids.append(await repo.insert_backup(
            db, filename=f"b{i}.zip", file_size=i, status=BackupStatus.OK
        ))
    rows = await repo.list_backups(db, limit=50)
    # newest first -> reverse insertion order
    assert [r.id for r in rows] == list(reversed(ids))


async def test_count_backups(db: Database) -> None:
    from app.core.models import BackupStatus

    assert await repo.count_backups(db) == 0
    for _ in range(3):
        await repo.insert_backup(db, filename="x.zip", file_size=1, status=BackupStatus.OK)
    assert await repo.count_backups(db) == 3


async def test_last_backup_created_at(db: Database) -> None:
    from app.core.models import BackupStatus

    assert await repo.last_backup_created_at(db) is None
    # failed backups do not count as "last completed"
    await repo.insert_backup(db, filename="fail.zip", file_size=1, status=BackupStatus.FAILED)
    assert await repo.last_backup_created_at(db) is None
    # ok/sent count
    await repo.insert_backup(db, filename="ok.zip", file_size=1, status=BackupStatus.OK)
    assert await repo.last_backup_created_at(db) is not None
    await repo.insert_backup(db, filename="sent.zip", file_size=1, status=BackupStatus.SENT)
    assert await repo.last_backup_created_at(db) is not None
    # only completed (ok/sent) rows are ever returned
    ok_ts = await db.fetch_one("SELECT created_at FROM backups WHERE status='ok'")
    sent_ts = await db.fetch_one("SELECT created_at FROM backups WHERE status='sent'")
    last = await repo.last_backup_created_at(db)
    assert last in (ok_ts["created_at"], sent_ts["created_at"])


async def test_update_backup_status_terminal_is_write_once(db: Database) -> None:
    from app.core.models import BackupStatus

    bid = await repo.insert_backup(
        db, filename="x.zip", file_size=1, status=BackupStatus.RUNNING
    )
    assert await repo.update_backup_status(
        db, bid, BackupStatus.OK, file_size=4096, sent_to=123
    )
    rec = await repo.get_backup(db, bid)
    assert rec.file_size == 4096
    assert rec.sent_to == 123
    # cannot transition a final status (ok) again -> idempotent no-op
    assert not await repo.update_backup_status(
        db, bid, BackupStatus.SENT, file_size=9999
    )
    rec = await repo.get_backup(db, bid)
    assert rec.file_size == 4096  # unchanged


async def test_delete_backup(db: Database) -> None:
    from app.core.models import BackupStatus

    bid = await repo.insert_backup(db, filename="x.zip", file_size=1, status=BackupStatus.OK)
    assert await repo.delete_backup(db, bid)
    assert await repo.get_backup(db, bid) is None
    assert not await repo.delete_backup(db, bid)  # already gone


async def test_delete_backups_older_than_retention(db: Database) -> None:
    from app.core.models import BackupStatus

    # Insert 5 ok backups; keep newest 2.
    ids = []
    for i in range(5):
        ids.append(await repo.insert_backup(db, filename=f"b{i}.zip", file_size=1, status=BackupStatus.OK))
    deleted = await repo.delete_backups_older_than(db, keep=2)
    assert deleted == 3
    assert await repo.count_backups(db) == 2
    # The two newest (by id) survive; older ones are gone.
    surviving = {r["id"] for r in await db.fetch_all("SELECT id FROM backups")}
    assert surviving == set(ids[-2:])


async def test_count_active_jobs_is_global(db: Database) -> None:
    await repo.upsert_user(db, 1)
    await repo.upsert_user(db, 2)
    # insert_job requires an account row (FK); create a real one.
    await repo.upsert_account(
        db, owner_id=1, phone="+15550001111", tg_user_id=77,
        tg_username="u", display_name="Acct", session_encrypted="enc",
    )
    assert await repo.count_active_jobs(db) == 0
    await repo.insert_job(db, owner_id=1, account_id=1, source_ref="@a", dest_ref="@b")
    assert await repo.count_active_jobs(db) == 1
