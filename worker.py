"""
worker/worker.py — воркер рассылок.
 
МЕДИА-РЕФАКТОРИНГ (диск вместо БД+кеша):
  - Фото хранятся на диске: /app/media/task_{id}/photo_0.jpg, photo_1.jpg, ...
  - При каждой отправке читаем файлы с диска и передаём BytesIO в send_file()
  - Никакого кеша file_id, никакого TaskMedia/TaskMediaCache в БД
  - При удалении задачи task_service удаляет папку с диска
 
ИСПРАВЛЕНИЕ ping-pong failover:
  - _chat_banned_accounts отслеживает аккаунты заблокированные в конкретном чате
  - При бане аккаунта — сразу убирается из task_accounts, помечается в словаре
  - Failover кандидаты фильтруются, уже попробовавшие не переназначаются
 
ОБХОД ОГРАНИЧЕНИЙ ЧАТА (капчи/квизы ботов-админов + обязательная подписка):
  - При первой неудачной отправке (need_failover=True), ДО обычного failover,
    пробуем restriction_service.try_bypass_restriction() — решить капчу/квиз
    от бота-админа в ЛС аккаунта и/или вступить в требуемые каналы. Если это
    помогло — чат остаётся на этом же аккаунте, следующий тик заберёт его сам.
    Если нет — как и раньше, идём в _try_failover.
"""
import asyncio
import io
import json
import logging
import os
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
 
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from telethon.errors import (
    FloodWaitError,
    UserBannedInChannelError,
    ChatWriteForbiddenError,
    UserDeactivatedError,
    AuthKeyUnregisteredError,
)
from telethon.tl import types as tl_types
from aiogram import Bot
 
from config import BOT_TOKEN, MIN_SEND_INTERVAL
from database import SessionLocal, create_all_tables
from models import Task, TaskAccount, Account, Log
from services.account_service import make_client
 
log = logging.getLogger(__name__)
 
scheduler = AsyncIOScheduler(timezone="UTC")
 
# ── Медиа-папка ───────────────────────────────────────────────────────────────
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(Path(__file__).resolve().parent.parent / "media")))
 
 
def _task_media_dir(task_id: int) -> Path:
    return MEDIA_ROOT / f"task_{task_id}"
 
 
def _get_task_media_files(task_id: int) -> list[Path]:
    """Вернуть список файлов медиа задачи, отсортированных по индексу."""
    d = _task_media_dir(task_id)
    if not d.exists():
        return []
    files = sorted(d.glob("photo_*.jpg"), key=lambda p: int(p.stem.split("_")[1]))
    return files
 
 
# ── Пул клиентов ──────────────────────────────────────────────────────────────
_client_pool: dict[int, object] = {}
_pool_lock = asyncio.Lock()
 
# ── Rate limiter ───────────────────────────────────────────────────────────────
_account_busy: set[int] = set()
_account_last_send: dict[int, float] = {}
 
 
def _try_lock_account(account_id: int) -> bool:
    """Захватить слот аккаунта. Возвращает False если уже занят."""
    if account_id in _account_busy:
        return False
    _account_busy.add(account_id)
    return True
 
 
def _unlock_account(account_id: int):
    _account_busy.discard(account_id)
 
 
# ── Защита от ping-pong failover ──────────────────────────────────────────────
# Ключ: "task_id:chat_id", значение: set account_id-ов которые не могут писать.
# Предотвращает бесконечный цикл A→B→A→B когда оба аккаунта забанены в чате.
_chat_banned_accounts: dict[str, set[int]] = {}
 
 
def _ban_account_from_chat(task_id: int, account_id: int, chat_id: str):
    key = f"{task_id}:{chat_id}"
    _chat_banned_accounts.setdefault(key, set()).add(account_id)
 
 
def _is_banned_from_chat(task_id: int, account_id: int, chat_id: str) -> bool:
    key = f"{task_id}:{chat_id}"
    return account_id in _chat_banned_accounts.get(key, set())
 
 
# ── Отслеживание jobs ─────────────────────────────────────────────────────────
_loaded_jobs: dict[str, int] = {}
 
