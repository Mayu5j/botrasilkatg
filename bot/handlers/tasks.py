"""
bot/handlers/tasks.py — создание и управление задачами рассылок.
 
ИЗМЕНЕНИЯ (медиа-рефакторинг):
  - Фото больше не хранится как file_id.
  - При создании задачи байты фото скачиваются через Bot API
    и сохраняются в таблицу TaskMedia (LargeBinary).
  - Воркер при первой отправке читает байты → отправляет через client.send_file()
    → кеширует полученный Telethon file_id в TaskMediaCache
    → удаляет строки из TaskMedia.
  - При повторных отправках воркер использует кеш (без байт).
 
ИЗМЕНЕНИЯ (обход ограничений чата при создании задачи):
  - Часть чатов отсеивается ещё на этапе ЛЁГКОЙ проверки (account_service.
    check_and_join_chats — GetParticipantRequest без реальной отправки).
    Именно на этом этапе чаще всего всплывает капча/квиз от бота-админа
    или требование подписки на каналы. Поэтому для таких чатов сначала
    пробуем restriction_service.try_bypass_restriction() тем же аккаунтом,
    который проверял доступ, и при успехе повторяем лёгкую проверку.
  - Если чат прошёл лёгкую проверку, но не удержался при реальной отправке
    (live_failed из _verify_chats_by_real_message) — тоже пробуем bypass
    и повторную реальную проверку доставки.
"""
import asyncio
import html
import io
import logging
import json
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any
 
from telethon.errors import FloodWaitError
from telethon.tl import types as tl_types
 
from database import SessionLocal
from models import User, TaskMedia, TaskAccount, Account, Log
from services import task_service, account_service
from bot.keyboards import (
    kb_tasks, kb_task_detail, kb_task_delete_confirm,
    kb_cancel, kb_back_to_menu, kb_confirm_chats,
    kb_choose_sender, kb_access_error,
)
 
log = logging.getLogger(__name__)
router = Router()
 
 
# ── FSM ───────────────────────────────────────────────────────────────────────
 
class CreateTask(StatesGroup):
    name     = State()
    message  = State()
    interval = State()
    chats    = State()
    sender   = State()
 
 
# ── Отмена — ПЕРВОЙ в роутере ─────────────────────────────────────────────────
 
@router.callback_query(F.data == "menu")
async def cb_cancel_to_menu(query: CallbackQuery, state: FSMContext, user: User):
    current = await state.get_state()
    if current:
        await state.clear()
    from bot.keyboards import kb_main_menu
    await query.message.answer(
        f"👋 Главное меню\n{html.escape(user.subscription_status)}",
        reply_markup=kb_main_menu(user.has_access),
        parse_mode="HTML",
    )
 
 
# ── Утилиты ───────────────────────────────────────────────────────────────────
 
def _normalize_chat_id(raw: str) -> str:
    s = raw.strip()
    if s.startswith("@"):
        return f"@{s.lstrip('@')}"
    return s
 
 
def _chat_display_from_task_chat(c) -> str:
    ct = c.chat_title or ""
    if ct.startswith("@"):
        uname = ct.lstrip("@")
        return f'<a href="https://t.me/{uname}">{html.escape(ct)}</a>'
    return html.escape(ct) if ct else html.escape(c.chat_id)
 
 
async def _download_photo_bytes(bot, file_id: str) -> bytes | None:
    """
    Скачать фото через Bot API и вернуть сырые байты.
    Возвращает None при ошибке.
    """
    try:
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        return buf.getvalue()
    except Exception as e:
        log.error("Не удалось скачать фото file_id=%s: %s", file_id[:20], e)
        return None
 
 
# ── Список задач ──────────────────────────────────────────────────────────────
 
