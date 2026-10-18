import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
import logging
import random
from typing import Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.database import get_db_context, redis_client
from app.models import BotClone, DailyCloneMetrics, ProductOffer
from app.services.affiliate import (
    build_affiliate_url,
    build_public_shortlink,
    cache_shortlink_in_redis,
    generate_unique_short_code,
)
from app.services.gemini_engine import (
    BudgetExceededException,
    generate_offer_copy_with_gemini,
)
from app.services.harvester import MultiStoreHarvesterOrchestrator
from app.services.image_card import generate_offer_card
from app.services.indexer import notify_search_engines_for_offer
from app.services.publisher import (
    build_category_pseo_url,
    build_offer_pseo_url,
    publish_offer_to_telegram,
    publish_offer_to_whatsapp,
)
from app.services.seeder import ensure_system_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Semáforo global para limitar concurrencia máxima simultánea y mantener uso de RAM < 1.2GB en el VPS
MAX_CONCURRENT_CLONES = 3
clone_execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLONES)

harvester_orchestrator = MultiStoreHarvesterOrchestrator()
swarm_scheduler = AsyncIOScheduler(timezone="UTC")


async def _increment_offers_published(clone_id: int) -> None:
    """Incrementa atómicamente `+1` en `DailyCloneMetrics.offers_published` para el día actual."""
    today = date.today()
    async with get_db_context() as session:
        upsert_stmt = (
            pg_insert(DailyCloneMetrics)
            .values(
                clone_id=clone_id,
                date=today,
                offers_published=1,
                total_clicks=0,
                gemini_input_tokens=0,
                gemini_output_tokens=0,
                ai_cost_usd=Decimal("0.000000"),
                confirmed_commissions_usd=Decimal("0.00"),
            )
            .on_conflict_do_update(
                constraint="uq_daily_clone_metrics_clone_date",
                set_={
                    "offers_published": DailyCloneMetrics.offers_published + 1,
                },
            )
        )
        await session.execute(upsert_stmt)
        await session.commit()


