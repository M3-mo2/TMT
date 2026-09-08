"""Admin: broadcast messaging."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import (
    _btn,
    broadcast_center_kb,
    broadcast_confirm_kb,
    broadcast_live_kb,
    broadcast_target_kb,
)
from app.bot.texts import (
    PARSE_MODE,
    bcast_status_label,
    esc,
    render_audience_builder,
    render_bcast_preview,
    render_bcast_summary,
    render_broadcast_center,
)
from app.config import Config
from app.core.broadcast import Broadcaster, count_audience
from app.core.broadcast_models import AudienceFilter, BroadcastStatus
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit

router = Router()

HISTORY_PER_PAGE = 10

_AR_TIMEWORDS = {
    "غداً": "tomorrow",
    "غدا": "tomorrow",
    "اليوم": "today",
    "الآن": "now",
}


# ---------------------------------------------------------------- helpers


def _int_after(data: str, prefix: str) -> int | None:
    """Parse an int trailing callback data; None if malformed.

    Callback data is user-controlled input, not authorization (RULES §4),
    so a forged non-numeric suffix must not raise a 500."""
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


def _toggle_filter(filters: AudienceFilter, action: str) -> None:
    """Apply a toggle action to an ``AudienceFilter`` in place.

    Dimensions ``all``/``active``/``inactive``/``blocked`` set the target;
    ``accounts_min``/``accounts_max``/``registered_days``/``last_seen`` cycle
    through preset numeric values; ``exclude_admins``/``exclude_previous``
    flip booleans."""
    if action == "all":
        filters.target = "all"
    elif action == "active":
        filters.target = "active"
    elif action == "inactive":
        filters.target = "inactive"
    elif action == "blocked":
        filters.target = "blocked"
    elif action == "accounts_min":
        vals = [None, 1, 5, 10]
        idx = vals.index(filters.account_count_min) if filters.account_count_min in vals else -1
        filters.account_count_min = vals[(idx + 1) % len(vals)]
    elif action == "accounts_max":
        vals = [None, 5, 10, 20]
        idx = vals.index(filters.account_count_max) if filters.account_count_max in vals else -1
        filters.account_count_max = vals[(idx + 1) % len(vals)]
    elif action == "registered_days":
        vals = [None, 30, 90, 365]
        idx = vals.index(filters.registered_days_ago) if filters.registered_days_ago in vals else -1
        filters.registered_days_ago = vals[(idx + 1) % len(vals)]
    elif action == "last_seen":
        vals = [None, 7, 30, 90]
        idx = vals.index(filters.last_seen_days_ago) if filters.last_seen_days_ago in vals else -1
        filters.last_seen_days_ago = vals[(idx + 1) % len(vals)]
    elif action == "exclude_admins":
        filters.exclude_admins = not filters.exclude_admins
    elif action == "exclude_previous":
        filters.exclude_previously_contacted = not filters.exclude_previously_contacted


def _parse_schedule_time(text: str) -> str | None:
    """Parse schedule time from user input.

    Supported formats:
    - ISO: ``2024-01-15T20:00:00Z``
    - Relative: ``+2h``, ``+1d``, ``+30m``
    - Natural: ``tomorrow 20:00``, ``غداً 20:00``
    Returns an ISO-8601 UTC string or ``None`` if unparseable / in the past."""
    text = text.strip()
    now = datetime.now(timezone.utc)

    # Relative: +2h, +1d, +30m
    m = re.match(r"^\+(\d+)([hdm])$", text)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        if unit == "h":
            dt = now + timedelta(hours=amount)
        elif unit == "d":
            dt = now + timedelta(days=amount)
        elif unit == "m":
            dt = now + timedelta(minutes=amount)
        else:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Natural language / ISO via dateutil
    try:
        from dateutil import parser as du_parser

        translated = text
        now = datetime.now(timezone.utc)
        for ar, en in _AR_TIMEWORDS.items():
            translated = translated.replace(ar, en)

        # Replace "tomorrow"/"today" with actual date strings
        if "tomorrow" in translated.lower():
            tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            translated = re.sub(r"tomorrow", tomorrow, translated, flags=re.IGNORECASE)
        if "today" in translated.lower():
            today = now.strftime("%Y-%m-%d")
            translated = re.sub(r"today", today, translated, flags=re.IGNORECASE)
        if "now" in translated.lower():
            translated = re.sub(r"now", now.strftime("%H:%M:%S"), translated, flags=re.IGNORECASE)

        dt = du_parser.parse(translated)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        if dt <= now:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _build_history_kb(
    campaigns: list[dict], page: int, total_pages: int
) -> InlineKeyboardMarkup:
    """Build a history keyboard with per-campaign view buttons + pagination."""
    rows = []
    for c in campaigns:
        rows.append([_btn(f"› عرض #{c['id']}", f"{C.BCAST_VIEW}{c['id']}")])
    nav = []
    if page > 0:
        nav.append(_btn("← السابق", f"{C.BCAST_PAGE}{page - 1}"))
    if page < total_pages - 1:
        nav.append(_btn("التالي →", f"{C.BCAST_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("› رجوع", C.BCAST)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------- FSM


class BcastFSM(StatesGroup):
    compose = State()
    target = State()
    scheduled_for = State()


# ---------------------------------------------------------------- dashboard


@router.callback_query(F.data == C.BCAST)
async def cb_broadcast(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    limit = 50
    drafts = await repo.list_broadcasts(db, status="draft", limit=limit)
    scheduled = await repo.list_broadcasts(db, status="scheduled", limit=limit)
    running = await repo.list_broadcasts(db, status="running", limit=limit)
    completed = await repo.list_broadcasts(db, status="completed", limit=limit)
    text = render_broadcast_center(drafts, scheduled, running, completed)
    await safe_edit(cb, text, reply_markup=broadcast_center_kb())


# ---------------------------------------------------------------- compose


@router.callback_query(F.data == C.BCAST_NEW)
async def cb_bcast_new(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(BcastFSM.compose)
    await state.update_data(campaign_id=None)
    await safe_edit(cb, "📋 أرسل الرسالة التي تريد بثها:\nاستخدم <b>HTML</b> للتنسيق.")


@router.message(BcastFSM.compose)
async def bcast_message_entered(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    if message.from_user is None:
        return
    campaign_id = await repo.create_broadcast(
        db,
        admin_id=message.from_user.id,
        label=f"بث #{message.from_user.id}",
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        mode="copy",
        filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await state.update_data(campaign_id=campaign_id)
    await state.set_state(BcastFSM.target)

    filters = AudienceFilter.default()
    admin_ids = config.admin_id_list
    user_count = await count_audience(db, filters, admin_ids)
    await message.answer(
        render_audience_builder(filters, user_count),
        parse_mode=PARSE_MODE,
        reply_markup=broadcast_target_kb(filters),
    )


# ---------------------------------------------------------------- target


@router.callback_query(F.data.startswith(C.BCAST_TARGET_X))
async def cb_bcast_target_x(
    cb: CallbackQuery, state: FSMContext, db: Database, config: Config
) -> None:
    await cb.answer()
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if campaign_id is None:
        await safe_edit(
            cb, "❌ انتهت الجلسة، أنشئ مسودة جديدة.", reply_markup=broadcast_center_kb()
        )
        return

    sub_action = cb.data.removeprefix(C.BCAST_TARGET_X)
    filter_dict = data.get("filter") or {}
    filters = AudienceFilter.from_dict(filter_dict)

    if sub_action:
        _toggle_filter(filters, sub_action)
        await state.update_data(filter=filters.to_dict())

    admin_ids = config.admin_id_list
    user_count = await count_audience(db, filters, admin_ids)
    text = render_audience_builder(filters, user_count)
    await safe_edit(cb, text, reply_markup=broadcast_target_kb(filters))


# ---------------------------------------------------------------- draft resume


@router.callback_query(F.data.startswith(C.BCAST_DRAFT_RESUME))
async def cb_bcast_draft_resume(
    cb: CallbackQuery, state: FSMContext, db: Database, config: Config
) -> None:
    await cb.answer()
    campaign_id = _int_after(cb.data, C.BCAST_DRAFT_RESUME)
    if campaign_id is None:
        await safe_edit(
            cb, "❌ المسودة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return
    campaign = await repo.get_broadcast(db, campaign_id)
    if campaign is None or campaign["status"] != "draft":
        await safe_edit(
            cb, "❌ المسودة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return

    filter_dict = {}
    if campaign.get("filter_json"):
        filter_dict = json.loads(campaign["filter_json"])
    filters = AudienceFilter.from_dict(filter_dict)
    await state.update_data(campaign_id=campaign_id, filter=filter_dict)
    await state.set_state(BcastFSM.target)

    admin_ids = config.admin_id_list
    user_count = await count_audience(db, filters, admin_ids)
    text = render_audience_builder(filters, user_count)
    await safe_edit(cb, text, reply_markup=broadcast_target_kb(filters))


# ---------------------------------------------------------------- dry run / test


@router.callback_query(F.data == C.BCAST_DRY_RUN)
async def cb_bcast_dry_run(
    cb: CallbackQuery, state: FSMContext, db: Database, config: Config
) -> None:
    await cb.answer()
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if campaign_id is None:
        await safe_edit(
            cb, "❌ انتهت الجلسة، أنشئ مسودة جديدة.", reply_markup=broadcast_center_kb()
        )
        return

    filter_dict = data.get("filter") or {}
    filters = AudienceFilter.from_dict(filter_dict)
    admin_ids = config.admin_id_list
    recipient_count = await count_audience(db, filters, admin_ids)

    campaign = await repo.get_broadcast(db, campaign_id)
    if campaign is None:
        await safe_edit(
            cb, "❌ الحملة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return

    est_duration = recipient_count / max(1, config.bcast_max_rate_per_second)
    text = render_bcast_preview(campaign, recipient_count, est_duration)
    await safe_edit(cb, text, reply_markup=broadcast_confirm_kb(campaign_id))


@router.callback_query(F.data == C.BCAST_TEST_SEND)
async def cb_bcast_test_send(
    cb: CallbackQuery, state: FSMContext, db: Database, config: Config
) -> None:
    await cb.answer()
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if campaign_id is None:
        await safe_edit(
            cb, "❌ انتهت الجلسة، أنشئ مسودة جديدة.", reply_markup=broadcast_center_kb()
        )
        return

    campaign = await repo.get_broadcast(db, campaign_id)
    if campaign is None:
        await safe_edit(
            cb, "❌ الحملة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return

    for admin_id in config.admin_id_list:
        try:
            if campaign["mode"] == "copy":
                await cb.bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=campaign["source_chat_id"],
                    message_id=campaign["source_message_id"],
                )
            else:
                await cb.bot.send_message(
                    chat_id=admin_id,
                    text=campaign.get("content_html") or "",
                )
        except Exception:
            pass

    await safe_edit(
        cb,
        "✅ تم الإرسال التجريبي إلى المدراء.",
        reply_markup=broadcast_center_kb(),
    )


# ---------------------------------------------------------------- send / schedule


@router.callback_query(F.data == C.BCAST_SEND_NOW)
async def cb_bcast_send_now(
    cb: CallbackQuery,
    state: FSMContext,
    db: Database,
    broadcaster: Broadcaster | None,
) -> None:
    await cb.answer()
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if campaign_id is None:
        await safe_edit(
            cb, "❌ انتهت الجلسة، أنشئ مسودة جديدة.", reply_markup=broadcast_center_kb()
        )
        return

    if broadcaster is not None:
        await broadcaster.start(campaign_id, cb.bot)

    await state.clear()
    campaign = await repo.get_broadcast(db, campaign_id)
    if campaign:
        text = render_bcast_summary(campaign)
        await safe_edit(
            cb,
            text,
            reply_markup=broadcast_live_kb(campaign_id, BroadcastStatus.RUNNING.value),
        )


@router.callback_query(F.data == C.BCAST_SCHEDULE)
async def cb_bcast_schedule(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(BcastFSM.scheduled_for)
    await safe_edit(cb, "⏰ أرسل موعد الجدولة (مثال: <<غداً 20:00>> أو <+2h> أو ISO):")


@router.message(StateFilter(BcastFSM.scheduled_for))
async def bcast_scheduled_for_entered(
    message: Message, state: FSMContext, db: Database
) -> None:
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if campaign_id is None:
        await message.answer(
            "❌ انتهت الجلسة، أنشئ مسودة جديدة.", parse_mode=PARSE_MODE
        )
        await state.clear()
        return

    scheduled_for = _parse_schedule_time(message.text or "")
    if scheduled_for is None:
        await message.answer(
            "❌ صيغة الوقت غير صالحة. جرب <<غداً 20:00>> أو <+2h> أو ISO.",
            parse_mode=PARSE_MODE,
        )
        return

    await repo.set_broadcast_status(
        db, campaign_id, "scheduled", scheduled_for=scheduled_for
    )
    await state.clear()
    await message.answer(
        f"✅ تم جدولة البث للحملة #{campaign_id} في <code>{esc(scheduled_for)}</code>.",
        parse_mode=PARSE_MODE,
    )


# ---------------------------------------------------------------- history / view


@router.callback_query(F.data == C.BCAST_HISTORY)
async def cb_bcast_history(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    await _render_history_page(cb, db, 0)


@router.callback_query(F.data.startswith(C.BCAST_PAGE))
async def cb_bcast_history_page(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    page = _int_after(cb.data, C.BCAST_PAGE)
    if page is None:
        page = 0
    await _render_history_page(cb, db, page)


async def _render_history_page(cb: CallbackQuery, db: Database, page: int) -> None:
    per_page = HISTORY_PER_PAGE
    offset = page * per_page
    rows = await db.fetch_all(
        "SELECT * FROM broadcasts "
        "WHERE status IN ('completed','cancelled','failed') "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (per_page, offset),
    )
    total_rows = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM broadcasts "
        "WHERE status IN ('completed','cancelled','failed')"
    )
    total = total_rows["c"] if total_rows else 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page < 0:
        page = 0
    if page >= total_pages:
        page = max(0, total_pages - 1)

    campaigns = [dict(r) for r in rows]
    lines = ["📜 التاريخ البثوي:"]
    if not campaigns:
        lines.append("› لا توجد حملات سابقة.")
    else:
        for c in campaigns:
            lines.append(
                f"› <code>#{c['id']}</code> {esc(c.get('label') or '')} "
                f"{bcast_status_label(c['status'])}"
            )
    lines.append("")
    lines.append("استخدم الأزرار للتنقل ↓")

    kb = _build_history_kb(campaigns, page, total_pages)
    await safe_edit(cb, "\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith(C.BCAST_VIEW))
async def cb_bcast_view(
    cb: CallbackQuery, db: Database, broadcaster: Broadcaster | None
) -> None:
    await cb.answer()
    campaign_id = _int_after(cb.data, C.BCAST_VIEW)
    if campaign_id is None:
        await safe_edit(
            cb, "❌ الحملة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return
    campaign = await repo.get_broadcast(db, campaign_id)
    if campaign is None:
        await safe_edit(
            cb, "❌ الحملة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return

    text = render_bcast_summary(campaign)
    status = campaign["status"]
    if status in ("running", "scheduled"):
        kb = broadcast_live_kb(campaign_id, status)
    else:
        kb = broadcast_center_kb()
    await safe_edit(cb, text, reply_markup=kb)


# ---------------------------------------------------------------- live control


@router.callback_query(F.data.startswith(C.BCAST_CANCEL_LIVE))
async def cb_bcast_cancel_live(
    cb: CallbackQuery, db: Database, broadcaster: Broadcaster | None
) -> None:
    await cb.answer()
    campaign_id = _int_after(cb.data, C.BCAST_CANCEL_LIVE)
    if campaign_id is None:
        await safe_edit(
            cb, "❌ الحملة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return
    if broadcaster is not None:
        await broadcaster.cancel(campaign_id, cb.bot)
    campaign = await repo.get_broadcast(db, campaign_id)
    if campaign:
        await safe_edit(
            cb,
            render_bcast_summary(campaign),
            reply_markup=broadcast_center_kb(),
        )


@router.callback_query(F.data.startswith(C.BCAST_PAUSE_LIVE))
async def cb_bcast_pause_live(
    cb: CallbackQuery, db: Database, broadcaster: Broadcaster | None
) -> None:
    await cb.answer()
    campaign_id = _int_after(cb.data, C.BCAST_PAUSE_LIVE)
    if campaign_id is None:
        await safe_edit(
            cb, "❌ الحملة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return
    if broadcaster is not None:
        await broadcaster.pause(campaign_id)
    campaign = await repo.get_broadcast(db, campaign_id)
    if campaign:
        await safe_edit(
            cb,
            render_bcast_summary(campaign),
            reply_markup=broadcast_center_kb(),
        )


@router.callback_query(F.data.startswith(C.BCAST_RESUME_LIVE))
async def cb_bcast_resume_live(
    cb: CallbackQuery, db: Database, broadcaster: Broadcaster | None
) -> None:
    await cb.answer()
    campaign_id = _int_after(cb.data, C.BCAST_RESUME_LIVE)
    if campaign_id is None:
        await safe_edit(
            cb, "❌ الحملة غير موجودة.", reply_markup=broadcast_center_kb()
        )
        return
    if broadcaster is not None:
        await broadcaster.resume(campaign_id, cb.bot)
    campaign = await repo.get_broadcast(db, campaign_id)
    if campaign:
        await safe_edit(
            cb,
            render_bcast_summary(campaign),
            reply_markup=broadcast_center_kb(),
        )
