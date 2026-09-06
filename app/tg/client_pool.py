"""Per-account TelegramClient lifecycle: lazily connected, cached, idempotent.

The pool is the only component that creates transfer-side clients (RULES §1);
login creates its own temporary clients (see ``app.tg.login``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

__all__ = ["ClientPool"]


class _ClientFactory(Protocol):
    def __call__(self, account_id: int, session_string: str) -> Any: ...


class ClientPool:
    """Cached, lazily connected ``TelegramClient`` per account id.

    ``get`` is idempotent: concurrent callers for the same account serialize
    on a per-account lock and observe a single ``connect``.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        *,
        client_factory: _ClientFactory | None = None,
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._client_factory: _ClientFactory = client_factory or self._make_client
        self._clients: dict[int, Any] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _make_client(self, account_id: int, session_string: str) -> TelegramClient:
        return TelegramClient(
            StringSession(session_string),
            self._api_id,
            self._api_hash,
            receive_updates=False,
            auto_reconnect=True,
            device_model=f"TMT {account_id}",
        )

    async def get(self, account_id: int, session_string: str) -> Any:
        """Return a connected client for the account, creating/reconnecting
        as needed. Callers must not disconnect the returned client; use
        :meth:`discard` instead.

        Concurrency: :meth:`discard` and :meth:`close_all` serialize on the
        same per-account lock, so a ``get`` can never return a client that
        was discarded while its ``connect`` was still in flight. A client
        may still be discarded right *after* ``get`` returned it (inherent
        to the API) — callers must tolerate a closed client and re-``get``.
        """
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            client = self._clients.get(account_id)
            if client is not None and client.is_connected():
                return client
            if client is None:
                client = self._client_factory(account_id, session_string)
                self._clients[account_id] = client
            if not client.is_connected():
                await client.connect()
            return client

    async def discard(self, account_id: int) -> None:
        """Close the account's client (if any) and drop it from the cache;
        the next :meth:`get` builds a fresh one. Waits for any in-flight
        :meth:`get` on this account to finish first, so the discarded client
        is always fully closed. Idempotent; safe when no client was cached.
        """
        lock = self._locks.get(account_id)
        if lock is None:
            return  # no lock means no client was ever cached for the account
        async with lock:
            client = self._clients.pop(account_id, None)
            if client is not None and client.is_connected():
                await client.disconnect()
                logger.info("discarded client for account %s", account_id)

    async def close_all(self) -> None:
        """Close every cached client and empty the cache (shutdown path).

        Discards each account through :meth:`discard`, so per-account locks
        are honored and the lock registry stays consistent.
        """
        for account_id in list(self._clients):
            await self.discard(account_id)
        logger.info("client pool drained (%d lock(s) retained)", len(self._locks))
