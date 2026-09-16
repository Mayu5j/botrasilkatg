"""
services/task_service.py — управление задачами рассылок.

МЕДИА-РЕФАКТОРИНГ (диск вместо БД):
  - create_task принимает photo_bytes_list: list[bytes]
  - Байты сохраняются на диск: /app/media/task_{id}/photo_0.jpg, photo_1.jpg, ...
  - delete_task удаляет папку с медиа вместе с задачей
  - Никаких TaskMedia / TaskMediaCache в БД
"""
import json
import logging
import os
import shutil
from pathlib import Path

from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from config import MAX_CHATS_PER_USER
from models import Task, TaskChat, TaskAccount, Account, User, Log

log = logging.getLogger(__name__)

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(Path(__file__).resolve().parent.parent / "media")))


def _task_media_dir(task_id: int) -> Path:
    return MEDIA_ROOT / f"task_{task_id}"


def _save_media_to_disk(task_id: int, photo_bytes_list: list[bytes]) -> int:
    """Сохранить фото задачи на диск. Возвращает кол-во сохранённых файлов."""
    if not photo_bytes_list:
        return 0
    d = _task_media_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    saved = 0
    for idx, data in enumerate(photo_bytes_list):
        try:
            (d / f"photo_{idx}.jpg").write_bytes(data)
            saved += 1
        except Exception as e:
            log.error("Не удалось сохранить фото %d задачи %d: %s", idx, task_id, e)
    log.info("Задача %d: сохранено %d/%d фото в %s", task_id, saved, len(photo_bytes_list), d)
    return saved


def _delete_media_from_disk(task_id: int):
    """Удалить папку с медиа задачи."""
    d = _task_media_dir(task_id)
    if d.exists():
        try:
            shutil.rmtree(d)
            log.info("Задача %d: медиа-папка удалена (%s)", task_id, d)
        except Exception as e:
            log.error("Не удалось удалить медиа-папку задачи %d: %s", task_id, e)


def _normalize_chat_id(chat_id: str) -> str:
    s = str(chat_id).strip()
    if s.startswith("@"):
        username = s.lstrip("@")
        return f"@{username}"
    return s


def _make_stored_title(username: str | None, title: str | None, chat_id: str) -> str:
    if username:
        clean = username.lstrip("@")
        return f"@{clean}"
    if title and title.strip():
        return title.strip()
    return str(chat_id)


async def get_tasks(db: AsyncSession, user_id: int) -> list[Task]:
    result = await db.execute(
        select(Task)
        .options(
            selectinload(Task.chats),
            selectinload(Task.accounts).selectinload(TaskAccount.account),
        )
        .where(Task.user_id == user_id)
        .order_by(Task.created_at.desc())
    )
    return list(result.scalars().all())


