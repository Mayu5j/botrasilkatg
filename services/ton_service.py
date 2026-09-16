"""
services/ton_service.py — интеграция с TON:
  - получение курса TON/USD через CoinGecko
  - генерация уникального комментария для платежа
  - получение транзакций кошелька через TonCenter API v2
"""
import logging
import secrets

import aiohttp

log = logging.getLogger(__name__)

TONCENTER_URL = "https://toncenter.com/api/v2"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


async def get_ton_price_usd() -> float | None:
    """Курс TON в USD (CoinGecko)."""
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                COINGECKO_URL,
                params={"ids": "the-open-network", "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()
            return float(data["the-open-network"]["usd"])
    except Exception as e:
        log.error("Ошибка получения курса TON: %s", e)
        return None


def generate_comment(user_id: int) -> str:
    """Уникальный комментарий-идентификатор для платежа, например PAY-123456-a1b2c3d4."""
    rnd = secrets.token_hex(4)
    return f"PAY-{user_id}-{rnd}"


async def usd_to_ton(usd_amount: float) -> tuple[float, float] | None:
    """
    Конвертировать USD → TON.
    Возвращает (ton_amount, rate) или None если API недоступен.
    """
    rate = await get_ton_price_usd()
    if not rate:
        return None
    ton_amount = round(usd_amount / rate, 4)
    return ton_amount, rate


async def get_recent_transactions(wallet: str, api_key: str = "") -> list[dict]:
    """Последние 50 входящих транзакций кошелька через TonCenter v2."""
    try:
        headers = {"X-API-Key": api_key} if api_key else {}
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"{TONCENTER_URL}/getTransactions",
                params={"address": wallet, "limit": 50},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            data = await resp.json()
            if data.get("ok"):
                return data.get("result", [])
    except Exception as e:
        log.error("Ошибка TonCenter getTransactions: %s", e)
    return []


def parse_comment(tx: dict) -> str:
    """Комментарий из входящего сообщения транзакции."""
    return (tx.get("in_msg") or {}).get("message", "")


def parse_value_ton(tx: dict) -> float:
    """Сумма входящего перевода в TON (из наноTON)."""
    value_nano = int((tx.get("in_msg") or {}).get("value", 0))
    return value_nano / 1_000_000_000
