"""
bot/handlers/accounts.py — добавление и управление аккаунтами через FSM.

ИЗМЕНЕНИЯ:
  - view_account: показывает статус ограничения (frozen/spamblocked) с пояснением
  - Заблокированные/замороженные аккаунты нельзя включить — кнопка Toggle скрыта

Шаги добавления аккаунта:
  1. api_id
  2. api_hash
  3. номер телефона → отправляем код через Telethon
  4. код из Telegram
  5. (если 2FA) пароль
"""
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import SessionPasswordNeededError

from models import User, Account
from services import account_service
from bot.keyboards import kb_accounts, kb_account_detail, kb_cancel, kb_back_to_menu

log = logging.getLogger(__name__)
router = Router()


# ── FSM состояния ─────────────────────────────────────────────────────────────
class AddAccount(StatesGroup):
    api_id       = State()
    api_hash     = State()
    phone        = State()
    code         = State()
    password_2fa = State()


# ── Список аккаунтов ──────────────────────────────────────────────────────────

@router.message(Command("accounts"))
@router.callback_query(F.data == "accounts:list")
async def show_accounts(event, user: User, db: AsyncSession):
    """Показать список аккаунтов пользователя."""
    accounts = await account_service.get_accounts(db, owner_id=user.id)
    text = "🤖 *Ваши аккаунты*\n\nВыберите аккаунт для управления:"
    kb = kb_accounts(accounts)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("accounts:view:"))
async def view_account(query: CallbackQuery, user: User, db: AsyncSession):
    acc_id = int(query.data.split(":")[2])
    acc = await account_service.get_account_by_id(db, acc_id)

    if not acc or (acc.owner_id != user.id and not user.is_admin):
        await query.answer("Аккаунт не найден.", show_alert=True)
        return

    # ── Блок статуса ─────────────────────────────────────────────────────────
    status_line = acc.status_label  # из свойства модели

    # Дополнительное пояснение для проблемных аккаунтов
    restriction_note = ""
    if acc.status == "frozen":
        restriction_note = (
            "\n\n⚠️ *Аккаунт заморожен Telegram-ом.*\n"
            "Все задачи остановлены. Обратитесь в поддержку Telegram."
        )
    elif acc.status == "spamblocked":
        restriction_note = (
            "\n\n⚠️ *Аккаунт в спамблоке.*\n"
            "Рассылки остановлены. Попробуйте снять ограничение через @SpamBot."
        )

    text = (
        f"{acc.status_icon} *Аккаунт {acc.phone}*\n\n"
        f"Статус: {status_line}\n"
        f"Чатов: {acc.chats_count}\n"
        f"ID: `{acc.id}`"
        f"{restriction_note}"
    )
    await query.message.answer(text, reply_markup=kb_account_detail(acc), parse_mode="Markdown")


@router.callback_query(F.data.startswith("accounts:toggle:"))
async def toggle_account(query: CallbackQuery, user: User, db: AsyncSession):
    acc_id = int(query.data.split(":")[2])
    acc = await account_service.get_account_by_id(db, acc_id)

    if not acc or (acc.owner_id != user.id and not user.is_admin):
        await query.answer("Нет доступа.", show_alert=True)
        return

    # Запрещаем включать замороженные/заспамблоченные аккаунты
    if acc.status != "ok":
        await query.answer(
            f"Невозможно изменить: аккаунт {acc.status_label}",
            show_alert=True
        )
        return

    acc.is_active = not acc.is_active
    await db.commit()
    status = "включён ✅" if acc.is_active else "отключён ⏸"
    await query.answer(f"Аккаунт {status}")
    await view_account(query, user, db)


@router.callback_query(F.data.startswith("accounts:delete:"))
async def delete_account(query: CallbackQuery, user: User, db: AsyncSession):
    acc_id = int(query.data.split(":")[2])
    acc = await account_service.get_account_by_id(db, acc_id)

    if not acc or (acc.owner_id != user.id and not user.is_admin):
        await query.answer("Нет доступа.", show_alert=True)
        return

    await account_service.delete_account(db, acc_id)
    await query.answer("Аккаунт удалён.")
    await show_accounts(query, user, db)


# ── Добавление аккаунта (FSM) ─────────────────────────────────────────────────

