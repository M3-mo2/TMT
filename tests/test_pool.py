"""Offline tests for app.tg.client_pool — caching, locking, discard/close."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.tg.client_pool import ClientPool
from tests.fakes import FakeTelegramClient


class Factory:
    """Builds and tracks FakeTelegramClient instances."""

    def __init__(self) -> None:
        self.created: list[FakeTelegramClient] = []
        self.sessions: list[str] = []

    def __call__(self, account_id: int, session_string: str) -> FakeTelegramClient:
        client = FakeTelegramClient(session_string=f"{session_string}:{account_id}")
        self.created.append(client)
        self.sessions.append(session_string)
        return client


async def test_get_is_cached_same_object() -> None:
    factory = Factory()
    pool = ClientPool(12345, "api-hash", client_factory=factory)
    first = await pool.get(1, "session-1")
    second = await pool.get(1, "session-1")
    assert first is second
    assert first.connect_count == 1
    assert len(factory.created) == 1


async def test_concurrent_gets_connect_once() -> None:
    factory = Factory()
    pool = ClientPool(12345, "api-hash", client_factory=factory)
    results = await asyncio.gather(
        pool.get(7, "session-7"), pool.get(7, "session-7"), pool.get(7, "session-7")
    )
    assert results[0] is results[1] is results[2]
    assert factory.created[0].connect_count == 1
    assert len(factory.created) == 1


async def test_different_accounts_get_different_clients() -> None:
    factory = Factory()
    pool = ClientPool(12345, "api-hash", client_factory=factory)
    one = await pool.get(1, "session-1")
    two = await pool.get(2, "session-2")
    assert one is not two
    assert one.session.save() == "session-1:1"
    assert two.session.save() == "session-2:2"


async def test_discard_closes_and_next_get_reconnects() -> None:
    factory = Factory()
    pool = ClientPool(12345, "api-hash", client_factory=factory)
    client = await pool.get(1, "session-1")
    await pool.discard(1)
    assert client.disconnect_count == 1
    assert client.is_connected() is False

    fresh = await pool.get(1, "session-1")
    assert fresh is not client
    assert fresh.connect_count == 1
    assert len(factory.created) == 2


async def test_discard_unknown_account_is_noop() -> None:
    factory = Factory()
    pool = ClientPool(12345, "api-hash", client_factory=factory)
    await pool.discard(42)  # must not raise
    assert factory.created == []


async def test_disconnected_cached_client_is_reconnected() -> None:
    factory = Factory()
    pool = ClientPool(12345, "api-hash", client_factory=factory)
    client = await pool.get(1, "session-1")
    client.connected = False  # simulate a dropped connection
    again = await pool.get(1, "session-1")
    assert again is client
    assert client.connect_count == 2


async def test_close_all_closes_every_client() -> None:
    factory = Factory()
    pool = ClientPool(12345, "api-hash", client_factory=factory)
    one = await pool.get(1, "s1")
    two = await pool.get(2, "s2")
    await pool.close_all()
    assert one.disconnect_count == 1
    assert two.disconnect_count == 1

    fresh = await pool.get(1, "s1")
    assert fresh is not one


class TestDiscardVersusInFlightGet:
    """Regression: discard/close_all must serialize on the per-account lock.

    Previously discard popped client and lock without locking, so a get that
    was mid-connect could return a client discard had already (or failed to)
    closed, and the popped lock let two callers hold "the" account lock.
    """

    @staticmethod
    def _slow_client(state: SimpleNamespace) -> FakeTelegramClient:
        client = FakeTelegramClient()

        async def connect() -> None:
            state.started.set()
            await state.release.wait()
            client.connected = True
            client.connect_count += 1

        client.connect = connect  # type: ignore[method-assign]
        return client

    async def test_discard_waits_for_in_flight_connect(self) -> None:
        state = SimpleNamespace(started=asyncio.Event(), release=asyncio.Event())
        created: list[FakeTelegramClient] = []

        def factory(account_id: int, session_string: str) -> FakeTelegramClient:
            client = self._slow_client(state)
            created.append(client)
            return client

        pool = ClientPool(12345, "api-hash", client_factory=factory)

        get_task = asyncio.create_task(pool.get(1, "s1"))
        await asyncio.wait_for(state.started.wait(), timeout=1)

        discard_task = asyncio.create_task(pool.discard(1))
        await asyncio.sleep(0.05)
        # discard must be blocked on the account lock, not popping under get
        assert not discard_task.done()

        state.release.set()
        client = await asyncio.wait_for(get_task, timeout=1)
        assert client.connect_count == 1  # get returned a fully connected client

        await asyncio.wait_for(discard_task, timeout=1)
        assert client.disconnect_count == 1
        assert client.is_connected() is False

        # lock registry stays consistent: next get builds a fresh client
        fresh = await pool.get(1, "s1")
        assert fresh is not client
        assert fresh.is_connected() is True

    async def test_close_all_honors_in_flight_connect(self) -> None:
        state = SimpleNamespace(started=asyncio.Event(), release=asyncio.Event())

        def factory(account_id: int, session_string: str) -> FakeTelegramClient:
            return self._slow_client(state)

        pool = ClientPool(12345, "api-hash", client_factory=factory)

        get_task = asyncio.create_task(pool.get(1, "s1"))
        await asyncio.wait_for(state.started.wait(), timeout=1)

        close_task = asyncio.create_task(pool.close_all())
        await asyncio.sleep(0.05)
        assert not close_task.done()

        state.release.set()
        client = await asyncio.wait_for(get_task, timeout=1)
        await asyncio.wait_for(close_task, timeout=1)
        assert client.disconnect_count == 1

        fresh = await pool.get(1, "s1")
        assert fresh is not client
        await pool.close_all()  # second shutdown over an empty cache is fine
