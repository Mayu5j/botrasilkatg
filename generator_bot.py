"""
bot/generator_bot.py — бот-регистратор зеркал.

Флоу:
  1. Пользователь приходит → видит случайное рабочее зеркало + кнопку создать своё
  2. Если хочет своё — отдаёт токен от BotFather
  3. Мы валидируем токен, сохраняем MirrorBot, mirror_runner подхватывает автоматически
  4. Оплата — кнопка "написать админу"
"""
import asyncio
import logging
import random

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select

from config import GENERATOR_BOT_TOKEN, OWNER_ID
from database import SessionLocal, create_all_tables
from models import MirrorBot, User
from services.user_service import get_or_create_user

log = logging.getLogger(__name__)
router = Router()

ADMIN_USERNAME = "jstaskmebro"  # без @


# ── Утилиты ───────────────────────────────────────────────────────────────────

async def get_random_working_mirror(exclude_user_id: int | None = None) -> MirrorBot | None:
    """
    Возвращает случайное активное зеркало.
    Проверяет доступность через Telegram Bot API getMe.
    Исключает зеркало самого пользователя (если есть).
    """
    async with SessionLocal() as db:
        q = select(MirrorBot).where(MirrorBot.is_active == True)
        if exclude_user_id is not None:
            q = q.where(MirrorBot.user_id != exclude_user_id)
        result = await db.execute(q)
        all_mirrors = list(result.scalars().all())

    if not all_mirrors:
        return None

    # Перемешиваем и ищем первое рабочее
    random.shuffle(all_mirrors)

    async with aiohttp.ClientSession() as session:
        for m in all_mirrors:
            try:
                resp = await session.get(
                    f"https://api.telegram.org/bot{m.token}/getMe",
                    timeout=aiohttp.ClientTimeout(total=5),
                )
                data = await resp.json()
                if data.get("ok"):
                    return m
            except Exception:
                continue

    return None


async def get_user_mirror(user_id: int) -> MirrorBot | None:
    async with SessionLocal() as db:
        result = await db.execute(
            select(MirrorBot).where(MirrorBot.user_id == user_id)
        )
        return result.scalar_one_or_none()


