import asyncio
from decimal import Decimal
import hashlib
import html
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from google import genai
from google.genai import types
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.models import BotClone, HotmartProductPool, SystemSettings
from app.services.harvester import extract_live_hotmart_metadata
from app.services.seeder import ensure_system_settings

logger = logging.getLogger(__name__)
settings = get_settings()

HOTMART_OAUTH_URL = "https://api-sec-vlc.hotmart.com/security/oauth/token"
HOTMART_MARKETPLACE_SEARCH_URL = "https://hotmart.com/es/marketplace/productos"


class SingleHotmartAssignment(BaseModel):
    """Asignación estructurada decidida por Gemini 2.5 Flash entre un producto de Hotmart y uno de los 50 Micro-Bots."""

    hotlink_url: str = Field(..., description="URL original del HotLink o producto")
    assigned_clone_code: str = Field(..., description="Código exacto del micro-bot asignado (ej. bot_01 a bot_50)")
    optimized_title: str = Field(..., description="Título comercial limpio y atractivo en español")
    estimated_original_price_usd: float = Field(..., ge=15.0, description="Precio normal estimado en USD antes del descuento")
    estimated_sale_price_usd: float = Field(..., ge=7.0, description="Precio en oferta estimado en USD")


class BatchHotmartAssignmentResponse(BaseModel):
    """Lista estructurada de asignaciones de productos de Hotmart a Micro-Bots."""

    assignments: List[SingleHotmartAssignment]


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2.0, max=10.0),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
)
async def get_hotmart_oauth_access_token(sys_settings: SystemSettings) -> Optional[str]:
    """
    Obtiene un Access Token OAuth2 real desde el servidor de seguridad de Hotmart
    (`https://api-sec-vlc.hotmart.com/security/oauth/token`) usando `client_id`,
    `client_secret` y `basic_auth` configurados en `SystemSettings`.
    """
    client_id = (sys_settings.hotmart_client_id or settings.hotmart_client_id).strip()
    client_secret = (sys_settings.hotmart_client_secret or settings.hotmart_client_secret).strip()
    basic_auth = (sys_settings.hotmart_basic_auth or settings.hotmart_basic_auth).strip()

    if not client_id or not client_secret or not basic_auth:
        return None

    if not basic_auth.lower().startswith("basic "):
        basic_header = f"Basic {basic_auth}"
    else:
        basic_header = basic_auth

    params = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    headers = {
        "Authorization": basic_header,
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(HOTMART_OAUTH_URL, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("access_token")


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.2, min=1.5, max=10.0),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
)
async def search_public_hotmart_marketplace(
    keyword: str,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """
    Realiza una búsqueda HTTP real en el catálogo público de Hotmart Marketplace en español
    (`https://hotmart.com/es/marketplace/productos?q={keyword}`) extrayendo los productos
    desde el estado hidratado `__NEXT_DATA__`, JSON-LD o tarjetas HTML del mercado.
    """
    encoded_q = quote_plus(keyword.strip())
    url = f"{HOTMART_MARKETPLACE_SEARCH_URL}?q={encoded_q}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    }

    timeout = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        if response.status_code >= 500 or response.status_code == 429:
            response.raise_for_status()
        if response.status_code != 200:
            return []
        html_body = response.text

    discovered: List[Dict[str, Any]] = []

    # 1. Extraer desde __NEXT_DATA__ si está presente en el SSR de Hotmart Marketplace
    next_data_match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html_body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if next_data_match:
        try:
            next_json = json.loads(next_data_match.group(1))
            raw_str = json.dumps(next_json)
            # Buscar nodos de producto dentro del árbol JSON
            page_props = next_json.get("props", {}).get("pageProps", {})
            products_list = (
                page_props.get("products")
                or page_props.get("initialState", {}).get("search", {}).get("products")
                or []
            )
            if isinstance(products_list, list):
                for prod in products_list:
                    if not isinstance(prod, dict):
                        continue
                    title = str(prod.get("name") or prod.get("title") or "").strip()
                    slug = str(prod.get("slug") or prod.get("ucode") or "").strip()
                    image_url = str(
                        prod.get("image")
                        or prod.get("picture")
                        or prod.get("coverPhoto")
                        or "https://static-media.hotmart.com/img/hotmart-logo-share.png"
                    ).strip()
                    price_val = prod.get("price", {})
                    if isinstance(price_val, dict):
                        raw_price = price_val.get("value") or 49.0
                    else:
                        raw_price = price_val or 49.0

                    prod_url = (
                        f"https://hotmart.com/es/marketplace/productos/{slug}"
                        if slug and not slug.startswith("http")
                        else slug
                    )
                    if title and prod_url:
                        discovered.append(
                            {
                                "title": title,
                                "url": prod_url,
                                "image_url": image_url,
                                "sale_price": float(raw_price),
                            }
                        )
                    if len(discovered) >= max_results:
                        return discovered
            del raw_str
        except Exception as exc:
            logger.debug("Error parseando __NEXT_DATA__ de Hotmart Marketplace: %s", exc)

    # 2. Extraer enlaces directos a páginas de producto `/es/marketplace/productos/<slug>`
    link_matches = re.findall(
        r'href=["\']((?:https://hotmart\.com)?/es/marketplace/productos/[a-zA-Z0-9_-]+/[A-Z0-9]+)["\']',
        html_body,
        flags=re.IGNORECASE,
    )
    seen_urls = set()
    for href in link_matches:
        full_url = href if href.startswith("http") else f"https://hotmart.com{href}"
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        try:
            meta = await extract_live_hotmart_metadata(full_url)
            if meta.get("title"):
                sale_p = float(meta["sale_price"]) if meta.get("sale_price") else 39.00
                discovered.append(
                    {
                        "title": str(meta["title"]),
                        "url": full_url,
                        "image_url": str(
                            meta.get("image_url")
                            or "https://static-media.hotmart.com/img/hotmart-logo-share.png"
                        ),
                        "sale_price": sale_p,
                    }
                )
        except Exception:
            continue
        if len(discovered) >= max_results:
            break

    return discovered


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2.0, max=12.0),
    retry=retry_if_exception_type(Exception),
)
def _classify_hotlinks_with_gemini_sync(
    api_key: str,
    clones_catalog: List[Dict[str, str]],
    extracted_items: List[Dict[str, Any]],
) -> types.GenerateContentResponse:
    """Usa Gemini 2.5 Flash con salida estructurada para asignar automáticamente cada producto al Micro-Bot ideal."""
    client = genai.Client(api_key=api_key)
    prompt = (
        "Actúa como el Clasificador Maestro de SwarmEngine 50.\n"
        "Tienes la siguiente flota de 50 Micro-Bots especializados:\n"
        f"{json.dumps(clones_catalog, ensure_ascii=False)}\n\n"
        "Y tienes esta lista de productos/cursos extraídos de Hotmart:\n"
        f"{json.dumps(extracted_items, ensure_ascii=False)}\n\n"
        "Para cada producto:\n"
        "1. Escoge el `assigned_clone_code` (ej. `bot_01` a `bot_50`) cuyo nicho tenga la mayor afinidad temática y de audiencia.\n"
        "2. Redacta un `optimized_title` claro, persuasivo y sin símbolos basura.\n"
        "3. Define `estimated_original_price_usd` y `estimated_sale_price_usd` (donde original sea al menos el doble del precio de oferta para reflejar el cupón del 50% OFF)."
    )
    return client.models.generate_content(
        model=settings.gemini_model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BatchHotmartAssignmentResponse,
            temperature=0.2,
        ),
    )


