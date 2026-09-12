"""Router-level tests for the admin backup panel handlers.

These cover the handlers that had no coverage (review finding #7), including the
toggle NameError (#1) and the delete/restore paths that depend on a persisted
filename (#3). The BackupService boundary is a fake so the router wiring is
isolated from the core service; ``safe_edit`` is patched so handlers run without
a real aiogram Message.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.routers.admin import backups as bk
from app.bot.routers.admin import callbacks as C
from app.bot.texts import (
    M_BACKUP_CREATED,
    M_BACKUP_DELETED,
    M_BACKUP_EXPORT_FAILED,
    M_BACKUP_RESTORE_BLOCKED,
    M_BACKUP_RESTORE_DONE,
    M_BACKUP_RESTORE_FAILED,
    M_BACKUP_SENT,
    M_BACKUPS_TITLE,
)
from app.core.backup import BackupResult, RestoreResult
from app.core.models import BackupRecord, BackupSettings, BackupStatus
from app.db import repositories as repo

_ADMIN_ID = 900101


def _record(filename: str = "backup-test.zip") -> BackupRecord:
    return BackupRecord(
        id=1,
        filename=filename,
        file_size=2048,
        status=BackupStatus.OK,
        created_at="2026-09-12T12:00:00Z",
        sent_to=None,
        error=None,
    )


class FakeBackup:
    """Recorded-call double of BackupService surfacing the admin boundary."""

    def __init__(
        self,
        *,
        settings: BackupSettings | None = None,
        last: str | None = None,
        count: int = 0,
        record: BackupRecord | None = None,
    ) -> None:
        self._settings = settings or BackupSettings(
            enabled=False, interval_hours=24, chat_id=_ADMIN_ID
        )
        self._last = last
        self._count = count
        self._record = record
        self.get_settings_calls = 0
        self.set_setting_calls: list[tuple[str, str]] = []
        self.get_backup_calls: list[int] = []
        self.archive_path_for_calls: list[int] = []
        self.export_calls: list[tuple[int, int]] = []
        self.restore_calls: list[Path] = []
        self.delete_calls: list[int] = []
        # configurable outcomes
        self.run_now_result = BackupResult(ok=True, backup_id=1, file_size=1024)
        self.export_sent = True
        self.archive_path: Path | None = None
        self.restore_result: RestoreResult | None = None

    async def get_settings(self) -> BackupSettings:
        self.get_settings_calls += 1
        return self._settings

    async def set_setting(self, key: str, value: str) -> None:
        self.set_setting_calls.append((key, value))

    async def last_backup_created_at(self) -> str | None:
        return self._last

    async def count_backups(self) -> int:
        return self._count

    async def list_backups(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return []

    async def run_now(self) -> BackupResult:
        return self.run_now_result

    async def get_backup(self, backup_id: int) -> BackupRecord | None:
        self.get_backup_calls.append(backup_id)
        return self._record

    async def export_to(self, backup_id: int, chat_id: int) -> bool:
        self.export_calls.append((backup_id, chat_id))
        return self.export_sent

    async def archive_path_for(self, backup_id: int) -> Path | None:
        self.archive_path_for_calls.append(backup_id)
        return self.archive_path

    async def restore_from_file(self, archive_path: Path) -> RestoreResult:
        self.restore_calls.append(archive_path)
        if self.restore_result is not None:
            return self.restore_result
        return RestoreResult(ok=True, detail="تمت استعادة النسخة الاحتياطية بنجاح.")

    async def delete_backup(self, backup_id: int) -> bool:
        self.delete_calls.append(backup_id)
        return True

    @property
    def dir(self) -> Path:
        return Path("/tmp/fake-backup-dir")


def make_cb(data: str, user_id: int = _ADMIN_ID) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.id = "test-cb"
    cb.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Admin")
    cb.bot = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.chat = SimpleNamespace(id=user_id, type="private")
    return cb


@pytest.fixture
def mock_edit(monkeypatch):
    """Patch safe_edit so handlers run without a real aiogram Message."""
    mock = AsyncMock()
    monkeypatch.setattr(bk, "safe_edit", mock)
    return mock


# ------------------------------------------------------------------ root + settings


async def test_cb_backups_root_renders_settings_and_menu(mock_edit: AsyncMock) -> None:
    cb = make_cb(C.BAK)
    await bk.cb_backups_root(cb, backup=FakeBackup(count=3))
    cb.answer.assert_awaited_once()
    mock_edit.assert_awaited_once()
    text = mock_edit.call_args.args[1]
    assert M_BACKUPS_TITLE in text


async def test_cb_backup_toggle_flips_setting_and_rerenders(mock_edit: AsyncMock) -> None:
    # Finding #1: previously raised NameError on the undefined _render_settings.
    backup = FakeBackup(settings=BackupSettings(
        enabled=True, interval_hours=12, chat_id=_ADMIN_ID))
    cb = make_cb(C.BAK_TOGGLE)
    await bk.cb_backup_toggle(cb, backup=backup)
    cb.answer.assert_awaited_once()
    # enabled=True -> toggled to "false"
    assert backup.set_setting_calls == [(repo.BACKUP_KEY_ENABLED, "false")]
    mock_edit.assert_awaited_once()
    text = mock_edit.call_args.args[1]
    # _settings_view re-rendered the settings screen (not a NameError)
    assert "⟡ الإعدادات" in text


async def test_cb_backup_toggle_disabled_flips_on(mock_edit: AsyncMock) -> None:
    backup = FakeBackup(settings=BackupSettings(
        enabled=False, interval_hours=24, chat_id=_ADMIN_ID))
    cb = make_cb(C.BAK_TOGGLE)
    await bk.cb_backup_toggle(cb, backup=backup)
    assert backup.set_setting_calls == [(repo.BACKUP_KEY_ENABLED, "true")]
    mock_edit.assert_awaited_once()


# ------------------------------------------------------------------ on-demand + history


async def test_cb_backup_new_renders_result_on_success(mock_edit: AsyncMock) -> None:
    backup = FakeBackup()
    backup.run_now_result = BackupResult(ok=True, backup_id=1, file_size=1024)
    cb = make_cb(C.BAK_NEW)
    await bk.cb_backup_new(cb, backup=backup)
    mock_edit.assert_awaited_once()
    assert M_BACKUP_CREATED in mock_edit.call_args.args[1]


async def test_cb_backup_new_renders_failure(mock_edit: AsyncMock) -> None:
    backup = FakeBackup()
    backup.run_now_result = BackupResult(ok=False, backup_id=1, error="boom")
    cb = make_cb(C.BAK_NEW)
    await bk.cb_backup_new(cb, backup=backup)
    text = mock_edit.call_args.args[1]
    assert "boom" in text


# ------------------------------------------------------------------ detail/open


async def test_cb_backup_open_uses_service_not_db(mock_edit: AsyncMock) -> None:
    # Finding #8: router must go through BackupService.get_backup, not repo.
    backup = FakeBackup(record=_record())
    cb = make_cb(f"{C.BAK_OPEN}1")
    await bk.cb_backup_open(cb, backup=backup)
    assert backup.get_backup_calls == [1]
    mock_edit.assert_awaited_once()
    text = mock_edit.call_args.args[1]
    assert "#1" in text


async def test_cb_backup_open_unknown_id(mock_edit: AsyncMock) -> None:
    backup = FakeBackup(record=None)
    cb = make_cb(f"{C.BAK_OPEN}42")
    await bk.cb_backup_open(cb, backup=backup)
    assert backup.get_backup_calls == [42]
    mock_edit.assert_awaited_once()


# ------------------------------------------------------------------ export


async def test_cb_backup_export_sends_to_admin(mock_edit: AsyncMock) -> None:
    # Finding #3: export resolves the archive via the persisted filename.
    backup = FakeBackup()
    cb = make_cb(f"{C.BAK_EXPORT}1")
    await bk.cb_backup_export(cb, backup=backup)
    assert backup.export_calls == [(1, _ADMIN_ID)]
    mock_edit.assert_awaited_once()
    assert M_BACKUP_SENT.format(chat_id=_ADMIN_ID) in mock_edit.call_args.args[1]


async def test_cb_backup_export_failure_renders_error(mock_edit: AsyncMock) -> None:
    backup = FakeBackup()
    backup.export_sent = False
    cb = make_cb(f"{C.BAK_EXPORT}1")
    await bk.cb_backup_export(cb, backup=backup)
    assert backup.export_calls == [(1, _ADMIN_ID)]
    text = mock_edit.call_args.args[1]
    assert M_BACKUP_EXPORT_FAILED.format(error="غير موجود") in text


# ------------------------------------------------------------------ restore by list


async def test_cb_backup_restore_prompt_uses_service(mock_edit: AsyncMock) -> None:
    backup = FakeBackup(record=_record())
    cb = make_cb(f"{C.BAK_RESTORE}1")
    await bk.cb_backup_restore_prompt(cb, backup=backup)
    assert backup.get_backup_calls == [1]
    mock_edit.assert_awaited_once()
    text = mock_edit.call_args.args[1]
    assert "#1" in text


async def test_cb_backup_restore_do_restores_archive(mock_edit: AsyncMock) -> None:
    archive = Path("/tmp/fake-backup-dir/backup-test.zip")
    backup = FakeBackup()
    backup.archive_path = archive
    cb = make_cb(f"{C.BAK_RESTORE_CONFIRM}1")
    await bk.cb_backup_restore_do(cb, backup=backup)
    assert backup.archive_path_for_calls == [1]
    assert backup.restore_calls == [archive]
    mock_edit.assert_awaited_once()
    assert M_BACKUP_RESTORE_DONE in mock_edit.call_args.args[1]


async def test_cb_backup_restore_do_missing_archive_fails(mock_edit: AsyncMock) -> None:
    backup = FakeBackup()
    backup.archive_path = None  # filename not persisted / file gone
    cb = make_cb(f"{C.BAK_RESTORE_CONFIRM}1")
    await bk.cb_backup_restore_do(cb, backup=backup)
    assert backup.archive_path_for_calls == [1]
    assert backup.restore_calls == []
    text = mock_edit.call_args.args[1]
    assert M_BACKUP_RESTORE_FAILED.format(error="الملف غير موجود") in text


async def test_cb_backup_restore_blocked_by_active_jobs(mock_edit: AsyncMock) -> None:
    backup = FakeBackup()
    backup.archive_path = Path("/tmp/fake-backup-dir/backup-test.zip")
    backup.restore_result = RestoreResult(
        ok=False, error="active_jobs", detail="busy", active_jobs=1
    )
    cb = make_cb(f"{C.BAK_RESTORE_CONFIRM}1")
    await bk.cb_backup_restore_do(cb, backup=backup)
    assert backup.restore_calls == [Path("/tmp/fake-backup-dir/backup-test.zip")]
    mock_edit.assert_awaited_once()
    assert M_BACKUP_RESTORE_BLOCKED in mock_edit.call_args.args[1]


# ------------------------------------------------------------------ delete


async def test_cb_backup_delete_confirm_uses_persisted_filename(mock_edit: AsyncMock) -> None:
    # Finding #3: the confirm prompt must render the persisted filename, not "".
    backup = FakeBackup(record=_record(filename="backup-test.zip"))
    cb = make_cb(f"{C.BAK_DELETE}1")
    await bk.cb_backup_delete_confirm(cb, backup=backup)
    assert backup.get_backup_calls == [1]
    mock_edit.assert_awaited_once()
    text = mock_edit.call_args.args[1]
    assert "backup-test.zip" in text


async def test_cb_backup_delete_ok_calls_service(mock_edit: AsyncMock) -> None:
    backup = FakeBackup()
    cb = make_cb(f"{C.BAK_DELETE_OK}1")
    await bk.cb_backup_delete_ok(cb, backup=backup)
    assert backup.delete_calls == [1]
    mock_edit.assert_awaited_once()
    assert M_BACKUP_DELETED in mock_edit.call_args.args[1]


# ------------------------------------------------------------------ upload-start


async def test_cb_backup_upload_start_sets_state(mock_edit: AsyncMock) -> None:
    state = MagicMock()
    state.set_state = AsyncMock()
    cb = make_cb(C.BAK_UPLOAD_START)
    await bk.cb_backup_upload_start(cb, state=state)
    state.set_state.assert_awaited_once()
    mock_edit.assert_awaited_once()
    assert M_BACKUPS_TITLE in mock_edit.call_args.args[1]


async def test_backup_receive_upload_rejects_non_zip(mock_edit: AsyncMock) -> None:
    state = MagicMock()
    state.clear = AsyncMock()
    message = MagicMock()
    message.document = None
    message.answer = AsyncMock()
    await bk.backup_receive_upload(message, state=state, backup=FakeBackup())
    message.answer.assert_awaited_once()
    mock_edit.assert_not_awaited()


async def test_backup_receive_upload_rejects_bad_extension(mock_edit: AsyncMock) -> None:
    state = MagicMock()
    state.clear = AsyncMock()
    message = MagicMock()
    message.document = MagicMock()
    message.document.file_name = "notes.txt"
    message.answer = AsyncMock()
    await bk.backup_receive_upload(message, state=state, backup=FakeBackup())
    message.answer.assert_awaited_once()
    mock_edit.assert_not_awaited()
