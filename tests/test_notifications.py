"""Tests for the Notification System: NotificationService, repository CRUD,
and middleware integration (docs/notifications/NotificationSystem.md)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Config
from app.core.events import EventBus, SystemEvent
from app.core.notifications import NotificationService
from app.db import repositories as repo
from app.db.database import Database


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def admin_config() -> Config:
    return Config(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="700001,700002",
    )


class FakeBot:
    """Scriptable bot double for notification DMs."""

    def __init__(self, *, fail_for: set[int] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_for = fail_for or set()

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        if chat_id in self.fail_for:
            raise RuntimeError("telegram blocked")
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=999)


@pytest.fixture
def admin_ids(admin_config: Config) -> list[int]:
    return admin_config.admin_id_list


@pytest.fixture
def notifier(db: Database, admin_config: Config) -> NotificationService:
    service = NotificationService(db, admin_config, EventBus())
    service.subscribe()
    return service


# ---------------------------------------------------------------- migration


async def test_notifications_table_exists_and_defaults(db: Database) -> None:
    tables = {
        r["name"]
        for r in await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "notifications" in tables
    assert "notification_settings" in tables


# ---------------------------------------------------------------- repository: create / list


async def test_create_notification_inserts_row(db: Database) -> None:
    nid = await repo.create_notification(
        db, owner_id=700001, event_type="user_joined",
        severity="info", title="joined", body="user 42",
        data={"user_id": 42},
    )
    assert nid > 0
    row = await db.fetch_one("SELECT * FROM notifications WHERE id=?", (nid,))
    assert row is not None
    assert row["owner_id"] == 700001
    assert row["event_type"] == "user_joined"
    assert row["severity"] == "info"
    assert row["title"] == "joined"
    assert row["body"] == "user 42"
    assert row["delivered"] == 0
    assert row["dismissed"] == 0
    assert row["read_at"] is None


async def test_create_notification_defaults_severity_and_empty_data(db: Database) -> None:
    nid = await repo.create_notification(
        db, owner_id=1, event_type="job_failed", title="t", body="b"
    )
    row = await db.fetch_one("SELECT severity, data FROM notifications WHERE id=?", (nid,))
    assert row["severity"] == "info"
    assert row["data"] == "{}"


async def test_list_notifications_newest_first(db: Database) -> None:
    for i in range(5):
        await repo.create_notification(
            db, owner_id=700001, event_type="user_joined",
            title=f"t{i}", body="b",
        )
    rows = await repo.list_notifications(db, 700001)
    assert [r["title"] for r in rows] == ["t4", "t3", "t2", "t1", "t0"]


async def test_list_notifications_scoped_to_owner(db: Database) -> None:
    await repo.create_notification(db, owner_id=700001, event_type="x", title="a", body="b")
    await repo.create_notification(db, owner_id=700002, event_type="x", title="c", body="d")
    rows = await repo.list_notifications(db, 700001)
    assert len(rows) == 1
    assert rows[0]["title"] == "a"


async def test_list_notifications_unread_only(db: Database) -> None:
    await repo.create_notification(db, owner_id=1, event_type="x", title="r1", body="b")
    n2 = await repo.create_notification(db, owner_id=1, event_type="x", title="r2", body="b")
    await repo.mark_notification_read(db, n2)
    rows = await repo.list_notifications(db, 1, unread_only=True)
    titles = [r["title"] for r in rows]
    assert "r1" in titles
    assert "r2" not in titles


async def test_list_notifications_unread_only_excludes_dismissed(db: Database) -> None:
    n1 = await repo.create_notification(db, owner_id=1, event_type="x", title="unread", body="b")
    n2 = await repo.create_notification(db, owner_id=1, event_type="x", title="dismissed", body="b")
    await repo.dismiss_notification(db, n2)
    rows = await repo.list_notifications(db, 1, unread_only=True)
    titles = [r["title"] for r in rows]
    assert "unread" in titles
    assert "dismissed" not in titles


async def test_list_notifications_excludes_dismissed_by_default(db: Database) -> None:
    await repo.create_notification(db, owner_id=1, event_type="x", title="visible", body="b")
    n2 = await repo.create_notification(db, owner_id=1, event_type="x", title="hidden", body="b")
    await repo.dismiss_notification(db, n2)
    rows = await repo.list_notifications(db, 1)
    titles = [r["title"] for r in rows]
    assert "visible" in titles
    assert "hidden" not in titles


async def test_list_notifications_includes_dismissed(db: Database) -> None:
    await repo.create_notification(db, owner_id=1, event_type="x", title="visible", body="b")
    n2 = await repo.create_notification(db, owner_id=1, event_type="x", title="hidden", body="b")
    await repo.dismiss_notification(db, n2)
    rows = await repo.list_notifications(db, 1, include_dismissed=True)
    titles = [r["title"] for r in rows]
    assert "visible" in titles
    assert "hidden" in titles


async def test_list_notifications_limit_and_offset(db: Database) -> None:
    for i in range(5):
        await repo.create_notification(db, owner_id=1, event_type="x",
                                       title=f"n{i}", body="b")
    page1 = await repo.list_notifications(db, 1, limit=2, offset=0)
    assert [r["title"] for r in page1] == ["n4", "n3"]
    page2 = await repo.list_notifications(db, 1, limit=2, offset=2)
    assert [r["title"] for r in page2] == ["n2", "n1"]


# ---------------------------------------------------------------- repository: counts


async def test_count_unread_notifications(db: Database) -> None:
    await repo.create_notification(db, owner_id=1, event_type="x", title="u1", body="b")
    await repo.create_notification(db, owner_id=1, event_type="x", title="u2", body="b")
    un1 = await repo.create_notification(db, owner_id=1, event_type="x", title="r1", body="b")
    await repo.mark_notification_read(db, un1)
    un2 = await repo.create_notification(db, owner_id=1, event_type="x", title="d1", body="b")
    await repo.dismiss_notification(db, un2)
    # u1 and u2 are unread + not dismissed; r1 is read, d1 is dismissed→auto-read
    assert await repo.count_unread_notifications(db, 1) == 2


# ---------------------------------------------------------------- repository: read / dismiss


async def test_mark_notification_read(db: Database) -> None:
    nid = await repo.create_notification(db, owner_id=1, event_type="x", title="t", body="b")
    ok = await repo.mark_notification_read(db, nid)
    assert ok
    row = await db.fetch_one("SELECT read_at FROM notifications WHERE id=?", (nid,))
    assert row["read_at"] is not None


async def test_mark_notification_read_idempotent(db: Database) -> None:
    nid = await repo.create_notification(db, owner_id=1, event_type="x", title="t", body="b")
    assert await repo.mark_notification_read(db, nid)
    assert not await repo.mark_notification_read(db, nid)  # already read


async def test_mark_all_notifications_read(db: Database) -> None:
    n1 = await repo.create_notification(db, owner_id=1, event_type="x", title="t1", body="b")
    n2 = await repo.create_notification(db, owner_id=1, event_type="x", title="t2", body="b")
    n3 = await repo.create_notification(db, owner_id=2, event_type="x", title="t3", body="b")  # other owner
    affected = await repo.mark_all_notifications_read(db, 1)
    assert affected == 2
    for nid in (n1, n2):
        row = await db.fetch_one("SELECT read_at FROM notifications WHERE id=?", (nid,))
        assert row["read_at"] is not None
    # n3 untouched
    row = await db.fetch_one("SELECT read_at FROM notifications WHERE id=?", (n3,))
    assert row["read_at"] is None


async def test_dismiss_notification(db: Database) -> None:
    nid = await repo.create_notification(db, owner_id=1, event_type="x", title="t", body="b")
    ok = await repo.dismiss_notification(db, nid)
    assert ok
    row = await db.fetch_one("SELECT dismissed, read_at FROM notifications WHERE id=?", (nid,))
    assert row["dismissed"] == 1
    assert row["read_at"] is not None  # auto-marked read


async def test_dismiss_notification_idempotent(db: Database) -> None:
    nid = await repo.create_notification(db, owner_id=1, event_type="x", title="t", body="b")
    assert await repo.dismiss_notification(db, nid)
    assert not await repo.dismiss_notification(db, nid)


async def test_delete_notification(db: Database) -> None:
    nid = await repo.create_notification(db, owner_id=1, event_type="x", title="t", body="b")
    ok = await repo.delete_notification(db, nid)
    assert ok
    row = await db.fetch_one("SELECT * FROM notifications WHERE id=?", (nid,))
    assert row is None


# ---------------------------------------------------------------- repository: settings


async def test_notification_setting_defaults_enabled(db: Database) -> None:
    """Missing settings rows default to enabled (fail-open)."""
    assert await repo.is_notification_enabled(db, 1, "user_joined") is True


async def test_notification_setting_round_trip(db: Database) -> None:
    assert await repo.is_notification_enabled(db, 1, "user_joined") is True
    await repo.set_notification_setting(db, 1, "user_joined", False)
    assert await repo.is_notification_enabled(db, 1, "user_joined") is False
    await repo.set_notification_setting(db, 1, "user_joined", True)
    assert await repo.is_notification_enabled(db, 1, "user_joined") is True


async def test_notification_setting_upsert(db: Database) -> None:
    """Setting the same key twice updates, doesn't duplicate."""
    await repo.set_notification_setting(db, 1, "user_joined", False)
    await repo.set_notification_setting(db, 1, "user_joined", False)  # idempotent
    count = await db.fetch_one("SELECT COUNT(*) AS c FROM notification_settings")
    assert count["c"] == 1