async def get_task(db: AsyncSession, task_id: int, user_id: int) -> Task | None:
    result = await db.execute(
        select(Task)
        .options(
            selectinload(Task.chats),
            selectinload(Task.accounts).selectinload(TaskAccount.account),
        )
        .where(Task.id == task_id, Task.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create_task(
    db: AsyncSession,
    user: User,
    name: str,
    message: str,
    interval_minutes: int,
    chats: list[dict],
    preferred_account_id: int | None = None,
    photo_bytes_list: list[bytes] | None = None,
    format_entities: list[dict] | None = None,
) -> dict | None:
    """
    Создать задачу.
    Возвращает dict (не ORM) чтобы избежать MissingGreenlet после commit.

    photo_bytes_list — список байт фото. Сохраняются на диск в
    /app/media/task_{id}/photo_N.jpg и читаются оттуда при каждой отправке.
    """
    existing_tasks = await get_tasks(db, user.id)
    current_chats  = sum(len(t.chats) for t in existing_tasks)

    if current_chats + len(chats) > user.max_chats:
        log.warning("Пользователь %d превысил лимит чатов", user.id)
        return None

    has_media = bool(photo_bytes_list)

    task = Task(
        user_id=user.id,
        name=name,
        message=message,
        format_entities=json.dumps(format_entities or [], ensure_ascii=False),
        interval_minutes=interval_minutes,
        has_media=has_media,
    )
    db.add(task)
    await db.flush()  # получаем task.id

    # Сохраняем чаты
    for chat in chats:
        raw_id       = str(chat["id"])
        norm_id      = _normalize_chat_id(raw_id)
        username     = chat.get("username")
        title        = chat.get("title") or ""
        stored_title = _make_stored_title(username, title, norm_id)

        db.add(TaskChat(
            task_id=task.id,
            chat_id=norm_id,
            chat_title=stored_title,
        ))

    await _distribute_chats(db, task, user, chats, preferred_account_id=preferred_account_id)
    await db.commit()

    # Сохраняем медиа на диск ПОСЛЕ commit (task.id уже есть)
    if photo_bytes_list:
        _save_media_to_disk(task.id, photo_bytes_list)

    return {
        "id":               task.id,
        "name":             name,
        "chats_count":      len(chats),
        "interval_minutes": interval_minutes,
        "has_media":        has_media,
    }


async def delete_task(db: AsyncSession, task_id: int, user_id: int) -> bool:
    task = await get_task(db, task_id, user_id)
    if not task:
        return False
    await db.execute(delete(Log).where(Log.task_id == task_id))
    await db.delete(task)
    await db.commit()
    # Удаляем медиа с диска после успешного удаления из БД
    _delete_media_from_disk(task_id)
    return True


async def toggle_task(db: AsyncSession, task_id: int, user_id: int) -> bool | None:
    task = await get_task(db, task_id, user_id)
    if not task:
        return None
    task.is_active = not task.is_active
    await db.commit()
    return task.is_active


async def _distribute_chats(
    db,
    task,
    user,
    chats: list[dict],
    preferred_account_id: int | None = None,
):
    """Распределить чаты по аккаунтам."""
    chat_ids = [_normalize_chat_id(str(c["id"])) for c in chats]

    if preferred_account_id is not None:
        result = await db.execute(
            select(Account).where(
                Account.id == preferred_account_id,
                Account.is_active == True,
                Account.is_banned == False,
                Account.status == "ok",
            )
        )
        accounts = list(result.scalars().all())

        if not accounts:
            log.warning("Выбранный аккаунт %d недоступен для задачи %d",
                        preferred_account_id, task.id)
            return

        acc = accounts[0]
        db.add(TaskAccount(
            task_id=task.id,
            account_id=acc.id,
            chat_ids=json.dumps(chat_ids),
        ))
        acc.chats_count += len(chat_ids)
        log.info("Задача %d: %d чатов → %s (личный)", task.id, len(chat_ids), acc.phone)
        return

    # Системные аккаунты — round-robin по наименее загруженному
    result = await db.execute(
        select(Account).where(
            Account.is_active == True,
            Account.is_banned == False,
            Account.status == "ok",
            Account.is_system == True,
        ).order_by(Account.chats_count.asc())
    )
    system_accounts = list(result.scalars().all())

    # Fallback на личные аккаунты пользователя
    if not system_accounts:
        result = await db.execute(
            select(Account).where(
                Account.owner_id == user.id,
                Account.is_active == True,
                Account.is_banned == False,
                Account.status == "ok",
            ).order_by(Account.chats_count.asc())
        )
        system_accounts = list(result.scalars().all())

    if not system_accounts:
        log.warning("Нет доступных аккаунтов для задачи %d", task.id)
        return

    n = len(system_accounts)

    if n == 1:
        acc = system_accounts[0]
        db.add(TaskAccount(
            task_id=task.id,
            account_id=acc.id,
            chat_ids=json.dumps(chat_ids),
        ))
        acc.chats_count += len(chat_ids)
        log.info("Задача %d: %d чатов → %s (единственный системный)",
                 task.id, len(chat_ids), acc.phone)
        return

    # Round-robin
    distribution: dict[int, list[str]] = {acc.id: [] for acc in system_accounts}

    for cid in chat_ids:
        system_accounts.sort(
            key=lambda a: a.chats_count + len(distribution[a.id])
        )
        distribution[system_accounts[0].id].append(cid)

    for acc in system_accounts:
        ids = distribution[acc.id]
        if not ids:
            continue
        db.add(TaskAccount(
            task_id=task.id,
            account_id=acc.id,
            chat_ids=json.dumps(ids),
        ))
        acc.chats_count += len(ids)
        log.info("Задача %d: %d чатов → %s (round-robin)",
                 task.id, len(ids), acc.phone)