# ── zero-width символы для невидимой рандомизации ─────────────────────────────
_ZW_CHARS = ['\u200b', '\u200c', '\u200d']
 
DELIVERY_CONFIRM_SECONDS = 5
DELIVERY_CONFIRM_ATTEMPTS = 3
 
 
def _randomize_text(text: str) -> str:
    if not text:
        return text
    pos  = random.randint(0, len(text))
    char = random.choice(_ZW_CHARS)
    return text[:pos] + char + text[pos:]
 
 
# ── Пул клиентов: управление ──────────────────────────────────────────────────
 
async def get_client(account: Account):
    async with _pool_lock:
        client = _client_pool.get(account.id)
        if client is not None and client.is_connected():
            return client
 
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
 
        client = make_client(account)
        try:
            await client.connect()
            await asyncio.sleep(1)
            if not await client.is_user_authorized():
                log.warning("Аккаунт %s не авторизован", account.phone)
                await client.disconnect()
                _client_pool.pop(account.id, None)
                return None
            _client_pool[account.id] = client
            log.info("Клиент %s подключён (пул)", account.phone)
            return client
        except Exception as e:
            log.error("Не удалось подключить %s: %s", account.phone, e)
            try:
                await client.disconnect()
            except Exception:
                pass
            return None
 
 
async def remove_client(account_id: int):
    async with _pool_lock:
        client = _client_pool.pop(account_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
 
 
async def keepalive_clients():
    async with _pool_lock:
        dead = [aid for aid, c in _client_pool.items() if not c.is_connected()]
 
    for aid in dead:
        async with SessionLocal() as db:
            result = await db.execute(
                select(Account).where(
                    Account.id == aid,
                    Account.is_active == True,
                    Account.is_banned == False,
                    Account.status == "ok",
                )
            )
            acc = result.scalar_one_or_none()
        if acc:
            log.info("Keepalive: реконнект %s", acc.phone)
            await get_client(acc)
        else:
            async with _pool_lock:
                _client_pool.pop(aid, None)
 
 
# ── Rate limiter ───────────────────────────────────────────────────────────────
 
async def _wait_rate_limit(account_id: int):
    last = _account_last_send.get(account_id, 0)
    wait = MIN_SEND_INTERVAL - (time.monotonic() - last)
    if wait > 0:
        await asyncio.sleep(wait)
 
 
def _mark_sent(account_id: int):
    _account_last_send[account_id] = time.monotonic()
 
 
# ── Счётчик отправок ───────────────────────────────────────────────────────────
 
async def _increment_sends(db: AsyncSession, account: Account):
    now = datetime.now(timezone.utc)
    if account.sends_reset_at is None or (now - account.sends_reset_at).total_seconds() > 3600:
        account.sends_last_hour = 0
        account.sends_reset_at  = now
    account.sends_last_hour += 1
 
 
async def reset_sends_counters():
    async with SessionLocal() as db:
        result = await db.execute(select(Account))
        accounts = result.scalars().all()
        for acc in accounts:
            acc.sends_last_hour = 0
            acc.sends_reset_at = datetime.now(timezone.utc)
        await db.commit()
    log.info("Счётчики sends_last_hour сброшены.")
 
 
# ── Синхронизация планировщика ─────────────────────────────────────────────────
 
async def sync_tasks():
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task)
            .options(selectinload(Task.accounts), selectinload(Task.user))
            .where(Task.is_active == True)
        )
        active_tasks = result.scalars().all()
 
        desired_jobs: dict[str, tuple] = {}
 
        for task in active_tasks:
            if not task.user.has_access:
                continue
            for ta in task.accounts:
                try:
                    chat_ids = json.loads(ta.chat_ids or "[]")
                except Exception:
                    chat_ids = []
                for chat_id in chat_ids:
                    job_id = _make_job_id(task.id, ta.account_id, chat_id)
                    desired_jobs[job_id] = (task.id, ta.account_id, chat_id, task.interval_minutes)
 
        for job_id in list(_loaded_jobs.keys()):
            if job_id not in desired_jobs:
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                del _loaded_jobs[job_id]
 
        for job_id, (task_id, account_id, chat_id, interval) in desired_jobs.items():
            existing_interval = _loaded_jobs.get(job_id)
            if existing_interval is None:
                scheduler.add_job(
                    send_chat_job,
                    "interval",
                    minutes=interval,
                    id=job_id,
                    args=[task_id, account_id, chat_id],
                    next_run_time=datetime.now(timezone.utc),
                    replace_existing=True,
                    max_instances=2,
                    misfire_grace_time=60,
                    coalesce=True,
                )
                _loaded_jobs[job_id] = interval
            elif existing_interval != interval:
                scheduler.reschedule_job(job_id, trigger="interval", minutes=interval)
                _loaded_jobs[job_id] = interval
 
 