async def execute_clone_pipeline(clone_id: int, force_manual: bool = False) -> Dict[str, object]:
    """
    Ejecuta el ciclo completo de un Micro-Bot (`BotClone`):
    1. Adquiere lock distribuido en Redis para evitar ejecuciones solapadas del mismo clon.
    2. Cosecha ofertas reales en MercadoLibre / AliExpress / Amazon.
    3. Genera `short_code` y URL de afiliado con atribución `affiliate_sub_id`.
    4. Redacta titular, post HTML de Telegram y mini-reseña pSEO con Gemini 2.5 Flash.
    5. Renderiza tarjeta gráfica 1080x1080 en `BytesIO` con Pillow.
    6. Persiste `ProductOffer` en PostgreSQL y caché Redis.
    7. Publica en Telegram y WhatsApp (Evolution API).
    8. Notifica a Google Indexing API e IndexNow API.
    """
    lock_key = f"swarm:lock:clone:{clone_id}"
    lock_acquired = False

    try:
        lock_acquired = bool(await redis_client.set(lock_key, "running", nx=True, ex=300))
    except Exception as exc:
        logger.warning("Redis lock no disponible para clone_id=%d, continuando con semáforo local: %s", clone_id, exc)
        lock_acquired = True

    if not lock_acquired and not force_manual:
        logger.info("El clon id=%d ya se encuentra en ejecución en otro worker; omitiendo turno.", clone_id)
        return {"status": "skipped", "reason": "already_running"}

    async with clone_execution_semaphore:
        try:
            if not force_manual:
                # Retraso aleatorio (Jitter) adicional de 1 a 15 segundos para evitar ráfagas de red
                await asyncio.sleep(random.uniform(1.0, 15.0))

            async with get_db_context() as session:
                sys_settings = await ensure_system_settings(session)
                clone = (
                    await session.execute(select(BotClone).where(BotClone.id == clone_id).limit(1))
                ).scalar_one_or_none()

                if clone is None:
                    return {"status": "error", "reason": "clone_not_found"}

                if not clone.is_active and not force_manual:
                    return {"status": "skipped", "reason": "clone_paused"}

                candidates = await harvester_orchestrator.harvest_best_deals_for_clone(
                    session=session,
                    clone=clone,
                    sys_settings=sys_settings,
                    limit=1,
                )
                if not candidates:
                    logger.info("[%s] Sin ofertas nuevas que superen el %d%% de descuento.", clone.clone_code, clone.min_discount_percent)
                    return {"status": "no_deals", "clone_code": clone.clone_code}

                best_deal = candidates[0]
                short_code = await generate_unique_short_code(session)
                affiliate_url = build_affiliate_url(
                    raw_url=best_deal.raw_product_url,
                    marketplace=best_deal.source_marketplace,
                    clone=clone,
                    sys_settings=sys_settings,
                )
                public_shortlink = build_public_shortlink(short_code, sys_settings=sys_settings)

                try:
                    ai_content, _, _, _ = await generate_offer_copy_with_gemini(
                        session=session,
                        clone=clone,
                        candidate=best_deal,
                        sys_settings=sys_settings,
                    )
                except BudgetExceededException as budget_exc:
                    logger.warning("[%s] %s", clone.clone_code, budget_exc)
                    return {"status": "budget_exceeded", "reason": str(budget_exc)}

                card_buffer, _ = await generate_offer_card(
                    image_url=best_deal.image_url,
                    hook_headline=ai_content.hook_headline,
                    original_price=best_deal.original_price,
                    sale_price=best_deal.sale_price,
                    discount_percent=best_deal.discount_percent,
                    currency=best_deal.currency,
                    niche_name=clone.niche_name,
                    short_code=short_code,
                )

                # Verificar si existe un registro antiguo (>15 días) del mismo external_item_id para actualizarlo
                existing_offer_stmt = select(ProductOffer).where(
                    ProductOffer.clone_id == clone.id,
                    ProductOffer.external_item_id == best_deal.external_item_id,
                )
                offer = (await session.execute(existing_offer_stmt)).scalar_one_or_none()

                if offer is None:
                    offer = ProductOffer(
                        clone_id=clone.id,
                        external_item_id=best_deal.external_item_id,
                        source_marketplace=best_deal.source_marketplace,
                        title=best_deal.title,
                        original_price=best_deal.original_price,
                        sale_price=best_deal.sale_price,
                        discount_percent=best_deal.discount_percent,
                        currency=best_deal.currency,
                        raw_product_url=best_deal.raw_product_url,
                        affiliate_full_url=affiliate_url,
                        short_code=short_code,
                        image_url=best_deal.image_url,
                        ai_telegram_caption=ai_content.telegram_post_html,
                        ai_seo_article_html=ai_content.seo_mini_review_html,
                        published_telegram=False,
                        published_whatsapp=False,
                        indexed_google=False,
                        detected_at=datetime.now(timezone.utc),
                    )
                    session.add(offer)
                else:
                    offer.title = best_deal.title
                    offer.original_price = best_deal.original_price
                    offer.sale_price = best_deal.sale_price
                    offer.discount_percent = best_deal.discount_percent
                    offer.affiliate_full_url = affiliate_url
                    offer.short_code = short_code
                    offer.image_url = best_deal.image_url
                    offer.ai_telegram_caption = ai_content.telegram_post_html
                    offer.ai_seo_article_html = ai_content.seo_mini_review_html
                    offer.detected_at = datetime.now(timezone.utc)

                await session.commit()
                await session.refresh(offer)
                await cache_shortlink_in_redis(offer)

                # Publicación en Telegram
                telegram_error = ""
                tg_token = (sys_settings.telegram_bot_token or settings.telegram_bot_token).strip()
                if not tg_token:
                    telegram_error = "Falta configurar Telegram Bot Token en Configuración Global"
                elif not clone.telegram_channel_id:
                    telegram_error = "Falta asignar Canal de Telegram al bot"
                else:
                    try:
                        await publish_offer_to_telegram(
                            card_buffer=card_buffer,
                            caption_html=ai_content.telegram_post_html,
                            short_link=public_shortlink,
                            clone=clone,
                            sys_settings=sys_settings,
                        )
                        offer.published_telegram = True
                    except Exception as tg_exc:
                        telegram_error = str(tg_exc)
                        logger.warning("[%s] No se pudo publicar en Telegram: %s", clone.clone_code, tg_exc)

                # Publicación en WhatsApp (Evolution API)
                evo_key = (sys_settings.evolution_api_key or settings.evolution_api_key).strip()
                if evo_key and clone.whatsapp_group_jid:
                    try:
                        await publish_offer_to_whatsapp(
                            card_buffer=card_buffer,
                            caption_html=ai_content.telegram_post_html,
                            short_link=public_shortlink,
                            clone=clone,
                            sys_settings=sys_settings,
                        )
                        offer.published_whatsapp = True
                    except Exception as wa_exc:
                        logger.warning("[%s] No se pudo publicar en WhatsApp: %s", clone.clone_code, wa_exc)

                # Notificación SEO (Google Indexing API + IndexNow)
                offer_pseo_url = build_offer_pseo_url(
                    short_code=offer.short_code,
                    title=offer.title,
                    sys_settings=sys_settings,
                )
                category_pseo_url = build_category_pseo_url(
                    category_slug=clone.pseo_category_slug,
                    sys_settings=sys_settings,
                )
                indexed_ok = await notify_search_engines_for_offer(
                    offer_url=offer_pseo_url,
                    category_url=category_pseo_url,
                    sys_settings=sys_settings,
                )
                offer.indexed_google = indexed_ok

                await session.commit()
                await _increment_offers_published(clone.id)

                return {
                    "status": "published",
                    "clone_code": clone.clone_code,
                    "offer_id": offer.id,
                    "short_code": offer.short_code,
                    "title": offer.title,
                    "discount_percent": float(offer.discount_percent),
                    "published_telegram": offer.published_telegram,
                    "telegram_error": telegram_error,
                }

        finally:
            try:
                await redis_client.delete(lock_key)
            except Exception as unlock_exc:
                logger.debug("Error liberando lock Redis %s: %s", lock_key, unlock_exc)


