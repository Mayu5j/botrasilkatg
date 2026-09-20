"""
services/account_service.py — управление Telegram-аккаунтами (Telethon).
 
ИЗМЕНЕНИЯ:
  - make_client: поддержка прокси (socks5/http) из полей Account
  - can_write_to_chat: убрана тестовая отправка "." — используем только
    GetParticipantRequest + проверку прав. Тестовая отправка реального
    сообщения пользователя делается снаружи (в воркере) при первой рассылке.
  - check_and_join_chats: аналогично — только лёгкая проверка без отправки
  - get_accounts: параметр only_working
 
ИЗМЕНЕНИЯ (именованный пул прокси):
  - make_client теперь ПРИОРИТЕТНО берёт прокси из связи acc.proxy
    (модель Proxy, см. models.py). Если у аккаунта нет привязки к пулу —
    используется fallback на старые голые proxy_host/port/... поля
    (для аккаунтов, созданных до этого рефакторинга).
  - get_accounts / get_account_by_id теперь подгружают acc.proxy через
    selectinload — иначе обращение к acc.proxy в async-сессии за
    пределами исходного db приведёт к ошибке ленивой загрузки.
  - create_account принимает proxy_id и привязывает аккаунт к прокси
    из пула через services.proxy_service.
  - delete_account освобождает счётчик прокси перед удалением аккаунта.
  - send_code принимает proxy_id — код входа при добавлении аккаунта
    отправляется через тот же прокси, который будет закреплён за
    аккаунтом (чтобы не светить IP VPS даже на этапе логина).
"""
import asyncio
import logging
from models import Account, Task
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteRequestSentError,
    PeerIdInvalidError,
    SlowModeWaitError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)
 
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
 
from models import Account
 
log = logging.getLogger(__name__)
 
 
# ── CRUD ──────────────────────────────────────────────────────────────────────
 
async def get_accounts(
    db: AsyncSession,
    owner_id: int | None = None,
    only_working: bool = True,
) -> list[Account]:
    q = select(Account).options(selectinload(Account.proxy))
    if only_working:
        q = q.where(Account.is_active == True, Account.is_banned == False, Account.status == "ok")
    if owner_id is not None:
        q = q.where(Account.owner_id == owner_id)
    else:
        q = q.where(Account.is_system == True)
    result = await db.execute(q)
    return list(result.scalars().all())
 
 
async def get_account_by_id(db: AsyncSession, account_id: int) -> Account | None:
    result = await db.execute(
        select(Account).options(selectinload(Account.proxy)).where(Account.id == account_id)
    )
    return result.scalar_one_or_none()
 
 
async def create_account(
    db: AsyncSession,
    api_id: int,
    api_hash: str,
    phone: str,
    session_string: str,
    owner_id: int | None = None,
    is_system: bool = False,
    proxy_id: int | None = None,
    proxy_host: str | None = None,
    proxy_port: int | None = None,
    proxy_type: str | None = None,
    proxy_user: str | None = None,
    proxy_pass: str | None = None,
) -> Account:
    from services import proxy_service
 
    acc = Account(
        owner_id=owner_id,
        phone=phone,
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        is_system=is_system,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_type=proxy_type,
        proxy_user=proxy_user,
        proxy_pass=proxy_pass,
    )
    db.add(acc)
    await db.flush()
 
    await proxy_service.attach_proxy_to_account(db, acc, proxy_id)
 
    await db.commit()
 
    # db.refresh(acc) НЕ подгружает relationship 'proxy' — обращение к acc.proxy
    # после него вызвало бы синхронный lazy-load вне greenlet-контекста и падало
    # с MissingGreenlet. Поэтому вместо refresh() перечитываем через
    # get_account_by_id(), который использует selectinload(Account.proxy).
    acc = await get_account_by_id(db, acc.id)
    log.info("Аккаунт %s добавлен (id=%d, proxy_id=%s)", phone, acc.id, proxy_id)
    return acc
 