@router.message(Command("tasks"))
async def cmd_tasks(message: Message, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    tasks = await task_service.get_tasks(db, user.id)
    text = "📋 <b>Ваши задачи</b>" if tasks else "📋 У вас пока нет задач."
    await message.answer(text, reply_markup=kb_tasks(tasks), parse_mode="HTML")
 
 
@router.callback_query(F.data == "tasks:list")
async def cb_tasks_list(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    tasks = await task_service.get_tasks(db, user.id)
    text = "📋 <b>Ваши задачи</b>" if tasks else "📋 У вас пока нет задач."
    await query.message.answer(text, reply_markup=kb_tasks(tasks), parse_mode="HTML")
 
 
@router.callback_query(F.data.startswith("tasks:view:"))
async def view_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return
 
    icon = "▶️" if task.is_active else "⏸"
 
    chats_lines = []
    for c in task.chats[:15]:
        display = _chat_display_from_task_chat(c)
        status  = "" if c.is_ok else " ⚠️"
        chats_lines.append(f"• {display}{status}")
    chats_block = "\n".join(chats_lines) if chats_lines else "—"
    if len(task.chats) > 15:
        chats_block += f"\n…и ещё {len(task.chats) - 15}"
 
    acc_lines = []
    for link in task.accounts:
        try:
            ids = json.loads(link.chat_ids) if link.chat_ids else []
        except Exception:
            ids = []
        acc = getattr(link, "account", None)
        acc_name = acc.phone if acc else f"acc#{link.account_id}"
        if acc and acc.is_system:
            acc_name += " (system)"
        acc_lines.append(f"• {html.escape(acc_name)}: {len(ids)} чатов")
 
    accounts_block = "\n".join(acc_lines) if acc_lines else "—"
    media_note = " 📷" if task.has_media else ""
 
    text = (
        f"{icon} <b>{html.escape(task.name)}</b>{media_note}\n\n"
        f"💬 Сообщение:\n<i>{html.escape(task.message[:200])}</i>\n\n"
        f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
        f"📬 Чатов: {len(task.chats)}\n"
        f"🤖 Аккаунтов: {len(task.accounts)}\n\n"
        f"🏷 <b>Чаты рассылки:</b>\n{chats_block}\n\n"
        f"👤 <b>Распределение:</b>\n{accounts_block}"
    )
 
    await query.message.answer(
        text,
        reply_markup=kb_task_detail(task),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
 
 
@router.callback_query(F.data.startswith("tasks:toggle:"))
async def toggle_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return
    task_id   = int(query.data.split(":")[2])
    new_state = await task_service.toggle_task(db, task_id, user.id)
    if new_state is None:
        await query.answer("Задача не найдена.", show_alert=True)
        return
    status = "запущена ▶️" if new_state else "остановлена ⏸"
    await query.answer(f"Задача {status}")
    task = await task_service.get_task(db, task_id, user.id)
    if task:
        icon = "▶️" if task.is_active else "⏸"
        text = (
            f"{icon} <b>{html.escape(task.name)}</b>\n\n"
            f"💬 Сообщение:\n<i>{html.escape(task.message[:200])}</i>\n\n"
            f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
            f"📬 Чатов: {len(task.chats)}"
        )
        await query.message.answer(
            text, reply_markup=kb_task_detail(task), parse_mode="HTML"
        )
 
 
TASK_LOGS_PER_PAGE = 20
 
 
def _make_message_link(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    s = str(chat_id).strip()
    if s.startswith("@"):
        return f"https://t.me/{s.lstrip('@')}/{message_id}"
    if s.lstrip("-").isdigit():
        raw = str(int(s))
        if raw.startswith("-100"):
            return f"https://t.me/c/{raw[4:]}/{message_id}"
    return None
 
 
@router.callback_query(F.data.startswith("tasks:stats:"))
async def task_stats(query: CallbackQuery, user: User, db: AsyncSession):
    from sqlalchemy import func
    task_id = int(query.data.split(":")[2])
    task    = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return
 
    total_res = await db.execute(
        select(func.count(Log.id)).where(Log.task_id == task_id, Log.success == True)
    )
    total_sent: int = total_res.scalar() or 0
 
    linkable_res = await db.execute(
        select(func.count(Log.id)).where(
            Log.task_id == task_id,
            Log.success == True,
            Log.message_id.isnot(None),
        )
    )
    linkable: int = linkable_res.scalar() or 0
 
    icon   = "▶️" if task.is_active else "⏸"
    text   = (
        f"{icon} <b>{html.escape(task.name)}</b>\n\n"
        f"📊 <b>Статистика задачи:</b>\n"
        f"✅ Всего отправлено: <b>{total_sent}</b>\n"
        f"🔗 Сообщений с ссылкой: <b>{linkable}</b>\n\n"
    )
    if linkable > 0:
        text += "Нажмите кнопку ниже чтобы просмотреть ссылки на каждое сообщение."
    else:
        text += "Ссылки появятся после следующих отправок."
 
    if linkable > 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Посмотреть ссылки", callback_data=f"tasks:logs:{task_id}:0")],
            [InlineKeyboardButton(text="◀️ К задаче", callback_data=f"tasks:view:{task_id}")],
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К задаче", callback_data=f"tasks:view:{task_id}")],
        ])
 
    await query.message.answer(text, reply_markup=kb, parse_mode="HTML")
 
 
@router.callback_query(F.data.startswith("tasks:logs:"))
async def task_logs_page(query: CallbackQuery, user: User, db: AsyncSession):
    from sqlalchemy import func
    from bot.keyboards import kb_task_logs_page
 
    parts   = query.data.split(":")   # tasks:logs:TASK_ID:PAGE
    task_id = int(parts[2])
    page    = int(parts[3]) if len(parts) > 3 else 0
 
    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return
 
    count_res = await db.execute(
        select(func.count(Log.id)).where(
            Log.task_id == task_id,
            Log.success == True,
            Log.message_id.isnot(None),
        )
    )
    linkable: int = count_res.scalar() or 0
    total_pages   = max(1, -(-linkable // TASK_LOGS_PER_PAGE))
    page          = max(0, min(page, total_pages - 1))
 
    logs_res = await db.execute(
        select(Log)
        .where(Log.task_id == task_id, Log.success == True, Log.message_id.isnot(None))
        .order_by(Log.created_at.desc())
        .limit(TASK_LOGS_PER_PAGE)
        .offset(page * TASK_LOGS_PER_PAGE)
    )
    logs  = logs_res.scalars().all()
    lines = []
    for i, lg in enumerate(logs, start=page * TASK_LOGS_PER_PAGE + 1):
        link = _make_message_link(lg.chat_id, lg.message_id)
        ts   = lg.created_at.strftime("%d.%m %H:%M") if lg.created_at else "—"
        if link:
            lines.append(f'{i}. <a href="{link}">{html.escape(lg.chat_id)}</a> — {ts}')
        else:
            lines.append(f'{i}. {html.escape(lg.chat_id)} — {ts}')
 
    if not lines:
        lines = ["(нет отправок с публичной ссылкой)"]
 
    text = (
        f"🔗 <b>Сообщения задачи</b> «{html.escape(task.name)}»\n"
        f"Стр. {page + 1} / {total_pages} · {linkable} ссылок\n\n"
        + "\n".join(lines)
    )
 
    await query.message.answer(
        text,
        reply_markup=kb_task_logs_page(task_id, page, total_pages),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
 
 
@router.callback_query(F.data.startswith("tasks:delete:"))
async def ask_delete_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    task    = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return
    await query.message.answer(
        f"⚠️ Удалить задачу <b>{html.escape(task.name)}</b>?\n\nЭто действие нельзя отменить.",
        reply_markup=kb_task_delete_confirm(task_id),
        parse_mode="HTML",
    )
 
 
@router.callback_query(F.data.startswith("tasks:confirm_delete:"))
async def confirm_delete_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    deleted = await task_service.delete_task(db, task_id, user.id)
    await query.answer("✅ Задача удалена." if deleted else "❌ Не найдено.", show_alert=not deleted)
    tasks = await task_service.get_tasks(db, user.id)
    text  = "📋 <b>Ваши задачи</b>" if tasks else "📋 У вас пока нет задач."
    await query.message.answer(text, reply_markup=kb_tasks(tasks), parse_mode="HTML")
 
 
# ── Создание задачи (FSM) ─────────────────────────────────────────────────────
 
@router.callback_query(F.data == "tasks:new")
async def cb_new_task(query: CallbackQuery, state: FSMContext, user: User):
    await state.clear()
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return
    await query.message.answer(
        "➕ <b>Новая задача рассылки</b>\n\n"
        "<b>Шаг 1/4</b> — Введите название задачи:\n"
        "Например: <code>Реклама магазина</code>",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.name)
 
 
@router.message(Command("newtask"))
async def cmd_new_task(message: Message, state: FSMContext, user: User):
    await state.clear()
    if not user.has_access:
        await message.answer("⚠️ Нужна активная подписка.")
        return
    await message.answer(
        "➕ <b>Новая задача рассылки</b>\n\n"
        "<b>Шаг 1/4</b> — Введите название задачи:",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.name)
 
 
@router.message(CreateTask.name)
async def got_task_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(
        "<b>Шаг 2/4</b> — Введите текст сообщения.\n\n"
        "Можно прикрепить до 5 фото (отправьте как медиагруппу или по одному).\n"
        "После отправки фото напишите <code>ок</code> чтобы продолжить.",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.message)
 
 
@router.message(CreateTask.message)
async def got_task_message(message: Message, state: FSMContext):
    """
    Принимаем текст и/или фото.
    Байты фото скачиваются сразу и сохраняются в FSM-состоянии как список bytes.
    В БД байты попадут только при финальном создании задачи (confirm_chats).
    """
    text, entities_json = _extract_text_and_entities(message)
 
    # ── Одиночное фото ─────────────────────────────────────────────────────
    if message.photo and not message.media_group_id:
        photo_bytes = await _download_photo_bytes(message.bot, message.photo[-1].file_id)
        await state.update_data(
            message=text,
            format_entities=entities_json,
            # храним список байт-объектов как base64 чтобы FSM мог их сериализовать
            photo_bytes_b64=[_b64(photo_bytes)] if photo_bytes else [],
        )
        await message.answer(
            "<b>Шаг 3/4</b> — Введите интервал в минутах:\n\n"
            "Минимум: <b>1 минута</b>\n"
            "⚠️ РЕКОМЕНДУЕМ ОТ 5 ДО 15 минут\n"
            "Пример: <code>60</code> = каждый час",
            reply_markup=kb_cancel(),
            parse_mode="HTML",
        )
        await state.set_state(CreateTask.interval)
        return
 
    # ── Медиагруппа ────────────────────────────────────────────────────────
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        data = await state.get_data()
        mg = data.get("media_group", {"id": media_group_id, "photos_b64": [], "text": "", "entities": []})
        if mg.get("id") != media_group_id:
            mg = {"id": media_group_id, "photos_b64": [], "text": "", "entities": []}
 
        if message.photo:
            if len(mg["photos_b64"]) < 5:
                photo_bytes = await _download_photo_bytes(message.bot, message.photo[-1].file_id)
                if photo_bytes:
                    mg["photos_b64"].append(_b64(photo_bytes))
        if text:
            mg["text"]     = text
            mg["entities"] = entities_json
 
        await state.update_data(media_group=mg)
        await message.answer(
            f"📸 Принял фото: {len(mg['photos_b64'])}/5. "
            "Добавьте ещё или отправьте <code>ок</code> для продолжения.",
            parse_mode="HTML",
        )
        return
 
    # ── "ок" после медиагруппы ──────────────────────────────────────────────
    data = await state.get_data()
    mg   = data.get("media_group")
    if (message.text or "").strip().lower() in {"ок", "ok", "да", "done"} and mg and mg.get("photos_b64"):
        text           = mg.get("text", "")
        entities_json  = mg.get("entities", [])
        photos_b64     = mg.get("photos_b64", [])
        await state.update_data(
            message=text,
            format_entities=entities_json,
            photo_bytes_b64=photos_b64,
            media_group=None,
        )
        await message.answer(
            "<b>Шаг 3/4</b> — Введите интервал в минутах:\n\n"
            "Минимум: <b>1 минута</b>\n"
            "⚠️ РЕКОМЕНДУЕМ ОТ 5 ДО 15 минут\n"
            "Пример: <code>60</code> = каждый час",
            reply_markup=kb_cancel(),
            parse_mode="HTML",
        )
        await state.set_state(CreateTask.interval)
        return
 
    # ── Только текст ────────────────────────────────────────────────────────
    if not text:
        await message.answer("❌ Пришлите текст или фото (до 5 штук) с подписью.")
        return
 
    await state.update_data(
        message=text,
        format_entities=entities_json,
        photo_bytes_b64=[],
    )
    await message.answer(
        "<b>Шаг 3/4</b> — Введите интервал в минутах:\n\n"
        "Минимум: <b>1 минута</b>\n"
        "⚠️ РЕКОМЕНДУЕМ ОТ 5 ДО 15 минут\n"
        "Пример: <code>60</code> = каждый час",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.interval)
 
 
@router.message(CreateTask.interval)
async def got_task_interval(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ Минимум 1 минута. Введите число ≥ 1:")
        return
    await state.update_data(interval=int(text))
    await message.answer(
        "<b>Шаг 4/4</b> — Введите чаты:\n\n"
        "Вариант 1 — ссылка на папку:\n<code>https://t.me/addlist/XXXX</code>\n\n"
        "Вариант 2 — список через новую строку:\n"
        "<code>@username</code>\n<code>-1001234567890</code>",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.chats)
 
 
@router.message(CreateTask.chats)
async def got_task_chats(message: Message, state: FSMContext, user: User, db: AsyncSession):
    raw   = message.text.strip()
    chats = []
 
    if raw.startswith("https://t.me/addlist/"):
        await message.answer("🔍 Получаю список чатов из папки...")
 
        accounts = await account_service.get_accounts(db, owner_id=user.id)
        if not accounts:
            accounts = await account_service.get_accounts(db)
        if not accounts:
            await message.answer(
                "❌ Нет доступных аккаунтов.\n"
                "Добавьте аккаунт через /accounts или обратитесь к администратору."
            )
            return
 
        client = account_service.make_client(accounts[0])
        try:
            await client.connect()
            await client.get_dialogs()
            chats = await account_service.get_chats_from_folder(client, raw)
        except Exception as e:
            log.error("Ошибка получения папки: %s", e)
            await message.answer(
                f"❌ Не удалось получить чаты из папки.\n<code>{html.escape(str(e))}</code>\n\nПопробуйте ввести вручную:",
                parse_mode="HTML",
            )
            return
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
 
        if not chats:
            await message.answer(
                "❌ Папка пустая или недоступна.\n\n"
                "Убедитесь что ссылка вида <code>https://t.me/addlist/XXXX</code>\n\n"
                "Попробуйте ввести чаты вручную:",
                parse_mode="HTML",
            )
            return
 
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("@"):
                username = line.lstrip("@")
                chat_id  = f"@{username}"
            elif line.lstrip("-").isdigit():
                username = None
                chat_id  = line
            else:
                username = line.lstrip("@")
                chat_id  = f"@{username}"
 
            chats.append({
                "id":          chat_id,
                "title":       f"@{username}" if username else chat_id,
                "username":    username,
                "access_hash": None,
                "folder_slug": None,
            })
 
    if not chats:
        await message.answer("❌ Не нашёл чатов. Попробуйте снова:")
        return
 
    if len(chats) > user.max_chats:
        chats = chats[:user.max_chats]
 
    await state.update_data(chats=chats)
 
    preview_lines = []
    for c in chats[:10]:
        uname = c.get("username")
        title = c.get("title") or (f"@{uname}" if uname else c["id"])
        preview_lines.append(f"• {html.escape(title)}")
    preview = "\n".join(preview_lines)
    if len(chats) > 10:
        preview += f"\n...и ещё {len(chats) - 10}"
 
    accounts = await account_service.get_accounts(db, owner_id=user.id)
 
    await message.answer(
        f"✅ Найдено чатов: <b>{len(chats)}</b>\n\n"
        f"{preview}\n\n"
        f"<b>Шаг 5/5</b> — Выберите отправителя:",
        reply_markup=kb_choose_sender(accounts),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.sender)
 
 
@router.callback_query(CreateTask.sender, F.data.startswith("tasks:sender:"))
async def got_sender_choice(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    choice = query.data
 
    if choice == "tasks:sender:system":
        await state.update_data(sender_account_id=None)
        sender_text = "🤖 Системные аккаунты"
    else:
        account_id = int(choice.split(":")[-1])
        await state.update_data(sender_account_id=account_id)
        acc         = await account_service.get_account_by_id(db, account_id)
        sender_text = f"👤 {acc.phone}" if acc else "👤 Выбранный аккаунт"
 
    data  = await state.get_data()
    chats = data.get("chats", [])
    has_photo = bool(data.get("photo_bytes_b64"))
 
    await query.message.answer(
        f"✅ Отправитель: <b>{html.escape(sender_text)}</b>\n\n"
        f"📋 Задача: <b>{html.escape(data['name'])}</b>\n"
        f"📬 Чатов: <b>{len(chats)}</b>\n"
        f"⏱ Каждые {data['interval']} мин.\n"
        f"{'📷 С фото' if has_photo else '📝 Только текст'}\n\n"
        f"Нажмите <b>Продолжить</b> для создания задачи:",
        reply_markup=kb_confirm_chats(),
        parse_mode="HTML",
    )
 
 
def _to_telethon_entities(entities_json: list[dict]) -> list:
    out = []
    for e in entities_json or []:
        t      = (e.get("type") or "").lower()
        offset = int(e.get("offset", 0))
        length = int(e.get("length", 0))
        try:
            if t == "bold":
                out.append(tl_types.MessageEntityBold(offset=offset, length=length))
            elif t == "italic":
                out.append(tl_types.MessageEntityItalic(offset=offset, length=length))
            elif t == "underline":
                out.append(tl_types.MessageEntityUnderline(offset=offset, length=length))
            elif t in {"strikethrough", "strike"}:
                out.append(tl_types.MessageEntityStrike(offset=offset, length=length))
            elif t == "spoiler":
                out.append(tl_types.MessageEntitySpoiler(offset=offset, length=length))
            elif t == "code":
                out.append(tl_types.MessageEntityCode(offset=offset, length=length))
            elif t == "pre":
                out.append(tl_types.MessageEntityPre(offset=offset, length=length, language=""))
            elif t in {"blockquote", "quote"}:
                out.append(tl_types.MessageEntityBlockquote(offset=offset, length=length))
            elif t == "text_link":
                url = e.get("url")
                if url:
                    out.append(tl_types.MessageEntityTextUrl(offset=offset, length=length, url=url))
        except Exception:
            pass
    return out
 
 
def _is_real_message(msg) -> bool:
    return bool(msg) and not isinstance(msg, tl_types.MessageEmpty)
 
 
async def _resolve_check_entity(client, chat: dict):
    username = chat.get("username")
    chat_id = str(chat.get("id"))
    if username:
        try:
            return await client.get_entity(f"@{username}")
        except Exception:
            pass
    if chat_id.startswith("@"):
        return await client.get_entity(chat_id)
    if chat_id.lstrip("-").isdigit():
        numeric = int(chat_id)
        try:
            return await client.get_entity(numeric)
        except Exception:
            pass
        if numeric > 0:
            return await client.get_entity(int(f"-100{numeric}"))
    return await client.get_entity(chat_id)
 
 
async def _send_and_confirm_check_message(
    client,
    chat: dict,
    message_text: str,
    entities_json: list[dict],
) -> tuple[bool, str, int | None, str | None]:
    entities = _to_telethon_entities(entities_json)
    last_reason = "message_deleted_after_send"
    last_message_id: int | None = None
 
    try:
        entity = await _resolve_check_entity(client, chat)
    except Exception as e:
        return False, f"entity_not_found: {str(e)[:80]}", None, None
 
    for attempt in range(1, 4):
        try:
            sent = await client.send_message(
                entity,
                message_text or "Проверка доставки",
                formatting_entities=entities or None,
            )
            last_message_id = getattr(sent, "id", None)
            if not last_message_id:
                last_reason = "message_id_missing"
                continue
 
            msg = await client.get_messages(entity, ids=last_message_id)
            if not _is_real_message(msg):
                last_reason = "message_not_found_after_send"
                continue
 
            await asyncio.sleep(5)
 
            msg = await client.get_messages(entity, ids=last_message_id)
            if not _is_real_message(msg):
                last_reason = "message_deleted_after_send"
                log.warning(
                    "Проверочное сообщение исчезло из %s (attempt %d/3)",
                    chat.get("id"), attempt,
                )
                continue
 
            link = _make_message_link(str(chat.get("id")), last_message_id)
            return True, "ok", last_message_id, link
        except FloodWaitError as e:
            wait = min(e.seconds, 60)
            last_reason = f"flood_wait_{e.seconds}s"
            await asyncio.sleep(wait)
        except Exception as e:
            last_reason = str(e)[:120]
 
    return False, last_reason, last_message_id, None
 
 
async def _verify_chats_by_real_message(
    client,
    chats: list[dict],
    message_text: str,
    entities_json: list[dict],
) -> tuple[list[dict], list[dict]]:
    verified: list[dict] = []
    failed: list[dict] = []
 
    for chat in chats:
        ok, reason, message_id, link = await _send_and_confirm_check_message(
            client, chat, message_text, entities_json
        )
        item = dict(chat)
        item["reason"] = reason
        item["message_id"] = message_id
        item["message_link"] = link
        if ok:
            verified.append(item)
        else:
            item["can_write"] = False
            failed.append(item)
 
    return verified, failed
 
 
async def _join_secondary_accounts(task_id: int, primary_account_id: int, final_chats: list[dict]):
    """Join assigned chats for every system account except the one that already checked access."""
    chat_map = {_normalize_chat_id(str(c["id"])): c for c in final_chats}
 
    async with SessionLocal() as db:
        result = await db.execute(
            select(TaskAccount).where(TaskAccount.task_id == task_id)
        )
        task_accounts = list(result.scalars().all())
 
    for ta in task_accounts:
        if ta.account_id == primary_account_id:
            continue
 
        try:
            chat_ids = json.loads(ta.chat_ids or "[]")
        except Exception:
            continue
 
        ta_chats = [chat_map[cid] for cid in chat_ids if cid in chat_map]
        if not ta_chats:
            continue
 
        async with SessionLocal() as db:
            result = await db.execute(select(Account).where(Account.id == ta.account_id))
            acc = result.scalar_one_or_none()
 
        if not acc:
            continue
 
        client2 = account_service.make_client(acc)
        try:
            await client2.connect()
            await account_service.check_and_join_chats(client2, ta_chats)
            log.info("Задача %d: вступил в %d чатов для аккаунта %s", task_id, len(ta_chats), acc.phone)
        except Exception as e:
            log.warning("Задача %d: не удалось вступить в чаты для аккаунта %s: %s", task_id, acc.phone, e)
        finally:
            try:
                await client2.disconnect()
            except Exception:
                pass
 
 
@router.callback_query(F.data == "tasks:confirm_chats")
async def confirm_chats(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    data  = await state.get_data()
    chats = data.get("chats", [])
    if not chats:
        await query.answer("❌ Чаты не найдены.", show_alert=True)
        return
 
    sender_account_id = data.get("sender_account_id")
 
    check_account = None
    if sender_account_id is not None:
        check_account = await account_service.get_account_by_id(db, sender_account_id)
    else:
        accounts = await account_service.get_accounts(db)
        if accounts:
            check_account = accounts[0]
        if not check_account:
            accounts = await account_service.get_accounts(db, owner_id=user.id)
            if accounts:
                check_account = accounts[0]
 
    if check_account is None:
        await state.clear()
        await query.message.answer(
            "❌ Нет доступных аккаунтов для проверки.\nДобавьте аккаунт в /accounts",
            reply_markup=kb_back_to_menu(),
        )
        return
 
    from_folder = any(c.get("folder_slug") for c in chats)
    if from_folder:
        await query.message.answer(
            f"🔍 Вступаю в {len(chats)} чатов из папки и проверяю доступ...\n"
            f"Обычно занимает меньше минуты.",
        )
    else:
        await query.message.answer(
            f"🔍 Проверяю доступ к {len(chats)} чатам...\n"
            f"Это может занять несколько минут.",
        )
 
    client = account_service.make_client(check_account)
    try:
        await client.connect()
        await client.get_dialogs()
        results = await account_service.check_and_join_chats(client, chats)
 
        accessible   = [r for r in results if r["can_write"]]
        inaccessible = [r for r in results if not r["can_write"]]
 
        # ── Обход ограничений для чатов, которые ЛЁГКАЯ проверка сочла недоступными ──
        # Это самый частый случай капчи/квиза: GetParticipantRequest ещё до всякой
        # реальной отправки говорит "нельзя писать" (Telegram буквально не пускает,
        # пока не пройдёшь квиз/не подпишешься). Пробуем решить это тем же
        # аккаунтом, который выполнял проверку, и, если получилось, повторяем
        # ту же лёгкую проверку доступа именно для этих чатов.
        if inaccessible:
            from services.restriction_service import try_bypass_restriction
 
            still_inaccessible: list[dict] = []
            recovered_originals: list[dict] = []
 
            for r in inaccessible:
                bypassed = await try_bypass_restriction(check_account, str(r["id"]))
                if bypassed:
                    original = next(
                        (c for c in chats if str(c["id"]) == str(r["id"])), None
                    )
                    if original:
                        recovered_originals.append(original)
                        continue
                still_inaccessible.append(r)
 
            if recovered_originals:
                recheck_results = await account_service.check_and_join_chats(
                    client, recovered_originals
                )
                for rr in recheck_results:
                    if rr["can_write"]:
                        accessible.append(rr)
                    else:
                        still_inaccessible.append(rr)
 
            inaccessible = still_inaccessible
 
        def _fmt_chat(r: dict) -> str:
            uname = r.get("username")
            title = r.get("title") or (f"@{uname}" if uname else "—")
            link  = r.get("message_link") or (f"https://t.me/{uname}" if uname else r.get("link"))
            if link:
                return f'<a href="{link}">{html.escape(title)}</a>'
            return html.escape(title)
 
        if not accessible:
            await state.clear()
            lines = []
            for r in inaccessible[:20]:
                lines.append(f"• {_fmt_chat(r)} — {html.escape(_reason_label(r['reason']))}")
            await query.message.answer(
                f"❌ <b>Аккаунт не может писать ни в один чат.</b>\n\n"
                + "\n".join(lines),
                reply_markup=kb_access_error(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
 
        await query.message.answer(
            f"📨 Отправляю проверочное сообщение в {len(accessible)} чатов и жду 5 секунд, "
            f"чтобы убедиться, что оно осталось в чате..."
        )
 
        verified, live_failed = await _verify_chats_by_real_message(
            client,
            accessible,
            data.get("message", ""),
            data.get("format_entities", []),
        )
 
        # ── Обход ограничений для чатов, где проверочное сообщение не удержалось ──
        # Пробуем тем же аккаунтом, который проверял доступ: решить капчу/квиз
        # от бота-админа в ЛС и/или вступить в требуемые каналы, затем ещё раз
        # реально проверить доставку в эти же чаты.
        if live_failed:
            from services.restriction_service import try_bypass_restriction
 
            recovered_chats, still_failed = [], []
            for r in live_failed:
                bypassed = await try_bypass_restriction(check_account, str(r["id"]))
                if bypassed:
                    recovered_chats.append(r)
                else:
                    still_failed.append(r)
 
            if recovered_chats:
                re_verified, re_failed = await _verify_chats_by_real_message(
                    client,
                    recovered_chats,
                    data.get("message", ""),
                    data.get("format_entities", []),
                )
                verified.extend(re_verified)
                still_failed.extend(re_failed)
 
            live_failed = still_failed
 
        inaccessible.extend(live_failed)
 
    except Exception as e:
        log.error("Ошибка при проверке чатов: %s", e)
        await query.message.answer(
            "❌ Не удалось проверить доступ — аккаунт не отвечает "
            "(возможна заморозка/спамблок). Попробуйте позже или "
            "выберите другой аккаунт.",
            reply_markup=kb_back_to_menu(),
        )
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
 
    if not verified:
        await state.clear()
        lines = []
        for r in inaccessible[:20]:
            lines.append(f"• {_fmt_chat(r)} — {html.escape(_reason_label(r['reason']))}")
        await query.message.answer(
            f"❌ <b>Аккаунт не может писать в выбранные чаты.</b>\n\n"
            f"Проверочное сообщение не осталось ни в одном чате, поэтому задача не создана.\n\n"
            + "\n".join(lines),
            reply_markup=kb_access_error(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
 
    final_chats = [
        {"id": r["id"], "title": r.get("title", ""), "username": r.get("username")}
        for r in verified
    ]
 
    # ── Собираем байты фото из FSM ────────────────────────────────────────────
    photos_b64: list[str] = data.get("photo_bytes_b64", [])
    photos_bytes: list[bytes] = [_unb64(b) for b in photos_b64 if b]
 
    await state.clear()
 
    task = await task_service.create_task(
        db, user,
        name=data["name"],
        message=data.get("message", ""),
        interval_minutes=data["interval"],
        chats=final_chats,
        preferred_account_id=sender_account_id or check_account.id,
        format_entities=data.get("format_entities", []),
        photo_bytes_list=photos_bytes,   # <-- передаём байты напрямую
    )
 
    if not task:
        await query.message.answer(
            "❌ Не удалось создать задачу. Возможно превышен лимит чатов.",
            reply_markup=kb_back_to_menu(),
        )
        return
 
    # Задачу назначаем на тот же аккаунт, который реально отправил
    # и подтвердил проверочные сообщения перед созданием.
 
    if inaccessible:
        lines = []
        for r in inaccessible[:20]:
            lines.append(f"• {_fmt_chat(r)} — {html.escape(_reason_label(r['reason']))}")
        if len(inaccessible) > 20:
            lines.append(f"…и ещё {len(inaccessible) - 20}")
        await query.message.answer(
            f"⚠️ <b>Задача создана частично</b>\n\n"
            f"✅ Подтверждено сообщением: <b>{len(verified)}</b> из <b>{len(results)}</b>\n\n"
            f"❌ Недоступные:\n" + "\n".join(lines) + "\n\n"
            f"📋 {html.escape(task['name'])}\n"
            f"📬 Чатов: {task['chats_count']}\n"
            f"⏱ Каждые {task['interval_minutes']} мин.",
            reply_markup=kb_back_to_menu(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
 
    preview_lines = [f"• {_fmt_chat(r)}" for r in verified[:10]]
    if len(verified) > 10:
        preview_lines.append(f"…и ещё {len(verified) - 10}")
 
    media_note = " 📷 фото сохранено" if photos_bytes else ""
 
    await query.message.answer(
        f"✅ <b>Задача создана!</b>{media_note}\n\n"
        f"📋 {html.escape(task['name'])}\n"
        f"📬 Чатов: {task['chats_count']}\n"
        f"⏱ Каждые {task['interval_minutes']} мин.\n\n"
        f"🏷 <b>Чаты рассылки:</b>\n" + "\n".join(preview_lines),
        reply_markup=kb_back_to_menu(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    log.info("Создана задача %d для user %d (фото: %d)", task["id"], user.id, len(photos_bytes))
# ── Вспомогательные функции ───────────────────────────────────────────────────
 
import base64
 
def _b64(data: bytes) -> str:
    """bytes → base64-строка для хранения в FSM."""
    return base64.b64encode(data).decode()
 
 
def _unb64(s: str) -> bytes:
    """base64-строка → bytes."""
    return base64.b64decode(s)
 
 
def _entities_to_json(entities) -> list[dict[str, Any]]:
    if not entities:
        return []
    out = []
    for e in entities:
        d = {"type": e.type, "offset": e.offset, "length": e.length}
        url = getattr(e, "url", None)
        if url:
            d["url"] = url
        out.append(d)
    return out
 
 
def _extract_text_and_entities(msg: Message) -> tuple[str, list[dict[str, Any]]]:
    if msg.caption is not None:
        return msg.caption, _entities_to_json(msg.caption_entities)
    return msg.text or "", _entities_to_json(msg.entities)
 
 
def _reason_label(reason: str) -> str:
    labels = {
        "private":              "приватный чат",
        "invite_expired":       "invite-ссылка устарела",
        "banned":               "аккаунт заблокирован",
        "write_forbidden":      "нет прав писать",
        "too_many_channels":    "слишком много чатов",
        "join_pending":         "заявка отправлена",
        "not_found":            "чат не найден",
        "invalid_id":           "неверный ID",
        "entity_not_found":     "чат не найден",
        "message_id_missing":   "не получили ID сообщения",
        "message_not_found_after_send": "сообщение не найдено после отправки",
        "message_deleted_after_send":   "сообщение удалилось после отправки",
        "discussion_no_parent": "нужно вступить в канал вручную",
    }
    if reason.startswith("entity_not_found"):
        return "чат не найден"
    if reason.startswith("flood_wait"):
        return "Telegram попросил подождать"
    reason_low = reason.lower()
    if "forbidden" in reason_low or "can't write" in reason_low:
        return "нет прав писать"
    if "banned" in reason_low:
        return "аккаунт заблокирован"
    return labels.get(reason, reason)

