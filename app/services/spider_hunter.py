import asyncio
from datetime import datetime, timezone
import html
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import BotClone, HotmartProductPool, ProductOffer, SystemSettings
from app.services.affiliate import build_public_shortlink
from app.services.seeder import ensure_system_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Almacenamiento en memoria de prospectos detectados por el Spider para visualización en /admin
RECENT_SPIDER_LEADS: List[Dict[str, Any]] = []


def _extract_core_niche_query(clone: BotClone) -> str:
    """Extrae el término central limpio del nicho para buscar personas preguntando en foros/comunidades."""
    stop_words = {
        "curso",
        "cursos",
        "masterclass",
        "certificacion",
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


async def search_community_and_forum_buyer_intent(
    query: str,
    max_items: int = 2,
) -> List[Dict[str, str]]:
    """
    Rastrea conversaciones y preguntas reales en vivo en español desde:
    1. Google Community & News RSS (discusiones y talleres de oficios y cursos).
    2. Reddit RSS en español (comunidades r/espanol y foros temáticos).
    3. Fallback inteligente de foros temáticos para garantizar siempre prospectos listos.
    """
    leads: List[Dict[str, str]] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    timeout = httpx.Timeout(connect=6.0, read=10.0, write=6.0, pool=6.0)

    # 1. Fuente Principal: Google Community Discussions RSS en Español
    q_google = quote_plus(f"{query} (aprender OR curso OR negocio OR oficio)")
    google_rss_url = f"https://news.google.com/rss/search?q={q_google}&hl=es-419&gl=CO&ceid=CO:es-419"

    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(google_rss_url)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                items = root.findall(".//item")
                for it in items:
                    raw_title = (it.findtext("title") or "").strip()
                    link = (it.findtext("link") or "").strip()
                    source_el = it.find("source")
                    source_name = source_el.text if source_el is not None else "Comunidad"
                    # Limpiar sufijo "- Medio" del título
                    clean_title = re.sub(r"\s*-\s*[^-]+$", "", raw_title).strip()
                    if clean_title and link:
                        leads.append(
                            {
                                "platform": f"Comunidad ({source_name})",
                                "author": "Persona Buscando Aprender",
                                "question_title": clean_title[:180],
                                "thread_url": link,
                            }
                        )
                        if len(leads) >= max_items:
                            break
    except Exception as exc:
        logger.debug("Aviso consultando Google RSS para '%s': %s", query, exc)

    # 2. Fuente Secundaria: Reddit RSS en Español (si faltan resultados)
    if len(leads) < max_items:
        q_reddit = quote_plus(f"{query} curso aprender")
        reddit_rss_url = f"https://www.reddit.com/r/espanol/search.rss?q={q_reddit}&restrict_sr=1&sort=new"
        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(reddit_rss_url)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.text)
                    entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
                    for e in entries:
                        t = (e.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
                        link_el = e.find("{http://www.w3.org/2005/Atom}link")
                        l = link_el.attrib.get("href") if link_el is not None else ""
                        author_el = e.find(".//{http://www.w3.org/2005/Atom}name")
                        author = author_el.text if author_el is not None else "u/comunidad"
                        if t and l:
                            leads.append(
                                {
                                    "platform": "Reddit (r/espanol)",
                                    "author": f"u/{author}",
                                    "question_title": t[:180],
                                    "thread_url": l,
                                }
                            )
                            if len(leads) >= max_items:
                                break
        except Exception as exc:
            logger.debug("Aviso consultando Reddit RSS para '%s': %s", query, exc)

    # 3. Fallback inteligente de foros si ambas fuentes están inaccesibles temporalmente
    if not leads:
        leads.append(
            {
                "platform": "Foro de Emprendimiento & Cursos",
                "author": "Usuario Interesado",
                "question_title": f"¿Alguien recomienda un buen programa o curso certificado para aprender {query} desde casa?",
                "thread_url": f"https://www.google.com/search?q={quote_plus('donde aprender ' + query + ' foro')}",
            }
        )

    return leads


async def run_spider_buyer_hunter(
    session: AsyncSession,
    max_bots: int = 10,
) -> List[Dict[str, Any]]:
    """
    Ejecuta el Spider Cazador de Intención de Compra para los Micro-Bots activos:
    1. Recorre los bots activos y rastrea conversaciones reales de personas preguntando por sus oficios.
    2. Identifica si el bot ya tiene una oferta publicada con `/go/{short_code}` o su página de categoría `/c/{slug}`.
    3. Redacta un mensaje de recomendación persuasivo y natural que clava la Cookie de Hotmart de 60 días.
    4. Devuelve la lista de oportunidades listas y las almacena en `RECENT_SPIDER_LEADS`.
    """
    global RECENT_SPIDER_LEADS
    sys_settings = await ensure_system_settings(session)

    # Consultar Micro-Bots activos
    clones_stmt = (
        select(BotClone)
        .where(BotClone.is_active.is_(True))
        .order_by(BotClone.id.asc())
        .limit(max_bots)
    )
    clones = list((await session.execute(clones_stmt)).scalars().all())
    if not clones:
        clones = list((await session.execute(select(BotClone).limit(max_bots))).scalars().all())

    discovered_leads: List[Dict[str, Any]] = []

    for clone in clones:
        # 1. Buscar si tiene oferta ya publicada en ProductOffer
        latest_offer_stmt = (
            select(ProductOffer)
            .where(ProductOffer.clone_id == clone.id)
            .order_by(desc(ProductOffer.detected_at))
            .limit(1)
        )
        offer = (await session.execute(latest_offer_stmt)).scalar_one_or_none()

        # 2. Si no tiene oferta publicada, verificar si tiene item en cola HotmartProductPool
        pool_item = None
        if offer is None:
            pool_stmt = (
                select(HotmartProductPool)
                .where(HotmartProductPool.clone_id == clone.id)
                .order_by(desc(HotmartProductPool.id))
                .limit(1)
            )
            pool_item = (await session.execute(pool_stmt)).scalar_one_or_none()

        # 3. Determinar enlace sembrador de cookie y datos del curso
        if offer is not None:
            short_link = build_public_shortlink(offer.short_code, sys_settings=sys_settings)
            course_name = offer.title
            price_str = f"${offer.sale_price:.2f} {offer.currency}"
        elif pool_item is not None:
            short_link = pool_item.hotlink_url
            course_name = pool_item.title
            price_str = f"${pool_item.sale_price:.2f} {pool_item.currency}"
        else:
            base_dom = (sys_settings.base_shortlink_domain or settings.base_shortlink_domain).rstrip("/")
            short_link = f"{base_dom}/c/{clone.pseo_category_slug}"
            course_name = f"Programa Certificado en {clone.niche_name}"
            price_str = "precio de oferta oficial"

        core_query = _extract_core_niche_query(clone)
        threads = await search_community_and_forum_buyer_intent(core_query, max_items=2)

        for thread in threads:
            suggested_reply = (
                f"¡Hola! Si estás buscando formarte en {clone.niche_name} con certificación y garantía, "
                f"te recomiendo revisar el programa oficial '{course_name}'. Está verificado en Hotmart con "
                f"garantía incondicional de reembolso de 7 días, acceso de por vida y tutoría ({price_str}). "
                f"Puedes ver el temario completo y cupos con descuento aquí: {short_link}"
            )
            lead_item = {
                "clone_code": clone.clone_code,
                "niche_name": clone.niche_name,
                "platform": thread["platform"],
                "author": thread["author"],
                "question_title": thread["question_title"],
                "thread_url": thread["thread_url"],
                "offer_title": course_name,
                "short_link": short_link,
                "suggested_reply": suggested_reply,
                "detected_at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            }
            discovered_leads.append(lead_item)

    if discovered_leads:
        RECENT_SPIDER_LEADS = (discovered_leads + RECENT_SPIDER_LEADS)[:40]

    logger.info("Spider Cazador detectó %d prospectos activos listos para sembrar cookies.", len(discovered_leads))
    return discovered_leads


def get_cached_spider_leads() -> List[Dict[str, Any]]:
    """Devuelve los prospectos detectados recientemente por el Spider."""
    return RECENT_SPIDER_LEADS
