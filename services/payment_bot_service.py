"""
services/payment_bot_service.py — управление пулом «расходных» Stars-ботов.
"""
import logging

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import PaymentBot

log = logging.getLogger(__name__)


class InvalidTokenError(Exception):
    """Токен не прошёл проверку через Telegram Bot API."""


async def _check_token(token: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()
    except Exception as e:
        raise InvalidTokenError(str(e))
    if not data.get("ok"):
        raise InvalidTokenError(data.get("description", "ok=false"))
    return data["result"].get("username", "unknown")


async def get_active_bot(db: AsyncSession) -> PaymentBot | None:
    result = await db.execute(select(PaymentBot).where(PaymentBot.is_active == True))
    return result.scalar_one_or_none()


async def get_all_bots(db: AsyncSession) -> list[PaymentBot]:
    result = await db.execute(select(PaymentBot).order_by(PaymentBot.created_at.desc()))
    return list(result.scalars().all())


async def get_bot_by_id(db: AsyncSession, bot_id: int) -> PaymentBot | None:
    result = await db.execute(select(PaymentBot).where(PaymentBot.id == bot_id))
    return result.scalar_one_or_none()


async def get_bot_by_token(db: AsyncSession, token: str) -> PaymentBot | None:
    result = await db.execute(select(PaymentBot).where(PaymentBot.token == token))
    return result.scalar_one_or_none()


async def set_active_bot(db: AsyncSession, token: str) -> PaymentBot:
    token = token.strip()
    username = await _check_token(token)
    existing = await get_bot_by_token(db, token)
    result = await db.execute(select(PaymentBot).where(PaymentBot.is_active == True))
    for old in result.scalars().all():
        old.is_active = False
    if existing:
        existing.is_active = True
        existing.bot_username = username
        await db.commit()
        await db.refresh(existing)
        return existing
    new_bot = PaymentBot(token=token, bot_username=username, is_active=True)
    db.add(new_bot)
    await db.commit()
    await db.refresh(new_bot)
    return new_bot


async def activate_existing(db: AsyncSession, bot_id: int) -> bool:
    bot_row = await get_bot_by_id(db, bot_id)
    if not bot_row:
        return False
    result = await db.execute(select(PaymentBot).where(PaymentBot.is_active == True))
    for old in result.scalars().all():
        old.is_active = False
    bot_row.is_active = True
    await db.commit()
    return True


async def deactivate_all(db: AsyncSession):
    result = await db.execute(select(PaymentBot).where(PaymentBot.is_active == True))
    for old in result.scalars().all():
        old.is_active = False
    await db.commit()


async def delete_bot(db: AsyncSession, bot_id: int) -> bool:
    bot_row = await get_bot_by_id(db, bot_id)
    if not bot_row:
        return False
    await db.delete(bot_row)
    await db.commit()
    return True


async def increment_payments(db: AsyncSession, bot_token: str):
    bot_row = await get_bot_by_token(db, bot_token)
    if bot_row:
        bot_row.payments_count += 1
        await db.commit()
