from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List

from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select

from app.database import get_db_context
from app.models import BotClone, DailyCloneMetrics
from app.scheduler import schedule_single_clone_job, swarm_scheduler
from app.services.seeder import ensure_system_settings, generate_new_clone_batch_with_gemini

logger = logging.getLogger(__name__)


async def run_darwinian_evolution_cycle(
    min_active_days: int = 30,
    min_clicks_threshold: int = 10,
    force_evaluation: bool = False,
) -> Dict[str, Any]:
    """
    Motor Darwiniano de Auto-Poda y Replicación de Ganadores:
    1. Evalúa el rendimiento de los clones en los últimos 30 días.
    2. Identifica los clones con `total_clicks < 10` y `$0.00` en `confirmed_commissions_usd`
       tras 30 días activos (o todos los activos si `force_evaluation=True`).
    3. Los marca con `is_active = False` y los desprograma de APScheduler.
    4. Toma los 5 clones con mayor cantidad de clics y conversiones, pide a Gemini 2.5 Flash
       que genere sub-nichos hermanos de esos ganadores y crea automáticamente los nuevos registros en `BotClone`.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_created_at = now_utc - timedelta(days=min_active_days)
    metrics_window_start = date.today() - timedelta(days=min_active_days)

    pruned_codes: List[str] = []
    winner_names: List[str] = []
    spawned_codes: List[str] = []

    async with get_db_context() as session:
        sys_settings = await ensure_system_settings(session)

        stmt = (
            select(
                BotClone,
                func.coalesce(func.sum(DailyCloneMetrics.total_clicks), 0).label("clicks_30d"),
                func.coalesce(
                    func.sum(DailyCloneMetrics.confirmed_commissions_usd),
                    Decimal("0.00"),
                ).label("commissions_30d"),
            )
            .outerjoin(
                DailyCloneMetrics,
                (DailyCloneMetrics.clone_id == BotClone.id)
                & (DailyCloneMetrics.date >= metrics_window_start),
            )
            .where(BotClone.is_active.is_(True))
            .group_by(BotClone.id)
        )

        rows = (await session.execute(stmt)).all()

        scored_clones = []
        for clone, clicks_30d, commissions_30d in rows:
            clicks_int = int(clicks_30d or 0)
            comm_dec = Decimal(str(commissions_30d or "0.00"))
            scored_clones.append((clone, clicks_int, comm_dec))

            clone_created = clone.created_at
            if clone_created.tzinfo is None:
                clone_created = clone_created.replace(tzinfo=timezone.utc)

            is_mature = (clone_created <= cutoff_created_at) or force_evaluation
            if is_mature and clicks_int < min_clicks_threshold and comm_dec <= Decimal("0.00"):
                clone.is_active = False
                pruned_codes.append(clone.clone_code)
                schedule_single_clone_job(clone)
                logger.info(
                    "[Darwin] Clon %s (%s) podado por bajo rendimiento (clics=%d, comisiones=$%s).",
                    clone.clone_code,
                    clone.niche_name,
                    clicks_int,
                    comm_dec,
                )

        await session.commit()

        # Ordenar por comisiones confirmadas y luego por clics acumulados para elegir el Top 5
        scored_clones.sort(key=lambda item: (item[2], item[1]), reverse=True)
        top_5_winners = [item[0] for item in scored_clones[:5] if item[1] > 0 or item[2] > Decimal("0.00")]
        if not top_5_winners and scored_clones:
            top_5_winners = [item[0] for item in scored_clones[:5]]

        winner_names = [w.niche_name for w in top_5_winners]
        batches_to_spawn = max(1, min(len(pruned_codes), 5)) if pruned_codes else (5 if force_evaluation else 0)

        if batches_to_spawn > 0 and winner_names:
            seed_hint = ", ".join(winner_names)
            try:
                new_clones = await generate_new_clone_batch_with_gemini(
                    session=session,
                    batch_size=batches_to_spawn,
                    country_site=sys_settings.mercadolibre_country_site,
                    seed_hint=seed_hint,
                )
                for nc in new_clones:
                    schedule_single_clone_job(nc)
                    spawned_codes.append(nc.clone_code)
            except Exception as exc:
                logger.error("[Darwin] Error generando sub-nichos hijos con Gemini 2.5 Flash: %s", exc)

    summary = {
        "evaluated_at": now_utc.isoformat(),
        "pruned_count": len(pruned_codes),
        "pruned_clones": pruned_codes,
        "top_5_winners": winner_names,
        "spawned_count": len(spawned_codes),
        "spawned_clones": spawned_codes,
    }
    logger.info("Ciclo Darwiniano completado: %s", summary)
    return summary


def schedule_darwinian_monthly_job() -> None:
    """Programa en `APScheduler` la ejecución automática del Motor Darwiniano cada 30 días."""
    swarm_scheduler.add_job(
        run_darwinian_evolution_cycle,
        trigger=IntervalTrigger(days=30),
        id="darwin_monthly_evolution",
        name="Motor Darwiniano de Auto-Poda Mensual (30 días)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Tarea mensual del Motor Darwiniano registrada en APScheduler (cada 30 días).")
