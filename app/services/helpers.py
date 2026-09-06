"""Small helpers for safely editing callback-query messages."""
from __future__ import annotations

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def safe_edit(
    cb: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the callback's message text if it is an accessible Message.

    `cb.message` is typed as MaybeInaccessibleMessage (Message | InaccessibleMessage).
    For inline-button callbacks it is always an accessible Message in practice,
    but we guard the type so callers don't hit AttributeError on old messages.
    """
    msg = cb.message
    if isinstance(msg, Message):
        try:
            await msg.edit_text(text, reply_markup=reply_markup, parse_mode="html")
        except Exception:
            try:
                await cb.bot.send_message(msg.chat.id, text, reply_markup=reply_markup, parse_mode="html")
            except Exception:
                pass


async def safe_delete(cb: CallbackQuery) -> None:
    """Delete the callback's message if it is an accessible Message."""
    msg = cb.message
    if isinstance(msg, Message):
        try:
            await msg.delete()
        except Exception:
            pass
