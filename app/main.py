from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator, Dict

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import BASE_DIR, get_settings
from app.database import (
    close_database_connections,
    get_db_context,
    init_database,
    redis_client,
)
from app.routers import dashboard, pseo, redirect, webhooks
from app.scheduler import swarm_scheduler, sync_all_clone_jobs_in_scheduler
from app.services.darwin import schedule_darwinian_monthly_job
from app.services.seeder import seed_initial_clones

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("swarmengine50")
settings = get_settings()

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
settings.static_cards_dir.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Ciclo de vida de producción de SwarmEngine 50:
    1. Crea o verifica las tablas en PostgreSQL 16 (incluyendo soporte Hotmart).
    2. Siembra automáticamente los 50 Micro-Bots iniciales y SystemSettings si no existen.
    3. Registra en APScheduler los 50 trabajos escalonados con Jitter y el ciclo mensual Darwiniano.
    4. Arranca el planificador asíncrono en segundo plano.
    """
    logger.info("Iniciando plataforma %s...", settings.app_name)
    await init_database()

    async with get_db_context() as session:
        await seed_initial_clones(session)

    await sync_all_clone_jobs_in_scheduler()
    schedule_darwinian_monthly_job()

    if not swarm_scheduler.running:
        swarm_scheduler.start()
        logger.info("APScheduler iniciado con %d trabajos activos.", len(swarm_scheduler.get_jobs()))

    try:
        yield
    finally:
        logger.info("Deteniendo %s...", settings.app_name)
        if swarm_scheduler.running:
            swarm_scheduler.shutdown(wait=False)
        await close_database_connections()


app = FastAPI(
    title="SwarmEngine 50: Plataforma de Enjambre de 50 Micro-Bots de Afiliados (Hotmart + Multi-Tienda) y pSEO",
    description=(
        "Sistema autónomo de cosecha híbrida (Hotmart + MercadoLibre + AliExpress + Amazon), "
        "redacción estructurada con Gemini 2.5 Flash, tarjetas promocionales 1080x1080 con Pillow, "
        "publicación en Telegram/WhatsApp, portal pSEO con JSON-LD y Motor Darwiniano de Auto-Poda."
    ),
    version="1.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health", tags=[" Diagnóstico "])
async def healthcheck() -> Dict[str, object]:
    """Verifica el estado real de PostgreSQL, Redis y APScheduler."""
    db_ok = False
    redis_ok = False

    try:
        async with get_db_context() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception as exc:
        logger.error("Healthcheck PostgreSQL falló: %s", exc)

    try:
        redis_ok = bool(await redis_client.ping())
    except Exception as exc:
        logger.warning("Healthcheck Redis falló: %s", exc)

    return {
        "status": "ok" if db_ok else "degraded",
        "database_connected": db_ok,
        "redis_connected": redis_ok,
        "scheduler_running": swarm_scheduler.running,
        "scheduled_jobs_count": len(swarm_scheduler.get_jobs()),
    }


# Registro de routers en orden de especificidad de rutas
app.include_router(webhooks.router)
app.include_router(redirect.router)
app.include_router(dashboard.router)
app.include_router(pseo.router)
