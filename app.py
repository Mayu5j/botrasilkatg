"""
api/app.py — FastAPI backend.

Обрабатывает:
  - Webhook от CryptoBot (подтверждение оплаты)
  - Health check
"""
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy import select

from config import CRYPTOBOT_WEBHOOK_SECRET, API_HOST, API_PORT
from database import SessionLocal, create_all_tables
from models import Payment
from services.user_service import get_user
from services.payment_service import confirm_payment

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_tables()
    log.info("FastAPI запущен.")
    yield


app = FastAPI(title="TG SaaS Backend", lifespan=lifespan)


@app.post("/webhook/cryptobot")
async def cryptobot_webhook(request: Request):
    """Webhook CryptoBot — вызывается при успешной оплате."""
    body = await request.body()

    # Проверяем HMAC-подпись чтобы запрос был точно от CryptoBot
    signature = request.headers.get("crypto-pay-api-signature", "")
    expected  = hmac.new(
        CRYPTOBOT_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if signature != expected:
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = await request.json()
    if data.get("update_type") != "invoice_paid":
        return {"ok": True}

    payload    = data["payload"]["payload"]        # "sub_{plan}_{user_id}"
    invoice_id = str(data["payload"]["invoice_id"])

    try:
        _, plan, user_id_str = payload.split("_", 2)
        user_id = int(user_id_str)
    except ValueError:
        log.error("Неверный payload: %s", payload)
        return {"ok": True}

    async with SessionLocal() as db:
        result = await db.execute(
            select(Payment).where(
                Payment.external_id == invoice_id,
                Payment.status == "pending",
            )
        )
        payment = result.scalar_one_or_none()
        if not payment:
            return {"ok": True}

        user = await get_user(db, user_id)
        if not user:
            return {"ok": True}

        await confirm_payment(db, payment, user)
        log.info("CryptoBot: подтверждена оплата user=%d plan=%s", user_id, plan)

    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [API] %(levelname)s: %(message)s"
    )
    uvicorn.run(app, host=API_HOST, port=API_PORT)
