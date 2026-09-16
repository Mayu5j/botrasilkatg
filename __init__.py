"""
models/__init__.py — все модели базы данных.

ИЗМЕНЕНИЯ (медиа-рефакторинг):
  - Task: убраны photo_file_ids (Text) — теперь байты хранятся в TaskMedia
  - TaskMedia: байты фото (удаляются после первой успешной отправки)
  - TaskMediaCache: кеш Telethon file_id по аккаунту (остаётся навсегда)
  - Account: добавлены поля прокси + sends_last_hour / sends_reset_at
"""

from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, LargeBinary, String, Text,
)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# ПОЛЬЗОВАТЕЛИ
# ─────────────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int]              = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str]       = mapped_column(String(128), default="")
    is_blocked: Mapped[bool]     = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool]       = mapped_column(Boolean, default=False)

    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sub_ends_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    max_chats:  Mapped[int]      = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    accounts:   Mapped[list["Account"]]  = relationship(back_populates="owner",      lazy="selectin")
    tasks:      Mapped[list["Task"]]     = relationship(back_populates="user",        lazy="selectin")
    mirror_bot: Mapped["MirrorBot | None"] = relationship(back_populates="user",      uselist=False)
    payments:   Mapped[list["Payment"]] = relationship(back_populates="user",         lazy="selectin")

    @property
    def has_access(self) -> bool:
        now = now_utc()
        return bool(
            (self.trial_ends_at and self.trial_ends_at > now)
            or (self.sub_ends_at and self.sub_ends_at > now)
        )

    @property
    def subscription_status(self) -> str:
        now = now_utc()
        if self.sub_ends_at and self.sub_ends_at > now:
            days = (self.sub_ends_at - now).days
            return f"✅ Подписка: {days} дн."
        if self.trial_ends_at and self.trial_ends_at > now:
            hours = int((self.trial_ends_at - now).total_seconds() / 3600)
            return f"🎁 Триал: {hours} ч."
        return "❌ Нет доступа"


# ─────────────────────────────────────────────────────────────────────────────
# ЗЕРКАЛЬНЫЕ БОТЫ
# ─────────────────────────────────────────────────────────────────────────────

class MirrorBot(Base):
    __tablename__ = "mirror_bots"

    id:           Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:      Mapped[int]           = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    token:        Mapped[str]           = mapped_column(String(120), unique=True)
    bot_username: Mapped[str | None]    = mapped_column(String(64))
    is_active:    Mapped[bool]          = mapped_column(Boolean, default=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=now_utc)

    user: Mapped["User"] = relationship(back_populates="mirror_bot")


# ─────────────────────────────────────────────────────────────────────────────
# ПЛАТЁЖНЫЕ БОТЫ (Stars)
# ─────────────────────────────────────────────────────────────────────────────

class PaymentBot(Base):
    """
    «Расходный» бот для приёма оплаты Telegram Stars.

    Идея: Stars всегда падают на баланс того бота, чьим токеном был
    выставлен счёт (это особенность Bot API, изменить нельзя). Поэтому
    вместо того чтобы слать инвойс из главного бота / зеркала, мы
    шлём пользователя по deep-link'у в отдельного "одноразового" бота,
    который ничего не делает кроме выставления счёта.

    is_active=True — ровно один бот, на него ведут ВСЕ новые кнопки
    "Оплатить ⭐" (во всех зеркалах и в главном боте). Админ может
    сменить его в любой момент через /admin → Платёжный бот.

    Старые (is_active=False) боты НЕ останавливаются автоматически —
    payment_bot_runner.py продолжает их поллинг, чтобы уже выданные
    счета могли быть оплачены. Чтобы полностью остановить бота —
    удалить его в админке (это убирает строку из БД, runner снимет
    polling в течение ~10 секунд).
    """
    __tablename__ = "payment_bots"

    id:             Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    token:          Mapped[str]        = mapped_column(String(120), unique=True)
    bot_username:   Mapped[str | None] = mapped_column(String(64))
    is_active:      Mapped[bool]       = mapped_column(Boolean, default=False)
    payments_count: Mapped[int]        = mapped_column(Integer, default=0)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=now_utc)


