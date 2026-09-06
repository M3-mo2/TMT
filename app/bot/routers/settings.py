"""Settings router: per-user transfer parameter overrides.

Lets users adjust four knobs (max members, invite delay, flood-wait cap,
job timeout) that feed into :class:`~app.core.job_manager.JobManager` via
:class:`~app.core.settings.UserSettings`.  Values default to the global
Config; pressing a button drops into a numeric-input FSM that validates
and persists the override.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import SettingsCB
from app.bot.keyboards import settings_back, settings_keyboard
from app.bot.routers.common import edit_or_answer
from app.bot.states import SettingsFSM
from app.bot.texts import (
    M_SETTING_INVALID,
    M_SETTING_PROMPT,
    M_SETTING_SAVED,
    PARSE_MODE,
    _SETTING_SPECS,
    esc,
    render_settings,
)
from app.core.settings import UserSettings

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = Router(name="settings")


@router.callback_query(SettingsCB.filter(F.action == "main"))
async def show_settings(
    query: CallbackQuery, settings: UserSettings
) -> None:
    await query.answer()
    owner_id = query.from_user.id  # type: ignore[union-attr,assignment]
    current = settings.all_values(owner_id)
    text = render_settings(values=current)
    await edit_or_answer(query, text, settings_keyboard())


@router.callback_query(SettingsCB.filter(F.action == "change"))
async def change_setting(
    query: CallbackQuery, callback_data: SettingsCB, state: FSMContext
) -> None:
    await query.answer()
    key = callback_data.key
    if key not in _SETTING_SPECS:
        await query.answer("× إعداد غير معروف", show_alert=True)
        return
    label, _unit = _SETTING_SPECS[key]
    await state.set_state(SettingsFSM.value)
    await state.update_data(setting_key=key, label=label, current_value=0)
    await edit_or_answer(
        query,
        M_SETTING_PROMPT.format(label=esc(label), current="—"),
        settings_back(),
    )


@router.message(SettingsFSM.value)
async def setting_value_entered(
    message: Message, state: FSMContext, settings: UserSettings
) -> None:
    data = await state.get_data()
    key: str = data.get("setting_key", "")
    label: str = data.get("label", "")
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer(
            f"{M_SETTING_INVALID}\n↢ {esc(label)}",
            parse_mode=PARSE_MODE,
        )
        return
    if key in _SETTING_SPECS:
        settings.set(message.from_user.id, key, value)  # type: ignore[union-attr,arg-type]
        await message.answer(
            M_SETTING_SAVED,
            parse_mode=PARSE_MODE,
            reply_markup=settings_keyboard(),
        )
    else:
        await message.answer(
            f"× {esc(label)} — {esc(key)}",
            parse_mode=PARSE_MODE,
        )
    await state.clear()
