"""Entity resolution: raw user input -> ResolvedEntity, via Telethon.

Accepts usernames (``@name``, ``name``), t.me links (username and invite
forms) and numeric ids. All Telegram knowledge for resolution lives here
(RULES §1); failures are reported as :class:`ResolutionError` with an
Arabic, user-facing message.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from telethon import TelegramClient, errors, utils
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Chat, ChatInvite, Channel, User

from app.core.models import ResolvedEntity

logger = logging.getLogger(__name__)

__all__ = ["ResolutionError", "resolve_entity"]

#: Arabic, user-facing messages for each resolution failure reason.
_REASONS_AR = {
    "invalid": "الرابط أو المُعرّف المدخل غير صحيح، أدخل معرفاً عاماً أو رابط مجموعة صحيح.",
    "not_found": "لم يتم العثور على هذا المُعرّف، تأكد من الرابط أو المعرف العام.",
    "unresolvable": "تعذّر تحديد هذه المجموعة، جرّب رابط مجموعة أو معرفاً عاماً آخر.",
    "unresolvable_numeric": (
        "المُعرّفات الرقمية لا يمكن الوصول إليها إلا إذا كان الحساب يعرف "
        "المجموعة مسبقاً؛ استخدم رابط المجموعة أو معرفها العام."
    ),
    "no_access": "الحساب لا يستطيع الوصول إلى هذه المجموعة (خاصة أو محجوبة عنه).",
}

_INVITE_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)$"
)
_LINK_USER_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{2,31})/?$"
)
_USERNAME_RE = re.compile(r"^@([A-Za-z][A-Za-z0-9_]{2,31})$")
_BARE_USERNAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{2,31})$")
_NUMERIC_RE = re.compile(r"^-?\d{1,20}$")


class ResolutionError(Exception):
    """Entity resolution failed; ``reason`` drives bot-side handling."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason  # "invalid" | "not_found" | "unresolvable" | "no_access"
        self.message = message


def _fail(reason: str) -> ResolutionError:
    return ResolutionError(reason, _REASONS_AR[reason])


def _fail_numeric() -> ResolutionError:
    """Numeric refs always degrade to "unresolvable" (contract reason set);
    only the Arabic message differs."""
    return ResolutionError("unresolvable", _REASONS_AR["unresolvable_numeric"])


def _parse(raw: str) -> tuple[str, Any]:
    """Classify raw input into ("username", "@name") | ("invite", hash) |
    ("numeric", int). Raises :class:`ResolutionError` on invalid input."""
    text = raw.strip()
    if not text:
        raise _fail("invalid")

    match = _INVITE_RE.match(text)
    if match:
        return "invite", match.group(1)

    match = _LINK_USER_RE.match(text)
    if match:
        return "username", "@" + match.group(1)

    if _NUMERIC_RE.match(text):
        return "numeric", int(text)

    match = _USERNAME_RE.match(text) or _BARE_USERNAME_RE.match(text)
    if match:
        return "username", "@" + match.group(1)

    raise _fail("invalid")


def _private_or_invalid(exc: BaseException) -> ResolutionError | None:
    """Map the specific RPC errors that carry their own resolution reason."""
    if isinstance(exc, (errors.ChannelPrivateError, errors.ChatForbiddenError,
                        errors.ChannelInvalidError)):
        return _fail("no_access")
    if isinstance(exc, (errors.InviteHashInvalidError, errors.InviteHashExpiredError)):
        return _fail("invalid")
    return None


async def _members_count(client: TelegramClient, entity: Any) -> int | None:
    """Best-effort member count: entity attribute, else one full-channel
    request. Never raises — an unavailable count is not a failure."""
    count = getattr(entity, "participants_count", None)
    if count is not None:
        return int(count)
    if isinstance(entity, Channel):
        try:
            full = await client(GetFullChannelRequest(channel=entity))
            return int(full.full_chat.participants_count)
        except (errors.RPCError, ConnectionError, TimeoutError, OSError, ValueError) as exc:
            logger.debug("participants_count unavailable: %s", type(exc).__name__)
    return None


