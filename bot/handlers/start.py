import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import config
from bot.constants import (
    REFERRAL_CODE_LENGTH,
    REFERRAL_INVITED_TRIAL_BONUS_DAYS,
    REFERRAL_REFERRER_BONUS_DAYS,
    TIER_BADGE_CUSTOM_EMOJI_ID,
    TIER_BADGE_EMOJI,
    TIER_BADGE_LABEL,
    TIER_CUSTOM_EMOJI_ID,
    TIER_EMOJI,
    TIER_LABEL,
    TIERS,
)
from bot.database import (
    add_user,
    count_referrals,
    create_trial,
    find_user_by_referral_code,
    get_or_create_referral_code,
    log_event,
)
from bot.keyboards import back_kb, main_menu_kb
from bot.utils.html import tg_emoji
from bot.utils import safe_edit

logger = logging.getLogger(__name__)

router = Router()

WELCOME_TEXT = """
\U0001f44b <b>Добро пожаловать в FreelanceParser Bot!</b>

Я нахожу свежие заказы с фриланс-бирж и отправляю их тебе в Telegram.

\U0001f310 <b>Площадки:</b> Kwork, FL.ru, Freelance.ru, Weblancer, YouDo

\U0001f4a1 <b>Как это работает:</b>
1. Выбери интересующие категории
2. Выбери площадки для мониторинга
3. Получай новые заказы прямо в бот!

\U0001f381 <b>Тебе доступен бесплатный пробный период!</b>
"""


def _tariffs_help_lines() -> list[str]:
    """Render a one-liner per tier for the /help message, including the
    yearly-savings nudge and the marketing badge for Pro/Max. All emoji are
    wrapped as Telegram Premium custom emoji with Unicode fallback."""
    out: list[str] = []
    for tier in TIERS:
        mo = config.price_for(tier, "monthly")
        yr = config.price_for(tier, "yearly")
        savings = config.yearly_savings_pct(tier)
        savings_suffix = f" (\u2212{savings}%)" if savings > 0 else ""

        tier_icon = tg_emoji(TIER_CUSTOM_EMOJI_ID.get(tier), TIER_EMOJI[tier])
        b_label = TIER_BADGE_LABEL.get(tier, "")
        if b_label:
            b_icon = tg_emoji(
                TIER_BADGE_CUSTOM_EMOJI_ID.get(tier),
                TIER_BADGE_EMOJI.get(tier, ""),
            )
            badge_suffix = f"  {b_icon} <b>{b_label}</b>"
        else:
            badge_suffix = ""

        out.append(
            f"— {tier_icon} <b>{TIER_LABEL[tier]}</b>{badge_suffix}: "
            f"{mo}\u20bd/мес или {yr}\u20bd/год{savings_suffix}"
        )
    return out


def _help_text() -> str:
    support = config.support_username or "@support"
    if not support.startswith("@"):
        support = "@" + support
    trial_label = TIER_LABEL.get(config.trial_tier, config.trial_tier)
    return (
        "\u2753 <b>Помощь</b>\n\n"
        "<b>Команды:</b>\n"
        "/start — Запустить бота\n"
        "/menu — Главное меню\n"
        "/categories — Настроить категории\n"
        "/platforms — Настроить площадки\n"
        "/subscription — Информация о подписке\n"
        "/referral — Твоя реферальная ссылка\n"
        "/promo CODE — Активировать промокод\n"
        "/cancel — Отменить текущее действие\n"
        "/help — Показать это сообщение\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Настрой категории — выбери какие заказы тебе интересны\n"
        "2. Настрой площадки — откуда парсить заказы\n"
        f"3. Бот парсит свежие заказы каждые {config.parse_interval} мин\n\n"
        "<b>Подписка:</b>\n"
        f"— {config.trial_days} дня бесплатно при регистрации (фичи {trial_label})\n"
        + "\n".join(_tariffs_help_lines())
        + f"\n\nВопросы? Напиши {support}"
    )


