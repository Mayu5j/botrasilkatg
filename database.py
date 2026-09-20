"""
database.py — подключение к PostgreSQL через SQLAlchemy (async).
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
 
from config import DATABASE_URL
 
# Создаём движок (пул соединений к PostgreSQL)
engine = create_async_engine(
    DATABASE_URL,
    echo=False,       # True = выводить SQL в лог (удобно для отладки)
    pool_size=10,
    max_overflow=20,
)
 
# Фабрика сессий — используется во всех функциях для работы с БД
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # объекты не протухают после commit
)
 
 
# Базовый класс для всех моделей
class Base(DeclarativeBase):
    pass
 
 
async def get_db() -> AsyncSession:
    """
    Зависимость FastAPI — даёт сессию БД в route-функцию.
    Использование:
        async def my_route(db: AsyncSession = Depends(get_db)):
    """
    async with SessionLocal() as session:
        yield session
 
 
async def create_all_tables():
    """Создать все таблицы в БД (вызывать при старте)."""
    async with engine.begin() as conn:
        from models import Base as M
        await conn.run_sync(M.metadata.create_all)
        from sqlalchemy import text
        await conn.execute(text(
            "ALTER TABLE logs ADD COLUMN IF NOT EXISTS message_id BIGINT"
        ))
        await conn.execute(text(
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS restriction_strikes INTEGER DEFAULT 0"
        ))
        # Именованный пул прокси (см. models.Proxy) — добавляется отдельной
        # колонкой к уже существующей таблице accounts, старые legacy-поля
        # proxy_host/port/... не трогаем.
        await conn.execute(text(
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS proxy_id INTEGER REFERENCES proxies(id)"
        ))