def _make_job_id(task_id: int, account_id: int, chat_id: str) -> str:
    safe = chat_id.replace("@", "at_").replace("-", "m_")
    return f"chat_{task_id}_{account_id}_{safe}"
 
 
# ── Основной job ───────────────────────────────────────────────────────────────
 
async def send_chat_job(task_id: int, account_id: int, chat_id: str):
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task)
            .options(selectinload(Task.user))
            .where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task or not task.is_active or not task.user.has_access:
            return
 
        result = await db.execute(
            select(Account).where(Account.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account or not account.is_active or account.is_banned or account.status != "ok":
            await _try_failover(db, task, account_id, chat_id)
            return
 
        message_text = task.message or ""
        has_media    = task.has_media
 
        try:
            format_entities = json.loads(task.format_entities or "[]")
        except Exception:
            format_entities = []
 
    if not _try_lock_account(account_id):
        log.debug("Аккаунт %d занят, пропускаем тик для %s", account_id, chat_id)
        return
 
    try:
        await _wait_rate_limit(account_id)
 
        client = await get_client(account)
        if client is None:
            async with SessionLocal() as db:
                result = await db.execute(
                    select(Task).options(selectinload(Task.user)).where(Task.id == task_id)
                )
                task = result.scalar_one_or_none()
                if task:
                    await _try_failover(db, task, account_id, chat_id)
            return
 
        try:
            success, error, need_failover, sent_msg_id = await asyncio.wait_for(
                _send_with_client(
                    client, account, task_id, chat_id,
                    message_text, has_media, format_entities,
                ),
                timeout=270,
            )
        except asyncio.TimeoutError:
            log.error("Таймаут отправки %s → %s (>270s), пропускаем", account.phone, chat_id)
            _mark_sent(account_id)
            return
 
        _mark_sent(account_id)
 
        async with SessionLocal() as db:
            db.add(Log(
                task_id=task_id,
                account_id=account.id,
                chat_id=chat_id,
                message_id=sent_msg_id,
                success=success,
                error=error,
            ))
 
            if success:
                await _increment_sends(db, account)
                await db.commit()
 
                random_offset = random.randint(0, 90)
                job_id = _make_job_id(task_id, account_id, chat_id)
                if scheduler.get_job(job_id):
                    interval = _loaded_jobs.get(job_id, 60)
                    next_run = datetime.now(timezone.utc) + timedelta(
                        minutes=interval, seconds=random_offset
                    )
                    scheduler.reschedule_job(
                        job_id, trigger="interval", minutes=interval, start_date=next_run
                    )
                log.info("✓ [%s] → %s (+%ds)", account.phone, chat_id, random_offset)
 
            else:
                await db.commit()
                if need_failover:
                    from services.restriction_service import try_bypass_restriction
                    bypassed = await try_bypass_restriction(account, chat_id)
                    if bypassed:
                        log.info(
                            "✓ Обход ограничения удался: %s → %s остаётся на аккаунте",
                            account.phone, chat_id,
                        )
                        return
 
                    result = await db.execute(
                        select(Task).options(selectinload(Task.user)).where(Task.id == task_id)
                    )
                    task = result.scalar_one_or_none()
                    if task:
                        await _try_failover(db, task, account_id, chat_id, error)
 
    finally:
        _unlock_account(account_id)
 
 
# ── Отправка через клиент ──────────────────────────────────────────────────────
 
def _extract_message_id(sent) -> int | None:
    """Извлечь ID сообщения из результата send_message/send_file."""
    if sent is None:
        return None
    if isinstance(sent, list):
        sent = sent[0] if sent else None
    return getattr(sent, "id", None)
 
 
def _is_real_message(msg) -> bool:
    return bool(msg) and not isinstance(msg, tl_types.MessageEmpty)
 
 
async def _message_still_exists(client, entity, message_id: int | None) -> tuple[bool, str | None]:
    """Проверить, что отправленное сообщение есть в чате и не исчезло за 5 секунд."""
    if not message_id:
        return False, "message_id_missing"
 
    try:
        msg = await client.get_messages(entity, ids=message_id)
        if not _is_real_message(msg):
            return False, "message_not_found_after_send"
 
        await asyncio.sleep(DELIVERY_CONFIRM_SECONDS)
 
        msg = await client.get_messages(entity, ids=message_id)
        if not _is_real_message(msg):
            return False, "message_deleted_after_send"
 
        return True, None
    except Exception as e:
        return False, f"delivery_check_failed: {str(e)[:160]}"
 
 
async def _confirm_or_retry(
    client,
    entity,
    sent,
    account: Account,
    chat_id: str,
    attempt: int,
) -> tuple[bool, str | None, int | None]:
    message_id = _extract_message_id(sent)
    confirmed, reason = await _message_still_exists(client, entity, message_id)
    if confirmed:
        return True, None, message_id
 
    log.warning(
        "Сообщение %s → %s не закрепилось в чате (attempt %d/%d): %s",
        account.phone, chat_id, attempt, DELIVERY_CONFIRM_ATTEMPTS, reason,
    )
    return False, reason, message_id
 
 
async def _send_with_client(
    client,
    account: Account,
    task_id: int,
    chat_id: str,
    message_text: str,
    has_media: bool,
    entities_json: list[dict],
) -> tuple[bool, str | None, bool, int | None]:
    """
    Отправить сообщение в один чат.
    Возвращает (success, error, need_failover, message_id).
    Успех засчитывается только если сообщение реально есть в чате
    сразу после отправки и всё ещё есть через 5 секунд.
    """
    entities = _to_telethon_entities(entities_json)
 
    try:
        entity = await _resolve_entity(client, chat_id)
        if entity is None:
            return False, "entity_not_found", False, None
 
        last_error: str | None = None
        last_message_id: int | None = None
 
        for attempt in range(1, DELIVERY_CONFIRM_ATTEMPTS + 1):
            send_text = _randomize_text(message_text)
 
            if not has_media:
                sent = await client.send_message(
                    entity, send_text or "",
                    formatting_entities=entities or None,
                )
                confirmed, reason, message_id = await _confirm_or_retry(
                    client, entity, sent, account, chat_id, attempt
                )
                if confirmed:
                    return True, None, False, message_id
                last_error, last_message_id = reason, message_id
                continue
 
            media_files = _get_task_media_files(task_id)
 
            if not media_files:
                log.warning("Задача %d: файлы медиа не найдены на диске, отправляем текст", task_id)
                sent = await client.send_message(
                    entity, send_text or "",
                    formatting_entities=entities or None,
                )
                confirmed, reason, message_id = await _confirm_or_retry(
                    client, entity, sent, account, chat_id, attempt
                )
                if confirmed:
                    return True, None, False, message_id
                last_error, last_message_id = reason, message_id
                continue
 
            bufs = []
            for path in media_files:
                try:
                    data = path.read_bytes()
                    buf = io.BytesIO(data)
                    buf.name = path.name
                    bufs.append(buf)
                except Exception as e:
                    log.warning("Не удалось прочитать медиа %s: %s", path, e)
 
            if not bufs:
                log.warning("Задача %d: все медиа-файлы недоступны, отправляем текст", task_id)
                sent = await client.send_message(
                    entity, send_text or "",
                    formatting_entities=entities or None,
                )
                confirmed, reason, message_id = await _confirm_or_retry(
                    client, entity, sent, account, chat_id, attempt
                )
                if confirmed:
                    return True, None, False, message_id
                last_error, last_message_id = reason, message_id
                continue
 
            if len(bufs) == 1:
                sent = await client.send_file(
                    entity, file=bufs[0],
                    caption=send_text or "",
                    formatting_entities=entities or None,
                )
            else:
                sent = await client.send_file(
                    entity, file=bufs,
                    caption=send_text or "",
                    formatting_entities=entities or None,
                )
 
            confirmed, reason, message_id = await _confirm_or_retry(
                client, entity, sent, account, chat_id, attempt
            )
            if confirmed:
                return True, None, False, message_id
            last_error, last_message_id = reason, message_id
 
        return False, last_error or "message_deleted_after_send", True, last_message_id
 
    except FloodWaitError as e:
        wait = min(e.seconds, 60)
        log.warning("FloodWait %ds для %s в %s", e.seconds, account.phone, chat_id)
        await asyncio.sleep(wait)
 
        try:
            retry_text = _randomize_text(message_text)
            entity     = await _resolve_entity(client, chat_id)
            if entity:
                sent = await client.send_message(
                    entity, retry_text or "",
                    formatting_entities=_to_telethon_entities(entities_json) or None,
                )
                confirmed, reason, message_id = await _confirm_or_retry(
                    client, entity, sent, account, chat_id, DELIVERY_CONFIRM_ATTEMPTS
                )
                if confirmed:
                    return True, None, False, message_id
                return False, reason, True, message_id
        except Exception as retry_e:
            return False, str(retry_e)[:200], False, None
 
    except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
        err = f"{type(e).__name__}: {e}"
        log.warning("Нет доступа %s → %s: %s", account.phone, chat_id, err)
        return False, err, True, None
 
    except (UserDeactivatedError, AuthKeyUnregisteredError) as e:
        err = f"account_frozen: {type(e).__name__}"
        log.error("Аккаунт %s заморожен/деактивирован", account.phone)
        asyncio.create_task(_mark_account_frozen(account.id))
        await remove_client(account.id)
        return False, err, True, None
 
    except Exception as e:
        err_str = str(e)
        err_low = err_str.lower()
        is_access_err = any(k in err_low for k in [
            "banned", "forbidden", "deactivated", "not allowed",
            "restricted", "kicked", "channel_private",
        ])
        log.error("Ошибка отправки %s → %s: %s", account.phone, chat_id, err_str)
        return False, err_str[:200], is_access_err, None
 
    return False, "unknown", False, None
 
 
# ── Failover ───────────────────────────────────────────────────────────────────
 
async def _remove_chat_from_account(
    db: AsyncSession,
    task_id: int,
    account_id: int,
    chat_id: str,
):
    """
    Убрать конкретный чат из назначения аккаунта в БД.
    Не переназначает другому — только удаляет.
    """
    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task_id,
            TaskAccount.account_id == account_id,
        )
    )
    ta = result.scalar_one_or_none()
    if ta:
        ids = json.loads(ta.chat_ids or "[]")
        ids = [x for x in ids if str(x) != str(chat_id)]
        if ids:
            ta.chat_ids = json.dumps(ids)
        else:
            await db.delete(ta)
 
    result_acc = await db.execute(select(Account).where(Account.id == account_id))
    acc = result_acc.scalar_one_or_none()
    if acc:
        acc.chats_count = max(0, acc.chats_count - 1)
 
    await db.commit()
 
 
