"""
services/restriction_service.py — обнаружение и обработка ограничений на аккаунтах.
 
ПЕРЕПИСАНО (надёжность детекта спамблока/заморозки):
 
  Проблема была в том, что:
    1. Спамблок определялся ГЛАВНЫМ ОБРАЗОМ по тексту ответа @SpamBot —
       а это произвольный текст, который может не совпасть с шаблоном,
       или в истории чата может попасться СТАРОЕ сообщение (не ответ на
       текущий /start), что давало ложный спамблок на чистом аккаунте.
    2. Резервная проверка через SPAMCHECK_USERNAME по факту ничего не решала:
       она просто отправляла сообщение и проглатывала почти все ошибки.
    3. Любая ошибка connect() в периodической проверке тихо пропускалась —
       статус аккаунта не менялся вообще, даже если он реально заморожен.
    4. Если аккаунт ОДНАЖДЫ помечался spamblocked — его больше никогда не
       перепроверяли (run_full_restriction_check фильтрует только status=="ok"),
       то есть даже после снятия ограничения Telegram'ом аккаунт навсегда
       оставался заблокированным в нашей системе.
 
  Новый подход:
    - Главный (авторитетный) сигнал спамблока — реальная ошибка Telegram
      API `PeerFloodError` при попытке отправить сообщение в SPAMCHECK_USERNAME.
      Это нативная ошибка именно про спам-ограничение аккаунта.
    - Ответ @SpamBot используется ТОЛЬКО как вспомогательный сигнал и
      ТОЛЬКО если это свежее сообщение (дата > времени нашего /start,
      не наше собственное сообщение) — иначе он не может перебить
      результат реальной отправки.
    - "Страйки": статус spamblocked ставится только после
      SPAM_STRIKE_THRESHOLD независимых подтверждений подряд (по умолчанию 2),
      разнесённых по разным циклам проверки. Любая сетевая/неопределённая
      ошибка НЕ считается подтверждением и не сбрасывает счётчик понапрасну.
    - Ошибки connect() классифицируются явно: либо это реальный маркер
      заморозки (UserDeactivatedError/AuthKeyUnregisteredError/...) — тогда
      аккаунт помечается frozen, либо это транзитная ошибка — тогда статус
      не трогаем и не делаем вид что всё ОК.
    - Добавлена run_recovery_check() — периодически перепроверяет уже
      заблокированные аккаунты и автоматически возвращает их в строй, если
      ограничение реально снято Telegram'ом.
 
  ИЗМЕНЕНИЯ (из прошлой сессии, про зеркала):
    - _get_bot_for_user(user_id): вместо Bot(token=BOT_TOKEN) ищет зеркало
      пользователя в БД (MirrorBot) и шлёт через него. Фолбек — BOT_TOKEN.
    - handle_frozen_account, handle_spamblocked_account, handle_chat_restriction,
      check_account_on_send_error, run_full_restriction_check — не принимают
      аргумент bot: Bot; каждый сам создаёт правильный бот через _get_bot_for_user.
 
  ИЗМЕНЕНИЯ (обход ограничений чата — капчи/квизы ботов-админов и обязательная
  подписка на каналы, см. блок в конце файла):
    - try_bypass_restriction(account, chat_id) — вызывается при ПЕРВОЙ
      неудачной отправке в чат (из worker.py и из tasks.py при создании
      задачи), до того как аккаунт помечается забаненным в этом чате /
      запускается обычный failover. Решает капчу/квиз от бота-админа в ЛС
      через Groq и/или вступает в каналы, если чат требует подписки.
      Ничего не сохраняет в БД — проверка выполняется каждый раз заново.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
 
import aiohttp
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    PeerFloodError,
    UserAlreadyParticipantError,
)
from telethon.tl import types as tl_types
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import GetHistoryRequest
 
from config import (
    OWNER_ID, BOT_TOKEN, SPAMCHECK_USERNAME,
    GROQ_API_KEY, GROQ_MODEL,
)
from models import Account, Task, TaskAccount, TaskChat, MirrorBot
from services.account_service import make_client, can_write_to_chat
 
log = logging.getLogger(__name__)
 
_SPAMCHECK_COOLDOWN = 1800  # 30 минут — троттлинг проверки одного аккаунта
_last_spamcheck: dict[int, float] = {}
 
# Сколько независимых подтверждений подряд нужно, прежде чем реально
# пометить аккаунт spamblocked. Защита от единичных глюков/таймаутов.
SPAM_STRIKE_THRESHOLD = 2
 
# Маркеры реальной заморозки аккаунта (не путать с обычной сетевой ошибкой)
_FROZEN_ERROR_MARKERS = [
    "userdeactivated", "authkeyunregistered",
    "session_revoked", "sessionrevoked",
    "auth_key_unregistered", "user_deactivated",
]
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ПОЛУЧЕНИЕ БОТА ДЛЯ УВЕДОМЛЕНИЯ
# ─────────────────────────────────────────────────────────────────────────────
 
async def _get_bot_for_user(user_id: int) -> Bot:
    """
    Вернуть Bot через который пользователь получит уведомление.
 
    Цепочка поиска:
      1. Собственное зеркало пользователя (MirrorBot.user_id == user_id)
      2. Любое активное зеркало из БД (пользователь работает через чужое/демо-зеркало)
      3. Фолбек на GENERATOR_BOT_TOKEN / BOT_TOKEN — крайний случай
 
    Сообщение в любом случае отправляется конкретному user_id,
    а не всем пользователям бота — Telegram Bot API гарантирует это.
 
    ВАЖНО: вызывающий код должен закрыть сессию через await bot.session.close().
    """
    try:
        from database import SessionLocal
        from config import GENERATOR_BOT_TOKEN
        async with SessionLocal() as db:
            # 1. Собственное зеркало пользователя
            result = await db.execute(
                select(MirrorBot).where(
                    MirrorBot.user_id == user_id,
                    MirrorBot.is_active == True,
                )
            )
            mirror = result.scalar_one_or_none()
            if mirror and mirror.token:
                log.debug("_get_bot_for_user(%d): используем своё зеркало @%s",
                          user_id, mirror.bot_username)
                return Bot(token=mirror.token)
 
            # 2. Любое активное зеркало
            result = await db.execute(
                select(MirrorBot).where(MirrorBot.is_active == True).limit(1)
            )
            any_mirror = result.scalar_one_or_none()
            if any_mirror and any_mirror.token:
                log.debug("_get_bot_for_user(%d): своего зеркала нет, "
                          "используем @%s", user_id, any_mirror.bot_username)
                return Bot(token=any_mirror.token)
 
        # 3. Фолбек на generator бот (основная точка входа)
        return Bot(token=GENERATOR_BOT_TOKEN)
 
    except Exception as e:
        log.warning("_get_bot_for_user(%d): ошибка поиска зеркала: %s", user_id, e)
 
    return Bot(token=BOT_TOKEN)
 
 
def _normalize_chat_id(chat_id: str) -> str:
    s = str(chat_id).strip()
    if s.startswith("@"):
        username = s.lstrip("@")
        return f"@{username}"
    return s
 
 
# ─────────────────────────────────────────────────────────────────────────────
# КЛАССИФИКАЦИЯ ОШИБОК
# ─────────────────────────────────────────────────────────────────────────────
 
def _classify_connect_error(e: Exception) -> str:
    """
    Классифицирует ошибку connect()/is_user_authorized().
    Возвращает "frozen" только если это реальный маркер заморозки,
    иначе "transient" — статус аккаунта трогать нельзя, мог быть просто
    сетевой сбой.
    """
    low = (type(e).__name__ + " " + str(e)).lower()
    if any(m in low for m in _FROZEN_ERROR_MARKERS):
        return "frozen"
    return "transient"
 
 
def _classify_send_error(e: Exception) -> str:
    """
    Классифицирует ошибку реальной отправки сообщения при spam-проверке.
    "restricted" — авторитетный сигнал спамблока.
    "transient"  — сетевая/неопределённая ошибка, ничего не доказывает.
    """
    if isinstance(e, PeerFloodError):
        return "restricted"
    if isinstance(e, FloodWaitError):
        return "transient"
    low = (type(e).__name__ + " " + str(e)).lower()
    if "peer_flood" in low or "too many requests" in low:
        return "restricted"
    if any(m in low for m in _FROZEN_ERROR_MARKERS):
        return "frozen"
    return "transient"
 
 
# ─────────────────────────────────────────────────────────────────────────────
# НИЗКОУРОВНЕВЫЕ ПРОВЕРКИ
# ─────────────────────────────────────────────────────────────────────────────
 
async def is_account_frozen(client: TelegramClient) -> bool:
    """Дополнительная проверка заморозки уже ПОСЛЕ успешного connect()."""
    try:
        me = await client.get_me()
        return me is None
    except Exception as e:
        return _classify_connect_error(e) == "frozen"
 
 
async def _check_spambot_text(client: TelegramClient) -> str:
    """
    Вспомогательная (НЕ решающая) проверка через текст ответа @SpamBot.
 
    Возвращает "restricted" | "clear" | "unknown".
 
    Ключевое исправление: берём ТОЛЬКО сообщение, которое:
      - пришло СТРОГО ПОЗЖЕ момента нашего /start (не старое сообщение
        из истории, как было раньше — именно это давало ложные срабатывания)
      - не является нашим собственным исходящим сообщением (msg.out)
 
    Если свежий ответ не пришёл за разумное время — возвращаем "unknown",
    а не делаем вывод о блокировке.
    """
    try:
        sent = await client.send_message("SpamBot", "/start")
        sent_time = sent.date
    except FloodWaitError as e:
        log.info("FloodWait %ds при обращении к @SpamBot — результат неопределён", e.seconds)
        await asyncio.sleep(min(e.seconds, 10))
        return "unknown"
    except Exception as e:
        log.info("Не удалось написать @SpamBot (%s) — результат неопределён", e)
        return "unknown"
 
    OK_PHRASES = [
        "свободен от", "нет ограничений", "все в порядке", "всё в порядке",
        "good standing", "no limits", "free of", "no restrictions", "not limited",
    ]
    BAN_PHRASES = [
        "ваш аккаунт ограничен", "аккаунт ограничен", "ограничения на отправку",
        "заблокирован от отправки", "не можете отправлять сообщения",
        "your account is limited", "your account has been limited",
        "you can't send messages", "you cannot send messages",
        "sending messages has been limited",
    ]
 
    # Поллим до ~10 секунд, ждём именно СВЕЖИЙ ответ
    for _ in range(5):
        await asyncio.sleep(2)
        try:
            history = await client(GetHistoryRequest(
                peer="SpamBot",
                limit=5,
                offset_date=None,
                offset_id=0,
                max_id=0,
                min_id=0,
                add_offset=0,
                hash=0,
            ))
        except Exception as e:
            log.debug("Не удалось получить историю @SpamBot: %s", e)
            continue
 
        for msg in history.messages:
            # Пропускаем своё же исходящее сообщение и всё, что было ДО /start
            if getattr(msg, "out", False):
                continue
            if msg.date is None or msg.date <= sent_time:
                continue
            txt = (msg.message or "").lower()
            if not txt:
                continue
            if any(p in txt for p in OK_PHRASES):
                return "clear"
            if any(p in txt for p in BAN_PHRASES):
                return "restricted"
 
    return "unknown"
 
 
async def check_spam_restriction(client: TelegramClient) -> tuple[str, str]:
    """
    Главная функция проверки спам-ограничения.
    Возвращает (verdict, detail), verdict ∈ {"restricted", "clear", "unknown"}.
 
    Авторитетный сигнал — реальная отправка сообщения в SPAMCHECK_USERNAME:
      PeerFloodError ⇒ "restricted" (это нативная ошибка Telegram именно
      про спам-ограничение аккаунта, не угадывание по тексту).
 
    Ответ @SpamBot используется только как ДОПОЛНИТЕЛЬНЫЙ сигнал и никогда
    не может в одиночку перебить успешную реальную отправку.
    """
    if not SPAMCHECK_USERNAME:
        log.warning("SPAMCHECK_USERNAME не задан в .env — проверка спамблока пропущена")
        return "unknown", "SPAMCHECK_USERNAME не настроен"
 
    target = SPAMCHECK_USERNAME.lstrip("@")
 
    # ── Основная проверка: реальная отправка ───────────────────────────────
    send_verdict = "unknown"
    send_detail  = ""
    try:
        await client.send_message(target, "🔍")
        send_verdict = "clear"
        try:
            await client.delete_dialog(target, revoke=False)
        except Exception:
            pass  # удаление диалога не критично
    except Exception as e:
        kind = _classify_send_error(e)
        send_detail = str(e)[:120]
        if kind == "restricted":
            send_verdict = "restricted"
        elif kind == "frozen":
            # Заморозка обнаруживается раньше (на connect()), но на всякий
            # случай отмечаем — вызывающий код проверит is_user_authorized заново
            send_verdict = "unknown"
            log.warning("При spam-проверке поймали маркер заморозки: %s", e)
        else:
            log.info("Spamcheck-отправка не удалась некритично (%s) — результат неопределён", e)
            send_verdict = "unknown"
 
    # ── Вспомогательная проверка: текст ответа @SpamBot ─────────────────────
    spambot_verdict = await _check_spambot_text(client)
 
    if send_verdict == "restricted":
        return "restricted", f"PeerFlood при отправке в @{target} (SpamBot: {spambot_verdict})"
 
    if send_verdict == "clear":
        if spambot_verdict == "restricted":
            # Расхождение сигналов: реальная отправка прошла успешно, но
            # текст SpamBot похож на "ограничен". Верим реальной отправке —
            # текстовый разбор ненадёжен (см. шапку файла), но логируем для
            # ручной проверки если паттерн повторится.
            log.warning(
                "Расхождение: реальная отправка в @%s прошла, но SpamBot "
                "вернул похожий на ограничение текст — считаем чистым",
                target,
            )
        return "clear", f"реальная отправка в @{target} прошла (SpamBot: {spambot_verdict})"
 
    # send_verdict == "unknown"
    if spambot_verdict == "restricted":
        # Слабый сигнал сам по себе — не авторитетный, но не игнорируем
        # полностью, помечаем как restricted с пометкой "weak", чтобы страйк
        # всё же учитывался (порог в 2 подтверждения подряд защищает от ложных)
        return "restricted", f"только SpamBot (слабый сигнал, отправка неопределённая: {send_detail})"
 
    return "unknown", f"send={send_verdict} ({send_detail}) spambot={spambot_verdict}"
 
 
async def check_chat_access_light(
    client: TelegramClient,
    chat_id: str,
) -> tuple[bool, str]:
    chat_id = _normalize_chat_id(chat_id)
 
    entity = None
    try:
        if chat_id.startswith("@"):
            entity = await client.get_entity(chat_id)
        elif not chat_id.lstrip("-").isdigit():
            entity = await client.get_entity(f"@{chat_id}")
        else:
            try:
                entity = await client.get_entity(int(chat_id))
            except Exception:
                try:
                    n = int(chat_id)
                    if n > 0:
                        entity = await client.get_entity(int(f"-100{n}"))
                except Exception:
                    pass
    except Exception as e:
        err = str(e).lower()
        if "private" in err or "channel_private" in err:
            return False, "private"
        log.warning("check_chat_access_light: не удалось найти чат '%s': %s", chat_id, e)
        return True, "resolve_failed"
 
    if entity is None:
        log.warning("check_chat_access_light: entity=None для '%s'", chat_id)
        return True, "resolve_failed"
 
    if isinstance(entity, tl_types.Channel):
        try:
            me = await client.get_me()
            result = await client(GetParticipantRequest(
                channel=entity,
                participant=me.id,
            ))
            p = result.participant
 
            if isinstance(p, tl_types.ChannelParticipantBanned):
                return False, "banned"
 
            banned_rights = getattr(p, "banned_rights", None)
            if banned_rights and getattr(banned_rights, "send_messages", False):
                return False, "write_forbidden"
 
        except Exception as e:
            err = str(e).lower()
            if "not_participant" in err or "not participant" in err:
                return False, "kicked"
            if "channel_private" in err or "private" in err:
                return False, "private"
            if "banned" in err:
                return False, "banned"
            log.debug("GetParticipant %s: %s (игнорируем)", chat_id, e)
 
    return True, "ok"
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ОПЕРАЦИИ С БД
# ─────────────────────────────────────────────────────────────────────────────
 
async def stop_account_tasks(db: AsyncSession, account: Account) -> int:
    result = await db.execute(
        select(TaskAccount).where(TaskAccount.account_id == account.id)
    )
    task_accounts = result.scalars().all()
    task_ids = {ta.task_id for ta in task_accounts}
    stopped = 0
 
    for task_id in task_ids:
        result = await db.execute(
            select(Task).where(Task.id == task_id, Task.is_active == True)
        )
        task = result.scalar_one_or_none()
        if task:
            task.is_active = False
            stopped += 1
 
    await db.commit()
    return stopped
 
 
async def _notify_unavailable_system_chats(task: Task, count: int):
    if count <= 0:
        return
 
    bot = await _get_bot_for_user(task.user_id)
    try:
        await bot.send_message(
            task.user_id,
            f"⚠️ *Часть рассылки стала недоступна*\n\n"
            f"Задача: *{task.name}*\n\n"
            f"Рассылка в *{count}* чат(ов) стала недоступна из системных аккаунтов. "
            f"Попробуйте пользовательский аккаунт или обратитесь к админу.",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning(
            "Не удалось отправить сводное уведомление по задаче %d: %s",
            task.id, e,
        )
    finally:
        await bot.session.close()
 
 
async def _can_deliver_task_message(
    account: Account,
    task: Task,
    chat_id: str,
) -> tuple[bool, str | None]:
    from worker.worker import (
        _mark_sent,
        _send_with_client,
        _try_lock_account,
        _unlock_account,
        _wait_rate_limit,
        get_client,
    )
 
    client = await get_client(account)
    if client is None:
        return False, "client_unavailable"
 
    locked = False
    for _ in range(30):
        locked = _try_lock_account(account.id)
        if locked:
            break
        await asyncio.sleep(1)
    if not locked:
        return False, "account_busy"
 
    try:
        entities_json = []
        try:
            entities_json = json.loads(task.format_entities or "[]")
        except Exception:
            pass
 
        await _wait_rate_limit(account.id)
        success, error, _, _ = await _send_with_client(
            client=client,
            account=account,
            task_id=task.id,
            chat_id=chat_id,
            message_text=task.message or "",
            has_media=task.has_media,
            entities_json=entities_json,
        )
        if success:
            _mark_sent(account.id)
        return success, error
    finally:
        _unlock_account(account.id)
 
 
async def redistribute_system_chats(db: AsyncSession, blocked_account: Account) -> int:
    result = await db.execute(
        select(TaskAccount).where(TaskAccount.account_id == blocked_account.id)
    )
    old_task_accounts = result.scalars().all()
 
    if not old_task_accounts:
        return 0
 
    result = await db.execute(
        select(Account).where(
            Account.is_system == True,
            Account.is_active == True,
            Account.is_banned == False,
            Account.id != blocked_account.id,
            Account.status == "ok",
        ).order_by(Account.chats_count.asc())
    )
    available = list(result.scalars().all())
 
    used_ids: set[int] = set()
    unavailable_by_task: dict[int, int] = {}
 
    if not available:
        log.warning("Нет доступных системных аккаунтов для перераспределения от %s", blocked_account.phone)
        for ta in old_task_accounts:
            result = await db.execute(
                select(Task).where(Task.id == ta.task_id, Task.is_active == True)
            )
            task = result.scalar_one_or_none()
            if not task:
                continue
            try:
                chat_ids = json.loads(ta.chat_ids or "[]")
            except Exception:
                chat_ids = []
            for raw_chat_id in list(chat_ids):
                await remove_chat_from_task(
                    db, task, blocked_account, _normalize_chat_id(str(raw_chat_id))
                )
                unavailable_by_task[task.id] = unavailable_by_task.get(task.id, 0) + 1
            await db.commit()
 
        for task_id, count in unavailable_by_task.items():
            result = await db.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if task:
                await _notify_unavailable_system_chats(task, count)
        return 0
 
    for ta in old_task_accounts:
        result = await db.execute(
            select(Task).where(Task.id == ta.task_id, Task.is_active == True)
        )
        task = result.scalar_one_or_none()
        if not task:
            continue
 
        try:
            chat_ids = json.loads(ta.chat_ids or "[]")
        except Exception:
            chat_ids = []
 
        if not chat_ids:
            await db.delete(ta)
            await db.commit()
            continue
 
        for raw_chat_id in list(chat_ids):
            chat_id = _normalize_chat_id(str(raw_chat_id))
            available.sort(key=lambda a: a.chats_count)
 
            delivered = False
            last_error = None
            for acc in available:
                success, error = await _can_deliver_task_message(acc, task, chat_id)
                if not success:
                    last_error = error
                    log.warning(
                        "Редистрибуция: %s не смог отправить в %s (task=%d): %s",
                        acc.phone, chat_id, task.id, error,
                    )
                    continue
 
                await transfer_chat_to_account(db, task, blocked_account, acc, chat_id)
                await db.commit()
                used_ids.add(acc.id)
                delivered = True
                log.info(
                    "Редистрибуция: чат %s перенесён с %s на %s (task=%d)",
                    chat_id, blocked_account.phone, acc.phone, task.id,
                )
                break
 
            if delivered:
                continue
 
            log.warning(
                "Редистрибуция: ни один системный аккаунт не смог отправить в %s "
                "(task=%d), удаляем чат. Последняя ошибка: %s",
                chat_id, task.id, last_error,
            )
            await remove_chat_from_task(db, task, blocked_account, chat_id)
            await db.commit()
            unavailable_by_task[task.id] = unavailable_by_task.get(task.id, 0) + 1
 
    for task_id, count in unavailable_by_task.items():
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            await _notify_unavailable_system_chats(task, count)
 
    return len(used_ids)
 
 
async def find_replacement_system_account(
    db: AsyncSession,
    excluded_account: Account,
    chat_id: str,
) -> Optional[Account]:
    from services.account_service import can_write_to_chat as _can_write_to_chat
 
    chat_id = _normalize_chat_id(chat_id)
 
    result = await db.execute(
        select(Account).where(
            Account.is_system == True,
            Account.is_active == True,
            Account.is_banned == False,
            Account.id != excluded_account.id,
            Account.status == "ok",
        ).order_by(Account.chats_count.asc())
    )
    candidates = result.scalars().all()
 
    for acc in candidates:
        client = make_client(acc)
        try:
            await client.connect()
            await asyncio.sleep(1)
            can_write, _ = await _can_write_to_chat(client, chat_id)
            if can_write:
                return acc
        except Exception as e:
            log.warning("Ошибка проверки замены %s→%s: %s", acc.phone, chat_id, e)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
 
    return None
 
 
async def transfer_chat_to_account(
    db: AsyncSession,
    task: Task,
    old_account: Account,
    new_account: Account,
    chat_id: str,
):
    chat_id = _normalize_chat_id(chat_id)
 
    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task.id,
            TaskAccount.account_id == old_account.id,
        )
    )
    old_ta = result.scalar_one_or_none()
    if old_ta:
        old_ids = json.loads(old_ta.chat_ids or "[]")
        old_ids = [x for x in old_ids if str(x) != str(chat_id)]
        old_ta.chat_ids = json.dumps(old_ids)
        old_account.chats_count = max(0, old_account.chats_count - 1)
        if not old_ids:
            await db.delete(old_ta)
 
    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task.id,
            TaskAccount.account_id == new_account.id,
        )
    )
    new_ta = result.scalar_one_or_none()
    if new_ta:
        new_ids = json.loads(new_ta.chat_ids or "[]")
        if str(chat_id) not in [str(x) for x in new_ids]:
            new_ids.append(str(chat_id))
        new_ta.chat_ids = json.dumps(new_ids)
    else:
        db.add(TaskAccount(
            task_id=task.id,
            account_id=new_account.id,
            chat_ids=json.dumps([str(chat_id)]),
        ))
 
    new_account.chats_count += 1
 
 
async def remove_chat_from_task(
    db: AsyncSession,
    task: Task,
    account: Account,
    chat_id: str,
):
    chat_id = _normalize_chat_id(chat_id)
 
    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task.id,
            TaskAccount.account_id == account.id,
        )
    )
    ta = result.scalar_one_or_none()
    if ta:
        ids = json.loads(ta.chat_ids or "[]")
        ids = [x for x in ids if str(x) != str(chat_id)]
        ta.chat_ids = json.dumps(ids)
        account.chats_count = max(0, account.chats_count - 1)
        if not ids:
            await db.delete(ta)
 
    result = await db.execute(
        select(TaskChat).where(
            TaskChat.task_id == task.id,
            TaskChat.chat_id == str(chat_id),
        )
    )
    tc = result.scalar_one_or_none()
    if tc:
        await db.delete(tc)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ОБРАБОТЧИКИ ОГРАНИЧЕНИЙ
# ─────────────────────────────────────────────────────────────────────────────
 
async def handle_frozen_account(db: AsyncSession, account: Account):
    if account.status == "frozen" and not account.is_system:
        return
 
    log.warning("❄️  Аккаунт %s (id=%d) заморожен", account.phone, account.id)
    account.status = "frozen"
    account.is_banned = True
    account.is_active = False
    account.restriction_strikes = 0
    await db.commit()
 
    if account.is_system:
        redirected = await redistribute_system_chats(db, account)
        bot = await _get_bot_for_user(OWNER_ID)
        try:
            await bot.send_message(
                OWNER_ID,
                f"❄️ *Системный аккаунт заморожен*\n\n"
                f"📱 `{account.phone}`\n"
                f"ID: `{account.id}`\n\n"
                f"Telegram заблокировал аккаунт (deactivated).\n"
                f"Чаты проверены реальной отправкой и перенесены на *{redirected}* аккаунт(ов).\n\n"
                f"Используйте /admin для управления аккаунтами.",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("Ошибка уведомления о заморозке системного: %s", e)
        finally:
            await bot.session.close()
    else:
        stopped = await stop_account_tasks(db, account)
        if account.owner_id:
            bot = await _get_bot_for_user(account.owner_id)
            try:
                await bot.send_message(
                    account.owner_id,
                    f"❄️ *Ваш аккаунт заморожен Telegram*\n\n"
                    f"📱 `{account.phone}`\n\n"
                    f"Telegram деактивировал этот аккаунт.\n"
                    f"Все рассылки остановлены ({stopped} задач).\n\n"
                    f"Для восстановления обратитесь в поддержку Telegram.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            finally:
                await bot.session.close()
 
 
async def handle_spamblocked_account(db: AsyncSession, account: Account):
    if account.status == "spamblocked" and not account.is_system:
        return
 
    log.warning("🚫  Аккаунт %s (id=%d) в спамблоке", account.phone, account.id)
    account.status = "spamblocked"
    account.restriction_strikes = 0
    await db.commit()
 
    if account.is_system:
        redirected = await redistribute_system_chats(db, account)
        bot = await _get_bot_for_user(OWNER_ID)
        try:
            await bot.send_message(
                OWNER_ID,
                f"🚫 *Системный аккаунт в спамблоке*\n\n"
                f"📱 `{account.phone}`\n"
                f"ID: `{account.id}`\n\n"
                f"Чаты проверены реальной отправкой и перенесены на *{redirected}* аккаунт(ов).\n"
                f"Новые задачи на этот аккаунт не назначаются.\n\n"
                f"💡 Аккаунт будет автоматически перепроверен — если ограничение "
                f"снимется само, статус вернётся в норму.\n\n"
                f"Управление аккаунтами: /admin",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("Ошибка уведомления о спамблоке системного: %s", e)
        finally:
            await bot.session.close()
    else:
        stopped = await stop_account_tasks(db, account)
        if account.owner_id:
            bot = await _get_bot_for_user(account.owner_id)
            try:
                await bot.send_message(
                    account.owner_id,
                    f"🚫 *Ваш аккаунт получил спамблок*\n\n"
                    f"📱 `{account.phone}`\n\n"
                    f"Telegram ограничил возможность отправки сообщений с этого аккаунта.\n"
                    f"Остановлено задач: *{stopped}*\n\n"
                    f"💡 Попробуйте снять ограничение через @SpamBot\n\n"
                    f"Управление аккаунтами: /accounts",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            finally:
                await bot.session.close()
 
 
async def handle_chat_restriction(
    db: AsyncSession,
    account: Account,
    task_id: int,
    chat_id: str,
    reason: str,
):
    chat_id = _normalize_chat_id(chat_id)
 
    if reason == "resolve_failed":
        log.warning(
            "handle_chat_restriction: reason=resolve_failed для '%s' acc=%s — пропускаем",
            chat_id, account.phone,
        )
        return
 
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return
 
    result = await db.execute(
        select(TaskChat).where(
            TaskChat.task_id == task_id,
            TaskChat.chat_id == str(chat_id),
        )
    )
    tc = result.scalar_one_or_none()
    chat_title = (tc.chat_title or str(chat_id)) if tc else str(chat_id)
 
    reason_labels = {
        "banned":            "аккаунт заблокирован администратором чата",
        "write_forbidden":   "нет прав на отправку сообщений",
        "kicked":            "аккаунт исключён из чата",
        "private":           "чат стал приватным",
        "not_found":         "чат не найден",
        "too_many_channels": "аккаунт состоит в слишком многих чатах",
    }
    reason_text = reason_labels.get(reason, reason)
 
    if account.is_system:
        new_acc = await find_replacement_system_account(db, account, chat_id)
 
        notify_user_id = task.user_id
        bot = await _get_bot_for_user(notify_user_id)
        try:
            if new_acc:
                await transfer_chat_to_account(db, task, account, new_acc, chat_id)
                await db.commit()
                log.info("Чат %s перенесён с %s на %s (задача %d)",
                         chat_id, account.phone, new_acc.phone, task_id)
                try:
                    await bot.send_message(
                        notify_user_id,
                        f"🔄 *Автоматическая замена аккаунта в рассылке*\n\n"
                        f"Задача: *{task.name}*\n"
                        f"Чат: {chat_title}\n\n"
                        f"Причина: {reason_text}\n\n"
                        f"❌ Старый: `{account.phone}`\n"
                        f"✅ Новый: `{new_acc.phone}`\n\n"
                        f"Рассылка продолжается автоматически.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            else:
                await remove_chat_from_task(db, task, account, chat_id)
                await db.commit()
                try:
                    await bot.send_message(
                        notify_user_id,
                        f"⚠️ *Чат недоступен для рассылки*\n\n"
                        f"Задача: *{task.name}*\n"
                        f"Чат: {chat_title}\n"
                        f"Причина: {reason_text}\n\n"
                        f"Ни один системный аккаунт не может писать в этот чат.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
        finally:
            await bot.session.close()
 
    else:
        await remove_chat_from_task(db, task, account, chat_id)
        await db.commit()
 
        log.info(
            "Чат %s удалён из задачи %d (личный аккаунт %s, reason=%s). "
            "Задача продолжает работать.",
            chat_id, task_id, account.phone, reason,
        )
 
        if account.owner_id:
            import html
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
 
            msg_preview = (task.message or "").strip()
            if len(msg_preview) > 800:
                msg_preview = msg_preview[:800] + "…"
 
            remaining_result = await db.execute(
                select(TaskAccount).where(TaskAccount.task_id == task_id)
            )
            remaining_tas = remaining_result.scalars().all()
            remaining_count = sum(
                len(json.loads(ta.chat_ids or "[]")) for ta in remaining_tas
            )
 
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Перенести этот чат на другой аккаунт",
                    callback_data=f"tasks:transfer_start:{task.id}:{chat_id}",
                )],
                [InlineKeyboardButton(
                    text="📋 К задачам",
                    callback_data="tasks:list",
                )],
                [InlineKeyboardButton(text="◀️ Меню", callback_data="menu:new")],
            ])
 
            remaining_note = (
                f"Рассылка в остальные <b>{remaining_count}</b> чат(ов) продолжается."
                if remaining_count > 0
                else "⚠️ Других чатов в задаче не осталось."
            )
 
            bot = await _get_bot_for_user(account.owner_id)
            try:
                await bot.send_message(
                    account.owner_id,
                    f"⚠️ <b>Чат недоступен — рассылка в него остановлена</b>\n\n"
                    f"Аккаунт: <code>{account.phone}</code>\n"
                    f"Чат: <b>{html.escape(chat_title)}</b>\n"
                    f"Задача: <b>{html.escape(task.name)}</b>\n\n"
                    f"Причина: {html.escape(reason_text)}\n\n"
                    f"{remaining_note}\n\n"
                    f"<b>Текст рассылки:</b>\n"
                    f"<blockquote expandable>{html.escape(msg_preview)}</blockquote>\n\n"
                    f"Нажмите кнопку ниже чтобы перенести этот чат на другой аккаунт.",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("Ошибка отправки уведомления об ограничении: %s", e)
            finally:
                await bot.session.close()
 
 
# ─────────────────────────────────────────────────────────────────────────────
# СТРАЙКИ: РЕШЕНИЕ ПО ВЕРДИКТУ СПАМ-ПРОВЕРКИ
# ─────────────────────────────────────────────────────────────────────────────
 
async def _apply_spam_verdict(db: AsyncSession, account: Account, verdict: str, detail: str):
    """
    Применяет результат check_spam_restriction() к аккаунту со страйк-защитой:
      - "restricted" → увеличиваем счётчик, реальный spamblocked ставим только
        по достижении SPAM_STRIKE_THRESHOLD подряд.
      - "clear"      → сбрасываем счётчик в 0 (ложная тревога не накапливается).
      - "unknown"    → ничего не трогаем (сетевой сбой не должен ни обвинять,
        ни оправдывать аккаунт).
    """
    if verdict == "restricted":
        account.restriction_strikes = (account.restriction_strikes or 0) + 1
        log.warning(
            "⚠️  %s: признак спамблока (%d/%d) — %s",
            account.phone, account.restriction_strikes, SPAM_STRIKE_THRESHOLD, detail,
        )
        await db.commit()
        if account.restriction_strikes >= SPAM_STRIKE_THRESHOLD:
            await handle_spamblocked_account(db, account)
        return
 
    if verdict == "clear":
        if account.restriction_strikes:
            log.info(
                "%s: спамблок не подтвердился, сбрасываю счётчик страйков (%s)",
                account.phone, detail,
            )
            account.restriction_strikes = 0
            await db.commit()
        return
 
    # verdict == "unknown"
    log.info("%s: статус спама неопределён (%s) — оставляем без изменений", account.phone, detail)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ПРОВЕРКА ПРИ ОШИБКЕ ОТПРАВКИ
# ─────────────────────────────────────────────────────────────────────────────
 
async def check_account_on_send_error(
    account_id: int,
    task_id: int,
    chat_id: str,
    send_error: Exception,
):
    chat_id = _normalize_chat_id(chat_id)
 
    now = time.monotonic()
    last = _last_spamcheck.get(account_id, 0)
    if now - last < _SPAMCHECK_COOLDOWN:
        log.debug("Кулдаун проверки аккаунта %d — пропускаем", account_id)
        return
    _last_spamcheck[account_id] = now
 
    from database import SessionLocal
 
    async with SessionLocal() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if not account or account.status != "ok":
            return
 
        client = make_client(account)
        try:
            await client.connect()
            await asyncio.sleep(1)
 
            if not await client.is_user_authorized():
                await handle_frozen_account(db, account)
                return
 
            if await is_account_frozen(client):
                await handle_frozen_account(db, account)
                return
 
            verdict, detail = await check_spam_restriction(client)
            if verdict in ("restricted", "clear"):
                await _apply_spam_verdict(db, account, verdict, detail)
 
            # Если спам-проверка не дала окончательного вердикта, но мы попали
            # сюда из-за реальной ошибки отправки — разбираем саму ошибку
            # отдельно для конкретного чата (banned/forbidden/kicked и т.п.)
            err_name = type(send_error).__name__.lower()
            err_str = str(send_error).lower()
 
            if isinstance(send_error, UserBannedInChannelError) or "banned" in err_name + err_str:
                reason = "banned"
            elif isinstance(send_error, ChatWriteForbiddenError) or "forbidden" in err_name + err_str:
                reason = "write_forbidden"
            elif "participant" in err_str or "kicked" in err_str:
                reason = "kicked"
            else:
                reason = None
 
            if reason:
                await handle_chat_restriction(db, account, task_id, chat_id, reason)
 
        except Exception as e:
            kind = _classify_connect_error(e)
            if kind == "frozen":
                await handle_frozen_account(db, account)
            else:
                log.error("Ошибка в check_account_on_send_error (acc=%d): %s", account_id, e)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ПЕРИОДИЧЕСКАЯ ПРОВЕРКА ВСЕХ АКТИВНЫХ АККАУНТОВ (каждые 30 минут)
# ─────────────────────────────────────────────────────────────────────────────
 
async def run_full_restriction_check():
    from database import SessionLocal
 
    log.info("▶️  Запуск периодической проверки ограничений аккаунтов")
 
    try:
        async with SessionLocal() as db:
            result = await db.execute(
                select(Account).where(
                    Account.is_active == True,
                    Account.is_banned == False,
                    Account.status == "ok",
                )
            )
            accounts = result.scalars().all()
            log.info("Проверка ограничений: %d аккаунтов", len(accounts))
 
            for account in accounts:
                await _check_single_account(db, account)
                await asyncio.sleep(3)
 
    except Exception as e:
        log.error("Критическая ошибка проверки ограничений: %s", e)
 
    log.info("✅  Проверка ограничений завершена")
 
 
async def _check_single_account(db: AsyncSession, account: Account):
    client = make_client(account)
    try:
        await client.connect()
        await asyncio.sleep(1)
 
        if not await client.is_user_authorized():
            await handle_frozen_account(db, account)
            return
 
        if await is_account_frozen(client):
            await handle_frozen_account(db, account)
            return
 
        _last_spamcheck[account.id] = time.monotonic()
 
        verdict, detail = await check_spam_restriction(client)
        await _apply_spam_verdict(db, account, verdict, detail)
 
        if account.status == "ok":
            await _check_account_chat_access(db, account, client)
 
    except Exception as e:
        kind = _classify_connect_error(e)
        if kind == "frozen":
            log.warning(
                "%s: ошибка connect() похожа на заморозку (%s) — помечаю frozen",
                account.phone, e,
            )
            await handle_frozen_account(db, account)
        else:
            # Транзитная ошибка (сеть/таймаут) — статус НЕ трогаем.
            # Раньше здесь аккаунт молча оставался "ok" навсегда даже если
            # реально был мёртв — теперь хотя бы явно видно в логах, а
            # следующий цикл проверки (через 30 мин) попробует снова.
            log.error(
                "Ошибка проверки аккаунта %s (id=%d), статус не меняем "
                "(транзитная ошибка, попробуем в следующем цикле): %s",
                account.phone, account.id, e,
            )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
 
 
# ─────────────────────────────────────────────────────────────────────────────
# RECOVERY: ПЕРЕПРОВЕРКА УЖЕ ЗАБЛОКИРОВАННЫХ АККАУНТОВ
# ─────────────────────────────────────────────────────────────────────────────
 
async def run_recovery_check():
    """
    Раньше аккаунт, помеченный spamblocked, больше никогда не перепроверялся
    (run_full_restriction_check фильтрует только status=="ok") — то есть даже
    после того как Telegram сам снимал ограничение (обычно через 1-3 дня),
    аккаунт навсегда оставался заблокированным в нашей системе.
 
    Эта функция периодически (см. worker.py) перепроверяет именно
    status=="spamblocked" аккаунты и возвращает их в "ok", если ограничение
    реально снято.
 
    Замороженные (status=="frozen") аккаунты НЕ восстанавливаются —
    заморозка Telegram'ом обычно необратима через API, это сознательное
    решение, не баг.
    """
    from database import SessionLocal
 
    log.info("▶️  Проверка восстановления заблокированных аккаунтов")
 
    async with SessionLocal() as db:
        result = await db.execute(
            select(Account).where(Account.status == "spamblocked")
        )
        accounts = list(result.scalars().all())
 
    if not accounts:
        log.info("Recovery: нет аккаунтов в spamblocked")
        return
 
    for account in accounts:
        client = make_client(account)
        try:
            await client.connect()
            await asyncio.sleep(1)
 
            if not await client.is_user_authorized():
                # Аккаунт не просто в спамблоке, а уже и заморожен —
                # переключаем статус на frozen отдельным циклом
                async with SessionLocal() as db2:
                    fresh = await db2.get(Account, account.id)
                    if fresh:
                        await handle_frozen_account(db2, fresh)
                continue
 
            verdict, detail = await check_spam_restriction(client)
 
            if verdict == "clear":
                async with SessionLocal() as db2:
                    fresh = await db2.get(Account, account.id)
                    if fresh and fresh.status == "spamblocked":
                        fresh.status = "ok"
                        fresh.restriction_strikes = 0
                        await db2.commit()
                        log.info("✅ %s: спамблок снят, аккаунт восстановлен", fresh.phone)
 
                        if fresh.is_system:
                            bot = await _get_bot_for_user(OWNER_ID)
                            try:
                                await bot.send_message(
                                    OWNER_ID,
                                    f"✅ *Спамблок снят*\n\n"
                                    f"📱 `{fresh.phone}` снова в строю и "
                                    f"доступен для новых задач.",
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass
                            finally:
                                await bot.session.close()
            else:
                log.info("%s: спамблок ещё не снят (%s, %s)", account.phone, verdict, detail)
 
        except Exception as e:
            log.debug("Recovery check %s: %s", account.phone, e)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
 
        await asyncio.sleep(3)
 
    log.info("✅  Проверка восстановления завершена")
 
 
async def _check_account_chat_access(
    db: AsyncSession,
    account: Account,
    client: TelegramClient,
):
    result = await db.execute(
        select(TaskAccount).where(TaskAccount.account_id == account.id)
    )
    task_accounts = result.scalars().all()
 
    for ta in task_accounts:
        result = await db.execute(
            select(Task).where(Task.id == ta.task_id, Task.is_active == True)
        )
        task = result.scalar_one_or_none()
        if not task:
            continue
 
        try:
            raw_chat_ids = json.loads(ta.chat_ids or "[]")
        except Exception:
            continue
 
        chat_ids = [_normalize_chat_id(str(cid)) for cid in raw_chat_ids]
 
        for chat_id in list(chat_ids):
            can_write, reason = await check_chat_access_light(client, chat_id)
 
            if not can_write:
                log.warning(
                    "Аккаунт %s не может писать в %s: %s",
                    account.phone, chat_id, reason,
                )
                await handle_chat_restriction(
                    db, account, ta.task_id, chat_id, reason
                )
 
            await asyncio.sleep(1)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ОБХОД ОГРАНИЧЕНИЙ: капчи/квизы ботов-админов + обязательная подписка на каналы
# ─────────────────────────────────────────────────────────────────────────────
#
# Запускается при ПЕРВОЙ неудачной отправке в чат (до failover и до пометки
# аккаунта заблокированным в этом чате). Причина блокировки в таких чатах
# обычно не в самом аккаунте, а в двух сценариях:
#   1. Бот-админ чата написал аккаунту в ЛС капчу/квиз с условием вступления.
#   2. В самом чате аккаунт отметили (@упоминание) с требованием подписаться
#      на сторонние каналы прежде чем писать.
# Если удалось решить хотя бы один сценарий — ждём 10 сек и реально
# перепроверяем доступ к чату. Ничего не пишем в БД — проверка выполняется
# заново при каждой неудачной отправке.
 
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
 
 
def _poll_text(x) -> str:
    """poll.question / answer.text могут быть TextWithEntities либо str в зависимости от layer'а."""
    return getattr(x, "text", x) or ""
 
 