# ─────────────────────────────────────────────────────────────────────────────
# АККАУНТЫ (Telethon userbots)
# ─────────────────────────────────────────────────────────────────────────────

class Account(Base):
    """
    Telegram-аккаунт для рассылок.

    status:
      "ok"          — работает нормально
      "frozen"      — Telegram деактивировал аккаунт
      "spamblocked" — спамблок

    Прокси:
      proxy_host / proxy_port / proxy_type ("socks5"|"http") / proxy_user / proxy_pass

    Нагрузка:
      sends_last_hour — отправок за текущий час (сбрасывается каждый час)
      sends_reset_at  — когда был последний сброс
    """
    __tablename__ = "accounts"

    id:       Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None]    = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)

    phone:          Mapped[str]           = mapped_column(String(32), unique=True)
    api_id:         Mapped[int]           = mapped_column(Integer)
    api_hash:       Mapped[str]           = mapped_column(String(64))
    session_string: Mapped[str | None]    = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    status:      Mapped[str] = mapped_column(String(16), default="ok")
    chats_count: Mapped[int] = mapped_column(Integer, default=0)
    restriction_strikes: Mapped[int] = mapped_column(Integer, default=0)

    # ── Прокси ───────────────────────────────────────────────────────────────
    proxy_host: Mapped[str | None] = mapped_column(String(128))
    proxy_port: Mapped[int | None] = mapped_column(Integer)
    proxy_type: Mapped[str | None] = mapped_column(String(16))
    proxy_user: Mapped[str | None] = mapped_column(String(64))
    proxy_pass: Mapped[str | None] = mapped_column(String(128))

    # ── Нагрузка ──────────────────────────────────────────────────────────────
    sends_last_hour: Mapped[int]            = mapped_column(Integer, default=0)
    sends_reset_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    owner:      Mapped["User | None"]        = relationship(back_populates="accounts")
    task_links: Mapped[list["TaskAccount"]]  = relationship(back_populates="account")

    @property
    def proxy_label(self) -> str:
        if not self.proxy_host:
            return "нет"
        t = self.proxy_type or "socks5"
        user_part = f"{self.proxy_user}@" if self.proxy_user else ""
        return f"{t}://{user_part}{self.proxy_host}:{self.proxy_port}"

    @property
    def status_icon(self) -> str:
        if self.status == "frozen":      return "❄️"
        if self.status == "spamblocked": return "🚫"
        return "✅" if self.is_active else "⏸"

    @property
    def status_label(self) -> str:
        if self.status == "frozen":      return "❄️ Заморожен Telegram"
        if self.status == "spamblocked": return "🚫 Спамблок"
        if not self.is_active:           return "⏸ Приостановлен"
        if self.is_banned:               return "❌ Заблокирован"
        return "✅ Активен"


# ─────────────────────────────────────────────────────────────────────────────
# ЗАДАЧИ РАССЫЛОК
# ─────────────────────────────────────────────────────────────────────────────

class Task(Base):
    __tablename__ = "tasks"

    id:      Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    name:    Mapped[str] = mapped_column(String(128), default="Задача")
    message: Mapped[str] = mapped_column(Text)

    # Форматирование текста (JSON-список entities aiogram)
    format_entities: Mapped[str] = mapped_column(Text, default="[]")

    # has_media=True означает что к задаче прикреплены фото
    # (байты в TaskMedia, либо уже удалены — тогда кеш в TaskMediaCache)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)

    interval_minutes: Mapped[int]           = mapped_column(Integer, default=60)
    is_active:        Mapped[bool]          = mapped_column(Boolean, default=True)
    created_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=now_utc)
    last_run_at:      Mapped[datetime|None] = mapped_column(DateTime(timezone=True))

    user:       Mapped["User"]               = relationship(back_populates="tasks")
    chats:      Mapped[list["TaskChat"]]     = relationship(back_populates="task", cascade="all, delete")
    accounts:   Mapped[list["TaskAccount"]]  = relationship(back_populates="task", cascade="all, delete")
    media:      Mapped[list["TaskMedia"]]    = relationship(back_populates="task", cascade="all, delete",
                                                            order_by="TaskMedia.index")
    media_cache: Mapped[list["TaskMediaCache"]] = relationship(back_populates="task",
                                                               cascade="all, delete")