async def delete_account(db: AsyncSession, account_id: int) -> bool:
    from services import proxy_service
 
    acc = await get_account_by_id(db, account_id)
    if not acc:
        return False
    await proxy_service.release_proxy_from_account(db, acc)
    await db.delete(acc)
    await db.commit()
    return True
 
 
async def get_task_ids_for_account(db: AsyncSession, account_id: int) -> list[int]:
    """Все ID задач, где участвует аккаунт (через TaskAccount)."""
    from models import TaskAccount
    result = await db.execute(
        select(TaskAccount.task_id).where(TaskAccount.account_id == account_id).distinct()
    )
    return [row[0] for row in result.all()]
 
 
async def delete_system_account_cascade(db: AsyncSession, account_id: int) -> tuple[bool, int]:
    """
    Удалить системный аккаунт вместе со ВСЕМИ задачами, где он участвует.
    Задачи удаляются целиком (а не просто отвязываются) — по требованию:
    "все задачи с аккаунта тоже прекращались и удалялись из БД".
 
    Возвращает (успех, кол-во удалённых задач).
    """
    from services import task_service
 
    acc = await get_account_by_id(db, account_id)
    if not acc:
        return False, 0
 
    task_ids = await get_task_ids_for_account(db, account_id)
 
    deleted_tasks = 0
    for task_id in task_ids:
        # user_id не важен для владения — берём владельца задачи из самой задачи
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            continue
        ok = await task_service.delete_task(db, task_id, task.user_id)
        if ok:
            deleted_tasks += 1
 
    # Аккаунт мог быть уже удалён внутри delete_task-каскадов — перепроверим
    acc = await get_account_by_id(db, account_id)
    if acc:
        await delete_account(db, acc.id)
 
    log.info("Системный аккаунт id=%d удалён, удалено задач: %d", account_id, deleted_tasks)
    return True, deleted_tasks
 
 
async def set_banned(db: AsyncSession, account_id: int, banned: bool):
    acc = await get_account_by_id(db, account_id)
    if acc:
        acc.is_banned = banned
        await db.commit()
 
 
async def update_chats_count(db: AsyncSession, account_id: int, count: int):
    acc = await get_account_by_id(db, account_id)
    if acc:
        acc.chats_count = count
        await db.commit()
 
 
async def set_proxy(
    db: AsyncSession,
    account_id: int,
    proxy_host: str | None,
    proxy_port: int | None,
    proxy_type: str | None,
    proxy_user: str | None,
    proxy_pass: str | None,
) -> bool:
    """
    Legacy-функция: установить или убрать голые прокси-поля напрямую
    у аккаунта (без именованного пула). Оставлена для обратной
    совместимости — новый флоу должен использовать
    services.proxy_service.reassign_account_proxy() с именованным пулом.
    """
    acc = await get_account_by_id(db, account_id)
    if not acc:
        return False
    acc.proxy_host = proxy_host
    acc.proxy_port = proxy_port
    acc.proxy_type = proxy_type
    acc.proxy_user = proxy_user
    acc.proxy_pass = proxy_pass
    await db.commit()
    return True
 
 
# ── Telethon: создание клиента ────────────────────────────────────────────────
 
