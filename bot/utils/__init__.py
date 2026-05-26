from aiogram.types import InlineKeyboardMarkup, Message


async def safe_edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit message text, falling back to sending a new message if edit fails (e.g. DOCUMENT_INVALID)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await message.answer(text, reply_markup=reply_markup)