async def test_notification_setting_per_admin_per_type(db: Database) -> None:
    await repo.set_notification_setting(db, 1, "user_joined", False)
    await repo.set_notification_setting(db, 2, "user_joined", False)
    await repo.set_notification_setting(db, 1, "job_failed", False)
    row = await db.fetch_one(
        "SELECT enabled FROM notification_settings WHERE owner_id=1 AND event_type='user_joined'"
    )
    assert row["enabled"] == 0
    row = await db.fetch_one(
        "SELECT enabled FROM notification_settings WHERE owner_id=2 AND event_type='user_joined'"
    )
    assert row["enabled"] == 0
    row = await db.fetch_one(
        "SELECT enabled FROM notification_settings WHERE owner_id=1 AND event_type='job_failed'"
    )
    assert row["enabled"] == 0


# ---------------------------------------------------------------- NotificationService


async def test_notifier_writes_row_and_dms_each_admin(
    notifier: NotificationService, db: Database, admin_ids: list[int],
) -> None:
    bot = FakeBot()
    notifier.set_bot(bot)
    bus = notifier._bus
    await bus.publish(SystemEvent(
        event_type="user_joined", severity="info", title="T", body="B",
        data={"user_id": 42},
    ))
    dms = [d for d in bot.sent]
    assert len(dms) == len(admin_ids)
    for admin_id in admin_ids:
        target = [d for d in dms if d["chat_id"] == admin_id]
        assert len(target) == 1
        assert "<b>T</b>" in target[0]["text"]
    # DB rows: one per admin
    rows = await db.fetch_all("SELECT owner_id FROM notifications")
    owner_ids = {r["owner_id"] for r in rows}
    assert owner_ids == set(admin_ids)


