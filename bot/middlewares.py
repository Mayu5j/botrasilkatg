"""
bot/middlewares.py — middleware для aiogram.

AuthMiddleware:
  - при каждом сообщении/callback регистрирует пользователя (если новый)
  - передаёт объект user и db-сессию в handler через data[]
  - блокирует заблокированных пользователей
  - уведомляет владельца о новых пользователях
"""
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from config import OWNER_ID
from database import SessionLocal
from services.user_service import get_or_create_user

log = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """Регистрация и авторизация пользователей."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Получаем TG-пользователя из события
        tg_user = None
        if isinstance(event, Message):
            tg_user = event.from_user
        elif isinstance(event, CallbackQuery):
            tg_user = event.from_user

        if not tg_user:
            return await handler(event, data)

        # Открываем сессию БД
        async with SessionLocal() as db:
            user, is_new = await get_or_create_user(
                db,
                tg_id=tg_user.id,
                username=tg_user.username,
                full_name=tg_user.full_name,
            )

            # Блокируем заблокированных
            if user.is_blocked:
                if isinstance(event, Message):
                    await event.answer("🚫 Ваш аккаунт заблокирован.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("🚫 Заблокирован.", show_alert=True)
                return

            # Уведомляем владельца о новом пользователе
            if is_new and tg_user.id != OWNER_ID:
                try:
                    bot = data["bot"]
                    await bot.send_message(
                        OWNER_ID,
                        f"👤 *Новый пользователь!*\n"
                        f"ID: `{tg_user.id}`\n"
                        f"Имя: {tg_user.full_name}\n"
                        f"Username: @{tg_user.username or '—'}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass  # не страшно если уведомление не дошло
                log.info("Новый пользователь: %d @%s", tg_user.id, tg_user.username)

            # Передаём user и db в обработчик
            data["user"] = user
            data["db"]   = db

            return await handler(event, data)
