"""
services/user_service.py — работа с пользователями:
регистрация, проверка доступа, управление подпиской.
"""
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import TRIAL_DAYS, OWNER_ID
from models import User


async def get_or_create_user(
    db: AsyncSession,
    tg_id: int,
    username: str | None,
    full_name: str,
) -> tuple[User, bool]:
    """
    Получить пользователя из БД или создать нового.
    Возвращает (user, is_new).
    """
    result = await db.execute(select(User).where(User.id == tg_id))
    user = result.scalar_one_or_none()

    if user:
        # Обновляем имя/username при каждом входе
        user.username  = username
        user.full_name = full_name
        await db.commit()
        return user, False

    # Новый пользователь — даём триал
    now = datetime.now(timezone.utc)
    user = User(
        id=tg_id,
        username=username,
        full_name=full_name,
        is_admin=(tg_id == OWNER_ID),
        trial_ends_at=now + timedelta(days=TRIAL_DAYS),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user, True


async def get_user(db: AsyncSession, tg_id: int) -> User | None:
    """Получить пользователя по Telegram ID."""
    result = await db.execute(select(User).where(User.id == tg_id))
    return result.scalar_one_or_none()


async def add_subscription(db: AsyncSession, user: User, days: int):
    """
    Добавить дни подписки пользователю.
    Если подписка уже есть — продлеваем от её конца,
    иначе — от сегодня.
    """
    now = datetime.now(timezone.utc)
    start = user.sub_ends_at if (user.sub_ends_at and user.sub_ends_at > now) else now
    user.sub_ends_at = start + timedelta(days=days)
    await db.commit()


async def block_user(db: AsyncSession, user_id: int) -> bool:
    """Заблокировать пользователя (True = успешно)."""
    user = await get_user(db, user_id)
    if not user:
        return False
    user.is_blocked = True
    await db.commit()
    return True


async def unblock_user(db: AsyncSession, user_id: int) -> bool:
    """Разблокировать пользователя."""
    user = await get_user(db, user_id)
    if not user:
        return False
    user.is_blocked = False
    await db.commit()
    return True


async def set_max_chats(db: AsyncSession, user_id: int, max_chats: int) -> bool:
    """Установить лимит чатов вручную (через админку)."""
    user = await get_user(db, user_id)
    if not user:
        return False
    user.max_chats = max_chats
    await db.commit()
    return True


async def get_all_users(db: AsyncSession) -> list[User]:
    """Все пользователи (для админки)."""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return list(result.scalars().all())


async def count_active_users(db: AsyncSession) -> int:
    """Активные = у кого есть запущенные задачи."""
    from models import Task
    result = await db.execute(
        select(Task.user_id).where(Task.is_active == True).distinct()
    )
    return len(result.scalars().all())
