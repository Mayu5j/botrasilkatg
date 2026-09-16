"""
services/proxy_service.py — управление именованным пулом прокси.
 
Два типа (Proxy.kind):
  "user"   — для пользовательских аккаунтов. Весь трафик пользовательского
             аккаунта (отправка кода при добавлении + вся дальнейшая
             рассылка) идёт через один и тот же прокси этого типа.
             Подбирается автоматически (round-robin по наименее
             загруженному активному прокси) в момент добавления аккаунта —
             сам пользователь прокси не видит и не выбирает.
  "system" — для системных аккаунтов. Админ выбирает прокси вручную,
             самым первым шагом при добавлении системного аккаунта.
 
Прокси НЕ меняется у аккаунта после привязки — ротация происходит
только в момент создания нового аккаунта (round-robin для пользователей,
ручной выбор для системных).
 
Ничего не хранится "в коде" — только в БД (таблица proxies).
"""
import logging
 
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
 
from models import Proxy, Account
 
log = logging.getLogger(__name__)
 
VALID_KINDS = ("user", "system")
 
 
async def create_proxy(
    db: AsyncSession,
    name: str,
    kind: str,
    host: str,
    port: int,
    proxy_type: str = "socks5",
    username: str | None = None,
    password: str | None = None,
) -> Proxy:
    if kind not in VALID_KINDS:
        raise ValueError(f"kind должен быть одним из {VALID_KINDS}, получено: {kind}")
 
    proxy = Proxy(
        name=name,
        kind=kind,
        host=host,
        port=port,
        proxy_type=proxy_type,
        username=username,
        password=password,
    )
    db.add(proxy)
    await db.commit()
    await db.refresh(proxy)
    log.info("Прокси добавлен: %s (%s:%d) kind=%s", name, host, port, kind)
    return proxy
 
 
async def get_proxies(
    db: AsyncSession,
    kind: str | None = None,
    only_active: bool = False,
) -> list[Proxy]:
    q = select(Proxy)
    if kind is not None:
        q = q.where(Proxy.kind == kind)
    if only_active:
        q = q.where(Proxy.is_active == True)
    q = q.order_by(Proxy.created_at.desc())
    result = await db.execute(q)
    return list(result.scalars().all())
 
 
async def get_proxy_by_id(db: AsyncSession, proxy_id: int) -> Proxy | None:
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    return result.scalar_one_or_none()
 
 
async def toggle_proxy(db: AsyncSession, proxy_id: int) -> bool | None:
    """Включить/выключить прокси. None если прокси не найден.
    Выключенный прокси не участвует в round-robin для новых аккаунтов,
    но уже привязанные к нему аккаунты продолжают им пользоваться."""
    proxy = await get_proxy_by_id(db, proxy_id)
    if not proxy:
        return None
    proxy.is_active = not proxy.is_active
    await db.commit()
    return proxy.is_active
 
 
async def delete_proxy(db: AsyncSession, proxy_id: int) -> bool:
    """
    Удалить прокси из пула.
    Аккаунты, привязанные к нему, НЕ удаляются — просто теряют привязку
    (proxy_id=None) и продолжат работать напрямую, без прокси, пока им
    не назначат новый вручную.
    """
    proxy = await get_proxy_by_id(db, proxy_id)
    if not proxy:
        return False
 
    result = await db.execute(select(Account).where(Account.proxy_id == proxy_id))
    affected = list(result.scalars().all())
    for acc in affected:
        acc.proxy_id = None
 
    await db.delete(proxy)
    await db.commit()
    log.info("Прокси id=%d удалён, отвязано аккаунтов: %d", proxy_id, len(affected))
    return True
 
 
async def pick_user_proxy(db: AsyncSession) -> Proxy | None:
    """
    Вернуть наименее загруженный активный прокси типа 'user'
    (для автоматической привязки при добавлении пользовательского
    аккаунта). None, если активных прокси такого типа нет — в этом
    случае аккаунт создаётся без прокси.
    """
    result = await db.execute(
        select(Proxy)
        .where(Proxy.kind == "user", Proxy.is_active == True)
        .order_by(Proxy.accounts_count.asc())
    )
    return result.scalars().first()
 
 
async def attach_proxy_to_account(db: AsyncSession, account: Account, proxy_id: int | None) -> None:
    """
    Привязать прокси к аккаунту (используется при создании аккаунта)
    и увеличить счётчик загрузки прокси. Не коммитит — коммит делает
    вызывающий код (account_service.create_account).
    """
    if proxy_id is None:
        account.proxy_id = None
        return
    proxy = await get_proxy_by_id(db, proxy_id)
    if not proxy:
        log.warning("attach_proxy_to_account: прокси id=%d не найден, аккаунт без прокси", proxy_id)
        account.proxy_id = None
        return
    account.proxy_id = proxy.id
    proxy.accounts_count += 1
 
 
async def release_proxy_from_account(db: AsyncSession, account: Account) -> None:
    """Уменьшить счётчик прокси при удалении аккаунта. Не коммитит."""
    if not account.proxy_id:
        return
    proxy = await get_proxy_by_id(db, account.proxy_id)
    if proxy and proxy.accounts_count > 0:
        proxy.accounts_count -= 1
 
 
async def reassign_account_proxy(db: AsyncSession, account: Account, new_proxy_id: int | None) -> bool:
    """
    Вручную сменить прокси уже существующего аккаунта (например, через
    админку). В обычном потоке жизни аккаунта прокси не меняется сам по
    себе — это явное административное действие.
    """
    await release_proxy_from_account(db, account)
    await attach_proxy_to_account(db, account, new_proxy_id)
    await db.commit()
    return True