def kb_main(has_mirror: bool, mirror_username: str | None = None) -> InlineKeyboardMarkup:
    buttons = []

    if has_mirror and mirror_username:
        buttons.append([InlineKeyboardButton(
            text=f"🤖 Мой бот (@{mirror_username})",
            url=f"https://t.me/{mirror_username}",
        )])
        buttons.append([InlineKeyboardButton(
            text="📊 Статус моего бота", callback_data="mirror:status"
        )])
        buttons.append([InlineKeyboardButton(
            text="🗑 Удалить и подключить другой", callback_data="mirror:delete_confirm"
        )])
    else:
        buttons.append([InlineKeyboardButton(
            text="➕ Подключить своего бота", callback_data="mirror:register"
        )])

    buttons.append([InlineKeyboardButton(
        text="💳 Оплатить подписку", callback_data="pay:info"
    )])
    buttons.append([InlineKeyboardButton(
        text="❓ Как это работает?", callback_data="howto"
    )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    async with SessionLocal() as db:
        user, _ = await get_or_create_user(
            db,
            tg_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )

    user_mirror = await get_user_mirror(message.from_user.id)

    # Ищем случайное рабочее зеркало для демонстрации
    demo_mirror = await get_random_working_mirror(exclude_user_id=message.from_user.id)

    if user_mirror:
        # У пользователя уже есть свой бот
        text = (
            f"👋 *Добро пожаловать!*\n\n"
            f"У вас подключён бот: @{user_mirror.bot_username}\n\n"
            f"Перейдите в него для управления рассылками."
        )
        if demo_mirror and demo_mirror.bot_username:
            text += (
                f"\n\n💡 *Попробовать сервис* можно в боте "
                f"[@{demo_mirror.bot_username}](https://t.me/{demo_mirror.bot_username})"
            )
        await message.answer(
            text,
            reply_markup=kb_main(True, user_mirror.bot_username),
            parse_mode="Markdown",
        )
    else:
        # Новый пользователь — показываем рабочее зеркало + предлагаем своё
        if demo_mirror and demo_mirror.bot_username:
            demo_text = (
                f"🚀 *Попробуйте прямо сейчас:*\n"
                f"[@{demo_mirror.bot_username}](https://t.me/{demo_mirror.bot_username})\n\n"
                f"Или подключите *своего бота* — он будет работать только для вас."
            )
            extra_btn = [[InlineKeyboardButton(
                text=f"🚀 Попробовать (@{demo_mirror.bot_username})",
                url=f"https://t.me/{demo_mirror.bot_username}",
            )]]
        else:
            demo_text = "Подключите *свое зеркало* — он будет работать только для вас."
            extra_btn = []

        text = (
            f"👋 *Добро пожаловать в сервис рассылок!*\n\n"
            f"Автоматические рассылки в Telegram-чаты через ваши аккаунты.\n\n"
            f"{demo_text}"
        )

        buttons = extra_btn + [
            [InlineKeyboardButton(text="➕ Подключить своего бота", callback_data="mirror:register")],
            [InlineKeyboardButton(text="💳 Оплатить подписку", callback_data="pay:info")],
            [InlineKeyboardButton(text="❓ Как это работает?", callback_data="howto")],
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        await message.answer(text, reply_markup=kb, parse_mode="Markdown")


# ── Инструкция ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "howto")
async def howto(query: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Подключить своего бота", callback_data="mirror:register")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back:start")],
    ])
    await query.message.answer(
        "❓ *Как это работает?*\n\n"
        "1️⃣ Создайте бота у @BotFather → /newbot\n\n"
        "2️⃣ Скопируйте токен бота\n"
        "   Вид: `1234567890:AABBccdd...`\n\n"
        "3️⃣ Вставьте токен здесь\n\n"
        "4️⃣ Через минуту ваш бот готов к работе!\n\n"
        "5️⃣ Перейдите в него и нажмите /start\n\n"
        "⚠️ Токен — это пароль от бота. Никому не передавайте.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── Оплата ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "pay:info")
async def pay_info(query: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✉️ Написать администратору",
            url=f"https://t.me/{ADMIN_USERNAME}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back:start")],
    ])
    await query.message.answer(
        "💳 *Оплата подписки*\n\n"
        "Напишите администратору и укажите:\n"
        f"• Ваш Telegram ID: `{query.from_user.id}`\n"
        "• Желаемый тариф\n\n"
        "💰 *Тарифы:*\n"
        "• 1 неделя — 50 ⭐ Stars / $1\n"
        "• 1 месяц — 150 ⭐ Stars / $3\n"
        "• 6 месяцев — 450 ⭐ Stars / $20\n\n"
        "✅ Принимаем: Telegram Stars, USDT\n\n"
        f"👤 Администратор: @{ADMIN_USERNAME}",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── FSM регистрации ───────────────────────────────────────────────────────────

class RegisterMirror(StatesGroup):
    token = State()


@router.callback_query(F.data == "mirror:register")
async def start_register(query: CallbackQuery, state: FSMContext):
    existing = await get_user_mirror(query.from_user.id)
    if existing:
        await query.answer("У вас уже есть подключённый бот.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back:start")],
    ])
    await query.message.answer(
        "➕ *Подключение своего бота*\n\n"
        "Отправьте токен вашего бота.\n\n"
        "Как получить токен:\n"
        "1. Откройте @BotFather\n"
        "2. Команда /mybots → выберите бота → API Token\n"
        "   Или создайте нового: /newbot\n\n"
        "Формат токена: `1234567890:AABBccdd...`",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(RegisterMirror.token)


@router.message(RegisterMirror.token)
async def got_token(message: Message, state: FSMContext):
    token = message.text.strip() if message.text else ""

    if ":" not in token or len(token) < 30:
        await message.answer(
            "❌ Неверный формат токена.\n"
            "Должно быть: `1234567890:AABBccdd...`\n\n"
            "Попробуйте ещё раз:",
            parse_mode="Markdown",
        )
        return

    await message.answer("🔍 Проверяю токен...")

    # Проверка через Bot API
    bot_username = None
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()

        if not data.get("ok"):
            await message.answer(
                "❌ Токен не прошёл проверку.\n"
                "Убедитесь что токен скопирован полностью и бот существует.\n\n"
                "Попробуйте ещё раз:"
            )
            return

        bot_username = data["result"].get("username", "unknown")

    except Exception as e:
        log.error("Ошибка проверки токена: %s", e)
        await message.answer(
            "❌ Сетевая ошибка при проверке токена. Попробуйте позже."
        )
        await state.clear()
        return

    # Проверяем что токен не занят
    async with SessionLocal() as db:
        result = await db.execute(select(MirrorBot).where(MirrorBot.token == token))
        if result.scalar_one_or_none():
            await message.answer(
                "❌ Этот токен уже зарегистрирован.\n"
                "Создайте нового бота у @BotFather."
            )
            await state.clear()
            return

        result = await db.execute(
            select(MirrorBot).where(MirrorBot.user_id == message.from_user.id)
        )
        if result.scalar_one_or_none():
            await message.answer("❌ У вас уже есть подключённый бот.")
            await state.clear()
            return

        mirror = MirrorBot(
            user_id=message.from_user.id,
            token=token,
            bot_username=bot_username,
            is_active=True,
        )
        db.add(mirror)
        await db.commit()

    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🚀 Перейти к @{bot_username}",
            url=f"https://t.me/{bot_username}",
        )],
        [InlineKeyboardButton(text="📊 Статус", callback_data="mirror:status")],
    ])

    await message.answer(
        f"✅ *Бот @{bot_username} подключён!*\n\n"
        f"⏳ Запуск занимает до 60 секунд.\n"
        f"После этого нажмите /start в своём боте.",
        reply_markup=kb,
        parse_mode="Markdown",
    )

    # Уведомляем владельца сервиса
    try:
        notify_bot = Bot(token=GENERATOR_BOT_TOKEN)
        try:
            await notify_bot.send_message(
                OWNER_ID,
                f"🆕 *Новое зеркало*\n\n"
                f"Пользователь: `{message.from_user.id}` "
                f"@{message.from_user.username or '—'}\n"
                f"Бот: @{bot_username}",
                parse_mode="Markdown",
            )
        finally:
            await notify_bot.session.close()
    except Exception:
        pass

    log.info("Новое зеркало: user=%d @%s", message.from_user.id, bot_username)


