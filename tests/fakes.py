"""Offline test doubles for the Telethon surface.

Shared by all tg-layer tests (Tasks 2+). Extend this file, do not replace
it. ``FakeTelegramClient`` implements exactly the surface the tg layer uses
so far; anything unscripted fails loudly instead of silently succeeding.
"""

from __future__ import annotations

from typing import Any

from telethon import utils as tg_utils
from telethon.errors import SessionPasswordNeededError
from telethon.tl.custom.participantpermissions import ParticipantPermissions
from telethon.tl.types import (
    ChatAdminRights,
    ChannelParticipant,
    ChannelParticipantAdmin,
    User,
)


def admin_permissions(invite_users: bool) -> ParticipantPermissions:
    """Real ``ParticipantPermissions`` for an admin with one toggled right."""
    participant = ChannelParticipantAdmin(
        can_edit=True,
        admin_rights=ChatAdminRights(invite_users=invite_users),
        user_id=1,
        date=None,
        promoted_by=1,
    )
    return ParticipantPermissions(participant, chat=False)


def member_permissions() -> ParticipantPermissions:
    """Real ``ParticipantPermissions`` for a plain (non-admin) member."""
    return ParticipantPermissions(ChannelParticipant(user_id=1, date=None), chat=False)


class FakeSession:
    """Mimics ``StringSession``: ``save()`` returns the canned string."""

    def __init__(self, session_string: str = "fake-session-string") -> None:
        self._session_string = session_string
        self.save_count = 0

    def save(self) -> str:
        self.save_count += 1
        return self._session_string


