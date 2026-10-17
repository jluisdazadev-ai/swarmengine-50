from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import logging

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class Base(DeclarativeBase):
    """Clase base declarativa para todos los modelos de SQLAlchemy 2.0."""


engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

redis_client: aioredis.Redis = aioredis.from_url(
    settings.redis_url,
    encoding="utf-8",
    decode_responses=True,
    socket_timeout=5.0,
    socket_connect_timeout=5.0,
    retry_on_timeout=True,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependencia de FastAPI para inyectar una sesión asíncrona de PostgreSQL por petición."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager asíncrono para tareas en segundo plano (APScheduler, Harvester, Darwin)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_database() -> None:
    """Inicializa las tablas del esquema en PostgreSQL y aplica migraciones ligeras para columnas de Hotmart."""
    import app.models  # noqa: F401 - Asegura que todos los modelos estén registrados en Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        migration_statements = [
            "ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS preferred_marketplace_mode VARCHAR(30) NOT NULL DEFAULT 'HYBRID'",
            "ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS hotmart_hottok VARCHAR(255) NOT NULL DEFAULT ''",
            "ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS hotmart_client_id VARCHAR(255) NOT NULL DEFAULT ''",
            "ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS hotmart_client_secret VARCHAR(255) NOT NULL DEFAULT ''",
            "ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS hotmart_basic_auth VARCHAR(512) NOT NULL DEFAULT ''",
        ]
        for sql in migration_statements:
            await conn.execute(text(sql))

    logger.info("Esquema de base de datos PostgreSQL (con soporte Hotmart) verificado e inicializado correctamente.")


async def close_database_connections() -> None:
    """Cierra limpiamente el pool de PostgreSQL y la conexión a Redis al apagar el servidor."""
    await engine.dispose()
    await redis_client.aclose()
    logger.info("Conexiones a PostgreSQL y Redis cerradas correctamente.")