def make_client(acc: Account) -> TelegramClient:
    """
    Создать TelegramClient для аккаунта.
 
    Приоритет источника прокси:
      1. acc.proxy — именованный прокси из пула (Proxy). Это основной
         механизм после введения пула прокси.
      2. Голые acc.proxy_host/port/... — fallback для аккаунтов,
         созданных до введения пула (proxy_id не заполнен).
 
    Поддерживаемые типы: socks5 (default), http.
    """
    host: str | None = None
    port: int | None = None
    ptype: str | None = None
    user: str | None = None
    password: str | None = None
    source = None
 
    linked_proxy = getattr(acc, "proxy", None)
    if linked_proxy and linked_proxy.host and linked_proxy.port:
        host, port, ptype = linked_proxy.host, linked_proxy.port, linked_proxy.proxy_type
        user, password = linked_proxy.username, linked_proxy.password
        source = f"пул '{linked_proxy.name}'"
    elif acc.proxy_host and acc.proxy_port:
        host, port, ptype = acc.proxy_host, acc.proxy_port, acc.proxy_type
        user, password = acc.proxy_user, acc.proxy_pass
        source = "legacy-поля"
 
    proxy = None
    if host and port:
        try:
            import socks  # PySocks
            proxy_type = socks.SOCKS5 if (ptype or "socks5").lower() == "socks5" else socks.HTTP
            proxy = (
                proxy_type,
                host,
                port,
                True,               # rdns
                user or None,
                password or None,
            )
            log.debug("Аккаунт %s → прокси %s:%d (%s)", acc.phone, host, port, source)
        except ImportError:
            log.error("PySocks не установлен — pip install PySocks. Прокси игнорирован.")
 
    return TelegramClient(
        StringSession(acc.session_string),
        int(acc.api_id),
        acc.api_hash,
        proxy=proxy,
    )
 
 
def _build_raw_proxy_tuple(proxy_type: str, host: str, port: int, user: str | None, password: str | None):
    """Вспомогательная сборка кортежа-прокси для PySocks (используется до
    создания Account, например при send_code, где Account ещё не существует)."""
    try:
        import socks
    except ImportError:
        log.error("PySocks не установлен — pip install PySocks. Прокси игнорирован.")
        return None
    t = socks.SOCKS5 if (proxy_type or "socks5").lower() == "socks5" else socks.HTTP
    return (t, host, port, True, user or None, password or None)
 
 
# ── Telethon: auth ────────────────────────────────────────────────────────────
 
async def send_code(
    api_id: int,
    api_hash: str,
    phone: str,
    proxy_id: int | None = None,
) -> tuple[TelegramClient, str]:
    """
    Отправить код входа на телефон.
 
    Если передан proxy_id — код отправляется ЧЕРЕЗ этот прокси из пула
    (и тот же клиент затем донашивает всю авторизацию/2FA через него),
    чтобы IP VPS не светился даже на этапе логина. Это тот же прокси,
    который будет закреплён за аккаунтом после его создания.
    """
    proxy = None
    if proxy_id is not None:
        from database import SessionLocal
        from services import proxy_service
 
        async with SessionLocal() as db:
            p = await proxy_service.get_proxy_by_id(db, proxy_id)
 
        if p and p.host and p.port:
            proxy = _build_raw_proxy_tuple(p.proxy_type, p.host, p.port, p.username, p.password)
        else:
            log.warning("send_code: proxy_id=%s не найден в пуле, отправляю без прокси", proxy_id)
 
    client = TelegramClient(StringSession(), api_id, api_hash, proxy=proxy)
    await client.connect()
    await asyncio.sleep(1)
    log.info("Отправка кода на %s (proxy_id=%s)", phone, proxy_id)
    sent = await client.send_code_request(phone)
    return client, sent.phone_code_hash
 
 
async def sign_in_code(client, phone, code, phone_code_hash) -> str | None:
    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    return StringSession.save(client.session)
 
 
async def sign_in_2fa(client, password) -> str:
    await client.sign_in(password=password)
    return StringSession.save(client.session)
 
 
async def get_me_name(client: TelegramClient) -> str:
    me = await client.get_me()
    return me.first_name or me.username or str(me.id)
 
 
# ── Папки ─────────────────────────────────────────────────────────────────────
 