async def _try_failover(
    db: AsyncSession,
    task: Task,
    failed_account_id: int,
    chat_id: str,
    error: str | None = None,
):
    from services.restriction_service import handle_chat_restriction
 
    # 1. Помечаем упавший аккаунт как заблокированный для этого чата
    _ban_account_from_chat(task.id, failed_account_id, chat_id)
 
    # 2. Сразу убираем чат из назначения упавшего аккаунта в БД
    await _remove_chat_from_account(db, task.id, failed_account_id, chat_id)
 
    # 3. Убираем job для упавшего аккаунта чтобы не запускался снова
    old_job_id = _make_job_id(task.id, failed_account_id, chat_id)
    if scheduler.get_job(old_job_id):
        scheduler.remove_job(old_job_id)
    _loaded_jobs.pop(old_job_id, None)
 
    # 4. Ищем кандидатов — исключаем всех уже попробовавших для этого чата
    result = await db.execute(
        select(Account).where(
            Account.is_system == True,
            Account.is_active == True,
            Account.is_banned == False,
            Account.status == "ok",
            Account.id != failed_account_id,
        ).order_by(Account.sends_last_hour.asc(), Account.chats_count.asc())
    )
    candidates = [
        c for c in result.scalars().all()
        if not _is_banned_from_chat(task.id, c.id, chat_id)
    ]
 
    pool_candidates = [c for c in candidates if c.id in _client_pool]
    ordered = pool_candidates + [c for c in candidates if c.id not in pool_candidates]
 
    message_text    = task.message or ""
    has_media       = task.has_media
    format_entities = []
    try:
        format_entities = json.loads(task.format_entities or "[]")
    except Exception:
        pass
 
    for new_acc in ordered[:5]:
        client = await get_client(new_acc)
        if client is None:
            continue
 
        _try_lock_account(new_acc.id)
        try:
            await _wait_rate_limit(new_acc.id)
            success, err, _, failover_msg_id = await _send_with_client(
                client, new_acc, task.id, chat_id,
                message_text, has_media, format_entities,
            )
            _mark_sent(new_acc.id)
        finally:
            _unlock_account(new_acc.id)
 
        if success:
            # Назначаем чат новому аккаунту и создаём job
            await _reassign_chat(db, task.id, failed_account_id, new_acc.id, chat_id)
            await _increment_sends(db, new_acc)
            await db.commit()
 
            log.info("✓ Failover: %s → %s (task=%d)", chat_id, new_acc.phone, task.id)
 
            new_job_id = _make_job_id(task.id, new_acc.id, chat_id)
            interval   = task.interval_minutes
 
            scheduler.add_job(
                send_chat_job,
                "interval",
                minutes=interval,
                id=new_job_id,
                args=[task.id, new_acc.id, chat_id],
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=interval),
                replace_existing=True,
            )
            _loaded_jobs[new_job_id] = interval
 
            db.add(Log(task_id=task.id, account_id=new_acc.id,
                       chat_id=chat_id, message_id=failover_msg_id,
                       success=True, error="failover_success"))
            await db.commit()
            return
        else:
            # Этот кандидат тоже не смог — помечаем, берём следующего
            _ban_account_from_chat(task.id, new_acc.id, chat_id)
            log.warning(
                "Failover кандидат %s тоже не может писать в %s (task=%d): %s",
                new_acc.phone, chat_id, task.id, err,
            )
 
    # 5. Ни один аккаунт не смог — удаляем чат из задачи и уведомляем пользователя
    log.warning(
        "Все аккаунты заблокированы в чате %s (task=%d) — удаляем чат из задачи",
        chat_id, task.id,
    )
    acc_obj = await db.get(Account, failed_account_id)
    if acc_obj:
        await handle_chat_restriction(
            db=db,
            account=acc_obj,
            task_id=task.id,
            chat_id=chat_id,
            reason=error or "all_accounts_banned",
        )
 
 
