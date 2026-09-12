"""Tests for the BackupService: snapshotting, settings, scheduler, restore."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config import Config
from app.core.backup import BackupService, SnapshotError, fmt_ts
from app.core.models import BackupStatus
from app.db import repositories as repo
from app.db.database import Database
from app.security.crypto import SessionCrypto


def _config(tmp_path: Path, **overrides) -> Config:
    kwargs = dict(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="900101",
        data_dir=tmp_path / "data",
        backup_sweep_interval=15,
        backup_retention_count=2,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


@pytest.fixture
async def svc(tmp_path: Path) -> tuple[BackupService, Database, Config]:
    config = _config(tmp_path)
    db = Database(config.db_path)
    await db.connect()
    # seed a master key file (the source we snapshot)
    key_dir = config.key_file.parent
    key_dir.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    config.key_file.write_bytes(key)
    crypto = SessionCrypto(key)

    # seed a user + account so the DB has real content to snapshot
    await repo.upsert_user(db, 900101)
    await repo.upsert_account(
        db, owner_id=900101, phone="+15550001111", tg_user_id=77,
        tg_username="u", display_name="Acct",
        session_encrypted=crypto.encrypt("session-string"),
    )
    # Flush the WAL into the main db file so test helpers that copy the file
    # (e.g. _archive_bytes) capture a complete, self-contained snapshot.
    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    service = BackupService(db, config)
    yield service, db, config
    await db.close()
    await service.shutdown()


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_document(self, *, chat_id, document, caption=None, **kwargs) -> dict:
        self.sent.append({"chat_id": chat_id, "document": str(document), "caption": caption})
        return {"message_id": 1}


def _archive_bytes(db_path: Path, key_path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_path, arcname="bot.db")
        zf.write(key_path, arcname="master.key")
    return buf.getvalue()


# ---------------------------------------------------------------- snapshot/create


async def test_create_backup_writes_zip_and_record(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    result = await service.create_backup(send=True)
    assert result.ok
    assert result.backup_id is not None
    assert result.file_size > 0

    rec = await repo.get_backup(db, result.backup_id)
    assert rec is not None
    assert rec.status is BackupStatus.SENT
    assert rec.file_size == result.file_size
    assert rec.sent_to == 900101

    path = service.dir / rec.filename
    assert path.exists()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    assert "bot.db" in names
    assert "master.key" in names


async def test_create_backup_without_send_keeps_ok(svc: tuple) -> None:
    service, db, _ = svc
    result = await service.create_backup(send=False)
    assert result.ok
    rec = await repo.get_backup(db, result.backup_id)
    assert rec.status is BackupStatus.OK
    assert rec.sent_to is None
    # exactly one archive on disk
    assert len(list(service.dir.glob("backup-*.zip"))) == 1


async def test_create_backup_dms_admin(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    result = await service.create_backup(send=True)
    assert result.ok
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 900101


async def test_create_backup_no_bot_still_succeeds(svc: tuple) -> None:
    service, db, _ = svc
    result = await service.create_backup(send=True)  # no bot set
    assert result.ok
    rec = await repo.get_backup(db, result.backup_id)
    assert rec.status is BackupStatus.OK  # archived but not sent


async def test_create_backup_missing_db_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "missing" / "bot.db")
    config = _config(tmp_path, data_dir=tmp_path / "missing")
    service = BackupService(db, config)
    service._dir.mkdir(parents=True, exist_ok=True)
    # db file does not exist yet -> snapshot must fail fast
    with pytest.raises(SnapshotError):
        await service._make_archive()
    await db.close()


# ---------------------------------------------------------------- settings


async def test_get_settings_defaults_from_config(svc: tuple) -> None:
    from app.core.models import BackupSettings

    service, db, config = svc
    s = await service.get_settings()
    assert isinstance(s, BackupSettings)
    # defaults: disabled, interval from config, chat = first admin
    assert s.enabled is False
    assert s.interval_hours == config.backup_interval_hours
    assert s.chat_id == 900101


async def test_get_settings_reads_db_overrides(svc: tuple) -> None:
    service, db, config = svc
    await repo.set_setting(db, repo.BACKUP_KEY_ENABLED, "true")
    await repo.set_setting(db, repo.BACKUP_KEY_INTERVAL, "6")
    await repo.set_setting(db, repo.BACKUP_KEY_CHAT_ID, "900101")
    s = await service.get_settings()
    assert s.enabled is True
    assert s.interval_hours == 6
    assert s.chat_id == 900101


async def test_get_settings_empty_interval_falls_back(svc: tuple) -> None:
    service, db, config = svc
    await repo.set_setting(db, repo.BACKUP_KEY_ENABLED, "true")
    # leave interval empty -> config default
    s = await service.get_settings()
    assert s.interval_hours == config.backup_interval_hours


async def test_set_setting_persists(svc: tuple) -> None:
    service, db, _ = svc
    await service.set_setting(repo.BACKUP_KEY_ENABLED, "true")
    assert await repo.get_setting(db, repo.BACKUP_KEY_ENABLED) == "true"


# ---------------------------------------------------------------- scheduler


async def test_sweeper_creates_backup_when_due(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    service.start_sweeper(bot)
    try:
        await repo.set_setting(db, repo.BACKUP_KEY_ENABLED, "true")
        await repo.set_setting(db, repo.BACKUP_KEY_INTERVAL, "0")  # any past interval triggers
        # No prior backup exists -> `_is_due` treats the last-ts as far in the past.
        # _tick runs a full due-check; with interval 0 and no last backup -> due
        await service._tick()
        assert await repo.count_backups(db) == 1
        assert len(bot.sent) == 1
    finally:
        await service.stop_sweeper()


async def test_sweeper_skips_when_disabled(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    service.start_sweeper(bot)
    try:
        await service._tick()  # disabled by default
        assert await repo.count_backups(db) == 0
        assert bot.sent == []
    finally:
        await service.stop_sweeper()


async def test_sweeper_skips_when_not_due(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    service.start_sweeper(bot)
    try:
        await repo.set_setting(db, repo.BACKUP_KEY_ENABLED, "true")
        # create a backup right now -> next _tick should skip (just backed up)
        await service.create_backup(send=False)
        await service._tick()
        assert await repo.count_backups(db) == 1
    finally:
        await service.stop_sweeper()


async def test_stop_sweeper_cancels_task(svc: tuple) -> None:
    service, db, config = svc
    service.start_sweeper(FakeBot())
    assert service._sweep_task is not None
    await service.stop_sweeper()
    assert service._sweep_task is None
    # idempotent
    await service.stop_sweeper()


# ---------------------------------------------------------------- retention


async def test_retention_prunes_to_config(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    # create 4 backups; retention_count=2 -> keep 2
    for _ in range(4):
        await service.create_backup(send=False)
    assert await repo.count_backups(db) == 2
    assert len(list(service.dir.glob("backup-*.zip"))) == 2


# ---------------------------------------------------------------- export / delete


async def test_export_sends_archive(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    result = await service.create_backup(send=False)
    ok = await service.export_to(result.backup_id, 900101)
    assert ok is True
    assert len(bot.sent) == 1


async def test_export_unknown_backup_fails(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    assert await service.export_to(99999, 900101) is False
    assert bot.sent == []


async def test_delete_backup_removes_row_and_file(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    result = await service.create_backup(send=False)
    path = service.dir / (await repo.get_backup(db, result.backup_id)).filename
    assert path.exists()
    assert await service.delete_backup(result.backup_id)
    assert not path.exists()
    assert await repo.get_backup(db, result.backup_id) is None
    assert not await service.delete_backup(result.backup_id)  # already gone


# ---------------------------------------------------------------- restore


async def test_archive_path_for_missing(svc: tuple) -> None:
    service, db, config = svc
    assert await service.archive_path_for(99999) is None


async def test_restore_roundtrip(svc: tuple) -> None:
    service, db, config = svc
    bot = FakeBot()
    service._bot = bot
    # create a backup, then modify the DB, then restore from the backup
    result = await service.create_backup(send=False)
    archive = service.dir / (await repo.get_backup(db, result.backup_id)).filename

    # mutate the live DB
    await repo.set_setting(db, "probe_key", "after-mutation")
    before = await db.fetch_one("SELECT COUNT(*) AS c FROM accounts")
    assert before["c"] >= 1

    restore_result = await service.restore_from_file(archive)
    assert restore_result.ok, restore_result.detail
    # DB still works after reopen
    after = await db.fetch_one("SELECT COUNT(*) AS c FROM accounts")
    assert after["c"] == before["c"]


async def test_restore_rejects_invalid_zip(svc: tuple, tmp_path: Path) -> None:
    service, db, config = svc
    not_zip = tmp_path / "notzip.zip"
    not_zip.write_text("not a zip")
    res = await service.restore_from_file(not_zip)
    assert not res.ok
    assert res.error == "invalid_archive"


async def test_restore_rejects_missing_db_member(svc: tuple, tmp_path: Path) -> None:
    service, db, config = svc
    bad = tmp_path / "nokey.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("master.key", "x")  # no bot.db
    bad.write_bytes(buf.getvalue())
    res = await service.restore_from_file(bad)
    assert not res.ok
    assert res.error == "invalid_archive"


async def test_restore_blocked_when_active_jobs(svc: tuple, tmp_path: Path) -> None:
    service, db, config = svc
    # insert a second account + an active job (status defaults to 'created')
    account_id = await repo.upsert_account(
        db, owner_id=900101, phone="+15550002222", tg_user_id=88,
        tg_username="u2", display_name="A2", session_encrypted="enc",
    )
    await repo.insert_job(db, owner_id=900101, account_id=account_id,
                          source_ref="@a", dest_ref="@b")
    p = tmp_path / "restore-blocked.zip"
    p.write_bytes(_archive_bytes(config.db_path, config.key_file))
    res = await service.restore_from_file(p)
    assert not res.ok
    assert res.error == "active_jobs"
    assert res.detail  # Arabic detail present


async def test_extract_and_validate_ok(svc: tuple, tmp_path: Path) -> None:
    service, db, config = svc
    archive = _archive_bytes(config.db_path, config.key_file)
    p = tmp_path / "ok.zip"
    p.write_bytes(archive)
    result = service._extract_and_validate(p)
    assert result is not None
    db_tmp, key_tmp = result
    assert db_tmp.exists()
    assert key_tmp is not None and key_tmp.exists()


async def test_extract_and_validate_missing_db(svc: tuple, tmp_path: Path) -> None:
    service, db, config = svc
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("master.key", "x")
    p = tmp_path / "noddb.zip"
    p.write_bytes(buf.getvalue())
    assert service._extract_and_validate(p) is None


# ---------------------------------------------------------------- fmt_ts


def test_fmt_ts_renders_and_handles_bad():
    assert fmt_ts("2026-09-12T12:00:00Z") == "2026-09-12 12:00"
    assert fmt_ts(None) == "—"
    assert fmt_ts("garbage") == "—"
