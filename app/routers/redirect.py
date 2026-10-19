from datetime import date, datetime, timezone
from decimal import Decimal
import logging
from typing import Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_context, get_db_session, redis_client
from app.models import ClickEvent, DailyCloneMetrics, ProductOffer
from app.services.affiliate import (
    REDIS_SHORTCODE_PREFIX,
    cache_shortlink_in_redis,
    hash_visitor_ip,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Redireccionador de Afiliados"])


async def _record_click_telemetry(
    offer_id: int,
    clone_id: int,
    visitor_ip_hash: str,
    user_agent: str,
    referer: str,
) -> None:
    """
    Registra de forma asíncrona el evento en `ClickEvent` e incrementa atómicamente `+1`
    en `DailyCloneMetrics.total_clicks` para el `clone_id` correspondiente al día actual.
    """
    today = date.today()
    async with get_db_context() as session:
        click_event = ClickEvent(
            offer_id=offer_id,
            clone_id=clone_id,
            visitor_ip_hash=visitor_ip_hash,
            user_agent=user_agent[:512],
            referer=referer[:512],
            clicked_at=datetime.now(timezone.utc),
        )
        session.add(click_event)

        upsert_stmt = (
            pg_insert(DailyCloneMetrics)
            .values(
                clone_id=clone_id,
                date=today,
                offers_published=0,
                total_clicks=1,
                gemini_input_tokens=0,
                gemini_output_tokens=0,
                ai_cost_usd=Decimal("0.000000"),
                confirmed_commissions_usd=Decimal("0.00"),
            )
            .on_conflict_do_update(
                constraint="uq_daily_clone_metrics_clone_date",
                set_={
                    "total_clicks": DailyCloneMetrics.total_clicks + 1,
                },
            )
        )
        await session.execute(upsert_stmt)
        await session.commit()


async def _resolve_short_code(
    short_code: str,
    session: AsyncSession,
) -> Optional[Tuple[int, int, str]]:
    """
    Resuelve `short_code` buscando primero en caché Redis (latencia sub-milisegundo)
    y haciendo fallback a PostgreSQL (`ProductOffer`).
    Devuelve `(offer_id, clone_id, affiliate_full_url)` o `None` si no existe.
    """
    redis_key = f"{REDIS_SHORTCODE_PREFIX}{short_code}"
    try:
        cached = await redis_client.hgetall(redis_key)
        if cached and "affiliate_full_url" in cached and "offer_id" in cached and "clone_id" in cached:
            return (
                int(cached["offer_id"]),
                int(cached["clone_id"]),
                str(cached["affiliate_full_url"]),
            )
    except Exception as exc:
        logger.warning("Fallo consultando Redis para short_code %s: %s", short_code, exc)

    stmt = (
        select(ProductOffer)
        .where(ProductOffer.short_code == short_code)
        .limit(1)
    )
    offer = (await session.execute(stmt)).scalar_one_or_none()
    if offer is None:
        return None

    await cache_shortlink_in_redis(offer)
    return (offer.id, offer.clone_id, offer.affiliate_full_url)


BOT_USER_AGENT_KEYWORDS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "facebookexternalhit",
    "headless",
    "ahrefs",
    "semrush",
    "uptime",
    "google",
    "bing",
    "yandex",
    "baidu",
    "python",
    "curl",
    "wget",
    "httpclient",
    "petalbot",
    "dotbot",
    "feedly",
    "mediapartners",
)


def is_bot_or_crawler(user_agent: str) -> bool:
    """Detecta si el User-Agent corresponde a un crawler, bot de búsqueda o script automatizado."""
    if not user_agent or len(user_agent.strip()) < 10:
        return True
    ua = user_agent.lower()
    return any(k in ua for k in BOT_USER_AGENT_KEYWORDS)


def is_browser_prefetch(request: Request) -> bool:
    """Detecta si la petición es una precarga silenciosa en segundo plano del navegador."""
    for header in ("sec-purpose", "purpose", "x-moz", "x-purpose"):
        val = request.headers.get(header, "").lower()
        if "prefetch" in val or "prerender" in val:
            return True
    return False


@router.get("/go/{short_code}", response_class=RedirectResponse, status_code=302)
async def redirect_affiliate_offer(
    short_code: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """
    Endpoint de alta velocidad `/go/{short_code}`:
    1. Busca la oferta en caché Redis o PostgreSQL.
    2. Descarta prefetchs del navegador y rastreadores web (Googlebot, etc.) para que SOLO cuenten clics humanos reales.
    3. Aplica deduplicación de 5 minutos por IP y oferta para evitar clics inflados.
    4. Redirige inmediatamente con 302 hacia el enlace oficial de afiliado.
    """
    clean_code = short_code.strip()
    resolved = await _resolve_short_code(clean_code, session)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Oferta no encontrada o enlace expirado.")

    offer_id, clone_id, affiliate_full_url = resolved

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Robots-Tag": "noindex, nofollow",
    }

    # 1. Ignorar silenciosamente precargas automáticas del navegador
    if is_browser_prefetch(request):
        return RedirectResponse(url=affiliate_full_url, status_code=302, headers=headers)

    user_agent = request.headers.get("user-agent", "")

    # 2. Ignorar rastreadores de búsqueda y bots (no son compradores)
    if is_bot_or_crawler(user_agent):
        return RedirectResponse(url=affiliate_full_url, status_code=302, headers=headers)

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    elif request.client and request.client.host:
        client_ip = request.client.host
    else:
        client_ip = "0.0.0.0"

    visitor_ip_hash = hash_visitor_ip(client_ip)
    referer = request.headers.get("referer", "")

    # 3. Deduplicación por IP y oferta durante 5 minutos para evitar clics repetidos
    cooldown_key = f"swarm:cooldown:click:{visitor_ip_hash}:{offer_id}"
    is_duplicate = False
    try:
        is_duplicate = not bool(await redis_client.set(cooldown_key, "1", nx=True, ex=300))
    except Exception:
        is_duplicate = False

    if not is_duplicate:
        background_tasks.add_task(
            _record_click_telemetry,
            offer_id=offer_id,
            clone_id=clone_id,
            visitor_ip_hash=visitor_ip_hash,
            user_agent=user_agent,
            referer=referer,
        )

    return RedirectResponse(
        url=affiliate_full_url,
        status_code=302,
        headers=headers,
    )
