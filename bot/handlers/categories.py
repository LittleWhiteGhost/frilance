from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.database import (
    get_user_categories,
    get_user_platforms,
    toggle_user_category,
    toggle_user_platform,
)
from bot.keyboards import (
    PLATFORM_NAME_BY_CODE,
    categories_kb,
    main_menu_kb,
    platforms_kb,
)
from bot.utils.html import html_escape
from bot.utils import safe_edit

router = Router()

CATEGORIES_PROMPT = (
    "\U0001f4cb <b>Выбери интересующие категории:</b>\n\n"
    "Нажми на категорию, чтобы включить/выключить.\n"
    "Когда закончишь — нажми <b>Готово</b>."
)

PLATFORMS_PROMPT = (
    "\U0001f310 <b>Выбери площадки для мониторинга:</b>\n\n"
    "Нажми на площадку, чтобы включить/выключить.\n"
    "Когда закончишь — нажми <b>Готово</b>."
)


# ── Categories ──

@router.message(Command("categories"))
async def cmd_categories(message: Message):
    selected = await get_user_categories(message.from_user.id)
    await message.answer(CATEGORIES_PROMPT, reply_markup=categories_kb(selected))


@router.callback_query(F.data == "my_categories")
async def cb_my_categories(callback: CallbackQuery):
    selected = await get_user_categories(callback.from_user.id)
    await safe_edit(callback.message, CATEGORIES_PROMPT, reply_markup=categories_kb(selected))
    await callback.answer()


@router.callback_query(F.data.startswith("cat_toggle:"))
async def cb_toggle_category(callback: CallbackQuery):
    cat = callback.data.split(":", 1)[1]
    await toggle_user_category(callback.from_user.id, cat)
    selected = await get_user_categories(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=categories_kb(selected))
    await callback.answer()


@router.callback_query(F.data == "cat_done")
async def cb_categories_done(callback: CallbackQuery):
    selected = await get_user_categories(callback.from_user.id)
    if selected:
        cats_text = html_escape(", ".join(selected))
        text = f"\u2705 <b>Категории сохранены!</b>\n\n{cats_text}"
    else:
        text = "\u26a0\ufe0f Ты не выбрал ни одной категории. Заказы не будут приходить."
    await safe_edit(callback.message, text, reply_markup=main_menu_kb())
    await callback.answer()


# ── Platforms ──

@router.message(Command("platforms"))
async def cmd_platforms(message: Message):
    selected = await get_user_platforms(message.from_user.id)
    await message.answer(PLATFORMS_PROMPT, reply_markup=platforms_kb(selected))


@router.callback_query(F.data == "my_platforms")
async def cb_my_platforms(callback: CallbackQuery):
    selected = await get_user_platforms(callback.from_user.id)
    await safe_edit(callback.message, PLATFORMS_PROMPT, reply_markup=platforms_kb(selected))
    await callback.answer()


@router.callback_query(F.data.startswith("plat_toggle:"))
async def cb_toggle_platform(callback: CallbackQuery):
    plat = callback.data.split(":", 1)[1]
    await toggle_user_platform(callback.from_user.id, plat)
    selected = await get_user_platforms(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=platforms_kb(selected))
    await callback.answer()


@router.callback_query(F.data == "plat_done")
async def cb_platforms_done(callback: CallbackQuery):
    selected = await get_user_platforms(callback.from_user.id)
    if selected:
        names = html_escape(", ".join(PLATFORM_NAME_BY_CODE.get(p, p) for p in selected))
        text = f"\u2705 <b>Площадки сохранены!</b>\n\n{names}"
    else:
        text = "\u26a0\ufe0f Ты не выбрал ни одной площадки. Заказы не будут приходить."
    await safe_edit(callback.message, text, reply_markup=main_menu_kb())
    await callback.answer()