async def _reassign_chat(
    db: AsyncSession,
    task_id: int,
    old_account_id: int,
    new_account_id: int,
    chat_id: str,
):
    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task_id,
            TaskAccount.account_id == old_account_id,
        )
    )
    old_ta = result.scalar_one_or_none()
    if old_ta:
        old_ids = json.loads(old_ta.chat_ids or "[]")
        old_ids = [x for x in old_ids if str(x) != str(chat_id)]
        if old_ids:
            old_ta.chat_ids = json.dumps(old_ids)
        else:
            await db.delete(old_ta)
 
    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task_id,
            TaskAccount.account_id == new_account_id,
        )
    )
    new_ta = result.scalar_one_or_none()
    if new_ta:
        new_ids = json.loads(new_ta.chat_ids or "[]")
        if str(chat_id) not in [str(x) for x in new_ids]:
            new_ids.append(str(chat_id))
        new_ta.chat_ids = json.dumps(new_ids)
    else:
        from models import TaskAccount as TA
        db.add(TA(task_id=task_id, account_id=new_account_id,
                  chat_ids=json.dumps([str(chat_id)])))
 
    result_old = await db.execute(select(Account).where(Account.id == old_account_id))
    acc_old = result_old.scalar_one_or_none()
    if acc_old:
        acc_old.chats_count = max(0, acc_old.chats_count - 1)
 
    result_new = await db.execute(select(Account).where(Account.id == new_account_id))
    acc_new = result_new.scalar_one_or_none()
    if acc_new:
        acc_new.chats_count += 1
 
 
