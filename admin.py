"""
bot/handlers/admin.py — панель администратора.

ИЗМЕНЕНИЯ:
  - Управление прокси для системных аккаунтов (/proxy, FSM)
  - Отображение нагрузки (sends_last_hour) в списке системных аккаунтов
  - Статистика пула клиентов воркера
"""
import logging
from datetime import datetime, timezone
from services import account_service, payment_bot_service, task_service
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select, func, Integer
from sqlalchemy.ext.asyncio import AsyncSession

from config import OWNER_ID, BOT_TOKEN, GENERATOR_BOT_TOKEN
from models import User, Account, Task, Payment
from services import account_service, payment_bot_service
from services.user_service import (
    get_user, get_all_users, block_user, unblock_user,
    set_max_chats, add_subscription, count_active_users,
)
from bot.keyboards import (
    kb_admin_menu, kb_cancel, kb_back_to_menu,
    kb_paybot_menu, kb_paybot_history, kb_paybot_detail,
)

log = logging.getLogger(__name__)
router = Router()


def is_admin(user: User) -> bool:
    return user.is_admin or user.id == OWNER_ID


# ── FSM ───────────────────────────────────────────────────────────────────────

class BroadcastState(StatesGroup):
    message = State()
    confirm = State()


class AddSystemAccount(StatesGroup):
    api_id   = State()
    api_hash = State()
    phone    = State()
    code     = State()
    password = State()


class SetProxyState(StatesGroup):
    waiting = State()


class SetPaymentBotState(StatesGroup):
    token = State()


# ── Вход в панель ─────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message, user: User):
    if not is_admin(user):
        await message.answer("❌ Нет доступа.")
        return
    await message.answer("👑 *Панель администратора*", reply_markup=kb_admin_menu(), parse_mode="Markdown")


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(query: CallbackQuery, user: User):
    if not is_admin(user):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.message.edit_text("👑 *Панель администратора*", reply_markup=kb_admin_menu(), parse_mode="Markdown")


# ── Статистика ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats")
async def admin_stats(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return

    now = datetime.now(timezone.utc)

    total_users  = (await db.execute(select(func.count(User.id)))).scalar()
    active_users = await count_active_users(db)
    total_tasks  = (await db.execute(select(func.count(Task.id)).where(Task.is_active == True))).scalar()
    total_accs   = (await db.execute(select(func.count(Account.id)).where(Account.is_active == True))).scalar()

    result = await db.execute(
        select(User).where(User.sub_ends_at > now).order_by(User.sub_ends_at.asc())
    )
    paid_users = result.scalars().all()

    result = await db.execute(
        select(User).where(
            User.trial_ends_at > now,
            (User.sub_ends_at == None) | (User.sub_ends_at <= now),
        )
    )
    trial_users = result.scalars().all()

    result = await db.execute(
        select(Payment).where(Payment.status == "paid")
        .order_by(Payment.paid_at.desc()).limit(10)
    )
    recent_payments = result.scalars().all()

    subs_lines = []
    for u in paid_users[:20]:
        days_left = (u.sub_ends_at - now).days
        uname = f"@{u.username}" if u.username else f"`{u.id}`"
        subs_lines.append(f"• {uname} — {days_left} дн.")

    trial_lines = []
    for u in trial_users[:10]:
        hours_left = int((u.trial_ends_at - now).total_seconds() / 3600)
        uname = f"@{u.username}" if u.username else f"`{u.id}`"
        trial_lines.append(f"• {uname} — {hours_left} ч.")

    plan_names = {"1month": "1 мес", "3month": "3 мес", "6month": "6 мес", "1week": "1 нед"}
    pay_lines = []
    for p in recent_payments:
        date_str  = p.paid_at.strftime("%d.%m %H:%M") if p.paid_at else "—"
        plan_lbl  = plan_names.get(p.plan, p.plan)
        pay_lines.append(f"• `{p.user_id}` — {plan_lbl}, {int(p.amount)}⭐ ({date_str})")

    subs_block  = "\n".join(subs_lines)  or "  (нет)"
    trial_block = "\n".join(trial_lines) or "  (нет)"
    pay_block   = "\n".join(pay_lines)   or "  (нет)"

    text = (
        f"📊 *Статистика сервиса*\n\n"
        f"👥 Всего пользователей: *{total_users}*\n"
        f"👤 С активными задачами: *{active_users}*\n"
        f"📋 Активных задач: *{total_tasks}*\n"
        f"🤖 Активных аккаунтов: *{total_accs}*\n\n"
        f"✅ *Активные подписки ({len(paid_users)}):*\n{subs_block}\n\n"
        f"🎁 *На триале ({len(trial_users)}):*\n{trial_block}\n\n"
        f"💰 *Последние оплаты:*\n{pay_block}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin:stats:users")],
        [InlineKeyboardButton(text="🔄 Обновить",         callback_data="admin:stats")],
        [InlineKeyboardButton(text="◀️ Назад",            callback_data="admin:menu")],
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "admin:stats:users")
async def admin_stats_users(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(User).order_by(User.created_at.desc()).limit(50)
    )
    users = result.scalars().all()

    lines = []
    for u in users:
        if u.sub_ends_at and u.sub_ends_at > now:
            days = (u.sub_ends_at - now).days
            status = f"✅ {days}д"
        elif u.trial_ends_at and u.trial_ends_at > now:
            hours = int((u.trial_ends_at - now).total_seconds() / 3600)
            status = f"🎁 {hours}ч"
        else:
            status = "❌"
        blocked = " 🚫" if u.is_blocked else ""
        uname   = f"@{u.username}" if u.username else u.full_name or str(u.id)
        lines.append(f"`{u.id}` {uname} — {status}{blocked}")

    text = "👥 *Пользователи (последние 50):*\n\n" + "\n".join(lines)
    kb   = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin:stats")
    ]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


