"""
config.py — все настройки проекта, читаются из .env файла.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram боты ─────────────────────────────────────────────────────────────
BOT_TOKEN           = os.environ["BOT_TOKEN"]
GENERATOR_BOT_TOKEN = os.environ["GENERATOR_BOT_TOKEN"]
OWNER_ID            = int(os.environ["OWNER_ID"])

# ── База данных ───────────────────────────────────────────────────────────────
DATABASE_URL = os.environ["DATABASE_URL"]

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# ── CryptoBot ─────────────────────────────────────────────────────────────────
CRYPTOBOT_TOKEN          = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_WEBHOOK_SECRET = os.getenv("CRYPTOBOT_WEBHOOK_SECRET", "")

# ── TON кошелёк ───────────────────────────────────────────────────────────────
TON_WALLET          = os.getenv("TON_WALLET", "")
TONCENTER_API_KEY   = os.getenv("TONCENTER_API_KEY", "")

# ── Groq (решение капч/квизов ботов-админов при обходе ограничений чата) ──────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")  # быстрая и дешёвая модель

# ── Контакт администратора ────────────────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@admin")

# ── Тарифы подписки ───────────────────────────────────────────────────────────
SUBSCRIPTION_PRICES = {
    "1week":  {"stars": 1,  "usdt": 1.0,  "days": 7},
    "1month": {"stars": 150, "usdt": 3.0,  "days": 30},
    "6month": {"stars": 450, "usdt": 20.0, "days": 180},
}

# ── Лимиты ────────────────────────────────────────────────────────────────────
MAX_CHATS_PER_USER = 100
TRIAL_DAYS         = 3

# ── Rate limiting для воркера ─────────────────────────────────────────────────
# Минимальный интервал (секунды) между двумя отправками одного аккаунта.
# 8 секунд = ~450 отправок/час максимум (на практике меньше из-за интервалов задач).
# Рекомендуется: 8-15. Увеличьте при спамблоках.
MIN_SEND_INTERVAL = int(os.getenv("MIN_SEND_INTERVAL", "8"))

# Мягкий лимит отправок в час на аккаунт. При превышении аккаунт получает
# меньший приоритет при балансировке (новые чаты идут на менее нагруженные).
MAX_SENDS_PER_HOUR = int(os.getenv("MAX_SENDS_PER_HOUR", "40"))

# ── Ссылки ────────────────────────────────────────────────────────────────────
MAIN_BOT_LINK    = os.getenv("MAIN_BOT_LINK",    "https://t.me/your_generator_bot")
PAYMENT_BOT_LINK = os.getenv("PAYMENT_BOT_LINK", MAIN_BOT_LINK)

# ── FastAPI ───────────────────────────────────────────────────────────────────
API_HOST   = os.getenv("API_HOST", "0.0.0.0")
API_PORT   = int(os.getenv("API_PORT", "8000"))
API_SECRET = os.environ["API_SECRET"]

# ── Проверка спамблока ────────────────────────────────────────────────────────
SPAMCHECK_USERNAME = os.getenv("SPAMCHECK_USERNAME", "")

# ── Mirror runner ─────────────────────────────────────────────────────────────
_shard_min = os.getenv("MIRROR_SHARD_MIN")
_shard_max = os.getenv("MIRROR_SHARD_MAX")
MIRROR_SHARD_MIN: int | None = int(_shard_min) if _shard_min else None
MIRROR_SHARD_MAX: int | None = int(_shard_max) if _shard_max else None
MIRROR_RESTART_DELAY = int(os.getenv("MIRROR_RESTART_DELAY", "10"))
MEDIA_BOT_TOKEN = os.getenv("MEDIA_BOT_TOKEN", BOT_TOKEN)