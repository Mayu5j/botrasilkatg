"""
bot/main_bot.py — запуск главного бота.

Собирает все роутеры, подключает middleware, запускает polling.
Этот файл запускается как: python bot/main_bot.py
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, TON_WALLET, TONCENTER_API_KEY
from database import create_all_tables, SessionLocal
from bot.middlewares import AuthMiddleware
from bot.handlers import start, accounts, tasks, payment, admin, mirror

log = logging.getLogger(__name__)


async def _check_ton_payments(bot: Bot) -> None:
    """
    Периодическая задача: проверяет входящие TON-транзакции и активирует
    подписку пользователю при совпадении комментария и суммы.
    """
    if not TON_WALLET:
        return

    from services import payment_service
    from services import ton_service
    from services.user_service import get_user

    async with SessionLocal() as db:
        # Истекаем старые счета
        expired = await payment_service.expire_old_ton_payments(db)
        if expired:
            log.info("TON: истёк %d старый счёт(а)", expired)

        pending = await payment_service.get_pending_ton_payments(db)
        if not pending:
            return

        txs = await ton_service.get_recent_transactions(TON_WALLET, TONCENTER_API_KEY)
        if not txs:
            return

        for payment in pending:
            comment  = payment.external_id or ""
            expected = payment.amount  # TON

            for tx in txs:
                if ton_service.parse_comment(tx) != comment:
                    continue
                received = ton_service.parse_value_ton(tx)
                if received < expected * 0.99:
                    log.warning(
                        "TON: comment=%s пришло %.4f TON, ожидалось %.4f — мало",
                        comment, received, expected,
                    )
                    continue

                user = await get_user(db, payment.user_id)
                if user:
                    await payment_service.confirm_payment(db, payment, user)
                    try:
                        await bot.send_message(
                            user.id,
                            f"✅ *Оплата получена!*\n\n"
                            f"Зачислено: {received:.4f} TON\n"
                            f"Подписка активирована. Спасибо! 🎉",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                break


async def main():
    await create_all_tables()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    dp.include_router(start.router)
    dp.include_router(accounts.router)
    dp.include_router(tasks.router)
    dp.include_router(payment.router)
    dp.include_router(admin.router)
    dp.include_router(mirror.router)

    # Запускаем планировщик проверки TON-платежей
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _check_ton_payments,
        trigger="interval",
        seconds=30,
        args=[bot],
        id="ton_checker",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("TON-чекер запущен (интервал 30 сек)")

    log.info("Главный бот запущен...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [BOT] %(levelname)s: %(message)s"
    )
    asyncio.run(main())
