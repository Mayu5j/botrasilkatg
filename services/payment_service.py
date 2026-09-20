"""
services/payment_service.py — обработка платежей.
Поддерживаемые методы: Telegram Stars, CryptoBot, TON (по комментарию).
"""
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
 
from config import SUBSCRIPTION_PRICES, CRYPTOBOT_TOKEN, TON_WALLET, TONCENTER_API_KEY
from models import Payment, User
from services.user_service import add_subscription
 
log = logging.getLogger(__name__)
 
 
async def create_payment(
    db: AsyncSession,
    user_id: int,
    method: str,
    plan: str,
    external_id: str | None = None,
    amount_override: float | None = None,
) -> Payment:
    """Создать запись платежа со статусом 'pending'.
 
    amount_override — для TON передаётся реальная сумма в TON,
    иначе берётся из SUBSCRIPTION_PRICES.
    """
    price_info = SUBSCRIPTION_PRICES[plan]
    if amount_override is not None:
        amount = amount_override
    elif method == "stars":
        amount = price_info["stars"]
    else:
        amount = price_info["usdt"]
    currency = "XTR" if method == "stars" else ("TON" if method == "ton" else "USDT")
 
    payment = Payment(
        user_id=user_id,
        method=method,
        plan=plan,
        amount=amount,
        currency=currency,
        status="pending",
        external_id=external_id,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment
 
 
async def confirm_payment(db: AsyncSession, payment: Payment, user: User):
    """
    Подтвердить платёж — добавить подписку пользователю.
    Вызывается из webhook-обработчиков.
    """
    payment.status = "paid"
    payment.paid_at = datetime.now(timezone.utc)
    await db.commit()
 
    days = SUBSCRIPTION_PRICES[payment.plan]["days"]
    await add_subscription(db, user, days)
    log.info(
        "Платёж подтверждён: user=%d plan=%s days=%d",
        user.id, payment.plan, days
    )
 
 
# ── CryptoBot ─────────────────────────────────────────────────────────────────
 
async def create_cryptobot_invoice(plan: str, user_id: int) -> dict | None:
    """
    Создать инвойс через CryptoBot API.
    Возвращает словарь с полями: pay_url, invoice_id.
    """
    if not CRYPTOBOT_TOKEN:
        return None
    try:
        import aiohttp
        price_info = SUBSCRIPTION_PRICES[plan]
        payload = f"sub_{plan}_{user_id}"
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                "https://pay.crypt.bot/api/createInvoice",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                json={
                    "asset": "USDT",
                    "amount": str(price_info["usdt"]),
                    "description": f"Подписка {plan}",
                    "payload": payload,
                    "expires_in": 3600,  # 1 час
                }
            )
            data = await resp.json()
        if data.get("ok"):
            return {
                "pay_url":    data["result"]["pay_url"],
                "invoice_id": str(data["result"]["invoice_id"]),
            }
    except Exception as e:
        log.error("CryptoBot ошибка: %s", e)
    return None
 
 
# ── Telegram Stars ────────────────────────────────────────────────────────────
 
def get_stars_price(plan: str) -> int:
    """Получить цену в Stars для плана."""
    return SUBSCRIPTION_PRICES[plan]["stars"]
 
 
# ── TON ───────────────────────────────────────────────────────────────────────
 
async def create_ton_invoice(
    db: AsyncSession,
    user_id: int,
    plan: str,
) -> dict | None:
    """
    Создать счёт для оплаты TON.
    Возвращает dict с полями: payment_id, comment, ton_amount, usd_amount, wallet.
    Возвращает None если TON-кошелёк не настроен или API курса недоступен.
    """
    if not TON_WALLET:
        log.error("TON_WALLET не настроен в .env")
        return None
 
    from services.ton_service import usd_to_ton, generate_comment
 
    usd_amount = SUBSCRIPTION_PRICES[plan]["usdt"]
    result = await usd_to_ton(usd_amount)
    if result is None:
        return None
 
    ton_amount, rate = result
    comment = generate_comment(user_id)
 
    payment = await create_payment(
        db,
        user_id=user_id,
        method="ton",
        plan=plan,
        external_id=comment,
        amount_override=ton_amount,
    )
 
    log.info(
        "TON-инвойс создан: user=%d plan=%s %.4f TON (rate=%.4f) comment=%s",
        user_id, plan, ton_amount, rate, comment,
    )
    return {
        "payment_id": payment.id,
        "comment":    comment,
        "ton_amount": ton_amount,
        "usd_amount": usd_amount,
        "wallet":     TON_WALLET,
        "rate":       rate,
    }
 
 
async def get_pending_ton_payments(db: AsyncSession) -> list[Payment]:
    """Все незакрытые TON-платежи."""
    result = await db.execute(
        select(Payment).where(
            Payment.method == "ton",
            Payment.status == "pending",
        )
    )
    return list(result.scalars().all())
 
 
async def expire_old_ton_payments(db: AsyncSession) -> int:
    """Помечает как 'expired' TON-платежи старше 1 часа. Возвращает кол-во."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    result = await db.execute(
        select(Payment).where(
            Payment.method == "ton",
            Payment.status == "pending",
            Payment.created_at < cutoff,
        )
    )
    payments = result.scalars().all()
    for p in payments:
        p.status = "expired"
    if payments:
        await db.commit()
    return len(payments)