async def _mark_account_frozen(account_id: int):
    async with SessionLocal() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        acc = result.scalar_one_or_none()
        if acc and acc.status == "ok":
            acc.status    = "frozen"
            acc.is_banned = True
            acc.is_active = False
            await db.commit()
            log.warning("Аккаунт %s помечен как frozen", acc.phone)
 
 
# ── Вспомогательные функции ────────────────────────────────────────────────────
 
def _normalize_chat_id(chat_id: str) -> str:
    s = chat_id.strip()
    if s.startswith("@"):
        return f"@{s.lstrip('@')}"
    return s
 
 
async def _resolve_entity(client, chat_id: str):
    chat_id = _normalize_chat_id(chat_id)
    if chat_id.startswith("@"):
        try:
            return await client.get_entity(chat_id)
        except Exception:
            return None
    if chat_id.lstrip("-").isdigit():
        numeric = int(chat_id)
        try:
            return await client.get_entity(numeric)
        except Exception:
            pass
        if numeric > 0:
            try:
                return await client.get_entity(int(f"-100{numeric}"))
            except Exception:
                pass
        return None
    try:
        return await client.get_entity(chat_id)
    except Exception:
        return None
 
 
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
 
 
# ── Фоновые проверки ──────────────────────────────────────────────────────────
 