def _match_option(answer: str, options: list[str]) -> int | None:
    answer_norm = (answer or "").strip().lower()
    if not answer_norm:
        return None
    for i, opt in enumerate(options):
        if opt.strip().lower() == answer_norm:
            return i
    for i, opt in enumerate(options):
        o = opt.strip().lower()
        if o and (o in answer_norm or answer_norm in o):
            return i
    return None
 
 
async def _ask_ai_pick_option(question: str, options: list[str]) -> str | None:
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY не задан — обход капчи невозможен")
        return None
 
    prompt = (
        "Ты помогаешь выбрать правильный вариант ответа. "
        "Верни СТРОГО ОДНО слово или число, совпадающее с одним из вариантов ответа, "
        "без пояснений, префиксов, кавычек и знаков препинания. "
        "Даже если не уверен — всё равно выбери наиболее вероятный вариант из списка.\n\n"
        f"Вопрос: {question}\n"
        f"Варианты ответа: {', '.join(options)}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 30,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            )
            data = await resp.json()
 
        if "choices" not in data:
            log.error(
                "Groq не вернул choices (HTTP %d): %s",
                resp.status, json.dumps(data, ensure_ascii=False)[:500],
            )
            return None
 
        choice = data["choices"][0]
        content = choice.get("message", {}).get("content")
 
        if not content:
            log.error(
                "Groq вернул пустой content (finish_reason=%s): %s",
                choice.get("finish_reason"), json.dumps(choice, ensure_ascii=False)[:500],
            )
            return None
 
        return content.strip()
    except Exception as e:
        log.error("Groq ошибка: %s", e)
        return None
 
 
async def _ask_ai_is_captcha(text: str, button_texts: list[str]) -> bool | None:
    """
    Спрашивает LLM: сообщение — это капча/квиз (нужно выбрать ЕДИНСТВЕННО
    правильный вариант ответа среди нескольких, например арифметика или
    загадка), или это просто проходная кнопка-действие без выбора
    ("Пройти проверку", "Продолжить", "Я не робот" и т.п., где текст кнопки
    не является вариантом ответа на вопрос).
 
    Возвращает True (капча) / False (проходное действие) / None при ошибке
    ИИ — в этом случае вызывающий код должен считать результат неопределённым
    и не пытаться нажимать кнопки вслепую.
    """
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY не задан — не можем определить капча это или нет")
        return None
 
    prompt = (
        "Тебе дан текст сообщения от Telegram-бота и текст кнопок под ним. "
        "Определи: это КАПЧА/квиз, где нужно выбрать единственно правильный "
        "вариант ответа среди нескольких вариантов (например арифметика, "
        "загадка, проверка на бота с несколькими вариантами ответа) — "
        "или это просто ДЕЙСТВИЕ: техническая кнопка прохождения/подтверждения "
        "(например 'Пройти проверку', 'Я не робот', 'Продолжить', 'Подтвердить', "
        "'Проверить подписку'), где кнопка не является вариантом ответа на "
        "вопрос, а просто действием, которое надо выполнить.\n\n"
        "Ответь СТРОГО одним словом без пояснений: КАПЧА или ДЕЙСТВИЕ.\n\n"
        f"Текст сообщения: {text}\n"
        f"Кнопки: {', '.join(button_texts)}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 10,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            )
            data = await resp.json()
 
        if "choices" not in data:
            log.error(
                "Groq (is_captcha) не вернул choices (HTTP %d): %s",
                resp.status, json.dumps(data, ensure_ascii=False)[:500],
            )
            return None
 
        content = data["choices"][0].get("message", {}).get("content")
        if not content:
            log.error("Groq (is_captcha) вернул пустой content")
            return None
 
        content = content.strip().upper()
        if "КАПЧ" in content:
            return True
        if "ДЕЙСТВ" in content:
            return False
 
        log.warning("Groq (is_captcha) вернул неожиданный ответ: %r", content)
        return None
    except Exception as e:
        log.error("Groq ошибка (is_captcha): %s", e)
        return None
 
 
# ── Сценарий 1: капча/квиз от бота-админа в ЛС ────────────────────────────────
 
async def _solve_quiz(client: TelegramClient, message) -> bool:
    poll = message.media.poll
    question = _poll_text(poll.question)
    options  = [_poll_text(a.text) for a in poll.answers]
 
    answer_text = await _ask_ai_pick_option(question, options)
    if not answer_text:
        return False
 
    idx = _match_option(answer_text, options)
    if idx is None:
        log.warning("Groq вернул '%s' — не нашли совпадение среди %s", answer_text, options)
        return False
 
    from telethon.tl.functions.messages import SendVoteRequest
    for attempt in range(5):
        try:
            await client(SendVoteRequest(
                peer=message.peer_id,
                msg_id=message.id,
                options=[poll.answers[idx].option],
            ))
            log.info("✓ Квиз решён: выбран вариант '%s'", options[idx])
            return True
        except Exception as e:
            log.debug("SendVote попытка %d/5: %s", attempt + 1, e)
            await asyncio.sleep(2)
    return False
 
 
async def _solve_button_prompt(message) -> bool:
    button_texts = [
        btn.text for row in (message.buttons or []) for btn in row if getattr(btn, "text", None)
    ]
    if not button_texts:
        return False
 
    answer_text = await _ask_ai_pick_option(message.message or "", button_texts)
    if not answer_text:
        return False
 
    idx = _match_option(answer_text, button_texts)
    if idx is None:
        return False
 
    target_text = button_texts[idx]
    for attempt in range(5):
        try:
            await message.click(text=target_text)
            log.info("✓ Нажата кнопка '%s'", target_text)
            return True
        except Exception as e:
            log.debug("Клик по кнопке попытка %d/5: %s", attempt + 1, e)
            await asyncio.sleep(2)
    return False
 
 
async def _try_solve_bot_prompts(client: TelegramClient) -> bool:
    """Смотрим последние 30 диалогов, берём только сообщения не старше 5 минут."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    solved_any = False
 
    try:
        dialogs = await client.get_dialogs(limit=30)
    except Exception as e:
        log.debug("Не удалось получить диалоги: %s", e)
        return False
 
    for dialog in dialogs:
        msg = dialog.message
        if not msg or not msg.date or msg.date < cutoff or getattr(msg, "out", False):
            continue
 
        if isinstance(msg.media, tl_types.MessageMediaPoll) and msg.media.poll.quiz:
            if await _solve_quiz(client, msg):
                solved_any = True
            continue
 
        if getattr(msg, "buttons", None):
            if await _solve_button_prompt(msg):
                solved_any = True
 
    return solved_any
 
 
# ── Сценарий 2: обязательная подписка на каналы (упоминание в чате) ──────────
 
def _extract_channel_urls(message) -> list[str]:
    urls: list[str] = []
 
    if getattr(message, "buttons", None):
        for row in message.buttons:
            for btn in row:
                url = getattr(btn, "url", None)
                if url and "t.me/" in url:
                    urls.append(url)
 
    if not urls and message.entities:
        text = message.message or ""
        for e in message.entities:
            if isinstance(e, tl_types.MessageEntityTextUrl) and "t.me/" in (e.url or ""):
                urls.append(e.url)
            elif isinstance(e, tl_types.MessageEntityUrl):
                seg = text[e.offset: e.offset + e.length]
                if "t.me/" in seg:
                    urls.append(seg)
            elif isinstance(e, tl_types.MessageEntityMention):
                seg = text[e.offset: e.offset + e.length].lstrip("@")
                if seg:
                    urls.append(f"https://t.me/{seg}")
 
    return list(dict.fromkeys(urls))
 
 
async def _resolve_chat_entity(client: TelegramClient, chat_id: str):
    """
    Резолвит сущность чата для дальнейшего поиска упоминаний внутри него.
 
    ДИАГНОСТИКА: раньше любая ошибка резолва (в т.ч. вполне вероятная —
    аккаунт ещё не участник чата / заявка на модерации / чат недоступен
    напрямую по числовому ID без -100 префикса) тихо проглатывалась и
    функция просто возвращала None без единой строки в логе. Из-за этого
    было невозможно понять по логам, почему подписка на каналы не
    сработала — выглядело так, будто функция просто ничего не делала.
    Теперь причина резолва логируется явно.
    """
    chat_id = _normalize_chat_id(chat_id)
    last_error: Exception | None = None
    try:
        if chat_id.startswith("@"):
            return await client.get_entity(chat_id)
        if chat_id.lstrip("-").isdigit():
            n = int(chat_id)
            try:
                return await client.get_entity(n)
            except Exception as e1:
                last_error = e1
                if n > 0:
                    try:
                        return await client.get_entity(int(f"-100{n}"))
                    except Exception as e2:
                        last_error = e2
                log.warning(
                    "_resolve_chat_entity: не смог зарезолвить числовой chat_id=%s: %s",
                    chat_id, last_error,
                )
                return None
        return await client.get_entity(chat_id)
    except Exception as e:
        log.warning("_resolve_chat_entity: не смог зарезолвить chat_id=%s: %s", chat_id, e)
        return None
 
 
async def _join_channel_by_url(client: TelegramClient, url: str) -> bool:
    """
    Вступает в канал по ссылке (invite-ссылка или @username-ссылка).
 
    ИСПРАВЛЕНИЯ (диагностика "подписка не работает"):
      1. Username раньше вырезался наивным url.split("/")[-1] — если в
         кнопке была ссылка вида https://t.me/channel?start=xyz (частый
         кейс у ботов, добавляющих deep-link параметр), в username
         попадал "channel?start=xyz" целиком → get_entity падал с
         "No user has ... username" → вступление молча проваливалось.
         Теперь путь ссылки парсится через urlparse, query-параметры
         отбрасываются корректно.
      2. InviteRequestSentError (заявка на вступление в приватный канал/
         супергруппу с модерацией отправлена, но мгновенного вступления
         не произошло) раньше падала в общий except Exception и
         засчитывалась как ПОЛНАЯ неудача, хотя по факту заявка ушла.
         Теперь это отдельный, залогированный случай, засчитываемый как
         успех попытки (дальнейший доступ зависит от одобрения
         администратором канала — вне контроля бота).
      3. Все причины неудачи теперь логируются на уровне WARNING с типом
         исключения — раньше был log.debug, который не виден в проде
         (все процессы стартуют с level=logging.INFO).
    """
    from urllib.parse import urlparse
    from telethon.errors import InviteRequestSentError
 
    try:
        url = url.strip()
 
        if "/+" in url or "joinchat/" in url:
            invite_hash = url.rstrip("/").split("+")[-1].split("joinchat/")[-1]
            invite_hash = invite_hash.split("?")[0]  # на случай query-параметров
            from telethon.tl.functions.messages import ImportChatInviteRequest
            try:
                await client(ImportChatInviteRequest(invite_hash))
            except UserAlreadyParticipantError:
                pass
            except InviteRequestSentError:
                log.info(
                    "Заявка на вступление в приватный канал отправлена (ожидает "
                    "одобрения администратором): %s", url,
                )
            return True
 
        # Корректно вырезаем username из пути ссылки, отбрасывая query/fragment,
        # чтобы https://t.me/channel?start=xyz не превращался в username
        # "channel?start=xyz".
        parsed = urlparse(url if "://" in url else f"https://{url}")
        path = parsed.path.strip("/")
        username = path.split("/")[-1].lstrip("@") if path else ""
 
        if not username:
            log.warning("_join_channel_by_url: не удалось извлечь username из ссылки %s", url)
            return False
 
        try:
            entity = await client.get_entity(f"@{username}")
        except Exception as e:
            log.warning(
                "_join_channel_by_url: не удалось зарезолвить @%s (из %s): %s",
                username, url, e,
            )
            return False
 
        from telethon.tl.functions.channels import JoinChannelRequest
        try:
            await client(JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            pass
        except InviteRequestSentError:
            log.info(
                "Заявка на вступление в @%s отправлена (ожидает одобрения "
                "администратором): %s", username, url,
            )
        return True
    except Exception as e:
        log.warning("Не удалось вступить по ссылке %s (%s): %s", url, type(e).__name__, e)
        return False
 
 
async def _send_probe_message(client: TelegramClient, entity):
    """Отправляет короткое проверочное сообщение. Возвращает Message или None при ошибке."""
    try:
        return await client.send_message(entity, "🔍")
    except Exception as e:
        log.warning("_send_probe_message: не удалось отправить проверочное сообщение: %s", e)
        return None
 
 
async def _message_exists(client: TelegramClient, entity, message_id: int) -> bool:
    try:
        msg = await client.get_messages(entity, ids=message_id)
        return _is_real_message(msg)
    except Exception:
        return False
 
 
def _is_real_message(msg) -> bool:
    return bool(msg) and not isinstance(msg, tl_types.MessageEmpty)
 
 
async def _notify_bypass_parse_error(account: Account, chat_id: str, error: Exception):
    """
    Уведомляет владельца аккаунта (для системных — OWNER_ID) об ошибке
    парсинга ссылок на каналы при обходе требования подписки — чтобы
    проблема не оставалась незамеченной. Указывает серверное время и
    просит обратиться к администратору.
    """
    notify_user_id = account.owner_id if account.owner_id else OWNER_ID
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
 
    bot = await _get_bot_for_user(notify_user_id)
    try:
        await bot.send_message(
            notify_user_id,
            f"⚠️ *Ошибка автоматической подписки на канал*\n\n"
            f"Аккаунт: `{account.phone}`\n"
            f"Чат: `{chat_id}`\n"
            f"Время (сервер): `{ts}`\n"
            f"Причина: `{str(error)[:200]}`\n\n"
            f"Не удалось автоматически разобрать ссылки на каналы для подписки. "
            f"Обратитесь к администратору: @jstaskmebro",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Не удалось отправить уведомление об ошибке парсера обхода подписки: %s", e)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
 
 
async def _try_bot_gate_dm(client: TelegramClient, chat_id: str, account: Account) -> bool:
    """
    Сценарий "бот-привратник в ЛС": некоторые чаты не дают даже написать
    первое сообщение (или зайти в чат), пока не пройдёшь проверку у
    отдельного бота, который сам пишет аккаунту в личку сообщение вида
    "Имя добро пожаловать в @ЦелевойЧат, ..." с инлайн-кнопкой(ами)
    ("Пройти проверку" / подписка на канал).
 
    Ищем среди последних диалогов свежее (не старше 15 сек) входящее
    сообщение ИМЕННО от бота (Bot API аккаунт), где упомянут username
    целевого чата, и:
      - URL-кнопки, ведущие на t.me-канал → вступаем в канал напрямую
        (клик по URL-кнопке в Telethon не подписывает, а просто отдаёт
        ссылку, поэтому подписываемся отдельно через _join_channel_by_url).
      - Обычные (не-URL) кнопки → спрашиваем ИИ капча это или просто
        проходное действие:
          * капча — решаем как обычный сценарий выбора правильного
            варианта (_solve_button_prompt);
          * не капча — жмём кнопку(и) не задумываясь.
 
    Возвращает True, если удалось вступить в канал и/или нажать кнопку
    (то есть формально "прошли" гейт) — дальнейшую фактическую проверку
    доступа к чату делает вызывающий код повторной отправкой пробного
    сообщения.
    """
    target_username = _normalize_chat_id(chat_id).lstrip("@").lower()
    if not target_username:
        return False
 
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=15)
 
    try:
        dialogs = await client.get_dialogs(limit=30)
    except Exception as e:
        log.warning("_try_bot_gate_dm: не удалось получить диалоги: %s", e)
        return False
 
    gate_msg = None
    for dialog in dialogs:
        msg = dialog.message
        if not msg or not msg.date or msg.date < cutoff or getattr(msg, "out", False):
            continue
        sender = getattr(dialog, "entity", None)
        if not (sender is not None and getattr(sender, "bot", False)):
            continue
        text = msg.message or ""
        if target_username in text.lower():
            gate_msg = msg
            break
 
    if gate_msg is None:
        log.info(
            "_try_bot_gate_dm: не нашёл свежее (±15с) сообщение от бота с "
            "упоминанием @%s в личных сообщениях", target_username,
        )
        return False
 
    buttons = getattr(gate_msg, "buttons", None) or []
    url_buttons: list[str] = []
    plain_button_texts: list[str] = []
    for row in buttons:
        for btn in row:
            url = getattr(btn, "url", None)
            if url:
                url_buttons.append(url)
            elif getattr(btn, "text", None):
                plain_button_texts.append(btn.text)
 
    joined_any = False
    for url in url_buttons:
        if "t.me/" in url and await _join_channel_by_url(client, url):
            joined_any = True
 
    clicked_any = False
    if plain_button_texts:
        is_captcha = await _ask_ai_is_captcha(gate_msg.message or "", plain_button_texts)
 
        if is_captcha:
            clicked_any = await _solve_button_prompt(gate_msg)
        elif is_captcha is False:
            for text in plain_button_texts:
                for attempt in range(5):
                    try:
                        await gate_msg.click(text=text)
                        clicked_any = True
                        log.info(
                            "✓ Нажата проходная кнопка '%s' в ЛС-гейте для @%s",
                            text, target_username,
                        )
                        break
                    except Exception as e:
                        log.debug(
                            "_try_bot_gate_dm: клик по '%s' попытка %d/5: %s",
                            text, attempt + 1, e,
                        )
                        await asyncio.sleep(2)
        else:
            log.warning(
                "_try_bot_gate_dm: ИИ не смог определить капча это или нет для "
                "@%s — кнопки не нажимаем во избежание ошибки", target_username,
            )
 
    if not joined_any and not clicked_any:
        log.warning(
            "_try_bot_gate_dm: нашли гейт-сообщение от бота для @%s, но не "
            "смогли ни вступить в канал, ни нажать кнопку", target_username,
        )
        return False
 
    log.info(
        "✓ Прошли ЛС-гейт для @%s (вступили в канал: %s, нажали кнопку: %s)",
        target_username, joined_any, clicked_any,
    )
    return True
 
 
async def _try_join_required_channels(client: TelegramClient, chat_id: str, account: Account) -> bool:
    """
    Обход требования подписки на канал(ы).
 
    Больше НЕ ищет сообщение через InputMessagesFilterMyMentions — на практике
    этот серверный фильтр находил 0 сообщений даже при подтверждённом
    (скриншотом) наличии сообщения с требованием подписки в чате. Новый
    алгоритм не полагается на индексацию упоминаний Telegram:
 
      0. Если не получилось даже зарезолвить чат или отправить в него
         пробное сообщение (частый случай — отдельный бот-привратник
         блокирует доступ к самому чату, пока не пройдёшь проверку у него
         в ЛС) — сначала пробуем пройти этот ЛС-гейт (_try_bot_gate_dm),
         и повторяем резолв+пробное сообщение один раз после этого.
      1. Отправляем в чат короткое проверочное сообщение.
      2. Если оно НЕ удалилось за несколько секунд — писать в чат можно,
         обходить нечего, сразу успех.
      3. Если удалилось — ждём 10 секунд, затем ищем среди последних
         сообщений чата (появившихся ПОСЛЕ нашего проверочного) сообщение
         с никнеймом нашего аккаунта; если по нику не нашли — берём просто
         самое свежее входящее сообщение после проверочного (это и есть
         ответ бота-администратора на нашу попытку писать).
      4. Если за 10 секунд такое сообщение не нашли — писать в чат
         невозможно.
      5. Если сообщение нашли, но парсинг ссылок на каналы упал с ошибкой
         (или ничего не нашёл) — уведомляем владельца аккаунта и просим
         обратиться к администратору.
      6. Если ссылки нашли — вступаем во все каналы и пробуем написать
         в чат ещё раз (новое проверочное сообщение).
    """
    entity = await _resolve_chat_entity(client, chat_id)
    probe = await _send_probe_message(client, entity) if entity is not None else None
 
    if probe is None:
        log.info(
            "_try_join_required_channels: не удалось написать пробное сообщение "
            "в %s (или зарезолвить чат) — пробую пройти ЛС-гейт от бота", chat_id,
        )
        gate_passed = await _try_bot_gate_dm(client, chat_id, account)
        if not gate_passed:
            log.warning(
                "_try_join_required_channels: ЛС-гейт не пройден, доступ к %s "
                "невозможен", chat_id,
            )
            return False
 
        entity = await _resolve_chat_entity(client, chat_id)
        probe = await _send_probe_message(client, entity) if entity is not None else None
        if probe is None:
            log.warning(
                "_try_join_required_channels: после прохождения ЛС-гейта всё "
                "равно не удалось написать в %s — писать в чат невозможно", chat_id,
            )
            return False
 
    await asyncio.sleep(5)
    if await _message_exists(client, entity, probe.id):
        log.info(
            "_try_join_required_channels: проверочное сообщение в %s не удалилось — "
            "писать можно, обход не требуется", chat_id,
        )
        return True
 
    log.info(
        "_try_join_required_channels: проверочное сообщение в %s удалилось — "
        "ищу требование подписки, жду 10 сек", chat_id,
    )
    await asyncio.sleep(10)
 
    try:
        me = await client.get_me()
        nickname_candidates = [n for n in (me.first_name, me.username) if n]
    except Exception:
        nickname_candidates = []
 
    try:
        recent = await client.get_messages(entity, limit=20)
    except Exception as e:
        log.warning(
            "_try_join_required_channels: не удалось получить сообщения чата %s: %s",
            chat_id, e,
        )
        recent = []
 
    incoming_after_probe = [
        m for m in (recent or [])
        if not getattr(m, "out", False) and m.date and probe.date and m.date > probe.date
    ]
 
    target_msg = None
    for msg in incoming_after_probe:
        text = msg.message or ""
        if any(n.lower() in text.lower() for n in nickname_candidates):
            target_msg = msg
            break
 
    if target_msg is None and incoming_after_probe:
        # Ник текстом не нашли (часто он рендерится как имя-упоминание/эмодзи,
        # а не как литеральный текст) — берём просто самое новое входящее
        # сообщение после проверочного, это и есть ответ на нашу попытку писать.
        target_msg = max(incoming_after_probe, key=lambda m: m.date)
 
    if target_msg is None:
        log.warning(
            "_try_join_required_channels: за 10 сек не нашли сообщение с "
            "требованием подписки в чате %s — писать в чат невозможно", chat_id,
        )
        return False
 
    try:
        urls = _extract_channel_urls(target_msg)
    except Exception as e:
        log.error(
            "_try_join_required_channels: ошибка парсинга ссылок на каналы "
            "в чате %s: %s", chat_id, e,
        )
        await _notify_bypass_parse_error(account, chat_id, e)
        return False
 
    if not urls:
        log.warning(
            "_try_join_required_channels: нашли сообщение с требованием подписки "
            "в чате %s, но не смогли распарсить ни одной ссылки на канал", chat_id,
        )
        await _notify_bypass_parse_error(
            account, chat_id, Exception("в найденном сообщении не найдено ни одной ссылки на канал"),
        )
        return False
 
    joined_any = False
    for url in urls:
        if await _join_channel_by_url(client, url):
            joined_any = True
 
    if not joined_any:
        log.warning(
            "_try_join_required_channels: нашли %d ссылок(и) в чате %s, но ни в один "
            "канал вступить не удалось — см. предупреждения _join_channel_by_url выше.",
            len(urls), chat_id,
        )
        return False
 
    log.info(
        "✓ Вступил в %d канал(ов) по требованию чата %s, пробую написать ещё раз",
        len(urls), chat_id,
    )
 
    retry_probe = await _send_probe_message(client, entity)
    if retry_probe is None:
        return False
    await asyncio.sleep(5)
    return await _message_exists(client, entity, retry_probe.id)
 
 
# ── Точка входа ────────────────────────────────────────────────────────────────
 
async def try_bypass_restriction(account: Account, chat_id: str) -> bool:
    """
    Вызывается при первой неудачной отправке в чат — до failover.
    1) пытается решить капчу/квиз от бота-админа в ЛС аккаунта
    2) пытается вступить в каналы, если чат требует подписки (см.
       _try_join_required_channels — сама проверяет доступ через
       проверочное сообщение, отдельный финальный can_write_to_chat
       для этого сценария больше не нужен)
    Возвращает True, если чат снова доступен для отправки.
    """
    client = make_client(account)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False
 
        joined_channels = await _try_join_required_channels(client, chat_id, account)
        if joined_channels:
            return True
 
        solved_prompt = await _try_solve_bot_prompts(client)
        if not solved_prompt:
            return False
 
        await asyncio.sleep(10)
        can_write, _ = await can_write_to_chat(client, chat_id)
        return bool(can_write)
 
    except Exception as e:
        log.warning("try_bypass_restriction(%s, %s): %s", account.phone, chat_id, e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