def _parse_start_payload(text: str | None) -> tuple[str, str | None]:
    """Split `/start <payload>` into (command, payload). Returns
    (command, None) when there's no payload.

    Telegram delivers a deep link like `t.me/<bot>?start=ref_ABC123` to the
    bot as the message text `/start ref_ABC123`. We don't try to parse the
    payload semantics here — caller decides what `ref_*`, `promo_*`, etc.
    means."""
    if not text:
        return "/start", None
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1]:
        return parts[0], None
    return parts[0], parts[1].strip()


async def _resolve_referrer(payload: str | None) -> int | None:
    """If the start payload is a referral link (`ref_<CODE>`), resolve it to
    the referrer's user_id. Returns None for invalid / unknown codes."""
    if not payload or not payload.startswith("ref_"):
        return None
    code = payload[len("ref_"):]
    if not code:
        return None
    referrer = await find_user_by_referral_code(code)
    return int(referrer["user_id"]) if referrer else None


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    if not user:
        return

    _, payload = _parse_start_payload(message.text)
    referred_by = await _resolve_referrer(payload)

    is_new = await add_user(
        user.id,
        user.username,
        user.full_name or "",
        referred_by=referred_by,
    )
    if is_new:
        # New user: trial gets a small bonus when they came in via a ref link.
        bonus = REFERRAL_INVITED_TRIAL_BONUS_DAYS if referred_by else 0
        await create_trial(user.id, bonus_days=bonus)

        await log_event(
            "user_registered",
            user_id=user.id,
            properties={
                "referred_by": referred_by,
                "trial_bonus_days": bonus,
            },
        )

        if referred_by:
            # Credit the referrer once the *invited* user pays for the first
            # time. The credit lives in the bonus_days_ledger and is spent on
            # the referrer's next paid activation.
            try:
                from bot.database import add_bonus_days
                await add_bonus_days(
                    referred_by,
                    REFERRAL_REFERRER_BONUS_DAYS,
                    source="referral",
                    source_ref=str(user.id),
                )
            except Exception:
                logger.exception("Failed to credit referrer %s", referred_by)
            await log_event(
                "referral_invited",
                user_id=referred_by,
                properties={"invited_user_id": user.id},
            )

    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("\U0001f3e0 <b>Главное меню</b>", reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(_help_text(), reply_markup=back_kb())


@router.message(Command("referral"))
async def cmd_referral(message: Message):
    """Show the user their personal referral link + current stats.

    The referral code is generated lazily on the first call; we don't pre-mint
    one for every user during /start because most users never click /referral.
    """
    user = message.from_user
    if not user:
        return

    code = await get_or_create_referral_code(user.id, REFERRAL_CODE_LENGTH)
    invited = await count_referrals(user.id)

    bot_username = config.bot_username or "this_bot"
    if bot_username.startswith("@"):
        bot_username = bot_username[1:]
    link = f"https://t.me/{bot_username}?start=ref_{code}"

    await log_event("referral_opened", user_id=user.id)
    await message.answer(
        "\U0001f465 <b>Реферальная программа</b>\n\n"
        f"<b>Твоя ссылка:</b>\n<code>{link}</code>\n\n"
        f"<b>Приглашено:</b> {invited}\n\n"
        f"\U0001f381 За каждого друга, который оплатит подписку, "
        f"ты получаешь <b>+{REFERRAL_REFERRER_BONUS_DAYS} дней</b> "
        f"к своей подписке.\n"
        f"Друг получает <b>+{REFERRAL_INVITED_TRIAL_BONUS_DAYS} дней</b> "
        f"к пробному периоду.",
        reply_markup=back_kb(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Drop any in-progress FSM state and return to the main menu.

    Currently no handler in the bot puts a user *into* a state, but we set up
    `/cancel` now so future flows (e.g. promo-code entry, ref-code linking)
    can rely on a single, predictable escape hatch.
    """
    current = await state.get_state()
    if current is not None:
        await state.clear()
    await message.answer(
        "\u274c <b>Действие отменено.</b>",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    await safe_edit(callback.message,
        "\U0001f3e0 <b>Главное меню</b>",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    try:
        await safe_edit(callback.message, _help_text(), reply_markup=back_kb())
    except Exception:
        await callback.message.answer(_help_text(), reply_markup=back_kb())
    await callback.answer()