def _kind_of(entity: Any) -> str:
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast and not entity.megagroup else "supergroup"
    if isinstance(entity, Chat):
        return "chat"
    if isinstance(entity, User):
        return "user"
    raise _fail("unresolvable")


def _entity_to_resolved(
    entity: Any, raw_ref: str, *, is_member: bool | None = None
) -> ResolvedEntity:
    kind = _kind_of(entity)
    if isinstance(entity, User):
        title = " ".join(
            part for part in (entity.first_name, entity.last_name) if part
        ).strip() or str(entity.id)
    else:
        title = entity.title or ""

    return ResolvedEntity(
        id=utils.get_peer_id(entity),
        kind=kind,
        title=title,
        username=getattr(entity, "username", None),
        members_count=None,  # filled async by caller when cheaply available
        is_broadcast=bool(getattr(entity, "broadcast", False)),
        is_megagroup=bool(getattr(entity, "megagroup", False)),
        is_member=is_member,
        via_invite_link=False,
        raw_ref=raw_ref,
    )


async def _resolve_via_invite(
    client: TelegramClient, invite_hash: str, raw_ref: str
) -> ResolvedEntity:
    """Resolve an invite hash WITHOUT joining (no side effects during
    resolution). ChatInviteAlready -> full entity, membership known;
    ChatInvite -> preview only (``via_invite_link=True``, ``is_member=False``).
    """
    try:
        result = await client(CheckChatInviteRequest(hash=invite_hash))
    except (errors.InviteHashInvalidError, errors.InviteHashExpiredError) as exc:
        raise _fail("invalid") from exc
    except (errors.ChannelPrivateError, errors.ChatForbiddenError,
            errors.ChannelInvalidError) as exc:
        raise _fail("no_access") from exc
    except errors.RPCError as exc:
        raise _fail("unresolvable") from exc

    chat = getattr(result, "chat", None)
    if chat is not None:  # ChatInviteAlready (or peek): the account knows the chat
        resolved = _entity_to_resolved(chat, raw_ref)
        count = await _members_count(client, chat)
        return replace(resolved, members_count=count, is_member=True,
                       via_invite_link=True)

    if isinstance(result, ChatInvite):
        kind = "channel" if result.broadcast and not result.megagroup else "supergroup"
        count = getattr(result, "participants_count", None)
        return ResolvedEntity(
            id=0,  # invite previews carry no channel id until joined
            kind=kind,
            title=result.title or "",
            username=None,
            members_count=int(count) if count is not None else None,
            is_broadcast=bool(result.broadcast),
            is_megagroup=bool(result.megagroup),
            is_member=False,
            via_invite_link=True,
            raw_ref=raw_ref,
        )

    raise _fail("unresolvable")


async def resolve_entity(client: TelegramClient, raw: str) -> ResolvedEntity:
    """Resolve user-supplied ``raw`` into a :class:`ResolvedEntity`.

    Accepts (after trimming whitespace, case-insensitive scheme):
    ``@name`` / ``name``, ``https://t.me/name`` style links,
    ``t.me/+HASH`` / ``t.me/joinchat/HASH`` invite links, and numeric ids
    (``-100...``, negative, bare). Numeric ids only resolve when the account
    already knows the entity.

    Never joins groups (invite links yield previews) and raises
    :class:`ResolutionError` for every expected failure mode.
    """
    form, value = _parse(raw)

    if form == "invite":
        return await _resolve_via_invite(client, value, raw)

    try:
        entity = await client.get_entity(value)
    except ValueError as exc:
        # Telethon raises ValueError("Could not find the input entity ...").
        reason = _fail_numeric() if form == "numeric" else _fail("not_found")
        raise reason from exc
    except errors.RPCError as exc:
        if form == "numeric":
            # Numeric refs only work when the account already knows the
            # entity; every failure degrades to the same explanation.
            raise _fail_numeric() from exc
        mapped = _private_or_invalid(exc)
        if mapped is None:
            mapped = _fail("unresolvable")
        raise mapped from exc

    resolved = _entity_to_resolved(entity, raw)
    count = await _members_count(client, entity)
    return replace(resolved, members_count=count)
