"""
bot/handlers/paybot.py — роутер для «расходных» Stars-ботов.

ВАЖНО: у этого роутера нет ни меню, ни FSM, ни доступа к задачам/аккаунтам.
Единственная задача — по deep-link'у /start pay_<plan> сразу выставить
счёт Telegram Stars и принять оплату. Пользователь видит только
стандартный платёжный интерфейс Telegram — ничего больше в этом боте
делать не нужно (и не получится).

Подключается ТОЛЬКО в bot/payment_bot_runner.py, никогда в main_bot.py
или mirror_runner.py — у обычных ботов своя полноценная логика.
"""
import html
import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select

from config import SUBSCRIPTION_PRICES, OWNER_ID
from database import SessionLocal
from models import Payment
from services import payment_service, payment_bot_service
from services.user_service import get_or_create_user, get_user

log = logging.getLogger(__name__)
router = Router()

PLAN_LABELS = {"1week": "1 неделя", "1month": "1 месяц", "3month": "3 месяца", "6month": "6 месяцев"}


# ── /start pay_<plan> → сразу инвойс ───────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    args = (command.args or "").strip()

    if not args.startswith("pay_"):
        await message.answer(
            "🤖 Этот бот используется только для оплаты подписки.\n\n"
            "Вернитесь в бота, где вы настраиваете рассылки, и нажмите "
            "кнопку «⭐ Оплатить» ещё раз."
        )
        return

    plan = args[len("pay_"):]
    if plan not in SUBSCRIPTION_PRICES:
        await message.answer("❌ Неизвестный тариф. Вернитесь в бота и попробуйте снова.")
        return

    async with SessionLocal() as db:
        user, _ = await get_or_create_user(
            db,
            tg_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        if user.is_blocked:
            await message.answer("🚫 Ваш аккаунт заблокирован.")
            return

        payment = await payment_service.create_payment(
            db, user_id=user.id, method="stars", plan=plan,
        )
        payment_id = payment.id

    info  = SUBSCRIPTION_PRICES[plan]
    label = PLAN_LABELS.get(plan, plan)

    await message.bot.send_invoice(
        chat_id=message.chat.id,
        title=f"Подписка — {label}",
        description="Доступ к сервису автоматических рассылок в Telegram.",
        payload=f"starpay:{payment_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=label, amount=int(info["stars"]))],
    )


# ── Подтверждение перед оплатой ────────────────────────────────────────────────

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    payload = query.invoice_payload
    try:
        payment_id = int(payload.split(":")[1])
    except Exception:
        await query.answer(ok=False, error_message="Платёж устарел. Вернитесь в бота и попробуйте снова.")
        return

    async with SessionLocal() as db:
        result = await db.execute(select(Payment).where(Payment.id == payment_id))
        payment = result.scalar_one_or_none()

    if not payment or payment.status != "pending":
        await query.answer(ok=False, error_message="Платёж не найден или уже обработан.")
        return

    await query.answer(ok=True)


# ── Успешная оплата ────────────────────────────────────────────────────────────

@router.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    try:
        payment_id = int(payload.split(":")[1])
    except Exception:
        await message.answer("✅ Оплата получена, но не удалось определить тариф. Напишите администратору.")
        return

    async with SessionLocal() as db:
        result = await db.execute(select(Payment).where(Payment.id == payment_id))
        payment = result.scalar_one_or_none()

        if not payment or payment.status != "pending":
            await message.answer("⚠️ Платёж не найден, обратитесь к администратору.")
            return

        user = await get_user(db, message.from_user.id)
        if not user:
            return

        await payment_service.confirm_payment(db, payment, user)
        await payment_bot_service.increment_payments(db, bot_token=message.bot.token)

        plan_label = PLAN_LABELS.get(payment.plan, payment.plan)
        sub_status = user.subscription_status
        stars_amount = int(payment.amount)
        owner_user_id = user.id
        owner_full_name = user.full_name
        owner_username = user.username

    await message.answer(
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"Тариф: {html.escape(plan_label)}\n"
        f"{html.escape(sub_status)}\n\n"
        f"Можете вернуться в бота, где настраивали рассылки — доступ уже открыт.",
        parse_mode="HTML",
    )

    try:
        me = await message.bot.get_me()
        await message.bot.send_message(
            OWNER_ID,
            f"💰 *Новая оплата через Stars* (бот @{me.username})\n\n"
            f"👤 {owner_full_name}\n"
            f"🆔 `{owner_user_id}`\n"
            f"👤 @{owner_username or '—'}\n\n"
            f"📦 Тариф: *{plan_label}*\n"
            f"⭐ Сумма: *{stars_amount} Stars*",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Не удалось уведомить владельца о платеже: %s", e)