# ── Статус зеркала ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "mirror:status")
async def mirror_status(query: CallbackQuery):
    mirror = await get_user_mirror(query.from_user.id)
    if not mirror:
        await query.answer("Зеркало не найдено.", show_alert=True)
        return

    # Проверяем живость бота
    alive = False
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"https://api.telegram.org/bot{mirror.token}/getMe",
                timeout=aiohttp.ClientTimeout(total=5),
            )
            data = await resp.json()
            alive = data.get("ok", False)
    except Exception:
        pass

    status = "✅ Работает" if alive else "⚠️ Недоступен"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🚀 Перейти к @{mirror.bot_username}",
            url=f"https://t.me/{mirror.bot_username}",
        )],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="mirror:delete_confirm")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back:start")],
    ])
    await query.message.answer(
        f"🤖 *Ваш бот*\n\n"
        f"Username: @{mirror.bot_username}\n"
        f"Статус: {status}\n\n"
        f"Перейдите в бота для управления рассылками.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── Удаление ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "mirror:delete_confirm")
async def delete_confirm(query: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="mirror:delete_do")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="mirror:status")],
    ])
    await query.message.answer(
        "⚠️ *Удалить подключённого бота?*\n\n"
        "Задачи и аккаунты сохранятся в базе данных.\n"
        "Вы сможете подключить другого бота.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "mirror:delete_do")
async def delete_mirror(query: CallbackQuery):
    async with SessionLocal() as db:
        result = await db.execute(
            select(MirrorBot).where(MirrorBot.user_id == query.from_user.id)
        )
        mirror = result.scalar_one_or_none()
        if mirror:
            await db.delete(mirror)
            await db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Подключить нового бота", callback_data="mirror:register")],
    ])
    await query.message.answer(
        "✅ *Бот отключён.*\n\n"
        "Можете подключить другого бота в любой момент.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── Возврат на главную ────────────────────────────────────────────────────────

@router.callback_query(F.data == "back:start")
async def back_to_start(query: CallbackQuery, state: FSMContext):
    await state.clear()

    user_mirror = await get_user_mirror(query.from_user.id)
    demo_mirror = await get_random_working_mirror(exclude_user_id=query.from_user.id)

    if user_mirror:
        text = (
            f"👋 *Главное меню*\n\n"
            f"Ваш бот: @{user_mirror.bot_username}"
        )
        kb = kb_main(True, user_mirror.bot_username)
    else:
        demo_line = ""
        extra_btn = []
        if demo_mirror and demo_mirror.bot_username:
            demo_line = (
                f"\n\n🚀 *Попробуйте:* "
                f"[@{demo_mirror.bot_username}](https://t.me/{demo_mirror.bot_username})"
            )
            extra_btn = [[InlineKeyboardButton(
                text=f"🚀 Попробовать (@{demo_mirror.bot_username})",
                url=f"https://t.me/{demo_mirror.bot_username}",
            )]]

        text = f"👋 *Главное меню*{demo_line}"
        buttons = extra_btn + [
            [InlineKeyboardButton(text="➕ Подключить своего бота", callback_data="mirror:register")],
            [InlineKeyboardButton(text="💳 Оплатить подписку", callback_data="pay:info")],
            [InlineKeyboardButton(text="❓ Как это работает?", callback_data="howto")],
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await query.message.answer(text, reply_markup=kb, parse_mode="Markdown")


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    await create_all_tables()

    bot = Bot(token=GENERATOR_BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("Generator bot запущен...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [GENERATOR] %(levelname)s: %(message)s",
    )
    asyncio.run(main())