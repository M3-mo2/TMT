"""Smoke: build_dispatcher assembles routers, middleware, and workflow data.

aiogram routers are single-attach, so build_dispatcher is single-use per
process (the runtime builds exactly one) — this module builds it once.
"""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import build_dispatcher
from app.bot.middlewares import UserGateMiddleware
from app.config import Config


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
    # routers included: accounts, transfers, jobs, common (in that order)
    names = [r.name for r in dp.sub_routers]
    assert names == ["accounts", "transfers", "jobs", "common"]