class FakeTelegramClient:
    """Scriptable stand-in for ``TelegramClient``.

    Constructor knobs:
    - ``entities``: mapping ref -> TL entity for ``get_entity``. A value that
      is a ``BaseException`` instance is raised instead (per-ref failures).
    - ``invite_results``: canned result for ``CheckChatInviteRequest`` (an
      exception instance is raised).
    - ``participants``: canned result for ``GetFullChannelRequest`` (the
      resolver reads ``.full_chat.participants_count`` from it).
    - ``raises``: mapping method name -> exception, checked first in every
      implemented method (e.g. ``{"send_code_request": FloodWaitError(...)}``).
    - ``code_flow``: login success shapes, e.g. ``{"sign_in": "password"}``
      makes ``sign_in`` raise ``SessionPasswordNeededError``.
    - ``me``: object returned by ``get_me`` (default: a real TL ``User``).
    - ``extra_results``: canned returns for the generic ``__getattr__``
      surface, keyed by method name.
    - ``permissions``: peer id (``tg_utils.get_peer_id`` of the peer passed in)
      -> ``ParticipantPermissions``-like object or exception instance, for
      ``get_permissions``.
    - ``participants_by_peer``: peer id -> list of user objects or exception
      instance, consumed by ``iter_participants`` (unscripted peers fail
      loudly).
    - ``input_entities``: peer id -> input entity or exception instance, for
      ``get_input_entity`` (unscripted peers raise ValueError like Telethon).
    - ``invite_outcomes``: outcomes consumed in order per invite request
      (``AddChatUserRequest`` / ``InviteToChannelRequest``); ``None`` or an
      exhausted list means success.
    """

    def __init__(
        self,
        *,
        entities: dict[Any, Any] | None = None,
        invite_results: Any = None,
        participants: Any = None,
        raises: dict[str, BaseException] | None = None,
        code_flow: dict[str, str] | None = None,
        me: Any = None,
        session_string: str = "fake-session-string",
        extra_results: dict[str, Any] | None = None,
        permissions: dict[int, Any] | None = None,
        participants_by_peer: dict[int, Any] | None = None,
        input_entities: dict[int, Any] | None = None,
        invite_outcomes: list[Any] | None = None,
    ) -> None:
        self.session = FakeSession(session_string)
        self.entities: dict[Any, Any] = dict(entities or {})
        self.invite_results = invite_results
        self.participants = participants
        self.raises: dict[str, BaseException] = dict(raises or {})
        self.code_flow: dict[str, str] = dict(code_flow or {})
        self.me = me if me is not None else User(
            id=900001, is_self=True, first_name="Fake", username="fake_user"
        )
        self.extra_results: dict[str, Any] = dict(extra_results or {})
        self.permissions: dict[int, Any] = dict(permissions or {})
        self.participants_by_peer: dict[int, Any] = dict(participants_by_peer or {})
        self.input_entities: dict[int, Any] = dict(input_entities or {})
        self.invite_outcomes: list[Any] = list(invite_outcomes or [])
        self.connected = False
        self.connect_count = 0
        self.disconnect_count = 0
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    # -- lifecycle ---------------------------------------------------------------

    async def connect(self) -> None:
        self._raise_if_scripted("connect")
        self.connected = True
        self.connect_count += 1

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1

    def is_connected(self) -> bool:
        return self.connected

    # -- login flow ----------------------------------------------------------------

    async def send_code_request(self, phone: str) -> Any:
        self.calls.append(("send_code_request", (phone,)))
        self._raise_if_scripted("send_code_request")
        return self.code_flow.get("sent_code") or _SentCode()

    async def sign_in(
        self,
        phone: str | None = None,
        code: str | None = None,
        password: str | None = None,
        phone_code_hash: str | None = None,
    ) -> Any:
        if password is not None:
            self.calls.append(("sign_in_password", (password,)))
            self._raise_if_scripted("sign_in_password")
            return self.me
        self.calls.append(("sign_in", (phone, code, phone_code_hash)))
        self._raise_if_scripted("sign_in")
        if self.code_flow.get("sign_in") == "password":
            raise SessionPasswordNeededError(request=None)
        return self.me

    async def get_me(self) -> Any:
        self.calls.append(("get_me", ()))
        self._raise_if_scripted("get_me")
        return self.me

    # -- resolution surface -----------------------------------------------------

    async def get_entity(self, ref: Any) -> Any:
        self.calls.append(("get_entity", (ref,)))
        self._raise_if_scripted("get_entity")
        try:
            entity = self.entities[ref]
        except KeyError:
            raise ValueError(
                f"Could not find the input entity for {ref!r} "
                "(FakeTelegramClient has no scripted entity for it)"
            ) from None
        if isinstance(entity, BaseException):
            raise entity
        return entity

    # -- permissions / participants / invites ---------------------------------

    async def get_permissions(self, entity: Any, user: Any = None) -> Any:
        key = tg_utils.get_peer_id(entity)
        self.calls.append(("get_permissions", (key, user)))
        self._raise_if_scripted("get_permissions")
        result = self.permissions.get(key)
        if result is None:
            raise RuntimeError(f"no scripted permissions for peer {key}")
        if isinstance(result, BaseException):
            raise result
        return result

    def iter_participants(self, entity: Any, limit: int | None = None, **kwargs: Any) -> Any:
        key = tg_utils.get_peer_id(entity)
        self.calls.append(("iter_participants", (key, limit)))
        self._raise_if_scripted("iter_participants")
        if key not in self.participants_by_peer:
            raise RuntimeError(f"no scripted participants for peer {key}")
        scripted = self.participants_by_peer[key]

        async def _gen() -> Any:
            if isinstance(scripted, BaseException):
                raise scripted
            yielded = 0
            for item in scripted:
                if limit is not None and yielded >= limit:
                    break
                yielded += 1
                yield item

        return _gen()

    async def get_input_entity(self, peer: Any) -> Any:
        key = tg_utils.get_peer_id(peer)
        self.calls.append(("get_input_entity", (key,)))
        self._raise_if_scripted("get_input_entity")
        result = self.input_entities.get(key)
        if result is None:
            raise ValueError(
                f"Could not find the input entity for peer {key} "
                "(FakeTelegramClient has no scripted input entity for it)"
            )
        if isinstance(result, BaseException):
            raise result
        return result

    def __call__(self, request: Any) -> Any:
        """TL-request entry point (``await client(SomeRequest(...))``), keyed
        by the request's class name."""
        name = type(request).__name__

        async def _run() -> Any:
            self.calls.append((name, (request,)))
            if name in ("AddChatUserRequest", "InviteToChannelRequest"):
                if self.invite_outcomes:
                    outcome = self.invite_outcomes.pop(0)
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome
                return None  # unscripted invites succeed
            if name == "CheckChatInviteRequest":
                result = self.invite_results
            elif name == "GetFullChannelRequest":
                result = self.participants
            else:
                result = self.extra_results.get(name)
            if result is None:
                raise RuntimeError(f"no canned result scripted for {name}")
            if isinstance(result, BaseException):
                raise result
            return result

        return _run()

    def __getattr__(self, name: str) -> Any:
        """Generic surface: any unknown method returns an async callable that
        returns the canned value from ``extra_results`` (or ``None``)."""
        if name.startswith("_"):
            raise AttributeError(name)

        async def _method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args))
            result = self.extra_results.get(name)
            if isinstance(result, BaseException):
                raise result
            return result

        return _method

    # -- helpers -------------------------------------------------------------------

    def _raise_if_scripted(self, name: str) -> None:
        exc = self.raises.get(name)
        if exc is not None:
            raise exc


class _SentCode:
    """Minimal stand-in for ``types.auth.SentCode``."""

    def __init__(self, phone_code_hash: str = "fake-phone-code-hash") -> None:
        self.phone_code_hash = phone_code_hash


class FakeClientPool:
    """Stand-in for ``ClientPool``: records get/discard, hands out one stub
    client per account (optionally built by ``client_factory``)."""

    def __init__(self, client_factory: Any = None) -> None:
        self._factory = client_factory or (lambda account_id, session: object())
        self.clients: dict[int, Any] = {}
        self.gets: list[tuple[int, str]] = []
        self.discarded: list[int] = []

    async def get(self, account_id: int, session_string: str) -> Any:
        self.gets.append((account_id, session_string))
        if account_id not in self.clients:
            self.clients[account_id] = self._factory(account_id, session_string)
        return self.clients[account_id]

    async def discard(self, account_id: int) -> None:
        self.discarded.append(account_id)
        self.clients.pop(account_id, None)

    async def close_all(self) -> None:
        for account_id in list(self.clients):
            await self.discard(account_id)
