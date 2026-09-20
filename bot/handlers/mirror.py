"""
bot/handlers/mirror.py — управление зеркальным ботом пользователя.
 
Пользователь может добавить 1 свой бот (зеркало).
Зеркало работает с тем же backend, но оплата в нём недоступна.
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
 
from models import User, MirrorBot
from bot.keyboards import kb_cancel, kb_back_to_menu, kb_main_menu
 
router = Router()
 
 
class AddMirror(StatesGroup):
    token = State()
 
 
@router.message(Command("mirror"))
@router.callback_query(F.data == "mirror:menu")
async def show_mirror(event, user: User, db: AsyncSession):
    """Показать текущее зеркало или предложить добавить."""
    result = await db.execute(select(MirrorBot).where(MirrorBot.user_id == user.id))
    mirror = result.scalar_one_or_none()
 
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    if mirror:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить зеркало", callback_data="mirror:delete")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="menu")],
        ])
        text = (
            f"🤖 *Ваш зеркальный бот*\n\n"
            f"@{mirror.bot_username or 'неизвестно'}\n"
            f"Статус: {'✅ Активен' if mirror.is_active else '⏸ Остановлен'}"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить зеркало", callback_data="mirror:add")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="menu")],
        ])
        text = (
            "🤖 *Зеркальный бот*\n\n"
            "Вы можете добавить 1 свой Telegram-бот.\n"
            "Он будет работать так же, как этот.\n\n"
            "⚠️ В зеркале недоступна оплата — только в главном боте."
        )
 
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
 
 
@router.callback_query(F.data == "mirror:add")
async def start_add_mirror(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return
 
    result = await db.execute(select(MirrorBot).where(MirrorBot.user_id == user.id))
    if result.scalar_one_or_none():
        await query.answer("У вас уже есть зеркало.", show_alert=True)
        return
 
    await query.message.answer(
        "➕ *Добавление зеркала*\n\n"
        "Отправьте токен вашего бота.\n"
        "Получить можно у @BotFather.\n\n"
        "Формат: `1234567890:AABBccdd...`",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(AddMirror.token)
 
 
@router.message(AddMirror.token)
async def got_mirror_token(message: Message, state: FSMContext, user: User, db: AsyncSession):
    token = message.text.strip()
 
    # Проверяем формат токена
    if ":" not in token or len(token) < 20:
        await message.answer("❌ Неверный формат. Попробуйте снова:")
        return
 
    # Проверяем токен через Bot API
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            resp = await session.get(f"https://api.telegram.org/bot{token}/getMe")
            data = await resp.json()
        if not data.get("ok"):
            await message.answer("❌ Бот не найден. Проверьте токен:")
            return
        bot_username = data["result"]["username"]
    except Exception:
        await message.answer("❌ Не удалось проверить бот. Попробуйте позже.")
        await state.clear()
        return
 
    # Сохраняем зеркало
    mirror = MirrorBot(user_id=user.id, token=token, bot_username=bot_username)
    db.add(mirror)
    await db.commit()
    await state.clear()
 
    await message.answer(
        f"✅ Зеркало *@{bot_username}* добавлено!\n\n"
        f"Запустите его командой из README.",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
 
 
@router.callback_query(F.data == "mirror:delete")
async def delete_mirror(query: CallbackQuery, user: User, db: AsyncSession):
    result = await db.execute(select(MirrorBot).where(MirrorBot.user_id == user.id))
    mirror = result.scalar_one_or_none()
    if mirror:
        await db.delete(mirror)
        await db.commit()
    await query.answer("✅ Зеркало удалено.")
    await show_mirror(query, user, db)