class TaskChat(Base):
    __tablename__ = "task_chats"

    id:         Mapped[int]  = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id:    Mapped[int]  = mapped_column(Integer, ForeignKey("tasks.id"))
    chat_id:    Mapped[str]  = mapped_column(String(64))
    chat_title: Mapped[str]  = mapped_column(String(128), default="")
    is_ok:      Mapped[bool] = mapped_column(Boolean, default=True)

    task: Mapped["Task"] = relationship(back_populates="chats")


class TaskAccount(Base):
    __tablename__ = "task_accounts"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id:    Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"))
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"))
    chat_ids:   Mapped[str] = mapped_column(Text, default="")

    task:    Mapped["Task"]    = relationship(back_populates="accounts")
    account: Mapped["Account"] = relationship(back_populates="task_links")


# ─────────────────────────────────────────────────────────────────────────────
# МЕДИА ЗАДАЧИ
# ─────────────────────────────────────────────────────────────────────────────

class TaskMedia(Base):
    """
    Байты фото задачи рассылки.
    Создаётся при создании задачи.
    Удаляется сразу после первой успешной отправки любым аккаунтом —
    дальше используется кеш file_id из TaskMediaCache.
    """
    __tablename__ = "task_media"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id:    Mapped[int]      = mapped_column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"))
    index:      Mapped[int]      = mapped_column(Integer, default=0)   # порядок фото (0, 1, 2...)
    data:       Mapped[bytes]    = mapped_column(LargeBinary)
    mime:       Mapped[str]      = mapped_column(String(32), default="image/jpeg")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    task: Mapped["Task"] = relationship(back_populates="media")


class TaskMediaCache(Base):
    """
    Кеш Telethon file_id фото по аккаунту.
    Создаётся при первой успешной отправке фото через конкретный аккаунт.
    Позволяет не перезагружать байты при каждой отправке.
    Остаётся в БД навсегда (пока задача существует).
    """
    __tablename__ = "task_media_cache"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id:    Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"))
    index:      Mapped[int] = mapped_column(Integer, default=0)  # порядок фото
    file_id:    Mapped[str] = mapped_column(String(512))         # Telethon InputDocument / file_id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    task:    Mapped["Task"]    = relationship(back_populates="media_cache")
    account: Mapped["Account"] = relationship()


# ─────────────────────────────────────────────────────────────────────────────
# ПЛАТЕЖИ
# ─────────────────────────────────────────────────────────────────────────────

class Payment(Base):
    __tablename__ = "payments"

    id:          Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:     Mapped[int]           = mapped_column(BigInteger, ForeignKey("users.id"))
    method:      Mapped[str]           = mapped_column(String(32))
    plan:        Mapped[str]           = mapped_column(String(32))
    amount:      Mapped[float]         = mapped_column(Float)
    currency:    Mapped[str]           = mapped_column(String(16))
    status:      Mapped[str]           = mapped_column(String(16), default="pending")
    external_id: Mapped[str | None]    = mapped_column(String(256))
    created_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=now_utc)
    paid_at:     Mapped[datetime|None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="payments")


# ─────────────────────────────────────────────────────────────────────────────
# ЛОГИ
# ─────────────────────────────────────────────────────────────────────────────

class Log(Base):
    __tablename__ = "logs"

    id:         Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id:    Mapped[int | None]    = mapped_column(Integer, ForeignKey("tasks.id"), nullable=True)
    account_id: Mapped[int | None]    = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    chat_id:    Mapped[str]           = mapped_column(String(64))
    message_id: Mapped[int | None]    = mapped_column(BigInteger, nullable=True)
    success:    Mapped[bool]          = mapped_column(Boolean)
    error:      Mapped[str | None]    = mapped_column(Text)
    created_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=now_utc)