async def check_accounts():
    async with SessionLocal() as db:
        result = await db.execute(
            select(Account).where(Account.is_active == True, Account.is_banned == False)
        )
        accounts = result.scalars().all()
 
    for account in accounts:
        client = _client_pool.get(account.id)
        if client and client.is_connected():
            try:
                if not await client.is_user_authorized():
                    await _mark_account_frozen(account.id)
                    await remove_client(account.id)
            except Exception as e:
                log.error("check_accounts %s: %s", account.phone, e)
 
 
async def check_restrictions():
    try:
        from services.restriction_service import run_full_restriction_check
        await run_full_restriction_check()
    except Exception as e:
        log.error("check_restrictions: %s", e)
async def check_recovery():
    try:
        from services.restriction_service import run_recovery_check
        await run_recovery_check()
    except Exception as e:
        log.error("check_recovery: %s", e)
 
async def warmup_pool():
    async with SessionLocal() as db:
        result = await db.execute(
            select(Account).where(
                Account.is_system == True,
                Account.is_active == True,
                Account.is_banned == False,
                Account.status == "ok",
            )
        )
        accounts = result.scalars().all()
 
    log.info("Прогрев пула: %d системных аккаунтов...", len(accounts))
    for acc in accounts:
        await get_client(acc)
        await asyncio.sleep(0.5)
    log.info("Прогрев пула завершён. Подключено: %d", len(_client_pool))
 
 
# ── Точка входа ───────────────────────────────────────────────────────────────
 
async def main():
    await create_all_tables()
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    await warmup_pool()
 
    scheduler.add_job(sync_tasks,           "interval", seconds=30,  id="__sync__")
    scheduler.add_job(keepalive_clients,    "interval", minutes=5,   id="__keepalive__")
    scheduler.add_job(check_accounts,       "interval", hours=1,     id="__check_accs__")
    scheduler.add_job(reset_sends_counters, "interval", hours=1,     id="__reset_sends__")
    scheduler.add_job(check_restrictions,   "interval", minutes=30,  id="__restrictions__")
    scheduler.add_job(check_recovery,       "interval", hours=2,     id="__recovery__")
 
    scheduler.start()
    log.info("Воркер запущен.")
 
    await sync_tasks()
 
    while True:
        await asyncio.sleep(60)
 
 
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WORKER] %(levelname)s: %(message)s",
    )
    asyncio.run(main())
