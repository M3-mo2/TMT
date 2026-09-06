"""Deep preflight: runtime verification of every transfer precondition.

Consumes a connected Telethon client and :class:`ResolvedEntity` objects from
``app.tg.resolver`` and produces :class:`Check` items (Arabic ``message``,
technical ``detail``). Every Telegram capability is verified with a real API
call or reported UNKNOWN — never assumed (PRD §8, RULES §2). Classified RPC
failures never raise out of :func:`run_preflight`; they become FAIL/UNKNOWN
checks. Truly unexpected exceptions may propagate.
"""

from __future__ import annotations

import logging
from typing import Any

from telethon import errors, utils

from app.core.models import Check, CheckStatus, ResolvedEntity

logger = logging.getLogger(__name__)

__all__ = ["run_preflight"]

#: Size of the first participants page fetched to probe the source member list.
_PAGE_LIMIT = 200

#: Exception classes expected around probe calls: classified RPC failures,
#: unresolvable peers (ValueError) and plain network trouble. Anything else is
#: unexpected and propagates.
_PROBE_ERRORS = (errors.RPCError, ValueError, ConnectionError, TimeoutError, OSError)


def _check(key: str, status: CheckStatus, message: str, detail: str = "") -> Check:
    return Check(key=key, status=status, message=message, detail=detail)


