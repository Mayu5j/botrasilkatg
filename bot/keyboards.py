"""
bot/keyboards.py — все клавиатуры бота в одном месте.
 
ИЗМЕНЕНИЯ (именованный пул прокси):
  - kb_choose_proxy_for_account — выбор прокси ПЕРВЫМ шагом при добавлении
    системного аккаунта (админ видит имена прокси, а не голые host:port).
  - kb_admin_proxy_pool_menu / kb_proxy_pool_list — управление пулом
    прокси (раздельно для пользователей и системных аккаунтов).
  - Старые kb-функции точечного управления прокси АККАУНТА (host/port
    вручную на конкретный Account) удалены — прокси теперь только из
    именованного пула, см. bot/handlers/admin.py.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
 
from config import SUBSCRIPTION_PRICES, MAIN_BOT_LINK
from models import Task, Account
 
 
def kb_main_menu(has_access: bool) -> InlineKeyboardMarkup:
    """Главное меню."""
    builder = InlineKeyboardBuilder()
    if has_access:
        builder.button(text="📋 Мои задачи",    callback_data="tasks:list")
        builder.button(text="➕ Новая задача",  callback_data="tasks:new")
        builder.button(text="👤 Мои аккаунты", callback_data="accounts:list")
    builder.button(text="💳 Подписка",         callback_data="pay:menu")
    builder.button(text="📊 Статус",           callback_data="status")
    builder.adjust(2, 2, 1)
    return builder.as_markup()
 
 
def kb_subscription_plans(is_mirror: bool = False) -> InlineKeyboardMarkup:
    """
    Кнопки выбора тарифного плана.
 
    Список тарифов одинаков в главном боте и в зеркалах — оплата звёздами
    в любом случае происходит через отдельного платёжного бота (см.
    bot/handlers/payment.py → select_plan), поэтому прятать тарифы в
    зеркалах больше не нужно. Параметр is_mirror оставлен для совместимости
    вызова, но больше не меняет поведение.
    """
    builder = InlineKeyboardBuilder()
    labels = {"1month": "1 месяц", "1week": "1 неделя", "3month": "3 месяца", "6month": "6 месяцев"}
    for plan, info in SUBSCRIPTION_PRICES.items():
        label = f"{labels.get(plan, plan)} — {info['stars']}⭐"
        builder.button(text=label, callback_data=f"pay:select:{plan}")
    builder.button(text="◀️ Назад", callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_tasks(tasks: list[Task]) -> InlineKeyboardMarkup:
    """Список задач пользователя."""
    builder = InlineKeyboardBuilder()
    for t in tasks:
        icon = "▶️" if t.is_active else "⏸"
        builder.button(
            text=f"{icon} {t.name} ({len(t.chats)} чатов, каждые {t.interval_minutes}м)",
            callback_data=f"tasks:view:{t.id}"
        )
    builder.button(text="➕ Новая задача", callback_data="tasks:new")
    builder.button(text="◀️ Меню",        callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_task_detail(task: Task) -> InlineKeyboardMarkup:
    """Управление конкретной задачей."""
    builder = InlineKeyboardBuilder()
    toggle_text = "⏸ Остановить" if task.is_active else "▶️ Запустить"
    builder.button(text=toggle_text,          callback_data=f"tasks:toggle:{task.id}")
    builder.button(text="🗑 Удалить",         callback_data=f"tasks:delete:{task.id}")
    builder.button(text="📊 Статистика",      callback_data=f"tasks:stats:{task.id}")
    builder.button(text="◀️ К задачам",      callback_data="tasks:list")
    builder.adjust(2, 1, 1)
    return builder.as_markup()
 
 
def kb_task_logs_page(task_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Пагинация ссылок на сообщения задачи (для пользователя)."""
    builder = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"tasks:logs:{task_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"tasks:logs:{task_id}:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="◀️ К задаче", callback_data=f"tasks:view:{task_id}")],
    ])
 
 
def kb_task_delete_confirm(task_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления задачи."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить",  callback_data=f"tasks:confirm_delete:{task_id}")
    builder.button(text="❌ Отмена",       callback_data=f"tasks:view:{task_id}")
    builder.adjust(2)
    return builder.as_markup()
 
 
