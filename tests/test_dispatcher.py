"""Smoke: build_dispatcher assembles routers, middleware, and workflow data.

aiogram routers are single-attach, so build_dispatcher is single-use per
process (the runtime builds exactly one) — this module builds it once.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import build_dispatcher
from app.bot.middlewares import UserGateMiddleware
from app.config import Config
from app.db.database import Database


def _config() -> Config:
    return Config(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
    )


def test_build_dispatcher_wires_everything() -> None:
    config = _config()
    fakes = SimpleNamespace(
        db=SimpleNamespace(),
        accounts=SimpleNamespace(),
        jobs=SimpleNamespace(),
        logins=SimpleNamespace(),
        bus=SimpleNamespace(),
        pool=SimpleNamespace(),
        reporter=SimpleNamespace(),
    )
    dp = build_dispatcher(
        config=config,
        db=fakes.db,
        accounts=fakes.accounts,
        jobs=fakes.jobs,
        logins=fakes.logins,
        bus=fakes.bus,
        pool=fakes.pool,
        reporter=fakes.reporter,
    )
    assert isinstance(dp.storage, MemoryStorage)
    assert dp["config"] is config
    for key in ("db", "accounts", "jobs", "logins", "pool", "reporter"):
        assert dp[key] is getattr(fakes, key)
    # user gate registered as outer middleware on Update
    middleware = dp.update.outer_middleware
    assert any(isinstance(getattr(m, "__self__", m), UserGateMiddleware) for m in middleware)
    # routers included: accounts, transfers, jobs, settings, admin, common
    names = [r.name for r in dp.sub_routers]
    assert names == ["accounts", "transfers", "jobs", "settings", "admin", "common"]


# ---------------------------------------------------------------- gate middleware


class _FakeMember:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeBot:
    """Minimal bot double with get_chat_member for membership checks."""

    def __init__(self, statuses: dict[int, _FakeMember] | None = None) -> None:
        self._statuses = statuses or {}
        self.sent_messages: list[dict] = []
        self.answered: list[dict] = []

    async def get_chat_member(self, chat_id: int, user_id: int) -> _FakeMember:
        if chat_id in self._statuses:
            return self._statuses[chat_id]
        raise RuntimeError("not a member")

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=1)


class _FakeUpdate:
    """Minimal Update-like object for middleware testing."""

    def __init__(self, inner: Any, event_type: str = "message") -> None:
        self.event = inner
        self.event_type = event_type


class _FakeUser:
    def __init__(self, id: int, first_name: str = "T", last_name: Any = None, username: Any = None) -> None:
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class _FakeChat:
    def __init__(self, type: str = "private") -> None:
        self.type = type


async def test_gate_blocks_unsubscribed_user(db: Database) -> None:
    from app.db import repositories as repo
    from app.bot.middlewares import UserGateMiddleware

    await repo.add_channel(
        db, channel_id=-1001, title="Test Ch", invite_link="https://t.me/test", entry_type="channel",
    )
    mw = UserGateMiddleware(db)
    bot = _FakeBot(statuses={-1001: _FakeMember("left")})  # user not a member

    msg = SimpleNamespace(
        from_user=_FakeUser(42),
        chat=_FakeChat(),
        text="/start",
    )
    async def _answer(*a: Any, **kw: Any) -> Any:
        bot.sent_messages.append(kw)
        return None
    msg.answer = _answer

    event = _FakeUpdate(msg)
    data: dict[str, Any] = {"bot": bot}

    called = []

    async def handler(ev: Any, d: dict[str, Any]) -> str:
        called.append((ev, d))
        return "ok"

    await mw(handler, event, data)
    assert called == []  # handler blocked
    assert not await repo.is_gate_cleared(db, 42)


async def test_gate_allows_verified_callback(db: Database) -> None:
    from app.db import repositories as repo
    from app.bot.middlewares import UserGateMiddleware

    await repo.add_channel(
        db, channel_id=-1001, title="Test Ch", invite_link="https://t.me/test", entry_type="channel",
    )
    mw = UserGateMiddleware(db)
    bot = _FakeBot()

    cb = SimpleNamespace(
        from_user=_FakeUser(42),
        data="gate:verify",
        message=SimpleNamespace(chat=_FakeChat()),
    )

    async def _cb_answer(*a: Any, **kw: Any) -> Any:
        return None
    cb.answer = _cb_answer

    event = _FakeUpdate(cb, event_type="callback_query")
    data: dict[str, Any] = {"bot": bot}

    called = []

    async def handler(ev: Any, d: dict[str, Any]) -> str:
        called.append((ev, d))
        return "ok"

    result = await mw(handler, event, data)
    # handler IS called for gate:verify (passes through before gate check)
    assert len(called) == 1
    assert data["db"] is db
    assert result == "ok"


async def test_gate_clears_when_user_already_member(db: Database) -> None:
    """If the user is already a member of all mandatory channels, the gate is
    auto-cleared and the handler proceeds normally."""
    from app.db import repositories as repo
    from app.bot.middlewares import UserGateMiddleware

    await repo.add_channel(
        db, channel_id=-1001, title="Test Ch", invite_link="https://t.me/test", entry_type="channel",
    )
    mw = UserGateMiddleware(db)
    bot = _FakeBot(statuses={-1001: _FakeMember("member")})  # IS a member

    msg = SimpleNamespace(
        from_user=_FakeUser(42),
        chat=_FakeChat(),
        text="/start",
    )
    async def _answer(*a: Any, **kw: Any) -> Any:
        return None
    msg.answer = _answer

    event = _FakeUpdate(msg)
    data: dict[str, Any] = {"bot": bot}
    called = []

    async def handler(ev: Any, d: dict[str, Any]) -> str:
        called.append((ev, d))
        return "ok"

    await mw(handler, event, data)
    assert len(called) == 1  # handler called — gate auto-cleared
    assert await repo.is_gate_cleared(db, 42)  # gate was set in DB