async def test_notifier_marks_rows_delivered(
    notifier: NotificationService, db: Database, admin_ids: list[int],
) -> None:
    bot = FakeBot()
    notifier.set_bot(bot)
    await notifier._bus.publish(SystemEvent(
        event_type="user_joined", severity="info", title="T", body="B",
    ))
    rows = await db.fetch_all("SELECT owner_id, delivered FROM notifications")
    # `delivered` is not auto-set by the service; DMed rows default to 0.
    assert [r["owner_id"] for r in rows] == list(admin_ids)
    assert all(r["delivered"] == 0 for r in rows)
    assert len(bot.sent) == len(admin_ids)


async def test_notifier_respects_config_kill_switch(db: Database) -> None:
    config = Config(
        bot_token="123456:test-token", api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="1", notify_on_user_join=False,
    )
    notifier = NotificationService(db, config, EventBus())
    notifier.subscribe()
    bot = FakeBot()
    notifier.set_bot(bot)
    await notifier._bus.publish(SystemEvent(event_type="user_joined", title="T", body="B"))
    assert bot.sent == []
    rows = await db.fetch_all("SELECT * FROM notifications")
    assert rows == []


async def test_notifier_respects_per_admin_setting(
    notifier: NotificationService, db: Database, admin_ids: list[int],
) -> None:
    # Mute user_joined for the first admin
    await repo.set_notification_setting(db, admin_ids[0], "user_joined", False)
    bot = FakeBot()
    notifier.set_bot(bot)
    await notifier._bus.publish(SystemEvent(
        event_type="user_joined", title="T", body="B",
    ))
    sent_ids = {d["chat_id"] for d in bot.sent}
    assert admin_ids[0] not in sent_ids
    assert admin_ids[1] in sent_ids
    # Only the second admin got a DB row
    rows = await db.fetch_all("SELECT owner_id FROM notifications")
    assert {r["owner_id"] for r in rows} == {admin_ids[1]}


