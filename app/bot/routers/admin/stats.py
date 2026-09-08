"""Admin: statistics overview."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import stats_kb
from app.bot.texts import PARSE_MODE
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit

router = Router()


@router.callback_query(F.data == C.STATS)
async def cb_stats(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    user_count = await repo.count_users(db)
    channel_count = await repo.count_channels_by_type(db, "channel")
    group_count = await repo.count_channels_by_type(db, "group")
    jobs_count = await repo.count_jobs(db)
    completed_count = await repo.count_completed_jobs(db)

    lines = [
        "📊|إحصاءات البوت",
        f"👥|المستخدمون: <code>{user_count}</code>",
        f"📢|قنوات إجبارية: <code>{channel_count}</code>",
        f"👥|مجموعات إجبارية: <code>{group_count}</code>",
        f"📦|إجمالي العمليات: <code>{jobs_count}</code>",
        f"✅|مكتملة: <code>{completed_count}</code>",
    ]
    await safe_edit(cb, "\n".join(lines), reply_markup=stats_kb())
