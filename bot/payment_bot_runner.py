"""
bot/payment_bot_runner.py — запуск всех платёжных Stars-ботов из БД.
 
Архитектура — упрощённая копия bot/mirror_runner.py:
  - Один общий Dispatcher с единственным роутером paybot.router
    (никакого FSM, аккаунтов, задач — только /start, pre_checkout,
    successful_payment).
  - Запускаются ВСЕ строки таблицы payment_bots, а не только is_active=True.
    Это специально: когда админ меняет активного бота, старый НЕ должен
    обрываться на середине — пользователь, успевший открыть его deep-link,
    должен спокойно оплатить счёт.
  - watch-loop опрашивает БД каждые 10 секунд — поэтому смена бота в
    админке долетает до реального переключения кнопок практически
    мгновенно (кнопки формируются прямо в момент клика по плану, читая
    активного бота из БД — см. bot/handlers/payment.py).
  - Чтобы насовсем остановить бота (освободить ресурсы) — удалите его
    в админке (/admin → Платёжный бот → История → Удалить). Просто
    смена активного НЕ останавливает старый бот.
 
Запуск (отдельный процесс/сервис, как mirror_runner и worker):
    python bot/payment_bot_runner.py
"""
import asyncio
import logging
import signal
 
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
 
from config import MIRROR_RESTART_DELAY
from database import create_all_tables, SessionLocal
from models import PaymentBot
from bot.handlers import paybot
 
log = logging.getLogger(__name__)
 
WATCH_INTERVAL = 10  # секунд — быстрее, чем у mirror_runner (там 30), для бесшовной замены
 
# ── Shared Dispatcher ──────────────────────────────────────────────────────────
 
_shared_dp: Dispatcher | None = None
 
 
def get_shared_dp() -> Dispatcher:
    global _shared_dp
    if _shared_dp is not None:
        return _shared_dp
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(paybot.router)
    _shared_dp = dp
    return dp
 
 
# ── Реестр запущенных ботов ────────────────────────────────────────────────────
 
_running: dict[int, asyncio.Task] = {}
_shutdown = False
 
 
# ── Polling loop для одного бота ──────────────────────────────────────────────
 
async def _polling_loop(bot: Bot, dp: Dispatcher):
    offset = 0
    allowed = dp.resolve_used_update_types()
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=allowed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("get_updates ошибка: %s", e)
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = update.update_id + 1
            asyncio.create_task(dp.feed_update(bot, update))
 
 
async def _run_bot_forever(bot_id: int, token: str, username: str):
    dp = get_shared_dp()
 
    while not _shutdown:
        bot = Bot(token=token)
        try:
            log.info("▶️  Запуск платёжного бота @%s (id=%d)", username, bot_id)
            await bot.delete_webhook(drop_pending_updates=False)
            await _polling_loop(bot, dp)
 
        except asyncio.CancelledError:
            log.info("⏹  Платёжный бот @%s остановлен", username)
            break
 
        except Exception as e:
            if _shutdown:
                break
            log.error("❌ Платёжный бот @%s упал: %s", username, e)
            await asyncio.sleep(MIRROR_RESTART_DELAY)
 
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass
 
 
# ── Управление ──────────────────────────────────────────────────────────────────
 
async def _start_bot(b: PaymentBot):
    task = asyncio.create_task(
        _run_bot_forever(b.id, b.token, b.bot_username or "?"),
        name=f"paybot_{b.id}",
    )
    _running[b.id] = task
 
 
async def _stop_bot(bot_id: int):
    task = _running.pop(bot_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    log.info("⏹  paybot id=%d остановлен и снят с поллинга", bot_id)
 
 
# ── Watch-loop ────────────────────────────────────────────────────────────────
 
async def watch_paybots():
    global _shutdown
 
    await create_all_tables()
    log.info("Payment bot runner стартовал (опрос БД каждые %ds).", WATCH_INTERVAL)
 
    while not _shutdown:
        try:
            async with SessionLocal() as db:
                result = await db.execute(select(PaymentBot))
                db_bots = {b.id: b for b in result.scalars().all()}
 
            # Удалённые в админке — останавливаем
            for bid in list(_running.keys()):
                if bid not in db_bots:
                    await _stop_bot(bid)
 
            # Новые или упавшие — (пере)запускаем
            for bid, b in db_bots.items():
                if bid not in _running:
                    await _start_bot(b)
                elif _running[bid].done():
                    log.warning("Задача paybot id=%d мертва — перезапуск", bid)
                    del _running[bid]
                    await _start_bot(b)
 
        except Exception as e:
            log.error("Ошибка watch_paybots: %s", e)
 
        await asyncio.sleep(WATCH_INTERVAL)
 
    log.info("🛑 Останавливаю все платёжные боты...")
    await asyncio.gather(*[_stop_bot(bid) for bid in list(_running.keys())], return_exceptions=True)
    log.info("✅ Все платёжные боты остановлены.")
 
 
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
 
    await watch_paybots()
 
 
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [PAYBOT-RUNNER] %(levelname)s: %(message)s",
    )
    asyncio.run(main())