async def test_notifier_no_admins_configured(db: Database) -> None:
    config = Config(
        bot_token="123456:test-token", api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="",  # no admins
    )
    notifier = NotificationService(db, config, EventBus())
    notifier.subscribe()
    bot = FakeBot()
    notifier.set_bot(bot)
    await notifier._bus.publish(SystemEvent(event_type="user_joined", title="T", body="B"))
    assert bot.sent == []
    rows = await db.fetch_all("SELECT * FROM notifications")
    assert rows == []  # no rows written when there are no admins


async def test_notifier_does_not_process_non_system_events(
    notifier: NotificationService, db: Database,
) -> None:
    bot = FakeBot()
    notifier.set_bot(bot)
    await notifier._bus.publish("not a system event")
    assert bot.sent == []


async def test_notifier_handles_dm_failure_gracefully(
    notifier: NotificationService, db: Database, admin_ids: list[int],
) -> None:
    bot = FakeBot(fail_for={admin_ids[0]})
    notifier.set_bot(bot)
    await notifier._bus.publish(SystemEvent(
        event_type="user_joined", title="T", body="B",
    ))
    # The failing admin still gets a DB row; the successful admin gets a DM
    sent_ids = {d["chat_id"] for d in bot.sent}
    assert admin_ids[0] not in sent_ids
    assert admin_ids[1] in sent_ids
    rows = await db.fetch_all("SELECT owner_id FROM notifications")
    assert set(r["owner_id"] for r in rows) == set(admin_ids)


async def test_notifier_error_events_silent_notification(
    notifier: NotificationService, db: Database, admin_ids: list[int],
) -> None:
    """Non-error severities are sent silently (disable_notification=True);
    error severities ring the bell (disable_notification=False)."""
    bot = FakeBot()
    notifier.set_bot(bot)
    await notifier._bus.publish(SystemEvent(
        event_type="user_joined", title="info event", body="B", severity="info",
    ))
    await notifier._bus.publish(SystemEvent(
        event_type="peer_flood", title="error event", body="B", severity="error",
    ))
    assert len(bot.sent) == 2 * len(admin_ids)
    for sent in bot.sent:
        if "info event" in sent["text"]:
            assert sent["disable_notification"] is True
        elif "error event" in sent["text"]:
            assert sent["disable_notification"] is False
