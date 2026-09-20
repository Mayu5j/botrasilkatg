"""
bot/mirror_runner.py — запуск всех зеркальных ботов из БД.

ИСПРАВЛЕНИЕ: dp.start_polling() блокировал event loop → каждое зеркало теперь
запускается через собственный polling loop в asyncio.Task.
Все зеркала работают ОДНОВРЕМЕННО.

Архитектура:
  - Один Dispatcher + роутеры создаётся ОДИН РАЗ (экономия ~5 МБ на зеркало)
  - Каждый Bot получает отдельный asyncio.Task с polling loop
  - При падении — автоматический перезапуск через RESTART_DELAY секунд
  - Watch-loop каждые 30 сек синхронизирует запущенные зеркала с БД
  - Graceful shutdown по SIGINT/SIGTERM
"""
import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from config import MIRROR_RESTART_DELAY, MIRROR_SHARD_MIN, MIRROR_SHARD_MAX
from database import create_all_tables, SessionLocal
from models import MirrorBot
from bot.middlewares import AuthMiddleware
from bot.handlers import start, accounts, tasks, admin, mirror
from bot.handlers import payment as payment_handler

log = logging.getLogger(__name__)

# ── Shared Dispatcher (создаётся один раз для всех зеркал) ───────────────────

_shared_dp: Dispatcher | None = None


def get_shared_dp() -> Dispatcher:
    global _shared_dp
    if _shared_dp is not None:
        return _shared_dp

    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    payment_handler.IS_MIRROR = True  # оплата заблокирована во всех зеркалах

    dp.include_router(start.router)
    dp.include_router(accounts.router)
    dp.include_router(tasks.router)
    dp.include_router(payment_handler.router)
    dp.include_router(admin.router)
    dp.include_router(mirror.router)

    _shared_dp = dp
    return dp


# ── Реестр запущенных зеркал ──────────────────────────────────────────────────

_running: dict[int, asyncio.Task] = {}   # mirror_id → Task
_restarts: dict[int, int] = {}           # mirror_id → кол-во рестартов
_shutdown = False


# ── Polling loop для одного бота ──────────────────────────────────────────────

async def _polling_loop(bot: Bot, dp: Dispatcher):
    """
    Неблокирующий polling loop.
    Получает updates и передаёт их в dp.feed_update через отдельные Task-и.
    Выходит только при CancelledError.
    """
    offset = 0
    allowed = dp.resolve_used_update_types()

    while True:
        try:
            updates = await bot.get_updates(
                offset=offset,
                timeout=30,
                allowed_updates=allowed,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("get_updates ошибка: %s", e)
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update.update_id + 1
            # Обрабатываем каждый update в отдельной задаче — не блокируем polling
            asyncio.create_task(dp.feed_update(bot, update))


async def _run_mirror_forever(mirror_id: int, token: str, username: str, user_id: int):
    """
    Запускает polling для одного зеркала.
    При любом падении — перезапуск через RESTART_DELAY секунд.
    Выходит только при CancelledError или _shutdown=True.
    """
    dp = get_shared_dp()

    while not _shutdown:
        bot = Bot(token=token)
        try:
            log.info("▶️  Запуск зеркала @%s (mirror_id=%d, user=%d)", username, mirror_id, user_id)
            await bot.delete_webhook(drop_pending_updates=False)
            await _polling_loop(bot, dp)

        except asyncio.CancelledError:
            log.info("⏹  Зеркало @%s остановлено", username)
            break

        except Exception as e:
            if _shutdown:
                break
            cnt = _restarts.get(mirror_id, 0) + 1
            _restarts[mirror_id] = cnt
            log.error("❌ Зеркало @%s упало (рестарт #%d): %s", username, cnt, e)
            await asyncio.sleep(MIRROR_RESTART_DELAY)

        finally:
            try:
                await bot.session.close()
            except Exception:
                pass


# ── Управление зеркалами ──────────────────────────────────────────────────────

async def _start_mirror(m: MirrorBot):
    task = asyncio.create_task(
        _run_mirror_forever(m.id, m.token, m.bot_username or "?", m.user_id),
        name=f"mirror_{m.id}",
    )
    _running[m.id] = task
    _restarts.setdefault(m.id, 0)


async def _stop_mirror(mirror_id: int):
    task = _running.pop(mirror_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _restarts.pop(mirror_id, None)
    log.info("⏹  mirror_id=%d остановлен", mirror_id)


# ── Watch-loop ────────────────────────────────────────────────────────────────

async def watch_mirrors():
    global _shutdown

    await create_all_tables()
    log.info(
        "Mirror runner стартовал. Shard: [%s, %s)",
        MIRROR_SHARD_MIN or "0",
        MIRROR_SHARD_MAX or "∞",
    )

    iteration = 0
    while not _shutdown:
        try:
            async with SessionLocal() as db:
                q = select(MirrorBot).where(MirrorBot.is_active == True)
                if MIRROR_SHARD_MIN is not None:
                    q = q.where(MirrorBot.user_id >= MIRROR_SHARD_MIN)
                if MIRROR_SHARD_MAX is not None:
                    q = q.where(MirrorBot.user_id < MIRROR_SHARD_MAX)
                result = await db.execute(q)
                db_mirrors = {m.id: m for m in result.scalars().all()}

            # Останавливаем удалённые / деактивированные
            for mid in list(_running.keys()):
                if mid not in db_mirrors:
                    await _stop_mirror(mid)

            # Запускаем новые или перезапускаем упавшие задачи
            for mid, m in db_mirrors.items():
                if mid not in _running:
                    await _start_mirror(m)
                elif _running[mid].done():
                    log.warning("Задача mirror_id=%d мертва — перезапуск", mid)
                    del _running[mid]
                    await _start_mirror(m)

            # Статус каждые ~5 минут
            iteration += 1
            if iteration % 10 == 0:
                alive = sum(1 for t in _running.values() if not t.done())
                problems = {k: v for k, v in _restarts.items() if v > 0}
                log.info(
                    "📊 Активных зеркал: %d/%d | Рестарты: %s",
                    alive, len(_running),
                    problems or "нет",
                )

        except Exception as e:
            log.error("Ошибка watch_mirrors: %s", e)

        await asyncio.sleep(30)

    # Graceful shutdown
    log.info("🛑 Останавливаю все зеркала...")
    await asyncio.gather(
        *[_stop_mirror(mid) for mid in list(_running.keys())],
        return_exceptions=True,
    )
    log.info("✅ Все зеркала остановлены.")


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    global _shutdown
    loop = asyncio.get_running_loop()

    def _on_signal():
        global _shutdown
        _shutdown = True
        log.info("Получен сигнал завершения.")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows

    await watch_mirrors()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [MIRROR] %(levelname)s: %(message)s",
    )
    asyncio.run(main())
