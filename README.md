<div align="center">

# 📡 TG SaaS — Telegram Broadcast Platform

**SaaS-сервис автоматических рассылок в Telegram-чаты через управляемый пул userbot-аккаунтов**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![Telethon](https://img.shields.io/badge/Telethon-1.36-26A5E4)](https://docs.telethon.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-async%20SQLAlchemy%202.0-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-uvicorn-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](#-лицензия)

</div>

---

## 📖 Содержание

- [О проекте](#-о-проекте)
- [Ключевые возможности](#-ключевые-возможности)
- [Архитектура](#-архитектура)
- [Технологический стек](#-технологический-стек)
- [Структура репозитория](#-структура-репозитория)
- [Модель данных](#-модель-данных)
- [Платёжная система](#-платёжная-система)
- [Обход ограничений при рассылке](#-обход-ограничений-при-рассылке)
- [Быстрый старт](#-быстрый-старт)
- [Переменные окружения](#-переменные-окружения)
- [Тарифы](#-тарифы)
- [Статус реализации](#-статус-реализации)
- [Лицензия](#-лицензия)

---

## 🚀 О проекте

**TG SaaS** — это multi-tenant платформа для автоматизации рассылок сообщений в Telegram-чаты и каналы. В основе сервиса лежит пул авторизованных Telegram-аккаунтов (userbots на базе Telethon), которые распределяют нагрузку по рассылке между собой, с автоматическим failover при блокировках, детектом ограничений (спамблок/заморозка) и обходом капч ботов-администраторов чатов с помощью LLM.

Продукт построен по модели подписки с 3-дневным триалом. Пользователь может работать:

- **напрямую** в демонстрационном/зеркальном боте сервиса, либо
- **через собственного бота-зеркало** — регистрирует токен, полученного у [@BotFather](https://t.me/BotFather), и получает изолированный интерфейс с тем же функционалом, работающий на общем бэкенде.

Проект спроектирован с прицелом на горизонтальное масштабирование (шардирование зеркал), отказоустойчивость (health-checks, автоперезапуск процессов, ротация ботов без даунтайма) и устойчивость аккаунтов-отправителей к банам платформы.

---

## ✨ Ключевые возможности

### Рассылки
- 📬 Гибкая настройка задач: текст с форматированием (bold/italic/spoiler/quote и т.д.), до 5 фото, произвольный интервал отправки.
- 🔄 **Round-robin распределение** чатов по пулу системных аккаунтов с учётом текущей нагрузки.
- 🛡️ **Реальная проверка доставки**: перед созданием задачи каждое сообщение действительно отправляется в чат и проверяется, что оно не исчезло — это исключает "мёртвые" рассылки в чаты, где сообщения тихо удаляются модерацией.
- ⚡ **Failover без даунтайма**: при бане/ограничении аккаунта в конкретном чате задача автоматически переезжает на другой аккаунт из пула, защита от ping-pong между двумя забаненными аккаунтами.
- 🗂️ Импорт списка чатов из папки Telegram (`t.me/addlist/...`) с bulk-вступлением.

### Мультиаккаунтность и устойчивость
- 🧠 **Детект ограничений аккаунта**: заморозка (`UserDeactivatedError`/`AuthKeyUnregisteredError`) и спамблок (`PeerFloodError`) определяются по нативным ошибкам Telegram API, а не по эвристикам — с защитой от ложных срабатываний через систему "страйков".
- ♻️ **Recovery-проверка**: аккаунты, помеченные `spamblocked`, периодически перепроверяются и автоматически возвращаются в строй, если Telegram снял ограничение.
- 🤖 **AI-обход капч и квизов ботов-администраторов** (через Groq API): сервис распознаёт капчу/квиз бота-модератора чата и обязательную подписку на сторонние каналы, сам решает капчу и вступает в требуемые каналы — задача не создаётся вручную заново.
- 🌐 **Именованный пул прокси**: раздельные пулы для пользовательских и системных аккаунтов, привязка при добавлении аккаунта, round-robin по нагрузке.

### Мультиарендность (multi-tenancy)
- 🪞 **Зеркальные боты**: каждый пользователь может подключить своего Telegram-бота, который работает поверх общего бэкенда и общей БД — единый Dispatcher экономит память (~5 МБ на зеркало).
- 🧩 Шардирование зеркал по диапазону `user_id` (`MIRROR_SHARD_MIN` / `MIRROR_SHARD_MAX`) для горизонтального масштабирования на несколько инстансов.

### Оплата
- ⭐ **Telegram Stars через ротацию "расходных" ботов** — обходит ограничение Bot API, при котором звёзды всегда падают на баланс бота, выставившего счёт (см. [раздел ниже](#-платёжная-система)).
- 💎 **TON** — генерация уникального инвойса с комментарием, автоматическое подтверждение платежа по факту поступления через TonCenter API.
- 💳 **CryptoBot** — приём платежей через webhook с HMAC-подписью.
- 🧑‍💼 Ручное подтверждение оплаты администратором как fallback.

### Администрирование
- 📊 Полная админ-панель: статистика сервиса, управление пользователями/подписками/блокировками, просмотр и модерация задач и аккаунтов **любого** пользователя, массовая рассылка.
- 🔧 Управление пулом системных аккаунтов и прокси без перезапуска сервиса.
- 🔁 Смена платёжного бота "на лету" — переключение подхватывается runner'ом за ~10 секунд, старые счета продолжают закрываться.

---

## 🏗 Архитектура

Сервис состоит из пяти независимых процессов, взаимодействующих через общую PostgreSQL-базу:

```mermaid
flowchart LR
    subgraph Entry["Точки входа"]
        GEN[generator_bot<br/>регистрация зеркал]
        MIR[mirror_runner<br/>N зеркальных ботов]
    end

    subgraph Payment["Оплата"]
        PAY[payment_bot_runner<br/>расходные Stars-боты]
        API[FastAPI<br/>CryptoBot webhook]
    end

    subgraph Core["Ядро"]
        WRK[worker<br/>APScheduler рассылок]
        DB[(PostgreSQL)]
        TG[Telegram Bot API /<br/>MTProto]
    end

    GEN --> DB
    MIR --> DB
    PAY --> DB
    API --> DB
    WRK --> DB
    WRK -- Telethon --> TG
    MIR -- aiogram --> TG
    GEN -- aiogram --> TG
    PAY -- aiogram --> TG
```

| Процесс | Файл | Назначение |
|---|---|---|
| **generator** | `bot/generator_bot.py` | Точка входа новых пользователей: демо-доступ, регистрация собственного бота-зеркала |
| **mirrors** | `bot/mirror_runner.py` | Держит все зеркальные боты пользователей одновременно (по `asyncio.Task` на бота), watch-loop 30с |
| **paybots** | `bot/payment_bot_runner.py` | Держит пул "расходных" ботов для приёма Telegram Stars, watch-loop 10с |
| **worker** | `worker/worker.py` | `APScheduler` — синхронизирует задачи каждые 30с и выполняет рассылку через Telethon |
| **api** | `api/app.py` | `FastAPI` — webhook CryptoBot, health-check |

### Поток создания рассылки

```mermaid
sequenceDiagram
    participant U as Пользователь
    participant Bot as aiogram (FSM)
    participant TS as task_service
    participant TL as Telethon-клиент
    participant W as worker (APScheduler)

    U->>Bot: Название, текст, фото, интервал, чаты
    Bot->>TL: Проверка доступа к чатам (light-check)
    TL-->>Bot: OK / нужен обход ограничений
    Bot->>TL: Реальная отправка проверочного сообщения
    TL-->>Bot: Сообщение "прижилось" (5с delay-check)
    Bot->>TS: create_task(...)
    TS->>TS: round-robin распределение чатов по аккаунтам
    TS-->>Bot: Задача создана
    loop каждые N минут
        W->>TL: Отправка в чат
        alt Ошибка / бан
            W->>W: Обход капчи или Failover на другой аккаунт
        end
    end
```

---

## 🛠 Технологический стек

| Категория | Технологии |
|---|---|
| Язык | Python 3.12 |
| Telegram Bot API | [aiogram 3.x](https://docs.aiogram.dev/) (FSM, middlewares, роутеры) |
| Telegram MTProto (userbots) | [Telethon 1.36](https://docs.telethon.dev/) (`StringSession`) |
| База данных | PostgreSQL, [SQLAlchemy 2.0](https://www.sqlalchemy.org/) (async ORM), Alembic (миграции) |
| Планировщик задач | APScheduler 3.10 |
| Web / Webhooks | FastAPI + uvicorn |
| HTTP-клиент | aiohttp |
| AI (обход капч) | Groq API (LLaMA 3.1) |
| Инфраструктура | Docker / docker-compose |

---

## 📁 Структура репозитория

```
├── config.py                    # Все настройки проекта (из .env)
├── database.py                  # Async engine, SessionLocal, создание таблиц
├── models/
│   └── __init__.py              # Все ORM-модели (User, Account, Task, Payment...)
│
├── bot/
│   ├── main_bot.py               # Точка входа основного бота
│   ├── generator_bot.py          # Бот-регистратор зеркал
│   ├── mirror_runner.py          # Раннер всех зеркальных ботов
│   ├── payment_bot_runner.py     # Раннер пула Stars-ботов
│   ├── middlewares.py            # AuthMiddleware — user/db в каждый handler
│   ├── keyboards.py              # Все InlineKeyboardMarkup
│   └── handlers/
│       ├── start.py              # /start, /help, главное меню
│       ├── accounts.py           # FSM добавления Telethon-аккаунта пользователя
│       ├── tasks.py              # FSM создания задачи, медиа, проверка доставки
│       ├── payment.py            # Меню оплаты (Stars / TON / администратор)
│       ├── paybot.py             # Роутер расходных Stars-ботов
│       ├── admin.py              # Панель администратора
│       └── mirror.py             # Управление зеркальным ботом
│
├── services/
│   ├── user_service.py           # Регистрация, подписка, блокировка
│   ├── account_service.py        # Telethon: авторизация, проверка доступа к чатам
│   ├── task_service.py           # CRUD задач, распределение чатов по аккаунтам
│   ├── payment_service.py        # Платежи: Stars / TON / CryptoBot
│   ├── payment_bot_service.py    # Управление пулом расходных Stars-ботов
│   ├── proxy_service.py          # Именованный пул прокси
│   ├── restriction_service.py    # Детект банов/спамблока, обход капч (Groq)
│   └── ton_service.py            # Курс TON, проверка транзакций (TonCenter)
│
├── worker/
│   └── worker.py                 # APScheduler-воркер рассылок
│
├── api/
│   └── app.py                    # FastAPI: webhook CryptoBot, /health
│
├── alembic/
│   └── env.py                    # Конфигурация миграций
│
└── docker-compose.yml
```

---

## 🗄 Модель данных

| Таблица | Назначение |
|---|---|
| `users` | Пользователи сервиса: триал, подписка, лимит чатов, блокировка |
| `mirror_bots` | Зеркальные боты пользователей (1 пользователь → 1 зеркало) |
| `payment_bots` | Пул расходных Stars-ботов (ровно один `is_active=True`) |
| `proxies` | Именованный пул прокси (`kind = user \| system`) |
| `accounts` | Telethon-аккаунты для рассылок: сессия, статус, прокси, нагрузка |
| `tasks` | Задачи рассылок: текст, форматирование, интервал, медиа |
| `task_chats` | Чаты, привязанные к задаче |
| `task_accounts` | Связка задача ↔ аккаунт + список закреплённых чатов |
| `payments` | История платежей (Stars / TON / CryptoBot) |
| `logs` | Лог каждой попытки отправки (успех/ошибка, ссылка на сообщение) |

> Медиафайлы задач хранятся **на диске** (`/app/media/task_{id}/photo_N.jpg`), а не в БД — это сознательное архитектурное решение для снижения нагрузки на PostgreSQL.

---

## 💳 Платёжная система

Ключевая инженерная особенность проекта — обход ограничения Telegram Bot API, при котором **звёзды всегда зачисляются на баланс того бота, чьим токеном выставлен счёт**, и это нельзя изменить программно.

**Решение — пул "расходных" ботов:**

```mermaid
flowchart TD
    A["Пользователь нажимает «⭐ Оплатить N Stars»<br/>в любом боте (зеркало/основной)"] --> B["Кнопка = deep-link<br/>t.me/paybot?start=pay_plan"]
    B --> C["Расходный бот сразу шлёт<br/>send_invoice(currency=XTR)"]
    C --> D["Оплата в нативном интерфейсе Telegram"]
    D --> E["Звёзды падают на баланс<br/>расходного бота"]
    E --> F["confirm_payment() обновляет<br/>sub_ends_at в общей БД"]
    F --> G["Подписка активна<br/>во ВСЕХ ботах пользователя"]
```

Администратор может сменить активного расходного бота в любой момент через `/admin → 💳 Платёжный бот → 🔄 Сменить бота` — новый бот подхватывается `payment_bot_runner.py` (watch-loop 10с) **без перезапуска сервиса**, а старый бот продолжает работать до полного удаления, чтобы не обрывать уже выставленные счета.

---

## 🧩 Обход ограничений при рассылке

Часть Telegram-чатов защищена ботами-модераторами, которые перед разрешением писать требуют пройти капчу/квиз в личных сообщениях или подписаться на сторонние каналы. Вместо того чтобы просто помечать такие чаты недоступными, сервис пытается пройти проверку автоматически:

1. При первой неудачной отправке (при создании задачи или в воркере) запускается `restriction_service.try_bypass_restriction`.
2. Сервис сканирует последние диалоги аккаунта на предмет квиза (`Poll`) или инлайн-кнопок от бота-администратора.
3. LLM (Groq, `llama-3.1-8b-instant`) определяет: это капча с вариантами ответа, или проходная кнопка-действие — и выбирает корректный вариант/нажимает кнопку.
4. Если чат требует подписки на канал — сервис парсит ссылки из сообщения-требования и вступает в них.
5. Доступ перепроверяется повторной реальной отправкой; только после этого чат считается рабочим.

---

## ⚙️ Быстрый старт

### Требования
- Python 3.12+
- PostgreSQL 14+
- Redis (для FSM-хранилища)
- Docker & docker-compose (рекомендуется)

### Установка

```bash
git clone https://github.com/oprosmem4-dev/fantastic-octo-engine.git
cd fantastic-octo-engine

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env            # заполните переменные окружения (см. ниже)
```

### Миграции БД

```bash
alembic upgrade head
```

### Запуск (5 процессов)

Каждый процесс — отдельный сервис, поэтому запускается отдельно (systemd, Docker или screen/tmux):

```bash
python bot/generator_bot.py       # регистратор зеркал
python bot/mirror_runner.py       # зеркальные боты
python bot/payment_bot_runner.py  # платёжные Stars-боты
python worker/worker.py           # рассылка
python api/app.py                 # FastAPI webhook
```

### Через Docker Compose

```bash
docker compose up -d --build
```

---

## 🔑 Переменные окружения

<details>
<summary>Полный список (.env)</summary>

```env
# Telegram боты
BOT_TOKEN=                  # основной бот
GENERATOR_BOT_TOKEN=        # бот-регистратор зеркал
OWNER_ID=                   # telegram id владельца

# База данных / кэш
DATABASE_URL=                # postgresql+asyncpg://user:pass@host/db
REDIS_URL=redis://localhost:6379/0

# FastAPI
API_SECRET=
API_HOST=0.0.0.0
API_PORT=8000

# CryptoBot
CRYPTOBOT_TOKEN=
CRYPTOBOT_WEBHOOK_SECRET=

# TON
TON_WALLET=
TONCENTER_API_KEY=

# Groq (обход капч/квизов ботов-администраторов чатов)
GROQ_API_KEY=
GROQ_MODEL=llama-3.1-8b-instant

# Контакт администратора
ADMIN_USERNAME=@admin

# Ссылки
MAIN_BOT_LINK=
PAYMENT_BOT_LINK=

# Рассылка / rate-limiting
MIN_SEND_INTERVAL=8          # секунд между отправками одного аккаунта
MAX_SENDS_PER_HOUR=40

# Зеркала
MIRROR_RESTART_DELAY=10
MIRROR_SHARD_MIN=            # для шардирования нескольких инстансов
MIRROR_SHARD_MAX=

# Прочее
SPAMCHECK_USERNAME=          # аккаунт для проверки спамблока
MEDIA_BOT_TOKEN=             # опционально
MEDIA_ROOT=./media
```

</details>

---

## 💰 Тарифы

| Тариф | Telegram Stars | USDT | Длительность |
|---|---|---|---|
| 1 неделя | 50 ⭐ | $1 | 7 дней |
| 1 месяц | 150 ⭐ | $3 | 30 дней |
| 6 месяцев | 450 ⭐ | $20 | 180 дней |

Новые пользователи получают **3 дня бесплатного триала** без ограничений функционала.

---

## 📌 Статус реализации

| Функция | Статус |
|---|---|
| Регистрация, триальный период | ✅ |
| Добавление Telethon-аккаунтов (FSM, 2FA) | ✅ |
| Создание задач рассылки + медиа на диске | ✅ |
| Рассылка с round-robin и failover | ✅ |
| Оплата Stars через ротацию расходных ботов | ✅ |
| Смена платёжного бота без даунтайма | ✅ |
| Детект спамблока / заморозки аккаунта | ✅ |
| AI-обход капч и обязательных подписок | ✅ |
| Именованный пул прокси (user/system) | ✅ |
| Зеркальные боты пользователей | ✅ |
| CryptoBot webhook | ⚠️ частично (требуется SSL) |
| Оплата TON | ⚠️ фоновая проверка транзакций |
| Статистика отправок по пользователю | ❌ в разработке |
| Напоминания об окончании подписки | ❌ в разработке |

---

## 📄 Лицензия

Проект распространяется под лицензией **MIT**. Подробности — в файле [LICENSE](LICENSE).

---

<div align="center">

Сделано с использованием Python, aiogram и Telethon

</div>