def kb_accounts(accounts: list[Account]) -> InlineKeyboardMarkup:
    """Список аккаунтов пользователя."""
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        icon = acc.status_icon
        builder.button(
            text=f"{icon} {acc.phone} ({acc.chats_count} чатов)",
            callback_data=f"accounts:view:{acc.id}"
        )
    builder.button(text="➕ Добавить аккаунт", callback_data="accounts:add")
    builder.button(text="◀️ Меню",             callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_account_detail(acc: Account) -> InlineKeyboardMarkup:
    """Управление аккаунтом."""
    builder = InlineKeyboardBuilder()
 
    if acc.status == "ok":
        toggle_text = "⏸ Отключить" if acc.is_active else "▶️ Включить"
        builder.button(text=toggle_text, callback_data=f"accounts:toggle:{acc.id}")
 
    builder.button(text="🗑 Удалить",       callback_data=f"accounts:delete:{acc.id}")
    builder.button(text="◀️ К аккаунтам",  callback_data="accounts:list")
 
    if acc.status == "ok":
        builder.adjust(2, 1)
    else:
        builder.adjust(1)
 
    return builder.as_markup()
 
 
def kb_cancel() -> InlineKeyboardMarkup:
    """Простая кнопка отмены."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="menu:new")
    ]])
 
 
def kb_back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Меню", callback_data="menu:new")
    ]])
 
 
def kb_access_error() -> InlineKeyboardMarkup:
    """Кнопки после неудачной проверки доступа к чатам."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 К задачам", callback_data="tasks:list")
    builder.button(text="◀️ Меню",     callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_confirm_chats() -> InlineKeyboardMarkup:
    """Кнопка подтверждения после ввода чатов."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Продолжить", callback_data="tasks:confirm_chats")
    builder.button(text="❌ Отмена",     callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_choose_sender(accounts: list[Account]) -> InlineKeyboardMarkup:
    """Клавиатура выбора аккаунта-отправителя при создании задачи."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 Системные аккаунты", callback_data="tasks:sender:system")
    for acc in accounts:
        if acc.status != "ok":
            continue
        icon = "✅" if acc.is_active else "⏸"
        builder.button(
            text=f"{icon} {acc.phone}",
            callback_data=f"tasks:sender:acc:{acc.id}"
        )
    builder.button(text="❌ Отмена", callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()
 
 
# ── Админ ─────────────────────────────────────────────────────────────────────
 
def kb_admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Пользователи",        callback_data="admin:users")
    builder.button(text="🤖 Сист. аккаунты",      callback_data="admin:accounts")
    builder.button(text="📊 Статистика",          callback_data="admin:stats")
    builder.button(text="💳 Платёжный бот",       callback_data="admin:paybot")
    builder.button(text="📋 Задачи польз.",       callback_data="admin:all_tasks")
    builder.button(text="🔑 Аккаунты польз.",     callback_data="admin:all_accounts")
    builder.button(text="📢 Рассылка всем",       callback_data="admin:broadcast")
    builder.button(text="◀️ Меню",               callback_data="menu:new")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()
 
 
def kb_admin_task_detail(task_id: int, is_active: bool, user_id: int) -> InlineKeyboardMarkup:
    """Карточка задачи в админ-панели."""
    toggle_text = "⏸ Остановить" if is_active else "▶️ Запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"admin:task:toggle:{task_id}")],
        [InlineKeyboardButton(text="🔗 Ссылки на сообщения", callback_data=f"admin:task:logs:{task_id}:0")],
        [InlineKeyboardButton(text="🗑 Удалить задачу", callback_data=f"admin:task:delete:{task_id}")],
        [InlineKeyboardButton(text="◀️ К задачам пользователя", callback_data=f"admin:tasks:user:{user_id}")],
    ])
 
 
def kb_admin_system_accounts(accounts: list) -> InlineKeyboardMarkup:
    """Список системных аккаунтов с кнопкой удаления на каждый."""
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        builder.button(
            text=f"{acc.status_icon} {acc.phone} ({acc.chats_count} чатов)",
            callback_data=f"admin:accdetail:{acc.id}",
        )
        builder.button(text=f"🗑 Удалить {acc.phone}", callback_data=f"admin:delacc:{acc.id}")
    builder.button(text="➕ Добавить системный", callback_data="admin:addacc")
    builder.button(text="🌐 Прокси",              callback_data="admin:proxies")
    builder.button(text="◀️ Назад",               callback_data="admin:menu")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_admin_delacc_confirm(account_id: int, tasks_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Да, удалить (+{tasks_count} задач)",
                              callback_data=f"admin:delacc_confirm:{account_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:accounts")],
    ])
 
 