async def get_chats_from_folder(client: TelegramClient, folder_link: str) -> list[dict]:
    slug = folder_link.rstrip("/").split("/")[-1]
    chats: list[dict] = []
    try:
        from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
        result = await client(CheckChatlistInviteRequest(slug=slug))
 
        for peer in result.chats[:500]:
            peer_id = getattr(peer, "id", None)
            if peer_id is None:
                continue
            title       = getattr(peer, "title", None) or getattr(peer, "first_name", None) or str(peer_id)
            username    = getattr(peer, "username", None)
            access_hash = getattr(peer, "access_hash", None)
            str_id      = f"-100{peer_id}" if access_hash is not None else str(-abs(peer_id))
            chats.append({
                "id":          str_id,
                "title":       title,
                "username":    username,
                "access_hash": access_hash,
                "folder_slug": slug,
            })
        log.info("Получено %d чатов из папки %s", len(chats), folder_link)
    except Exception as e:
        log.warning("Ошибка получения папки %s: %s", folder_link, e)
    return chats
 
 
# ── Вспомогательные функции ───────────────────────────────────────────────────
 
async def _resolve_entity(client: TelegramClient, chat_id: str):
    s = str(chat_id).strip()
    if s.startswith("@"):
        return await client.get_entity(s)
    if s.lstrip("-").isdigit():
        numeric = int(s)
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
    return await client.get_entity(s)
 
 
async def _join_single(client: TelegramClient, entity) -> tuple[bool, str]:
    from telethon.tl.functions.channels import JoinChannelRequest
    try:
        await client(JoinChannelRequest(entity))
        await asyncio.sleep(1)
        return True, "ok"
    except UserAlreadyParticipantError:
        return True, "ok"
    except ChannelsTooMuchError:
        return False, "too_many_channels"
    except InviteRequestSentError:
        return True, "join_pending"
    except FloodWaitError as e:
        # FloodWait при вступлении не означает отсутствие доступа —
        # воркер попробует отправить сам и обнаружит реальные ограничения
        log.warning("FloodWait %ds при вступлении в чат — считаем доступным", e.seconds)
        return True, "ok"
    except Exception as e:
        return False, str(e)[:80]
 
 
async def _join_folder_bulk(client: TelegramClient, slug: str) -> bool:
    try:
        from telethon.tl.functions.chatlists import JoinChatlistInviteRequest
        await client(JoinChatlistInviteRequest(slug=slug, peers=[]))
        log.info("JoinChatlistInviteRequest для папки %s — успешно", slug)
        return True
    except Exception as e:
        log.warning("JoinChatlistInviteRequest %s: %s", slug, e)
        return False
 
 
async def _check_write_light(client: TelegramClient, chat_id: str) -> tuple[bool | None, str]:
    """
    Лёгкая проверка прав без отправки сообщений.
    Использует GetParticipantRequest для каналов/супергрупп.
 
    Возвращает:
      (True, "ok")              — можно писать
      (False, reason)           — нельзя
      (None, "not_participant") — не вступили ещё
      (None, "ok")              — обычная группа, считаем OK
    """
    from telethon.tl import types as tl_types
    from telethon.tl.functions.channels import GetParticipantRequest
 
    entity = None
    try:
        entity = await _resolve_entity(client, chat_id)
    except Exception as e:
        err = str(e).lower()
        if "private" in err or "channel_private" in err:
            return False, "private"
        return False, str(e)[:80]
 
    if entity is None:
        return False, "not_found"
 
    if not isinstance(entity, tl_types.Channel):
        return None, "ok"
 
    try:
        me = await client.get_me()
        result = await client(GetParticipantRequest(channel=entity, participant=me.id))
        p = result.participant
 
        if isinstance(p, tl_types.ChannelParticipantBanned):
            return False, "banned"
 
        banned_rights = getattr(p, "banned_rights", None)
        if banned_rights and getattr(banned_rights, "send_messages", False):
            return False, "write_forbidden"
 
        return True, "ok"
    except Exception as e:
        err = str(e).lower()
        if "not_participant" in err or "not participant" in err:
            return None, "not_participant"
        if "banned" in err:
            return False, "banned"
        if "private" in err or "channel_private" in err:
            return False, "private"
        log.debug("GetParticipant %s: %s", chat_id, e)
        return None, "ok"
 
 
