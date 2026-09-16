"""
bot/handlers/payment.py — обработка оплаты подписки.

Способы оплаты:
  1. Telegram Stars — через отдельного платёжного бота (deep-link).
  2. TON (Tonkeeper) — генерируется счёт с уникальным комментарием,
     бот ожидает поступления на кошелёк через TonCenter API.
  3. Написать администратору — для ручной оплаты / проблем.
"""
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy.ext.asyncio import AsyncSession

from config import SUBSCRIPTION_PRICES, ADMIN_USERNAME
from models import User
from services import payment_bot_service
from services.payment_service import create_ton_invoice
from bot.keyboards import kb_subscription_plans

log = logging.getLogger(__name__)
router = Router()

IS_MIRROR = False

PLAN_LABELS = {"1month": "1 месяц", "1week": "1 неделя", "3month": "3 месяца", "6month": "6 месяцев"}


# ── Меню подписки ─────────────────────────────────────────────────────────────

@router.message(Command("pay"))
@router.callback_query(F.data == "pay:menu")
async def show_pay_menu(event, user: User):
    text = (
        f"💳 *Подписка*\n\n"
        f"{user.subscription_status}\n\n"
        "Выберите тариф:"
    )
    kb = kb_subscription_plans(is_mirror=IS_MIRROR)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("pay:select:"))
async def select_plan(query: CallbackQuery, db: AsyncSession):
    """Выбрали тариф → показываем способы оплаты."""
    plan = query.data.split(":")[2]
    info = SUBSCRIPTION_PRICES.get(plan)
    if not info:
        await query.answer("Неизвестный тариф.", show_alert=True)
        return

    plan_label = PLAN_LABELS.get(plan, plan)
    active_bot = await payment_bot_service.get_active_bot(db)

    buttons = []

    # Кнопка Stars
    if active_bot and active_bot.bot_username:
        buttons.append([InlineKeyboardButton(
            text=f"⭐ {info['stars']} Stars",
            url=f"https://t.me/{active_bot.bot_username}?start=pay_{plan}",
        )])
        stars_note = ""
    else:
        stars_note = "⚠️ Оплата Stars временно недоступна.\n"

    # Кнопка TON
    buttons.append([InlineKeyboardButton(
        text=f"💎 Оплатить TON (~${info['usdt']})",
        callback_data=f"pay:ton:{plan}",
    )])

    # Кнопка администратора
    buttons.append([InlineKeyboardButton(
        text="✉️ Написать администратору",
        callback_data=f"pay:admin:{plan}",
    )])

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="pay:menu")])

    text = (
        f"🛒 *{plan_label}*\n\n"
        f"⭐ Telegram Stars: {info['stars']}\n"
        f"💎 TON: ~${info['usdt']}\n\n"
        f"{stars_note}"
        "Выберите способ оплаты:"
    )

    await query.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )


# ── Оплата TON ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:ton:"))
async def pay_ton(query: CallbackQuery, user: User, db: AsyncSession):
    """Создаём TON-инвойс: генерируем комментарий, показываем реквизиты."""
    plan = query.data.split(":")[2]
    info = SUBSCRIPTION_PRICES.get(plan)
    if not info:
        await query.answer("Неизвестный тариф.", show_alert=True)
        return

    await query.answer()
    await query.message.answer("⏳ Получаю актуальный курс TON...")

    invoice = await create_ton_invoice(db, user.id, plan)

    if invoice is None:
        await query.message.answer(
            "⚠️ Не удалось создать счёт. Возможно, кошелёк не настроен "
            "или API курса временно недоступен.\n\n"
            f"Обратитесь к администратору: {ADMIN_USERNAME}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✉️ Написать администратору",
                                      url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"pay:select:{plan}")],
            ]),
        )
        return

    plan_label = PLAN_LABELS.get(plan, plan)
    text = (
        f"💎 *Оплата TON — {plan_label}*\n\n"
        f"Сумма: `{invoice['ton_amount']} TON` (~${invoice['usd_amount']})\n"
        f"Курс: 1 TON ≈ ${invoice['rate']:.4f}\n\n"
        f"Кошелёк:\n`{invoice['wallet']}`\n\n"
        f"⚠️ *Обязательно укажите комментарий:*\n"
        f"`{invoice['comment']}`\n\n"
        f"Счёт действителен *1 час*. Как только платёж поступит — "
        f"подписка активируется автоматически."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💎 Открыть в Tonkeeper",
            url=(
                f"https://app.tonkeeper.com/transfer/{invoice['wallet']}"
                f"?amount={int(invoice['ton_amount'] * 1_000_000_000)}"
                f"&text={invoice['comment']}"
            ),
        )],
        [InlineKeyboardButton(
            text="✉️ Написать администратору",
            url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"pay:select:{plan}")],
    ])

    await query.message.answer(text, reply_markup=kb, parse_mode="Markdown")


# ── Написать администратору ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:admin:"))
async def pay_admin(query: CallbackQuery, user: User):
    """Контакт администратора для ручной оплаты или решения проблем."""
    plan = query.data.split(":")[2]
    plan_label = PLAN_LABELS.get(plan, plan)
    info = SUBSCRIPTION_PRICES.get(plan, {})

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✉️ Написать администратору",
            url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"pay:select:{plan}")],
    ])

    await query.message.answer(
        f"🛒 *Покупка у администратора*\n\n"
        f"Тариф: *{plan_label}*\n"
        f"Стоимость: *{info.get('stars', '?')}⭐* / ${info.get('usdt', '?')}\n\n"
        f"Напишите администратору {ADMIN_USERNAME} и укажите:\n"
        f"• Ваш Telegram ID: `{query.from_user.id}`\n"
        f"• Тариф: {plan_label}\n\n"
        f"Администратор активирует подписку вручную.",
        reply_markup=kb,
        parse_mode="Markdown",
    )