def schedule_single_clone_job(clone: BotClone) -> None:
    """Registra o actualiza en `APScheduler` la tarea periódica de un Micro-Bot con Jitter escalonado."""
    job_id = f"clone_job_{clone.id}"
    if not clone.is_active:
        if swarm_scheduler.get_job(job_id):
            swarm_scheduler.remove_job(job_id)
        return

    interval_minutes = max(15, int(clone.schedule_interval_minutes or 60))
    # Escalonar el primer disparo según el ID del clon para distribuir uniformemente los 50 bots en la hora
    initial_offset_seconds = (clone.id * 71) % (interval_minutes * 60)

    trigger = IntervalTrigger(
        minutes=interval_minutes,
        jitter=120,  # Jitter aleatorio de +/- 120 segundos en cada ciclo
    )

    swarm_scheduler.add_job(
        execute_clone_pipeline,
        trigger=trigger,
        id=job_id,
        args=[clone.id, False],
        name=f"SwarmBot [{clone.clone_code}] {clone.niche_name}",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.debug(
        "Programado %s cada %d min (offset=%ds, jitter=120s).",
        clone.clone_code,
        interval_minutes,
        initial_offset_seconds,
    )


async def sync_all_clone_jobs_in_scheduler() -> int:
    """Lee todos los `BotClone` activos de PostgreSQL y sincroniza sus trabajos en `APScheduler`."""
    async with get_db_context() as session:
        stmt = select(BotClone).order_by(BotClone.id.asc())
        clones = list((await session.execute(stmt)).scalars().all())

    active_count = 0
    for clone in clones:
        schedule_single_clone_job(clone)
        if clone.is_active:
            active_count += 1

    logger.info("APScheduler sincronizó %d micro-bots activos de %d totales.", active_count, len(clones))
    return active_count