def _technical(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _peer_of(resolved: ResolvedEntity) -> Any:
    """Peer reference addressable by the client, or None (invite previews
    carry ``id=0`` and cannot be addressed until the account joins)."""
    if resolved.id == 0:
        return None
    if resolved.kind not in ("chat", "supergroup", "channel"):
        return None
    # ResolvedEntity.id is Telethon's marked id; get_peer turns it back into
    # the correct PeerChat / PeerChannel reference.
    return utils.get_peer(resolved.id)


def _same_group(source: ResolvedEntity, dest: ResolvedEntity) -> Check:
    if source.id != 0 and source.id == dest.id:
        return _check(
            "dest_diff",
            CheckStatus.FAIL,
            "المجموعة المصدر والهدف هما نفس المجموعة، النقل داخل نفس المجموعة غير ممكن.",
            f"source_id={source.id} dest_id={dest.id}",
        )
    return _check(
        "dest_diff",
        CheckStatus.PASS,
        "المجموعة المصدر والهدف مختلفتان.",
        f"source_id={source.id} dest_id={dest.id}",
    )


def _supported_type(key: str, resolved: ResolvedEntity) -> Check:
    if resolved.is_groupish:
        return _check(
            key,
            CheckStatus.PASS,
            "المجموعة مدعومة.",
            f"id={resolved.id} kind={resolved.kind}",
        )
    return _check(
        key,
        CheckStatus.FAIL,
        "النوع غير مدعوم: القنوات التي لا تقبل الأعضاء والحسابات الشخصية لا يمكن استخدامها.",
        f"id={resolved.id} kind={resolved.kind} broadcast={resolved.is_broadcast}",
    )


async def _membership(
    client: Any, key: str, resolved: ResolvedEntity, peer: Any
) -> Check:
    """Verify the account is a member of ``resolved`` via get_permissions."""
    label = "المصدر" if key == "source_member" else "الهدف"
    if resolved.via_invite_link and resolved.is_member is False:
        return _check(
            key,
            CheckStatus.FAIL,
            f"الحساب ليس عضواً في مجموعة {label} (تعرف عليها عبر رابط دعوة فقط)، "
            "يجب الانضمام إلى المجموعة أولاً.",
            f"id={resolved.id} via_invite_link=True",
        )
    if peer is None:
        return _check(
            key,
            CheckStatus.UNKNOWN,
            f"تعذر التحقق من عضوية الحساب في مجموعة {label}.",
            f"id={resolved.id} kind={resolved.kind} not addressable",
        )
    try:
        permissions = await client.get_permissions(peer, "me")
    except errors.UserNotParticipantError:
        return _check(
            key,
            CheckStatus.FAIL,
            f"الحساب ليس عضواً في مجموعة {label}، يجب الانضمام إلى المجموعة أولاً.",
            f"peer={peer!r} UserNotParticipantError",
        )
    except _PROBE_ERRORS as exc:
        return _check(
            key,
            CheckStatus.UNKNOWN,
            f"تعذر التحقق من عضوية الحساب في مجموعة {label}.",
            f"peer={peer!r} {_technical(exc)}",
        )
    if permissions is None:
        return _check(
            key,
            CheckStatus.UNKNOWN,
            f"تعذر التحقق من عضوية الحساب في مجموعة {label}.",
            f"peer={peer!r} get_permissions returned None",
        )
    return _check(
        key,
        CheckStatus.PASS,
        f"الحساب عضو في مجموعة {label}.",
        f"peer={peer!r} participant={type(getattr(permissions, 'participant', None)).__name__}",
    )


async def _source_participants(
    client: Any, source: ResolvedEntity, peer: Any, max_members: int
) -> Check:
    """Probe the source member list: hidden lists surface as UNKNOWN and an
    over-cap member count as WARN."""
    if peer is None:
        return _check(
            "source_participants",
            CheckStatus.UNKNOWN,
            "تعذر جلب قائمة أعضاء المصدر.",
            f"id={source.id} not addressable",
        )
    fetched = 0
    try:
        async for _user in client.iter_participants(peer, limit=_PAGE_LIMIT):
            fetched += 1
    except _PROBE_ERRORS as exc:
        return _check(
            "source_participants",
            CheckStatus.UNKNOWN,
            "تعذر جلب قائمة أعضاء المصدر.",
            f"peer={peer!r} {_technical(exc)}",
        )

    if fetched == 0 and (source.members_count is None or source.members_count > 0):
        return _check(
            "source_participants",
            CheckStatus.UNKNOWN,
            "قائمة الأعضاء غير متاحة قد تكون مخفية",
            f"fetched=0 members_count={source.members_count}",
        )
    if fetched == 0:
        return _check(
            "source_participants",
            CheckStatus.PASS,
            "قائمة أعضاء المصدر فارغة ولا يوجد أعضاء لنقلهم.",
            f"fetched=0 members_count={source.members_count}",
        )
    count = source.members_count if source.members_count is not None else fetched
    if count > max_members:
        return _check(
            "source_participants",
            CheckStatus.WARN,
            f"عدد الأعضاء ({count}) أكبر من الحد المسموح ({max_members})؛ "
            f"ستتم معالجة {max_members} عضو فقط.",
            f"fetched={fetched} members_count={source.members_count} max={max_members}",
        )
    return _check(
        "source_participants",
        CheckStatus.PASS,
        "قائمة الأعضاء متاحة ويمكن جلبها.",
        f"fetched={fetched} members_count={source.members_count}",
    )


async def _dest_invite_rights(client: Any, dest: ResolvedEntity, peer: Any) -> Check:
    """Invite permission is only provable for readable admin rights; a plain
    membership or a basic chat yields UNKNOWN (PRD §8)."""
    if peer is None:
        return _check(
            "dest_invite_rights",
            CheckStatus.UNKNOWN,
            "تعذر التحقق من صلاحية الإضافة في الهدف.",
            f"id={dest.id} not addressable",
        )
    if dest.kind == "chat":
        return _check(
            "dest_invite_rights",
            CheckStatus.UNKNOWN,
            "لا يمكن التحقق المسبق من صلاحية الإضافة في المجموعات العادية؛ "
            "أول محاولة إضافة هي الاختبار الفعلي.",
            f"kind=chat id={dest.id}",
        )
    try:
        permissions = await client.get_permissions(peer, "me")
    except _PROBE_ERRORS as exc:
        return _check(
            "dest_invite_rights",
            CheckStatus.UNKNOWN,
            "تعذر التحقق من صلاحيات الحساب في الهدف.",
            f"peer={peer!r} {_technical(exc)}",
        )
    if permissions is None:
        return _check(
            "dest_invite_rights",
            CheckStatus.UNKNOWN,
            "تعذر التحقق من صلاحيات الحساب في الهدف.",
            f"peer={peer!r} get_permissions returned None",
        )
    if getattr(permissions, "is_admin", False):
        if getattr(permissions, "invite_users", False):
            return _check(
                "dest_invite_rights",
                CheckStatus.PASS,
                "الحساب مشرف في الهدف وله صلاحية إضافة الأعضاء.",
                f"participant={type(permissions.participant).__name__}",
            )
        return _check(
            "dest_invite_rights",
            CheckStatus.FAIL,
            "الحساب مشرف في الهدف لكن دون صلاحية إضافة الأعضاء.",
            f"participant={type(permissions.participant).__name__} invite_users=False",
        )
    return _check(
        "dest_invite_rights",
        CheckStatus.UNKNOWN,
        "الحساب عضو عادي في الهدف؛ لا تكشف الواجهة البرمجية صلاحية الإضافة للعضو "
        "العادي، أول محاولة إضافة هي الاختبار الفعلي.",
        f"participant={type(permissions.participant).__name__} not admin",
    )


async def _account_restriction(client: Any) -> Check:
    try:
        me = await client.get_me()
    except _PROBE_ERRORS as exc:
        return _check(
            "account_restriction",
            CheckStatus.UNKNOWN,
            "تعذر التحقق من حالة حساب تيليجرام.",
            _technical(exc),
        )
    if getattr(me, "restricted", False):
        reasons = getattr(me, "restriction_reason", None) or []
        summary = "; ".join(
            str(getattr(reason, "reason", "") or getattr(reason, "platform", ""))
            for reason in reasons
        )
        return _check(
            "account_restriction",
            CheckStatus.WARN,
            "حساب تيليجرام مقيّد حالياً؛ قد تفشل بعض العمليات على الحساب.",
            f"restriction_reason={summary or 'unspecified'}",
        )
    return _check(
        "account_restriction",
        CheckStatus.PASS,
        "حساب تيليجرام غير مقيّد.",
        f"me_id={getattr(me, 'id', None)}",
    )


async def run_preflight(
    client: Any,
    source: ResolvedEntity,
    dest: ResolvedEntity,
    *,
    max_members: int,
) -> list[Check]:
    """Run every inspection and return the checks in presentation order."""
    source_peer = _peer_of(source)
    dest_peer = _peer_of(dest)
    return [
        _same_group(source, dest),
        _supported_type("source_type", source),
        _supported_type("dest_type", dest),
        await _membership(client, "source_member", source, source_peer),
        await _membership(client, "dest_member", dest, dest_peer),
        await _source_participants(client, source, source_peer, max_members),
        await _dest_invite_rights(client, dest, dest_peer),
        await _account_restriction(client),
        _check(
            "flood_note",
            CheckStatus.INFO,
            "لا يمكن التحقق من حدود تكرار الإضافة مسبقاً؛ تتم مراقبة حدود الانتظار "
            "من تيليجرام أثناء التنفيذ وإدارتها تلقائياً.",
        ),
    ]