# ── Публичные функции проверки ────────────────────────────────────────────────
 
async def can_write_to_chat(
    client: TelegramClient,
    chat_id: str,
    username: str | None = None,
    access_hash: int | None = None,
) -> tuple[bool, str]:
    """
    Проверка доступа к чату БЕЗ тестовой отправки.
    Использует только GetParticipantRequest + проверку прав.
    Фактическая отправка происходит при первой рассылке в воркере.
    """
    from telethon.tl.functions.messages import ImportChatInviteRequest
 
    entity = None
    if username:
        try:
            entity = await client.get_entity(f"@{username}")
        except Exception:
            pass
    if entity is None:
        try:
            entity = await _resolve_entity(client, chat_id)
        except ChannelPrivateError:
            return False, "private"
        except (PeerIdInvalidError, ValueError):
            return False, "invalid_id"
        except Exception as e:
            if "private" in str(e).lower():
                return False, "private"
 
    if entity is None:
        return False, "not_found"
 
    # Invite-ссылки: вступаем, потом проверяем
    if chat_id.startswith("https://t.me/+") or chat_id.startswith("t.me/+"):
        invite_hash = chat_id.rstrip("/").split("+")[-1]
        try:
            await client(ImportChatInviteRequest(invite_hash))
            await asyncio.sleep(1)
            entity = await client.get_entity(entity.id)
        except UserAlreadyParticipantError:
            pass
        except Exception as e:
            err = str(e).lower()
            if "expired" in err:
                return False, "invite_expired"
            if "too_many" in err:
                return False, "too_many_channels"
    elif getattr(entity, "username", None):
        ok, reason = await _join_single(client, entity)
        if not ok:
            return False, reason
        try:
            entity = await client.get_entity(entity.id)
        except Exception:
            pass
 
    # Лёгкая проверка прав
    can, reason = await _check_write_light(client, chat_id)
    if can is None:
        # Обычная группа или не удалось определить — считаем OK
        return True, "ok"
    return can, reason
 
 
async def check_and_join_chats(
    client: TelegramClient,
    chats: list[dict],
) -> list[dict]:
    """
    Проверить доступ к чатам.
    Папки: bulk join + лёгкая проверка прав (без отправки).
    Ручной ввод: вступление + лёгкая проверка прав (без отправки).
    """
    done_slugs: set[str] = set()
    for chat in chats:
        slug = chat.get("folder_slug")
        if slug and slug not in done_slugs:
            await _join_folder_bulk(client, slug)
            done_slugs.add(slug)
 
    if done_slugs:
        await asyncio.sleep(3)
 
    results: list[dict] = []
 
    for chat in chats:
        chat_id  = str(chat["id"])
        title    = chat.get("title", chat_id)
        username = chat.get("username")
        slug     = chat.get("folder_slug")
        link     = f"https://t.me/{username}" if username else None
 
        if slug:
            can_write, reason = await _check_write_light(client, chat_id)
 
            if can_write is None and reason == "not_participant":
                if username:
                    try:
                        entity = await client.get_entity(f"@{username}")
                        ok, r  = await _join_single(client, entity)
                        if ok:
                            await asyncio.sleep(1)
                            can_write, reason = await _check_write_light(client, chat_id)
                            if can_write is None:
                                can_write, reason = True, "ok"
                        else:
                            can_write, reason = False, r
                    except Exception:
                        can_write, reason = False, "not_found"
                else:
                    can_write, reason = False, "private"
            elif can_write is None:
                can_write, reason = True, "ok"
        else:
            can_write, reason = await can_write_to_chat(
                client, chat_id,
                username=username,
                access_hash=chat.get("access_hash"),
            )
            await asyncio.sleep(1)
 
        results.append({
            "id":        chat_id,
            "title":     title,
            "username":  username,
            "can_write": bool(can_write),
            "reason":    reason,
            "link":      link,
        })
 
    return results