# ── Пользователи ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:users")
async def admin_users(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    text = (
        "👥 *Управление пользователями*\n\n"
        "Команды:\n"
        "`/giveday <user_id> <days>` — выдать подписку\n"
        "`/block <user_id>` — заблокировать\n"
        "`/unblock <user_id>` — разблокировать\n"
        "`/setlimit <user_id> <chats>` — лимит чатов\n"
        "`/userinfo <user_id>` — инфо о пользователе\n"
        "`/broadcast` — рассылка всем"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")
    ]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.message(Command("giveday"))
async def cmd_giveday(message: Message, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("Использование: `/giveday <user_id> <days>`", parse_mode="Markdown")
        return
    target_id, days = int(args[1]), int(args[2])
    target = await get_user(db, target_id)
    if not target:
        await message.answer(f"❌ Пользователь `{target_id}` не найден.", parse_mode="Markdown")
        return
    await add_subscription(db, target, days)
    await message.answer(f"✅ Пользователю `{target_id}` выдано *{days} дней* подписки.", parse_mode="Markdown")
    try:
        await message.bot.send_message(target_id, f"🎉 Вам выдана подписка на *{days} дней*!", parse_mode="Markdown")
    except Exception:
        pass


@router.message(Command("block"))
async def cmd_block(message: Message, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: `/block <user_id>`", parse_mode="Markdown")
        return
    ok = await block_user(db, int(args[1]))
    await message.answer("✅ Заблокирован." if ok else "❌ Не найден.", parse_mode="Markdown")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: `/unblock <user_id>`", parse_mode="Markdown")
        return
    ok = await unblock_user(db, int(args[1]))
    await message.answer("✅ Разблокирован." if ok else "❌ Не найден.", parse_mode="Markdown")


@router.message(Command("setlimit"))
async def cmd_setlimit(message: Message, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("Использование: `/setlimit <user_id> <chats>`", parse_mode="Markdown")
        return
    ok = await set_max_chats(db, int(args[1]), int(args[2]))
    await message.answer("✅ Лимит установлен." if ok else "❌ Не найден.", parse_mode="Markdown")


@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: `/userinfo <user_id>`", parse_mode="Markdown")
        return
    target = await get_user(db, int(args[1]))
    if not target:
        await message.answer("❌ Не найден.", parse_mode="Markdown")
        return
    text = (
        f"👤 *Пользователь {target.id}*\n"
        f"Имя: {target.full_name}\n"
        f"Username: @{target.username or '—'}\n"
        f"Статус: {target.subscription_status}\n"
        f"Заблокирован: {'Да' if target.is_blocked else 'Нет'}\n"
        f"Лимит чатов: {target.max_chats}\n"
        f"Задач: {len(target.tasks)}\n"
        f"Аккаунтов: {len(target.accounts)}\n"
        f"Регистрация: {target.created_at.strftime('%Y-%m-%d')}"
    )
    await message.answer(text, parse_mode="Markdown")


# ── Системные аккаунты ────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:accounts")
async def admin_accounts(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return

    result = await db.execute(
        select(Account).where(Account.is_system == True).order_by(Account.id)
    )
    accounts = result.scalars().all()

    from bot.keyboards import kb_admin_system_accounts
    text = "🤖 *Системные аккаунты*\n\nНажмите 🗑 чтобы удалить аккаунт и все его задачи."
    await query.message.edit_text(
        text,
        reply_markup=kb_admin_system_accounts(accounts),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("admin:delacc:"))
async def admin_delacc_ask(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    acc_id = int(query.data.split(":")[-1])
    acc = await account_service.get_account_by_id(db, acc_id)
    if not acc:
        await query.answer("Аккаунт не найден.", show_alert=True)
        return

    task_ids = await account_service.get_task_ids_for_account(db, acc_id)

    from bot.keyboards import kb_admin_delacc_confirm
    await query.message.edit_text(
        f"⚠️ *Удалить системный аккаунт {acc.phone}?*\n\n"
        f"Будет удалено задач (полностью, вместе с чатами и логами): *{len(task_ids)}*\n\n"
        f"Это действие нельзя отменить.",
        reply_markup=kb_admin_delacc_confirm(acc_id, len(task_ids)),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("admin:delacc_confirm:"))
async def admin_delacc_confirm(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    acc_id = int(query.data.split(":")[-1])

    ok, deleted_tasks = await account_service.delete_system_account_cascade(db, acc_id)

    if ok:
        await query.answer(f"✅ Удалён. Задач удалено: {deleted_tasks}", show_alert=True)
    else:
        await query.answer("❌ Аккаунт не найден.", show_alert=True)

    await admin_accounts(query, user, db)

# ── Управление прокси ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:proxies")
async def admin_proxies(query: CallbackQuery, user: User, db: AsyncSession):
    """Список системных аккаунтов с возможностью назначить прокси."""
    if not is_admin(user):
        return

    result = await db.execute(
        select(Account).where(Account.is_system == True).order_by(Account.id)
    )
    accounts = result.scalars().all()

    buttons = []
    for acc in accounts:
        proxy_label = f"🌐 {acc.proxy_host}:{acc.proxy_port}" if acc.proxy_host else "нет прокси"
        buttons.append([InlineKeyboardButton(
            text=f"{acc.status_icon} {acc.phone} — {proxy_label}",
            callback_data=f"admin:proxy:acc:{acc.id}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")])

    await query.message.edit_text(
        "🌐 *Прокси системных аккаунтов*\n\n"
        "Нажмите на аккаунт для изменения прокси.\n"
        "Рекомендуется: 3-5 аккаунтов на один прокси.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("admin:proxy:acc:"))
async def proxy_account_menu(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    acc_id = int(query.data.split(":")[-1])
    acc    = await account_service.get_account_by_id(db, acc_id)
    if not acc:
        await query.answer("Аккаунт не найден.", show_alert=True)
        return

    current = (
        f"`{acc.proxy_type or 'socks5'}://{acc.proxy_host}:{acc.proxy_port}`"
        if acc.proxy_host else "не задан"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Установить прокси",  callback_data=f"admin:proxy:set:{acc_id}")],
        [InlineKeyboardButton(text="🗑 Убрать прокси",       callback_data=f"admin:proxy:clear:{acc_id}")],
        [InlineKeyboardButton(text="◀️ Назад",               callback_data="admin:proxies")],
    ])
    await query.message.edit_text(
        f"🌐 *Прокси для {acc.phone}*\n\n"
        f"Текущий прокси: {current}\n\n"
        f"Нагрузка: {acc.sends_last_hour} отправок/ч\n"
        f"Чатов: {acc.chats_count}",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("admin:proxy:set:"))
async def proxy_set_start(query: CallbackQuery, state: FSMContext, user: User):
    if not is_admin(user):
        return
    acc_id = int(query.data.split(":")[-1])
    await state.update_data(proxy_acc_id=acc_id)
    await query.message.edit_text(
        "✏️ *Введите прокси в формате:*\n\n"
        "`socks5://user:pass@host:port`\n"
        "или\n"
        "`socks5://host:port`\n"
        "или\n"
        "`http://host:port`\n\n"
        "Примеры:\n"
        "`socks5://login:secret@123.45.67.89:1080`\n"
        "`socks5://10.0.0.1:1080`",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
    )
    await state.set_state(SetProxyState.waiting)


@router.message(SetProxyState.waiting)
async def proxy_set_got(message: Message, state: FSMContext, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    data   = await state.get_data()
    acc_id = data.get("proxy_acc_id")
    raw    = (message.text or "").strip()

    # Парсим строку вида socks5://user:pass@host:port или socks5://host:port
    try:
        proxy_type, rest = raw.split("://", 1)
        proxy_type = proxy_type.lower()
        if proxy_type not in ("socks5", "http"):
            raise ValueError("Тип должен быть socks5 или http")

        proxy_user = proxy_pass = None
        if "@" in rest:
            creds, hostport = rest.rsplit("@", 1)
            if ":" in creds:
                proxy_user, proxy_pass = creds.split(":", 1)
            else:
                proxy_user = creds
        else:
            hostport = rest

        host, port_str = hostport.rsplit(":", 1)
        proxy_port = int(port_str)
        if not host or proxy_port <= 0:
            raise ValueError("Неверный host или port")

    except Exception as e:
        await message.answer(
            f"❌ Неверный формат прокси: {e}\n\n"
            "Пример: `socks5://user:pass@host:1080`",
            parse_mode="Markdown",
        )
        return

    ok = await account_service.set_proxy(
        db, acc_id,
        proxy_host=host,
        proxy_port=proxy_port,
        proxy_type=proxy_type,
        proxy_user=proxy_user,
        proxy_pass=proxy_pass,
    )
    await state.clear()

    if ok:
        # Сбросить клиент в пуле воркера чтобы переподключился через новый прокси
        try:
            from worker.worker import remove_client
            await remove_client(acc_id)
        except Exception:
            pass

        await message.answer(
            f"✅ Прокси установлен: `{proxy_type}://{host}:{proxy_port}`\n\n"
            "Аккаунт переподключится автоматически.",
            reply_markup=kb_back_to_menu(),
            parse_mode="Markdown",
        )
    else:
        await message.answer("❌ Аккаунт не найден.", reply_markup=kb_back_to_menu())


@router.callback_query(F.data.startswith("admin:proxy:clear:"))
async def proxy_clear(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    acc_id = int(query.data.split(":")[-1])
    await account_service.set_proxy(db, acc_id, None, None, None, None, None)

    try:
        from worker.worker import remove_client
        await remove_client(acc_id)
    except Exception:
        pass

    await query.answer("✅ Прокси убран.")
    await admin_proxies(query, user, db)


# ── Платёжный бот (Stars) ─────────────────────────────────────────────────────
#
# Stars всегда падают на баланс того бота, который выставил счёт — поэтому
# мы используем отдельных "расходных" ботов, которых можно менять в любой
# момент без перезапуска сервиса. payment_bot_runner.py подхватывает новый
# бот (и продолжает держать живым старый, если он ещё в БД) в течение
# ~10 секунд после смены здесь.

@router.callback_query(F.data == "admin:paybot")
async def admin_paybot_menu(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return

    active = await payment_bot_service.get_active_bot(db)

    if active:
        text = (
            f"💳 *Платёжный бот для Stars*\n\n"
            f"Активен: @{active.bot_username}\n"
            f"Подключён: {active.created_at.strftime('%d.%m %H:%M')}\n"
            f"Принято оплат: *{active.payments_count}*\n\n"
            f"Кнопка «Оплатить ⭐» во всех ваших ботах и зеркалах ведёт сюда."
        )
    else:
        text = (
            "💳 *Платёжный бот для Stars*\n\n"
            "❌ Не настроен — кнопка оплаты звёздами сейчас не показывается "
            "пользователям (только «купить у администратора»).\n\n"
            "Нажмите «Сменить бота», чтобы подключить."
        )

    await query.message.edit_text(text, reply_markup=kb_paybot_menu(bool(active)), parse_mode="Markdown")


@router.callback_query(F.data == "admin:paybot:set")
async def admin_paybot_set_start(query: CallbackQuery, state: FSMContext, user: User):
    if not is_admin(user):
        return
    await query.message.edit_text(
        "🔄 *Смена платёжного бота*\n\n"
        "Создайте нового бота у @BotFather (или возьмите уже готового —\n"
        "специальных требований к нему нет, кроме включённых платежей)\n"
        "и пришлите его токен сюда.\n\n"
        "⚠️ Не используйте токен основного бота или зеркал — это вызовет "
        "конфликт polling'а.\n\n"
        "Через ~10 секунд все новые кнопки «Оплатить ⭐» переключатся "
        "на этого бота.",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
    )
    await state.set_state(SetPaymentBotState.token)


@router.message(SetPaymentBotState.token)
async def admin_paybot_set_got(message: Message, state: FSMContext, user: User, db: AsyncSession):
    if not is_admin(user):
        return

    token = (message.text or "").strip()
    await state.clear()

    if token in (BOT_TOKEN, GENERATOR_BOT_TOKEN):
        await message.answer(
            "❌ Нельзя использовать токен основного или генератор-бота — "
            "два процесса не могут одновременно делать polling одним токеном.",
            reply_markup=kb_back_to_menu(),
        )
        return

    await message.answer("🔍 Проверяю токен через Telegram...")

    try:
        bot_row = await payment_bot_service.set_active_bot(db, token)
    except payment_bot_service.InvalidTokenError as e:
        await message.answer(
            f"❌ Токен не прошёл проверку: `{e}`\n\nПопробуйте снова /admin → Платёжный бот.",
            reply_markup=kb_back_to_menu(),
            parse_mode="Markdown",
        )
        return

    await message.answer(
        f"✅ Платёжный бот сменён на *@{bot_row.bot_username}*\n\n"
        f"Если бот новый — запуск polling займёт до ~10 секунд.\n"
        f"Все новые кнопки «Оплатить ⭐» поведут именно туда.",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown",
    )
    log.info("Админ %d сменил платёжный бот на @%s", user.id, bot_row.bot_username)


@router.callback_query(F.data == "admin:paybot:deactivate")
async def admin_paybot_deactivate(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    await payment_bot_service.deactivate_all(db)
    await query.answer("⏸ Оплата Stars деактивирована.")
    await admin_paybot_menu(query, user, db)


@router.callback_query(F.data == "admin:paybot:history")
async def admin_paybot_history(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    bots = await payment_bot_service.get_all_bots(db)
    text = "📜 *История платёжных ботов*" if bots else "📜 Пока нет ни одного добавленного бота."
    await query.message.edit_text(text, reply_markup=kb_paybot_history(bots), parse_mode="Markdown")


@router.callback_query(F.data.startswith("admin:paybot:view:"))
async def admin_paybot_view(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    bot_id = int(query.data.split(":")[-1])
    bot_row = await payment_bot_service.get_bot_by_id(db, bot_id)
    if not bot_row:
        await query.answer("Не найден.", show_alert=True)
        return

    text = (
        f"🤖 *@{bot_row.bot_username}*\n\n"
        f"Статус: {'✅ Активен' if bot_row.is_active else '⏸ Не активен (но всё ещё принимает уже выданные счета)'}\n"
        f"Подключён: {bot_row.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Принято оплат: *{bot_row.payments_count}*"
    )
    await query.message.edit_text(text, reply_markup=kb_paybot_detail(bot_row), parse_mode="Markdown")


@router.callback_query(F.data.startswith("admin:paybot:activate:"))
async def admin_paybot_activate(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    bot_id = int(query.data.split(":")[-1])
    ok = await payment_bot_service.activate_existing(db, bot_id)
    await query.answer("✅ Активирован." if ok else "❌ Не найден.")
    await admin_paybot_history(query, user, db)


@router.callback_query(F.data.startswith("admin:paybot:delete:"))
async def admin_paybot_delete(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    bot_id = int(query.data.split(":")[-1])
    await payment_bot_service.delete_bot(db, bot_id)
    await query.answer("🗑 Удалён — polling остановится в течение ~10 секунд.")
    await admin_paybot_history(query, user, db)


# ── Добавление системного аккаунта ────────────────────────────────────────────

@router.callback_query(F.data == "admin:addacc")
async def admin_start_add_acc(query: CallbackQuery, state: FSMContext, user: User):
    if not is_admin(user):
        return
    await query.message.edit_text(
        "➕ *Добавление системного аккаунта*\n\n"
        "Этот аккаунт будет доступен всем пользователям.\n\n"
        "*Шаг 1/3* — Введите API\\_ID:",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
    )
    await state.set_state(AddSystemAccount.api_id)


@router.message(AddSystemAccount.api_id)
async def admin_got_apiid(message: Message, state: FSMContext, user: User):
    if not is_admin(user):
        return
    if not message.text.strip().isdigit():
        await message.answer("❌ Должно быть числом:")
        return
    await state.update_data(api_id=int(message.text.strip()))
    await message.answer("*Шаг 2/3* — Введите API\\_HASH:", reply_markup=kb_cancel(), parse_mode="Markdown")
    await state.set_state(AddSystemAccount.api_hash)


@router.message(AddSystemAccount.api_hash)
async def admin_got_apihash(message: Message, state: FSMContext, user: User):
    if not is_admin(user):
        return
    await state.update_data(api_hash=message.text.strip())
    await message.answer(
        "*Шаг 3/3* — Введите номер телефона:\nПример: `+998901234567`",
        reply_markup=kb_cancel(), parse_mode="Markdown",
    )
    await state.set_state(AddSystemAccount.phone)


@router.message(AddSystemAccount.phone)
async def admin_got_phone(message: Message, state: FSMContext, user: User):
    if not is_admin(user):
        return
    phone = message.text.strip()
    data  = await state.get_data()
    await message.answer(f"📨 Отправляю код на {phone}...")
    try:
        client, phone_code_hash = await account_service.send_code(data["api_id"], data["api_hash"], phone)
        await state.update_data(phone=phone, phone_code_hash=phone_code_hash)
        message.bot._pending_clients = getattr(message.bot, "_pending_clients", {})
        message.bot._pending_clients[message.from_user.id] = client
        await message.answer("✅ Код отправлен!\n\nВведите код из Telegram:", reply_markup=kb_cancel())
        await state.set_state(AddSystemAccount.code)
    except Exception as e:
        await message.answer(f"❌ Ошибка: `{e}`", parse_mode="Markdown")
        await state.clear()


@router.message(AddSystemAccount.code)
async def admin_got_code(message: Message, state: FSMContext, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    from telethon.errors import SessionPasswordNeededError
    code   = message.text.strip().replace(" ", "")
    data   = await state.get_data()
    client = getattr(message.bot, "_pending_clients", {}).get(message.from_user.id)
    if not client:
        await message.answer("❌ Сессия истекла. Начните заново.")
        await state.clear()
        return
    try:
        session_str = await account_service.sign_in_code(client, data["phone"], code, data["phone_code_hash"])
    except SessionPasswordNeededError:
        await message.answer("🔐 Введите пароль 2FA:", reply_markup=kb_cancel())
        await state.set_state(AddSystemAccount.password)
        return
    except Exception as e:
        await message.answer(f"❌ Неверный код: `{e}`", parse_mode="Markdown")
        await client.disconnect()
        message.bot._pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return
    await _finish_system_account(message, state, user, db, client, data["phone"], session_str)


@router.message(AddSystemAccount.password)
async def admin_got_password(message: Message, state: FSMContext, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    client = getattr(message.bot, "_pending_clients", {}).get(message.from_user.id)
    data   = await state.get_data()
    try:
        session_str = await account_service.sign_in_2fa(client, message.text.strip())
    except Exception as e:
        await message.answer(f"❌ Неверный пароль: `{e}`", parse_mode="Markdown")
        await client.disconnect()
        message.bot._pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return
    await _finish_system_account(message, state, user, db, client, data["phone"], session_str)


async def _finish_system_account(message, state, user, db, client, phone, session_str):
    data = await state.get_data()
    name = await account_service.get_me_name(client)
    await client.disconnect()
    message.bot._pending_clients.pop(message.from_user.id, None)
    await state.clear()

    acc = await account_service.create_account(
        db, api_id=data["api_id"], api_hash=data["api_hash"],
        phone=phone, session_string=session_str,
        owner_id=None, is_system=True,
    )
    await message.answer(
        f"✅ Системный аккаунт *{name}* (`{phone}`) добавлен!\n\n"
        f"Воркер автоматически подключит его через 30 секунд.\n\n"
        f"💡 Не забудьте назначить прокси: /admin → Аккаунты → Прокси",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown",
    )


# ── Рассылка всем пользователям ───────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast(query: CallbackQuery, state: FSMContext, user: User):
    if not is_admin(user):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.message.edit_text(
        "📢 *Рассылка всем пользователям*\n\n"
        "Напишите сообщение (текст, фото с подписью, Markdown).",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
    )
    await state.set_state(BroadcastState.message)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, user: User):
    if not is_admin(user):
        return
    await message.answer(
        "📢 *Рассылка всем пользователям*\n\nНапишите сообщение:",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
    )
    await state.set_state(BroadcastState.message)


@router.message(BroadcastState.message)
async def got_broadcast_message(message: Message, state: FSMContext):
    await state.update_data(
        broadcast_from_chat=message.chat.id,
        broadcast_message_id=message.message_id,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разослать всем", callback_data="admin:broadcast:confirm")],
        [InlineKeyboardButton(text="❌ Отмена",         callback_data="menu:new")],
    ])
    await message.answer(
        "👆 *Сообщение выше будет разослано всем.* Подтвердите:",
        reply_markup=kb, parse_mode="Markdown",
    )
    await state.set_state(BroadcastState.confirm)


@router.callback_query(BroadcastState.confirm, F.data == "admin:broadcast:confirm")
async def confirm_broadcast(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    data      = await state.get_data()
    from_chat = data.get("broadcast_from_chat")
    msg_id    = data.get("broadcast_message_id")
    await state.clear()

    result    = await db.execute(select(User).where(User.is_blocked == False))
    all_users = result.scalars().all()

    await query.message.edit_text(
        f"📢 Начинаю рассылку *{len(all_users)}* пользователям...",
        parse_mode="Markdown",
    )

    sent = failed = 0
    for u in all_users:
        if u.id == user.id:
            continue
        try:
            await query.bot.forward_message(chat_id=u.id, from_chat_id=from_chat, message_id=msg_id)
            sent += 1
        except Exception:
            failed += 1

    await query.message.edit_text(
        f"✅ *Рассылка завершена*\n\n"
        f"✉️ Отправлено: *{sent}*\n"
        f"❌ Ошибок: *{failed}*",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown",
    )
    log.info("Broadcast от %d: sent=%d failed=%d", user.id, sent, failed)

# ═══════════════════════════════════════════════════════════════════════════════
# АДМИН: ЗАДАЧИ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ
# ═══════════════════════════════════════════════════════════════════════════════

ADMIN_LOGS_PER_PAGE = 20


def _admin_make_message_link(chat_id: str, message_id: int | None) -> str | None:
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


@router.callback_query(F.data == "admin:all_tasks")
async def admin_all_tasks(query: CallbackQuery, user: User, db: AsyncSession):
    """Список всех пользователей у которых есть задачи."""
    if not is_admin(user):
        return

    from models import Task
    from sqlalchemy import func
    # Пользователи с задачами, сортировка по кол-ву задач убывающие
    result = await db.execute(
        select(User.id, User.username, User.full_name, func.count(Task.id).label("task_count"))
        .join(Task, Task.user_id == User.id)
        .group_by(User.id, User.username, User.full_name)
        .order_by(func.count(Task.id).desc())
        .limit(50)
    )
    rows = result.all()

    if not rows:
        await query.message.edit_text(
            "📋 Нет ни одной задачи в системе.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")
            ]]),
        )
        return

    buttons = []
    for uid, uname, fname, cnt in rows:
        label = f"@{uname}" if uname else (fname or str(uid))
        buttons.append([InlineKeyboardButton(
            text=f"👤 {label} — {cnt} задач",
            callback_data=f"admin:tasks:user:{uid}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")])

    await query.message.edit_text(
        f"📋 <b>Задачи пользователей</b> (топ-50 по кол-ву):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:tasks:user:"))
async def admin_tasks_of_user(query: CallbackQuery, user: User, db: AsyncSession):
    """Список задач конкретного пользователя (для администратора)."""
    if not is_admin(user):
        return

    from models import Task, Log
    from sqlalchemy import func

    uid = int(query.data.split(":")[-1])
    target = await get_user(db, uid)
    if not target:
        await query.answer("Пользователь не найден.", show_alert=True)
        return

    result = await db.execute(
        select(Task)
        .where(Task.user_id == uid)
        .order_by(Task.created_at.desc())
    )
    tasks = result.scalars().all()

    if not tasks:
        await query.message.edit_text(
            f"У пользователя нет задач.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:all_tasks")
            ]]),
        )
        return

    uname   = f"@{target.username}" if target.username else target.full_name
    buttons = []
    for t in tasks:
        icon   = "▶️" if t.is_active else "⏸"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {t.name}",
            callback_data=f"admin:task:view:{t.id}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:all_tasks")])

    await query.message.edit_text(
        f"📋 <b>Задачи пользователя {uname}</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:task:view:"))
async def admin_task_view(query: CallbackQuery, user: User, db: AsyncSession):
    """Полная карточка задачи для администратора."""
    if not is_admin(user):
        return

    from models import Task, TaskChat, TaskAccount, Log, Account
    from sqlalchemy import func
    from sqlalchemy.orm import selectinload

    task_id = int(query.data.split(":")[-1])

    result = await db.execute(
        select(Task)
        .options(
            selectinload(Task.chats),
            selectinload(Task.accounts).selectinload(TaskAccount.account),
            selectinload(Task.user),
        )
        .where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    # Статистика по аккаунтам
    acc_stats_res = await db.execute(
        select(
            Log.account_id,
            func.count(Log.id).label("total"),
            func.sum(Log.success.cast(Integer)).label("success_cnt"),
        )
        .where(Log.task_id == task_id)
        .group_by(Log.account_id)
    )
    acc_stats = {row.account_id: (row.total, int(row.success_cnt or 0)) for row in acc_stats_res.all()}

    total_sent    = sum(v[1] for v in acc_stats.values())
    total_any     = sum(v[0] for v in acc_stats.values())
    total_failed  = total_any - total_sent

    # Имя пользователя
    u = task.user
    uname = f"@{u.username}" if u.username else u.full_name

    # Чаты (первые 10)
    chats_lines = [f"• {c.chat_title or c.chat_id}" for c in task.chats[:10]]
    if len(task.chats) > 10:
        chats_lines.append(f"…и ещё {len(task.chats) - 10}")

    # Аккаунты с нагрузкой
    acc_lines = []
    for ta in task.accounts:
        import json as _json
        cnt  = len(_json.loads(ta.chat_ids or "[]"))
        acc  = ta.account
        name = acc.phone if acc else f"acc#{ta.account_id}"
        sent_cnt = acc_stats.get(ta.account_id, (0, 0))[1]
        acc_lines.append(f"• {name}: {cnt} чатов, {sent_cnt} отправок")

    # Медиа
    from pathlib import Path
    import os
    media_root  = Path(os.getenv("MEDIA_ROOT", "/app/media"))
    media_dir   = media_root / f"task_{task.id}"
    media_files = sorted(media_dir.glob("photo_*.jpg")) if media_dir.exists() else []

    icon     = "▶️" if task.is_active else "⏸"
    created  = task.created_at.strftime("%d.%m.%Y %H:%M") if task.created_at else "—"
    has_media_note = f"📷 {len(media_files)} фото" if media_files else "📝 без фото"

    text = (
        f"{icon} <b>{task.name}</b>  [ID: {task.id}]\n"
        f"👤 Владелец: {uname} (<code>{task.user_id}</code>)\n"
        f"📅 Создана: {created}\n"
        f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
        f"{has_media_note}\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"  Отправлено: {total_sent}  |  Ошибок: {total_failed}\n\n"
        f"🏷 <b>Чаты ({len(task.chats)}):</b>\n" + "\n".join(chats_lines or ["—"]) + "\n\n"
        f"🤖 <b>Аккаунты:</b>\n" + "\n".join(acc_lines or ["—"]) + "\n\n"
        f"💬 <b>Текст сообщения:</b>\n"
        f"<blockquote expandable>{task.message[:500]}</blockquote>"
    )

    from bot.keyboards import kb_admin_task_detail
    await query.message.edit_text(
        text,
        reply_markup=kb_admin_task_detail(task.id, task.is_active, task.user_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("admin:task:toggle:"))
async def admin_task_toggle(query: CallbackQuery, user: User, db: AsyncSession):
    """Остановить / запустить задачу любого пользователя (только для администратора)."""
    if not is_admin(user):
        return

    from models import Task
    task_id = int(query.data.split(":")[-1])
    result  = await db.execute(select(Task).where(Task.id == task_id))
    task    = result.scalar_one_or_none()
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    task.is_active = not task.is_active
    await db.commit()

    status = "запущена ▶️" if task.is_active else "остановлена ⏸"
    await query.answer(f"Задача {status}")
    # Обновить карточку
    await admin_task_view(query, user, db)


@router.callback_query(F.data.startswith("admin:task:delete:"))
async def admin_task_delete_ask(query: CallbackQuery, user: User, db: AsyncSession):
    """Запросить подтверждение перед удалением задачи."""
    if not is_admin(user):
        return

    # Пропускаем если это delete_confirm (он регистрируется отдельно ниже)
    if "delete_confirm" in query.data:
        return

    from models import Task
    task_id = int(query.data.split(":")[-1])
    result  = await db.execute(select(Task).where(Task.id == task_id))
    task    = result.scalar_one_or_none()
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    from bot.keyboards import kb_admin_task_delete_confirm
    await query.message.answer(
        f"⚠️ <b>Удалить задачу «{task.name}»?</b>\n\n"
        f"Владелец: <code>{task.user_id}</code>\n"
        f"Будут удалены: задача, чаты, логи и медиафайлы с диска.\n\n"
        f"<b>Действие необратимо.</b>",
        reply_markup=kb_admin_task_delete_confirm(task.id, task.user_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:task:delete_confirm:"))
async def admin_task_delete_confirmed(query: CallbackQuery, user: User, db: AsyncSession):
    """Выполнить удаление задачи администратором."""
    if not is_admin(user):
        return

    from models import Task, Log
    from sqlalchemy import delete as sql_delete
    from services.task_service import _delete_media_from_disk

    parts   = query.data.split(":")   # admin:task:delete_confirm:TASK_ID:USER_ID
    task_id = int(parts[3])
    user_id = int(parts[4])

    result = await db.execute(select(Task).where(Task.id == task_id))
    task   = result.scalar_one_or_none()
    if not task:
        await query.answer("Задача уже удалена.", show_alert=True)
        return

    task_name = task.name

    # Сначала удаляем логи (нет каскада на task), затем саму задачу
    await db.execute(sql_delete(Log).where(Log.task_id == task_id))
    await db.delete(task)
    await db.commit()

    # Удаляем медиафайлы с диска
    _delete_media_from_disk(task_id)

    await query.answer("✅ Задача удалена.", show_alert=True)
    log.info("Админ %d удалил задачу %d (%s) пользователя %d", user.id, task_id, task_name, user_id)

    await query.message.answer(
        f"🗑 Задача <b>{task_name}</b> полностью удалена.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 К задачам пользователя", callback_data=f"admin:tasks:user:{user_id}"),
        ], [
            InlineKeyboardButton(text="◀️ Все задачи", callback_data="admin:all_tasks"),
        ]]),
    )


@router.callback_query(F.data.startswith("admin:task:logs:"))
async def admin_task_logs(query: CallbackQuery, user: User, db: AsyncSession):
    """Постраничные ссылки на сообщения задачи (для администратора)."""
    if not is_admin(user):
        return

    from models import Task, Log, TaskAccount
    from sqlalchemy import func

    parts   = query.data.split(":")    # admin:task:logs:TASK_ID:PAGE
    task_id = int(parts[3])
    page    = int(parts[4]) if len(parts) > 4 else 0

    result = await db.execute(select(Task).where(Task.id == task_id))
    task   = result.scalar_one_or_none()
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
    total_pages   = max(1, -(-linkable // ADMIN_LOGS_PER_PAGE))
    page          = max(0, min(page, total_pages - 1))

    logs_res = await db.execute(
        select(Log)
        .where(Log.task_id == task_id, Log.success == True, Log.message_id.isnot(None))
        .order_by(Log.created_at.desc())
        .limit(ADMIN_LOGS_PER_PAGE)
        .offset(page * ADMIN_LOGS_PER_PAGE)
    )
    logs  = logs_res.scalars().all()
    lines = []
    for i, lg in enumerate(logs, start=page * ADMIN_LOGS_PER_PAGE + 1):
        link = _admin_make_message_link(lg.chat_id, lg.message_id)
        ts   = lg.created_at.strftime("%d.%m %H:%M") if lg.created_at else "—"
        if link:
            lines.append(f'{i}. <a href="{link}">{lg.chat_id}</a> — {ts}')
        else:
            lines.append(f'{i}. {lg.chat_id} — {ts}')

    if not lines:
        lines = ["  (нет отправок с публичной ссылкой)"]

    text = (
        f"🔗 <b>Сообщения задачи</b> «{task.name}»\n"
        f"Стр. {page + 1} / {total_pages} · {linkable} ссылок\n\n"
        + "\n".join(lines)
    )

    from bot.keyboards import kb_admin_tasks_logs_page
    await query.message.edit_text(
        text,
        reply_markup=kb_admin_tasks_logs_page(task_id, page, total_pages, task.user_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# АДМИН: АККАУНТЫ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ
# ═══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:all_accounts")
async def admin_all_user_accounts(query: CallbackQuery, user: User, db: AsyncSession):
    """
    Все пользовательские (не системные) аккаунты в системе,
    сгруппированные по владельцам.
    """
    if not is_admin(user):
        return

    from models import Account
    from sqlalchemy import func

    result = await db.execute(
        select(
            User.id,
            User.username,
            User.full_name,
            func.count(Account.id).label("acc_count"),
        )
        .join(Account, Account.owner_id == User.id)
        .where(Account.is_system == False)
        .group_by(User.id, User.username, User.full_name)
        .order_by(func.count(Account.id).desc())
        .limit(50)
    )
    rows = result.all()

    if not rows:
        await query.message.edit_text(
            "🤖 Пользователи ещё не добавляли личных аккаунтов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")
            ]]),
        )
        return

    buttons = []
    for uid, uname, fname, cnt in rows:
        label = f"@{uname}" if uname else (fname or str(uid))
        buttons.append([InlineKeyboardButton(
            text=f"👤 {label} — {cnt} акк.",
            callback_data=f"admin:accs:user:{uid}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")])

    await query.message.edit_text(
        "🤖 <b>Пользовательские аккаунты</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:accs:user:"))
async def admin_user_accounts_list(query: CallbackQuery, user: User, db: AsyncSession):
    """Аккаунты конкретного пользователя."""
    if not is_admin(user):
        return

    from models import Account
    uid    = int(query.data.split(":")[-1])
    target = await get_user(db, uid)
    if not target:
        await query.answer("Пользователь не найден.", show_alert=True)
        return

    result = await db.execute(
        select(Account).where(Account.owner_id == uid, Account.is_system == False)
        .order_by(Account.created_at.desc())
    )
    accounts = result.scalars().all()

    uname = f"@{target.username}" if target.username else target.full_name
    lines = []
    for acc in accounts:
        proxy = f" 🌐{acc.proxy_host}" if acc.proxy_host else ""
        lines.append(
            f"{acc.status_icon} <code>{acc.phone}</code> — {acc.chats_count} чатов"
            f", {acc.sends_last_hour}/ч{proxy}"
        )

    text = (
        f"🤖 <b>Аккаунты пользователя {uname}</b> (<code>{uid}</code>)\n\n"
        + ("\n".join(lines) or "(нет аккаунтов)")
    )

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="admin:all_accounts")
        ]]),
        parse_mode="HTML",
    )