async def bulk_auto_assign_hotlinks_with_ai(
    session: AsyncSession,
    raw_urls_text: str,
    default_coupon: str = "",
) -> List[HotmartProductPool]:
    """
    Recibe una lista de enlaces de Hotmart (`go.hotmart.com/...` o `hotmart.com/...`),
    visita cada enlace en tiempo real con `httpx` para extraer su título, foto y precio,
    y utiliza Gemini 2.5 Flash para asignarlos automáticamente al Micro-Bot (`BotClone`) correspondiente.
    """
    sys_settings = await ensure_system_settings(session)
    api_key = (sys_settings.gemini_api_key or settings.gemini_api_key).strip()

    urls = [
        line.strip()
        for line in re.split(r"[\r\n,\s]+", raw_urls_text.strip())
        if line.strip().startswith("http")
    ]
    if not urls:
        return []

    clones_stmt = select(BotClone).order_by(BotClone.id.asc())
    all_clones = list((await session.execute(clones_stmt)).scalars().all())
    clone_by_code: Dict[str, BotClone] = {c.clone_code: c for c in all_clones}
    clones_catalog = [
        {
            "clone_code": c.clone_code,
            "niche_name": c.niche_name,
            "keywords": ", ".join(c.search_keywords[:4]),
        }
        for c in all_clones
    ]

    extracted_items: List[Dict[str, Any]] = []
    for url in urls[:25]:
        try:
            meta = await extract_live_hotmart_metadata(url)
            extracted_items.append(
                {
                    "hotlink_url": url,
                    "raw_title": meta.get("title") or url,
                    "image_url": meta.get("image_url") or "https://static-media.hotmart.com/img/hotmart-logo-share.png",
                    "detected_price": float(meta["sale_price"]) if meta.get("sale_price") else 39.0,
                }
            )
        except Exception as exc:
            logger.warning("No se pudo inspeccionar URL %s: %s", url, exc)
            extracted_items.append(
                {
                    "hotlink_url": url,
                    "raw_title": f"Programa Especializado ({url.split('/')[-1]})",
                    "image_url": "https://static-media.hotmart.com/img/hotmart-logo-share.png",
                    "detected_price": 39.0,
                }
            )

    image_by_url = {item["hotlink_url"]: item["image_url"] for item in extracted_items}
    assignments_map: Dict[str, SingleHotmartAssignment] = {}

    if api_key and clones_catalog:
        try:
            response = await asyncio.to_thread(
                _classify_hotlinks_with_gemini_sync,
                api_key,
                clones_catalog,
                extracted_items,
            )
            parsed = BatchHotmartAssignmentResponse.model_validate_json(response.text)
            for a in parsed.assignments:
                assignments_map[a.hotlink_url] = a
        except Exception as exc:
            logger.warning("Fallo clasificación IA Gemini, aplicando emparejamiento por palabras clave: %s", exc)

    saved_entries: List[HotmartProductPool] = []

    for idx, item in enumerate(extracted_items):
        url = item["hotlink_url"]
        assignment = assignments_map.get(url)

        if assignment and assignment.assigned_clone_code in clone_by_code:
            target_clone = clone_by_code[assignment.assigned_clone_code]
            final_title = assignment.optimized_title
            orig_p = Decimal(str(round(assignment.estimated_original_price_usd, 2)))
            sale_p = Decimal(str(round(assignment.estimated_sale_price_usd, 2)))
        else:
            target_clone = all_clones[idx % len(all_clones)]
            final_title = str(item["raw_title"])[:450]
            sale_p = Decimal(str(round(float(item["detected_price"]), 2)))
            orig_p = (sale_p * Decimal("2.0")).quantize(Decimal("0.01"))

        if orig_p <= sale_p:
            orig_p = (sale_p * Decimal("2.0")).quantize(Decimal("0.01"))

        disc_float = round(float(((orig_p - sale_p) / orig_p) * Decimal("100")), 1)
        disc_dec = Decimal(str(disc_float))
        url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10].upper()
        external_id = f"HOT_{url_hash}"

        existing = (
            await session.execute(
                select(HotmartProductPool).where(HotmartProductPool.external_item_id == external_id).limit(1)
            )
        ).scalar_one_or_none()

        if existing is None:
            entry = HotmartProductPool(
                clone_id=target_clone.id,
                external_item_id=external_id,
                title=final_title[:500],
                hotlink_url=url,
                image_url=image_by_url.get(url, "https://static-media.hotmart.com/img/hotmart-logo-share.png"),
                original_price=orig_p,
                sale_price=sale_p,
                discount_percent=disc_dec,
                currency="USD",
                coupon_code=default_coupon.strip(),
                is_active=True,
            )
            session.add(entry)
            saved_entries.append(entry)
        else:
            existing.clone_id = target_clone.id
            existing.title = final_title[:500]
            existing.hotlink_url = url
            existing.original_price = orig_p
            existing.sale_price = sale_p
            existing.discount_percent = disc_dec
            existing.is_active = True
            saved_entries.append(existing)

    await session.commit()
    logger.info("Auto-asignados %d productos de Hotmart a sus respectivos Micro-Bots.", len(saved_entries))
    return saved_entries


