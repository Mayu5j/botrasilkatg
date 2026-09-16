"""
bot/handlers/start.py — /start, /help, главное меню, статус.
"""
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from models import User
from bot.keyboards import kb_main_menu, kb_back_to_menu

router = Router()

# Сообщение, которое отправляется и закрепляется при первом запуске пользователем.
# Изменяй эту переменную по своему усмотрению.
start_zakrep = (
    "📌 *Важная информация*\n\n"
    """Добро пожаловать! Ознакомьтесь с манулом для пользования ботом. 
    Если есть вопросы, можете задавать 24/7 
    Админ - @jstaskmebro
    Мануал - https://t.me/rasilkatextinfo """
)

# Кусок текста из start_zakrep, по которому определяем "наше" ли закреплённое сообщение.
# Берём первые 40 символов без форматирования — этого достаточно для уникальной идентификации.
_ZAKREP_FINGERPRINT = "Важная информация"


def status_text(user: User) -> str:
    """Текст статуса пользователя."""
    tasks_count    = len(user.tasks)
    accounts_count = len(user.accounts)
    active_tasks   = sum(1 for t in user.tasks if t.is_active)

    return (
        f"👤 *{user.full_name}*\n"
        f"🆔 `{user.id}`\n\n"
        f"📊 {user.subscription_status}\n\n"
        f"📋 Задач: {tasks_count} (активных: {active_tasks})\n"
        f"🤖 Аккаунтов: {accounts_count}\n"
        f"📬 Лимит чатов: {user.max_chats}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, user: User):
    """Приветствие при /start."""
    greeting = "👋 *Добро пожаловать!*" if len(user.tasks) == 0 else "👋 *С возвращением!*"
    text = (
        f"{greeting}\n\n"
        f"Я помогу делать рассылки в Telegram-чаты.\n\n"
        f"{user.subscription_status}\n\n"
        "Выбери действие:"
    )
    await message.answer(text, reply_markup=kb_main_menu(user.has_access), parse_mode="Markdown")

    # Только при первом запуске проверяем и при необходимости закрепляем сообщение
    if len(user.tasks) == 0:
        try:
            chat = await message.bot.get_chat(message.chat.id)
            pinned = chat.pinned_message

            # Проверяем текст закреплённого сообщения — наше ли оно
            already_pinned = (
                pinned is not None
                and pinned.text is not None
                and _ZAKREP_FINGERPRINT in pinned.text
            )

            if not already_pinned:
                sent = await message.answer(start_zakrep, parse_mode="Markdown")
                await message.bot.pin_chat_message(
                    chat_id=message.chat.id,
                    message_id=sent.message_id,
                    disable_notification=True,
                )
        except Exception:
            # Нет прав закреплять — просто отправляем без закрепления
            try:
                await message.answer(start_zakrep, parse_mode="Markdown")
            except Exception:
                pass


@router.message(Command("help"))
async def cmd_help(message: Message, user: User):
    """Справка."""
    text = (
        "📋 *Команды:*\n\n"
        "/start — главное меню\n"
        "/status — ваш статус\n"
        "/tasks — управление задачами\n"
        "/accounts — управление аккаунтами\n"
        "/pay — оплата подписки\n"
    )
    if user.is_admin:
        text += "\n*Администратор:*\n/admin — панель управления\n"
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("status"))
async def cmd_status(message: Message, user: User):
    await message.answer(status_text(user), parse_mode="Markdown", reply_markup=kb_back_to_menu())


# ── Callback-и ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.in_({"menu", "menu:new"}))
async def cb_menu(query: CallbackQuery, user: User):
    """Вернуться в главное меню. Обрабатывает и 'menu' и 'menu:new'."""
    await query.message.answer(
        f"👋 Главное меню\n{user.subscription_status}",
        reply_markup=kb_main_menu(user.has_access),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "status")
async def cb_status(query: CallbackQuery, user: User):
    await query.message.answer(
        status_text(user),
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "noop")
async def cb_noop(query: CallbackQuery):
    await query.answer()