@router.callback_query(F.data == "accounts:add")
async def start_add_account(query: CallbackQuery, state: FSMContext, user: User):
    """Начать мастер добавления аккаунта."""
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return

    await query.message.answer(
        "➕ *Добавление аккаунта*\n\n"
        "⚠️ *Важно:*\n"
        "• Не ставьте интервал рассылки < 15 минут\n"
        "• Нарушение может привести к спамблоку\n\n"
        "*Шаг 1/3* — Введите API\\_ID\n"
        "(получить на [my.telegram.org/apps](https://my.telegram.org/apps))",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    await state.set_state(AddAccount.api_id)


@router.message(AddAccount.api_id)
async def got_api_id(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Должно быть числом. Попробуйте снова:")
        return
    await state.update_data(api_id=int(message.text.strip()))
    await message.answer(
        "*Шаг 2/3* — Введите API\\_HASH:",
        reply_markup=kb_cancel(), parse_mode="Markdown"
    )
    await state.set_state(AddAccount.api_hash)


@router.message(AddAccount.api_hash)
async def got_api_hash(message: Message, state: FSMContext):
    await state.update_data(api_hash=message.text.strip())
    await message.answer(
        "*Шаг 3/3* — Введите номер телефона:\n"
        "Пример: `+1671234567`",
        reply_markup=kb_cancel(), parse_mode="Markdown"
    )
    await state.set_state(AddAccount.phone)


@router.message(AddAccount.phone)
async def got_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    data = await state.get_data()

    await message.answer(f"📨 Отправляю код на {phone}...")
    try:
        client, phone_code_hash = await account_service.send_code(
            data["api_id"], data["api_hash"], phone
        )
        await state.update_data(
            phone=phone,
            phone_code_hash=phone_code_hash,
        )
        message.bot._pending_clients = getattr(message.bot, "_pending_clients", {})
        message.bot._pending_clients[message.from_user.id] = client

        await message.answer(
            "✅ Код отправлен!\n\n"
            "Введите код из Telegram (без пробелов):\n`12345`",
            reply_markup=kb_cancel(), parse_mode="Markdown"
        )
        await state.set_state(AddAccount.code)
    except Exception as e:
        await message.answer(f"❌ Ошибка: `{e}`\n\nНачните заново /accounts", parse_mode="Markdown")
        await state.clear()


@router.message(AddAccount.code)
async def got_code(message: Message, state: FSMContext, user: User, db: AsyncSession):
    code = message.text.strip().replace(" ", "")
    data = await state.get_data()
    client = getattr(message.bot, "_pending_clients", {}).get(message.from_user.id)

    if not client:
        await message.answer("❌ Сессия истекла. Начните заново /accounts")
        await state.clear()
        return

    try:
        session_str = await account_service.sign_in_code(
            client, data["phone"], code, data["phone_code_hash"]
        )
    except SessionPasswordNeededError:
        await message.answer(
            "🔐 Включена двухфакторная аутентификация.\nВведите пароль:",
            reply_markup=kb_cancel()
        )
        await state.set_state(AddAccount.password_2fa)
        return
    except Exception as e:
        await message.answer(f"❌ Неверный код: `{e}`", parse_mode="Markdown")
        await client.disconnect()
        message.bot._pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return

    await _finish_add_account(message, state, user, db, client, data["phone"], session_str)


@router.message(AddAccount.password_2fa)
async def got_2fa(message: Message, state: FSMContext, user: User, db: AsyncSession):
    client = getattr(message.bot, "_pending_clients", {}).get(message.from_user.id)
    data = await state.get_data()

    try:
        session_str = await account_service.sign_in_2fa(client, message.text.strip())
    except Exception as e:
        await message.answer(f"❌ Неверный пароль: `{e}`", parse_mode="Markdown")
        await client.disconnect()
        message.bot._pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return

    await _finish_add_account(message, state, user, db, client, data["phone"], session_str)


async def _finish_add_account(message, state, user, db, client, phone, session_str):
    """Сохранить аккаунт в БД после успешного входа."""
    data = await state.get_data()
    name = await account_service.get_me_name(client)
    await client.disconnect()
    message.bot._pending_clients.pop(message.from_user.id, None)
    await state.clear()

    acc = await account_service.create_account(
        db,
        api_id=data["api_id"],
        api_hash=data["api_hash"],
        phone=phone,
        session_string=session_str,
        owner_id=user.id,
    )
    await message.answer(
        f"✅ Аккаунт *{name}* (`{phone}`) добавлен!\n"
        f"ID: `{acc.id}`",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
    log.info("Пользователь %d добавил аккаунт %s", user.id, phone)