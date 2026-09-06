"""Interactive account login flow (phone -> code [-> 2FA password]).

Flow state lives ONLY in memory (RULES §3): the temporary client, phone,
``phone_code_hash`` and stage are kept in :class:`LoginFlowManager` with a
TTL swept by a background task. Codes, passwords and session strings are
never logged and never leave this module until the caller receives the
final session string in a :class:`LoginResult`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from app.tg.errors import LoginFailure, classify_login_error, login_message

logger = logging.getLogger(__name__)

__all__ = ["LoginFlowManager", "LoginResult"]

_PHONE_RE = re.compile(r"^\+?\d{7,15}$")

#: How often the sweeper wakes, as a fraction of the flow TTL.
_SWEEP_DIVISOR = 4
_MIN_SWEEP_INTERVAL = 0.05


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Successful login: encrypted-at-rest by the caller, never logged."""

    session_string: str
    tg_user_id: int
    username: str | None
    display_name: str


@dataclass(slots=True)
class _Flow:
    """In-memory, TTL-bound login state for one owner."""

    client: Any
    phone: str
    phone_code_hash: str
    created_at: float
    stage: str  # "code" | "password"


async def _safe_disconnect(client: Any) -> None:
    """Disconnect a temp login client, swallowing teardown errors so they
    never mask the LoginFailure (or result) the caller must see."""
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception as exc:
        logger.debug("login client disconnect failed: %s", type(exc).__name__)


class LoginFlowManager:
    """Owns one temporary TelegramClient per in-progress login.

    Sweeper design: :meth:`run_sweeper` is the loop body (an infinite loop
    that drops expired flows); :meth:`start_sweeper` wraps it in
    ``asyncio.create_task`` for callers that just want it running, and
    :meth:`stop_sweeper` cancels and awaits that task.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        ttl_seconds: int = 600,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._ttl_seconds = ttl_seconds
        self._client_factory = client_factory or self._make_client
        self._flows: dict[int, _Flow] = {}
        self._sweeper_task: asyncio.Task[None] | None = None

    def _make_client(self) -> TelegramClient:
        return TelegramClient(
            StringSession(),
            self._api_id,
            self._api_hash,
            receive_updates=False,
        )

    # -- flow state ------------------------------------------------------------

    def has_flow(self, owner_id: int) -> bool:
        return owner_id in self._flows

    async def start(self, owner_id: int, phone: str) -> None:
        """Send a login code to ``phone`` and open a flow for ``owner_id``.

        Any failure raises :class:`LoginFailure`; the temporary client is
        always cleaned up on failure.
        """
        normalized = phone.strip()
        if not _PHONE_RE.match(normalized):
            raise LoginFailure("PHONE_INVALID", login_message("PHONE_INVALID"))
        if not normalized.startswith("+"):
            normalized = "+" + normalized

        await self.cancel(owner_id)  # restart cleanly if a flow is open

        client = self._client_factory()
        try:
            await client.connect()
            sent = await client.send_code_request(normalized)
            phone_code_hash = getattr(sent, "phone_code_hash", None)
            if not phone_code_hash:
                raise LoginFailure("UNEXPECTED", login_message("UNEXPECTED"))
        except LoginFailure:
            await _safe_disconnect(client)
            raise
        except Exception as exc:
            await _safe_disconnect(client)
            raise classify_login_error(exc) from exc

        self._flows[owner_id] = _Flow(
            client=client,
            phone=normalized,
            phone_code_hash=phone_code_hash,
            created_at=time.monotonic(),
            stage="code",
        )
        logger.info("login code sent for owner %s", owner_id)

    async def submit_code(self, owner_id: int, code: str) -> str:
        """Verify the login code.

        Returns the literal string ``"password"`` when 2FA is enabled (the
        caller must then call :meth:`submit_password`), otherwise the fresh
        session string. A wrong code keeps the flow alive so the user can
        retry; expired codes and other failures drop it.
        """
        flow = self._flows.get(owner_id)
        if flow is None or flow.stage != "code":
            raise LoginFailure("UNEXPECTED", login_message("UNEXPECTED"))

        try:
            await flow.client.sign_in(
                phone=flow.phone, code=code.strip(), phone_code_hash=flow.phone_code_hash
            )
        except errors.SessionPasswordNeededError:
            flow.stage = "password"
            return "password"
        except errors.PhoneCodeInvalidError as exc:
            raise classify_login_error(exc) from exc  # flow kept for retry
        except Exception as exc:
            await self.cancel(owner_id)
            raise classify_login_error(exc) from exc

        result = await self._finish(owner_id, flow)
        return result.session_string

    async def submit_password(self, owner_id: int, password: str) -> LoginResult:
        """Complete a 2FA login. A wrong password keeps the flow alive."""
        flow = self._flows.get(owner_id)
        if flow is None or flow.stage != "password":
            raise LoginFailure("UNEXPECTED", login_message("UNEXPECTED"))

        try:
            await flow.client.sign_in(password=password)
        except errors.PasswordHashInvalidError as exc:
            raise classify_login_error(exc) from exc  # flow kept for retry
        except Exception as exc:
            await self.cancel(owner_id)
            raise classify_login_error(exc) from exc

        return await self._finish(owner_id, flow)

    async def _finish(self, owner_id: int, flow: _Flow) -> LoginResult:
        """Extract the session string and profile, then tear the flow down."""
        try:
            me = await flow.client.get_me()
            session_string = flow.client.session.save()
        except Exception as exc:
            await self.cancel(owner_id)
            raise classify_login_error(exc) from exc
        await self.cancel(owner_id)

        if me is None:
            raise LoginFailure("UNEXPECTED", login_message("UNEXPECTED"))
        display_name = " ".join(
            part
            for part in (getattr(me, "first_name", None), getattr(me, "last_name", None))
            if part
        ).strip() or getattr(me, "username", None) or str(me.id)
        logger.info("login completed for owner %s", owner_id)
        return LoginResult(
            session_string=session_string,
            tg_user_id=int(me.id),
            username=getattr(me, "username", None),
            display_name=display_name,
        )

    async def cancel(self, owner_id: int) -> None:
        """Drop the owner's flow, disconnecting its client. Idempotent."""
        flow = self._flows.pop(owner_id, None)
        if flow is None:
            return
        await _safe_disconnect(flow.client)
        logger.info("login flow cancelled for owner %s", owner_id)

    # -- TTL sweeper -----------------------------------------------------------

    async def _sweep_once(self) -> None:
        now = time.monotonic()
        expired = [
            owner_id
            for owner_id, flow in self._flows.items()
            if now - flow.created_at > self._ttl_seconds
        ]
        for owner_id in expired:
            logger.info("login flow expired for owner %s", owner_id)
            await self.cancel(owner_id)

    async def run_sweeper(self) -> None:
        """Loop body: drop expired flows forever, until the task is cancelled.
        Schedule via :meth:`start_sweeper` (or your own ``create_task``)."""
        interval = max(_MIN_SWEEP_INTERVAL, self._ttl_seconds / _SWEEP_DIVISOR)
        try:
            while True:
                await asyncio.sleep(interval)
                await self._sweep_once()
        except asyncio.CancelledError:
            logger.debug("login sweeper stopped")

    def start_sweeper(self) -> None:
        """Start the sweeper as a background asyncio task (idempotent)."""
        if self._sweeper_task is None or self._sweeper_task.done():
            self._sweeper_task = asyncio.create_task(
                self.run_sweeper(), name="login-flow-sweeper"
            )

    async def stop_sweeper(self) -> None:
        """Cancel and await the sweeper task, if running."""
        task = self._sweeper_task
        self._sweeper_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