async def auto_discover_hotmart_catalog_for_clones(
    session: AsyncSession,
    max_clones_to_scan: int = 10,
) -> int:
    """
    Recorre los Micro-Bots activos que aún no tienen productos en `HotmartProductPool`,
    entra automáticamente al mercado público de Hotmart (`https://hotmart.com/es/marketplace/productos`),
    escoge productos reales afines a las palabras clave de cada clon y los asigna automáticamente.
    """
    stmt = select(BotClone).where(BotClone.is_active.is_(True)).order_by(BotClone.id.asc())
    clones = list((await session.execute(stmt)).scalars().all())

    total_assigned = 0
    scanned_clones = 0

    for clone in clones:
        if scanned_clones >= max_clones_to_scan:
            break

        count_stmt = (
            select(HotmartProductPool.id)
            .where(HotmartProductPool.clone_id == clone.id)
            .limit(1)
        )
        has_items = (await session.execute(count_stmt)).scalar_one_or_none() is not None
        if has_items:
            continue

        scanned_clones += 1
        search_term = clone.niche_name.split()[0] if clone.niche_name else "curso"
        if clone.search_keywords:
            search_term = clone.search_keywords[0].split()[0]

        discovered = await search_public_hotmart_marketplace(search_term, max_results=2)
        for prod in discovered:
            prod_url = prod["url"]
            url_hash = hashlib.sha1(prod_url.encode("utf-8")).hexdigest()[:10].upper()
            external_id = f"HOT_{url_hash}"

            exists = (
                await session.execute(
                    select(HotmartProductPool.id)
                    .where(HotmartProductPool.external_item_id == external_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue

            sale_p = Decimal(str(round(float(prod.get("sale_price") or 39.0), 2)))
            if sale_p <= Decimal("0.00"):
                sale_p = Decimal("39.00")
            orig_p = (sale_p * Decimal("2.0")).quantize(Decimal("0.01"))

            entry = HotmartProductPool(
                clone_id=clone.id,
                external_item_id=external_id,
                title=str(prod["title"])[:500],
                hotlink_url=prod_url,
                image_url=str(prod["image_url"]),
                original_price=orig_p,
                sale_price=sale_p,
                discount_percent=Decimal("50.00"),
                currency="USD",
                coupon_code="",
                is_active=True,
            )
            session.add(entry)
            total_assigned += 1

    if total_assigned > 0:
        await session.commit()

    logger.info("Auto-descubrimiento Hotmart asignó %d productos nuevos a %d clones.", total_assigned, scanned_clones)
    return total_assigned