def kb_admin_task_delete_confirm(task_id: int, user_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления задачи администратором."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin:task:delete_confirm:{task_id}:{user_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:task:view:{task_id}")],
    ])
 
 
def kb_admin_tasks_logs_page(task_id: int, page: int, total_pages: int, user_id: int) -> InlineKeyboardMarkup:
    """Пагинация ссылок в админ-панели."""
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:task:logs:{task_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:task:logs:{task_id}:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="◀️ К задаче", callback_data=f"admin:task:view:{task_id}")],
    ])
 
 
# ── Пул прокси (именованный, задаётся админом) ────────────────────────────────
 
def kb_choose_proxy_for_account(proxies: list) -> InlineKeyboardMarkup:
    """
    Выбор прокси САМЫМ ПЕРВЫМ шагом при добавлении системного аккаунта.
    Список — только прокси типа 'system' (kind == "system"), активные.
    Через выбранный прокси пойдёт код входа и вся дальнейшая рассылка
    этого аккаунта.
    """
    builder = InlineKeyboardBuilder()
    for p in proxies:
        builder.button(
            text=f"🌐 {p.name} ({p.accounts_count} акк.)",
            callback_data=f"admin:addacc:proxy:{p.id}",
        )
    builder.button(text="🚫 Без прокси", callback_data="admin:addacc:proxy:none")
    builder.button(text="❌ Отмена", callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_admin_proxy_pool_menu() -> InlineKeyboardMarkup:
    """Верхнее меню управления прокси — выбор типа пула."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Прокси для пользователей", callback_data="admin:proxypool:list:user")
    builder.button(text="🤖 Прокси для системных",      callback_data="admin:proxypool:list:system")
    builder.button(text="◀️ Назад",                      callback_data="admin:accounts")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_proxy_pool_list(proxies: list, kind: str) -> InlineKeyboardMarkup:
    """
    Список прокси одного типа (пользователи/системные).
    Нажатие на строку прокси переключает активность (вкл/выкл),
    отдельная кнопка — удаление.
    """
    builder = InlineKeyboardBuilder()
    for p in proxies:
        icon = "✅" if p.is_active else "⏸"
        builder.button(
            text=f"{icon} {p.name} — {p.host}:{p.port} ({p.accounts_count} акк.)",
            callback_data=f"admin:proxypool:toggle:{p.id}",
        )
        builder.button(text=f"🗑 Удалить {p.name}", callback_data=f"admin:proxypool:delete:{p.id}")
    builder.button(text="➕ Добавить прокси", callback_data=f"admin:proxypool:add:{kind}")
    builder.button(text="◀️ Назад", callback_data="admin:proxies")
    builder.adjust(1)
    return builder.as_markup()
 
 
# ── Платёжный бот (Stars) ────────────────────────────────────────────────────
 
def kb_paybot_menu(has_active: bool) -> InlineKeyboardMarkup:
    """Главный экран управления платёжным ботом."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Сменить бота",  callback_data="admin:paybot:set")
    if has_active:
        builder.button(text="⏸ Деактивировать", callback_data="admin:paybot:deactivate")
    builder.button(text="📜 История ботов", callback_data="admin:paybot:history")
    builder.button(text="◀️ Назад",          callback_data="admin:menu")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_paybot_history(bots: list) -> InlineKeyboardMarkup:
    """Список всех когда-либо добавленных платёжных ботов."""
    builder = InlineKeyboardBuilder()
    for b in bots:
        icon = "✅" if b.is_active else "⏸"
        uname = b.bot_username or f"id{b.id}"
        builder.button(
            text=f"{icon} @{uname} ({b.payments_count} оплат)",
            callback_data=f"admin:paybot:view:{b.id}",
        )
    builder.button(text="◀️ Назад", callback_data="admin:paybot")
    builder.adjust(1)
    return builder.as_markup()
 
 
def kb_paybot_detail(bot_row) -> InlineKeyboardMarkup:
    """Карточка одного платёжного бота из истории."""
    builder = InlineKeyboardBuilder()
    if not bot_row.is_active:
        builder.button(text="✅ Сделать активным", callback_data=f"admin:paybot:activate:{bot_row.id}")
    builder.button(text="🗑 Удалить (остановить)", callback_data=f"admin:paybot:delete:{bot_row.id}")
    builder.button(text="◀️ К истории", callback_data="admin:paybot:history")
    builder.adjust(1)
    return builder.as_markup()

