from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import BotClone, ProductOffer, SystemSettings
from app.services.affiliate import build_public_shortlink
from app.services.seeder import ensure_system_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Almacenamiento en memoria de los prospectos reales detectados por el Spider para visualización rápida en /admin
RECENT_SPIDER_LEADS: List[Dict[str, Any]] = []


def _extract_core_niche_query(clone: BotClone) -> str:
    """Extrae el término central limpio del nicho para buscar personas preguntando en foros/comunidades."""
    stop_words = {
        "curso",
        "cursos",
        "masterclass",
        "aprender",
        "profesional",
        "especializado",
        "avanzado",
        "avanzada",
        "desde",
        "casa",
        "online",
        "para",
        "con",
        "por",
        "del",
        "las",
        "los",
        "una",
        "uno",
    }
    raw = clone.niche_name.replace("(", " ").replace(")", " ").replace("/", " ")
    tokens = [
        t.strip()
        for t in raw.split()
        if len(t.strip()) > 2 and t.strip().lower() not in stop_words
    ]
    return " ".join(tokens[:3]) if tokens else clone.niche_name


async def search_reddit_spanish_buyer_intent(
    query: str,
    max_items: int = 3,
) -> List[Dict[str, str]]:
    """
    Rastrea conversaciones reales recientes en español en Reddit (`search.json`)
    donde usuarios preguntan cómo aprender, qué curso recomiendan o cómo iniciar en ese oficio.
    """
    encoded_q = quote_plus(f"{query} (curso OR aprender OR recomiendan OR trabajar OR negocio)")
    url = f"https://www.reddit.com/search.json?q={encoded_q}&sort=relevance&t=year&limit={max_items * 2}"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SwarmEngineSpider/2.0; +https://ofertas.veloz.digital)",
        "Accept": "application/json",
    }
    leads: List[Dict[str, str]] = []
    timeout = httpx.Timeout(connect=6.0, read=10.0, write=6.0, pool=6.0)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            data = resp.json()
            children = data.get("data", {}).get("children", [])
            for child in children:
                post = child.get("data", {})
                title = str(post.get("title") or "").strip()
                permalink = str(post.get("permalink") or "").strip()
                subreddit = str(post.get("subreddit_name_prefixed") or "r/comunidad").strip()
                author = str(post.get("author") or "usuario").strip()
                if not title or not permalink:
                    continue
                leads.append(
                    {
                        "platform": f"Reddit ({subreddit})",
                        "author": f"u/{author}",
                        "question_title": title[:180],
                        "thread_url": f"https://www.reddit.com{permalink}",
                    }
                )
                if len(leads) >= max_items:
                    break
    except Exception as exc:
        logger.debug("Spider Reddit aviso para '%s': %s", query, exc)

    return leads


async def run_spider_buyer_hunter(
    session: AsyncSession,
    max_bots: int = 8,
) -> List[Dict[str, Any]]:
    """
    Ejecuta el Spider Cazador de Intención de Compra para los Micro-Bots que ya tienen ofertas publicadas:
    1. Busca personas reales preguntando por el oficio/tema del bot en comunidades públicas.
    2. Genera un mensaje humano de recomendación con el enlace corto `/go/{short_code}` (que planta la cookie de 60 días).
    3. Devuelve la lista de oportunidades listas para sembrar cookies y guarda el historial en `RECENT_SPIDER_LEADS`.
    """
    global RECENT_SPIDER_LEADS
    sys_settings = await ensure_system_settings(session)

    offers_stmt = (
        select(ProductOffer)
        .options(selectinload(ProductOffer.clone))
        .order_by(desc(ProductOffer.detected_at))
        .limit(max_bots * 2)
    )
    offers = list((await session.execute(offers_stmt)).scalars().all())

    seen_clones = set()
    discovered_leads: List[Dict[str, Any]] = []

    for offer in offers:
        if offer.clone is None or offer.clone_id in seen_clones:
            continue
        seen_clones.add(offer.clone_id)
        if len(seen_clones) > max_bots:
            break

        clone = offer.clone
        core_query = _extract_core_niche_query(clone)
        short_link = build_public_shortlink(offer.short_code, sys_settings=sys_settings)

        raw_threads = await search_reddit_spanish_buyer_intent(core_query, max_items=2)
        for thread in raw_threads:
            suggested_reply = (
                f"¡Hola! Si estás buscando formarte en {clone.niche_name}, te recomiendo revisar "
                f"el programa '{offer.title}'. Ahora mismo tiene un -{int(round(offer.discount_percent))}% de descuento "
                f"verificado (${offer.sale_price:.2f} {offer.currency}), incluye certificado y 7 días de garantía en Hotmart. "
                f"Aquí puedes ver el temario y descuento oficial: {short_link}"
            )
            lead_item = {
                "clone_code": clone.clone_code,
                "niche_name": clone.niche_name,
                "platform": thread["platform"],
                "author": thread["author"],
                "question_title": thread["question_title"],
                "thread_url": thread["thread_url"],
                "offer_title": offer.title,
                "short_link": short_link,
                "suggested_reply": suggested_reply,
                "detected_at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            }
            discovered_leads.append(lead_item)

    if discovered_leads:
        RECENT_SPIDER_LEADS = (discovered_leads + RECENT_SPIDER_LEADS)[:30]

    return RECENT_SPIDER_LEADS


def get_cached_spider_leads() -> List[Dict[str, Any]]:
    """Devuelve los prospectos detectados recientemente por el Spider."""
    return RECENT_SPIDER_LEADS
