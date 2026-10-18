from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import html
import json
import logging
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.models import (
    BotClone,
    HotmartProductPool,
    MarketplaceEnum,
    ProductOffer,
    SystemSettings,
)

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class HarvestedCandidate:
    """Estructura normalizada de una oferta real detectada en cualquier marketplace."""

    external_item_id: str
    source_marketplace: MarketplaceEnum
    title: str
    original_price: Decimal
    sale_price: Decimal
    discount_percent: Decimal
    currency: str
    raw_product_url: str
    image_url: str


def normalize_text(text: str) -> str:
    """Normaliza cadenas a minúsculas sin tildes ni caracteres diacríticos para filtrado exacto."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def contains_negative_keyword(title: str, negative_keywords: List[str]) -> bool:
    """Verifica si el título del producto contiene alguna palabra o frase negativa del clon."""
    normalized_title = normalize_text(title)
    for neg in negative_keywords:
        neg_clean = normalize_text(neg.strip())
        if not neg_clean:
            continue
        pattern = rf"(?:^|\b){re.escape(neg_clean)}(?:\b|$)"
        if re.search(pattern, normalized_title):
            return True
    return False


def upgrade_mercadolibre_image_url(thumbnail_url: str) -> str:
    """
    Convierte las miniaturas de baja resolución de MercadoLibre (-I.jpg / -V.jpg)
    en imágenes de alta resolución (-O.jpg) aptas para tarjetas de 1080x1080px.
    """
    if not thumbnail_url:
        return ""
    secure_url = thumbnail_url.replace("http://", "https://")
    upgraded = re.sub(r"-[I|V|S|T|M|N]\.(jpg|webp|png)$", r"-O.\1", secure_url, flags=re.IGNORECASE)
    return upgraded


async def is_item_recently_published(
    session: AsyncSession,
    clone_id: int,
    external_item_id: str,
    cooldown_days: Optional[int] = None,
) -> bool:
    """
    Verifica en PostgreSQL (ProductOffer) si ese external_item_id ya fue publicado
    por ese clon Y enviado a Telegram dentro de la ventana de enfriamiento (por defecto 15 días).
    Si la oferta aún no pudo enviarse a Telegram (published_telegram=False), permite reintentarla.
    """
    days = cooldown_days if cooldown_days is not None else settings.duplicate_cooldown_days
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = (
        select(ProductOffer.id, ProductOffer.detected_at, ProductOffer.published_telegram)
        .where(
            ProductOffer.clone_id == clone_id,
            ProductOffer.external_item_id == external_item_id,
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return False

    published_telegram: bool = bool(row[2])
    if not published_telegram:
        return False

    detected_at: datetime = row[1]
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=timezone.utc)

    return detected_at >= cutoff_date


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.2, min=1.0, max=8.0),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
)
async def extract_live_hotmart_metadata(target_url: str) -> Dict[str, Any]:
    """
    Realiza una petición HTTP real a un HotLink (`https://go.hotmart.com/...` o página de ventas)
    siguiendo redirecciones y extrae en vivo:
    - Título (`og:title` / `<title>` / JSON-LD)
    - Imagen de portada (`og:image` / JSON-LD)
    - Precio si está expuesto en Schema.org/Product
    """
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
        response = await client.get(target_url)
        response.raise_for_status()
        body = response.text

    title = ""
    image_url = ""
    extracted_price: Optional[Decimal] = None
    currency = "USD"

    # 1. Intentar extraer desde OpenGraph meta tags
    og_title_match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        body,
        flags=re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        body,
        flags=re.IGNORECASE,
    )
    if og_title_match:
        title = html.unescape(og_title_match.group(1)).strip()

    og_img_match = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        body,
        flags=re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        body,
        flags=re.IGNORECASE,
    )
    if og_img_match:
        image_url = html.unescape(og_img_match.group(1)).strip()

    # 2. Fallback al tag <title>
    if not title:
        title_tag = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
        if title_tag:
            title = html.unescape(re.sub(r"\s+", " ", title_tag.group(1))).strip()

    # 3. Intentar extraer JSON-LD si existe en la página de ventas
    ld_matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for raw_ld in ld_matches:
        try:
            ld_obj = json.loads(raw_ld.strip())
            if isinstance(ld_obj, dict):
                if not title and ld_obj.get("name"):
                    title = str(ld_obj["name"]).strip()
                if not image_url and ld_obj.get("image"):
                    img_val = ld_obj["image"]
                    image_url = img_val[0] if isinstance(img_val, list) and img_val else str(img_val)
                offers_node = ld_obj.get("offers")
                if isinstance(offers_node, dict) and offers_node.get("price"):
                    extracted_price = Decimal(str(offers_node["price"]))
                    currency = str(offers_node.get("priceCurrency") or "USD").upper()
        except Exception:
            continue

    # 4. Intentar extraer precio exacto desde __NUXT_DATA__ (Checkout pay.hotmart.com)
    nuxt_match = re.search(
        r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if nuxt_match and extracted_price is None:
        try:
            nuxt_data = json.loads(nuxt_match.group(1))
            if isinstance(nuxt_data, list):
                for item in nuxt_data:
                    if isinstance(item, dict) and "value" in item and "currency" in item:
                        v_idx = item["value"]
                        c_idx = item["currency"]
                        if isinstance(v_idx, int) and v_idx < len(nuxt_data) and isinstance(nuxt_data[v_idx], (int, float)):
                            extracted_price = Decimal(str(round(float(nuxt_data[v_idx]), 2)))
                            if isinstance(c_idx, int) and c_idx < len(nuxt_data) and isinstance(nuxt_data[c_idx], str):
                                currency = nuxt_data[c_idx].upper()
                            break
                        elif isinstance(v_idx, (int, float)) and v_idx > 0:
                            extracted_price = Decimal(str(round(float(v_idx), 2)))
                            if isinstance(c_idx, str):
                                currency = c_idx.upper()
                            break
        except Exception:
            pass

    # 5. Intentar extraer precio desde __NEXT_DATA__ (hotmart.com marketplace)
    next_match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if next_match and extracted_price is None:
        try:
            next_data = json.loads(next_match.group(1))
            p_prod = next_data.get("props", {}).get("pageProps", {}).get("profile", {}).get("product", {})
            p_price = p_prod.get("price") or {}
            if isinstance(p_price, dict) and p_price.get("value"):
                extracted_price = Decimal(str(round(float(p_price["value"]), 2)))
                currency = str(p_price.get("currency") or "USD").upper()
        except Exception:
            pass

    # 6. Si no hay precio aún y la página de ventas contiene enlace directo a checkout de Hotmart
    if extracted_price is None:
        checkout_match = re.search(r'https://pay\.hotmart\.com/([A-Za-z0-9_-]+)', body)
        if checkout_match:
            pay_ref = checkout_match.group(1)
            try:
                pay_resp = await client.get(f"https://pay.hotmart.com/{pay_ref}")
                if pay_resp.status_code == 200:
                    pay_nuxt = re.search(
                        r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
                        pay_resp.text,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    if pay_nuxt:
                        p_data = json.loads(pay_nuxt.group(1))
                        if isinstance(p_data, list):
                            for p_it in p_data:
                                if isinstance(p_it, dict) and "value" in p_it and "currency" in p_it:
                                    v_idx = p_it["value"]
                                    c_idx = p_it["currency"]
                                    if isinstance(v_idx, int) and v_idx < len(p_data) and isinstance(p_data[v_idx], (int, float)):
                                        extracted_price = Decimal(str(round(float(p_data[v_idx]), 2)))
                                        if isinstance(c_idx, int) and c_idx < len(p_data) and isinstance(p_data[c_idx], str):
                                            currency = p_data[c_idx].upper()
                                        break
            except Exception:
                pass

    is_portuguese = False
    norm_url = str(response.url).lower()
    if any(ind in norm_url for ind in ("/pt-br/", ".com.br", "/br/")) or currency == "BRL":
        is_portuguese = True
    else:
        comb = f"{title}".lower()
        if any(pt in comb for pt in ("você", "voce", "não", "nao", "são", "também", "aulas", "do zero ao", "brasil", "r$", "reais")):
            is_portuguese = True

    return {
        "title": title,
        "image_url": image_url,
        "sale_price": extracted_price,
        "currency": currency,
        "is_portuguese": is_portuguese,
        "final_url": str(response.url),
    }


class BaseMarketplaceHarvester(ABC):
    """Interfaz base extensible para cosechadores de ofertas multi-tienda."""

    marketplace: MarketplaceEnum

    @abstractmethod
    async def search_keyword_deals(
        self,
        client: httpx.AsyncClient,
        clone: BotClone,
        keyword: str,
        sys_settings: Optional[SystemSettings] = None,
    ) -> List[HarvestedCandidate]:
        """Ejecuta una búsqueda real por palabra clave y devuelve candidatos pre-filtrados."""

    async def harvest_for_clone(
        self,
        session: AsyncSession,
        clone: BotClone,
        sys_settings: Optional[SystemSettings] = None,
        max_candidates: int = 5,
    ) -> List[HarvestedCandidate]:
        """
        Recorre las palabras clave del clon, filtra por condición nuevo, descuento real,
        rango de precios, palabras negativas y ausencia en ProductOffer durante los últimos 15 días.
        Devuelve las mejores ofertas ordenadas por mayor porcentaje de descuento.
        """
        collected: Dict[str, HarvestedCandidate] = {}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }
        if settings.mercadolibre_access_token and self.marketplace == MarketplaceEnum.MERCADOLIBRE:
            headers["Authorization"] = f"Bearer {settings.mercadolibre_access_token}"

        timeout = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            for keyword in clone.search_keywords:
                keyword_clean = keyword.strip()
                if not keyword_clean:
                    continue
                try:
                    candidates = await self.search_keyword_deals(
                        client=client,
                        clone=clone,
                        keyword=keyword_clean,
                        sys_settings=sys_settings,
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] Error cosechando keyword '%s' en %s: %s",
                        clone.clone_code,
                        keyword_clean,
                        self.marketplace.value,
                        exc,
                    )
                    continue

                for candidate in candidates:
                    if candidate.external_item_id in collected:
                        continue
                    if await is_item_recently_published(
                        session=session,
                        clone_id=clone.id,
                        external_item_id=candidate.external_item_id,
                    ):
                        continue
                    collected[candidate.external_item_id] = candidate

        sorted_deals = sorted(
            collected.values(),
            key=lambda item: (item.discount_percent, item.original_price - item.sale_price),
            reverse=True,
        )
        return sorted_deals[:max_candidates]


class HotmartHarvester(BaseMarketplaceHarvester):
    """
    Cosechador especializado en Hotmart para Colombia y Latinoamérica:
    1. Consulta primero los HotLinks activos registrados en `HotmartProductPool` para el clon
       (con soporte de cupón de descuento `offDiscount` y atribución `?src=`).
    2. Si se configuraron credenciales de Hotmart Developers API, consulta además el endpoint
       oficial de productos afiliados.
    """

    marketplace = MarketplaceEnum.HOTMART

    async def harvest_from_pool_for_clone(
        self,
        session: AsyncSession,
        clone: BotClone,
        max_candidates: int = 5,
    ) -> List[HarvestedCandidate]:
        """Obtiene los productos de Hotmart asignados a este clon que no se hayan publicado en 15 días."""
        stmt = (
            select(HotmartProductPool)
            .where(
                HotmartProductPool.clone_id == clone.id,
                HotmartProductPool.is_active.is_(True),
            )
            .order_by(HotmartProductPool.discount_percent.desc(), HotmartProductPool.id.desc())
        )
        pool_items = list((await session.execute(stmt)).scalars().all())
        results: List[HarvestedCandidate] = []

        for item in pool_items:
            # Descartar productos en portugués o de Brasil
            if item.currency == "BRL" or any(pt in (item.title or "").lower() for pt in ("você", "voce", "não", "nao", "brasil", "r$", "reais", "aulas", "do zero ao")):
                continue

            if await is_item_recently_published(
                session=session,
                clone_id=clone.id,
                external_item_id=item.external_item_id,
            ):
                continue

            hotlink = item.hotlink_url.strip()
            if item.coupon_code.strip():
                parsed = urlparse(hotlink)
                q_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
                q_params["offDiscount"] = item.coupon_code.strip()
                hotlink = urlunparse(
                    (
                        parsed.scheme or "https",
                        parsed.netloc,
                        parsed.path,
                        parsed.params,
                        urlencode(q_params, doseq=True),
                        "",
                    )
                )

            results.append(
                HarvestedCandidate(
                    external_item_id=item.external_item_id,
                    source_marketplace=MarketplaceEnum.HOTMART,
                    title=item.title,
                    original_price=item.original_price,
                    sale_price=item.sale_price,
                    discount_percent=item.discount_percent,
                    currency=item.currency or "USD",
                    raw_product_url=hotlink,
                    image_url=item.image_url,
                )
            )
            if len(results) >= max_candidates:
                break

        if not results:
            # Si el clon aún no tiene productos pendientes en su pool, consulta el catálogo filtrando español y 1-clic
            from app.services.hotmart_sync import search_public_hotmart_marketplace

            discovered = await search_public_hotmart_marketplace(clone=clone, max_results=max_candidates)
            for prod in discovered:
                prod_url = str(prod["url"]).strip()
                url_hash = hashlib.sha1(prod_url.encode("utf-8")).hexdigest()[:10].upper()
                external_id = f"HOT_{url_hash}"

                if await is_item_recently_published(
                    session=session,
                    clone_id=clone.id,
                    external_item_id=external_id,
                ):
                    continue

                sale_p = Decimal(str(round(float(prod.get("sale_price") or 49.0), 2)))
                if sale_p <= Decimal("0.00"):
                    sale_p = Decimal("49.00")
                orig_p = sale_p
                disc_p = Decimal("0.00")

                existing_pool = (
                    await session.execute(
                        select(HotmartProductPool)
                        .where(HotmartProductPool.external_item_id == external_id)
                        .limit(1)
                    )
                ).scalar_one_or_none()

                if existing_pool is None:
                    pool_item = HotmartProductPool(
                        clone_id=clone.id,
                        external_item_id=external_id,
                        title=str(prod["title"])[:500],
                        hotlink_url=prod_url,
                        image_url=str(prod["image_url"]),
                        original_price=orig_p,
                        sale_price=sale_p,
                        discount_percent=disc_p,
                        currency="USD",
                        coupon_code="",
                        is_active=True,
                    )
                    session.add(pool_item)
                    await session.commit()

                results.append(
                    HarvestedCandidate(
                        external_item_id=external_id,
                        source_marketplace=MarketplaceEnum.HOTMART,
                        title=str(prod["title"])[:500],
                        original_price=orig_p,
                        sale_price=sale_p,
                        discount_percent=disc_p,
                        currency="USD",
                        raw_product_url=prod_url,
                        image_url=str(prod["image_url"]),
                    )
                )
                if len(results) >= max_candidates:
                    break

        return results

    async def search_keyword_deals(
        self,
        client: httpx.AsyncClient,
        clone: BotClone,
        keyword: str,
        sys_settings: Optional[SystemSettings] = None,
    ) -> List[HarvestedCandidate]:
        """Implementación de la interfaz base para búsquedas por palabra clave en Hotmart."""
        return []


class MercadoLibreHarvester(BaseMarketplaceHarvester):
    """Cosechador oficial sobre la API pública de búsqueda de MercadoLibre."""

    marketplace = MarketplaceEnum.MERCADOLIBRE

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.2, min=1.5, max=10.0),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    )
    async def _fetch_search_json(
        self,
        client: httpx.AsyncClient,
        site_id: str,
        keyword: str,
        limit: int = 30,
    ) -> Dict[str, Any]:
        url = f"https://api.mercadolibre.com/sites/{site_id}/search"
        params = {
            "q": keyword,
            "condition": "new",
            "limit": str(limit),
        }
        response = await client.get(url, params=params)
        if response.status_code >= 500 or response.status_code == 429:
            response.raise_for_status()
        if response.status_code != 200:
            logger.warning(
                "MercadoLibre API devolvió HTTP %d para site=%s q='%s'",
                response.status_code,
                site_id,
                keyword,
            )
            return {"results": []}
        return response.json()

    async def search_keyword_deals(
        self,
        client: httpx.AsyncClient,
        clone: BotClone,
        keyword: str,
        sys_settings: Optional[SystemSettings] = None,
    ) -> List[HarvestedCandidate]:
        site_id = (
            clone.country_site
            or (sys_settings.mercadolibre_country_site if sys_settings else None)
            or settings.mercadolibre_country_site
        ).upper()

        payload = await self._fetch_search_json(
            client=client,
            site_id=site_id,
            keyword=keyword,
            limit=30,
        )
        results = payload.get("results") or []
        valid_candidates: List[HarvestedCandidate] = []

        for raw in results:
            if not isinstance(raw, dict):
                continue

            condition = str(raw.get("condition") or "").lower()
            if condition != "new":
                continue

            raw_price = raw.get("price")
            raw_original_price = raw.get("original_price")
            if raw_price is None or raw_original_price is None:
                continue

            try:
                price = Decimal(str(raw_price))
                original_price = Decimal(str(raw_original_price))
            except Exception:
                continue

            if original_price <= price or original_price <= Decimal("0"):
                continue

            discount_float = round(
                float(((original_price - price) / original_price) * Decimal("100")),
                1,
            )
            if discount_float < float(clone.min_discount_percent):
                continue

            if price < clone.min_price or price > clone.max_price:
                continue

            title = str(raw.get("title") or "").strip()
            if not title or contains_negative_keyword(title, clone.negative_keywords):
                continue

            item_id = str(raw.get("id") or "").strip()
            permalink = str(raw.get("permalink") or "").strip()
            thumbnail = str(raw.get("thumbnail") or "").strip()
            currency_id = str(raw.get("currency_id") or "COP").strip()

            if not item_id or not permalink or not thumbnail:
                continue

            valid_candidates.append(
                HarvestedCandidate(
                    external_item_id=item_id,
                    source_marketplace=MarketplaceEnum.MERCADOLIBRE,
                    title=title,
                    original_price=original_price.quantize(Decimal("0.01")),
                    sale_price=price.quantize(Decimal("0.01")),
                    discount_percent=Decimal(str(discount_float)),
                    currency=currency_id,
                    raw_product_url=permalink,
                    image_url=upgrade_mercadolibre_image_url(thumbnail),
                )
            )

        return valid_candidates


class AliExpressHarvester(BaseMarketplaceHarvester):
    """
    Cosechador para AliExpress Open Platform utilizando firma oficial HMAC-MD5
    sobre el método `aliexpress.affiliate.product.query`.
    """

    marketplace = MarketplaceEnum.ALIEXPRESS
    API_ENDPOINT = "https://api-sg.aliexpress.com/sync"

    @staticmethod
    def _sign_aliexpress_request(params: Dict[str, str], app_secret: str) -> str:
        """Calcula la firma HMAC-MD5 en mayúsculas exigida por AliExpress TOP/IOP."""
        sorted_keys = sorted(params.keys())
        concatenated = "".join(f"{k}{params[k]}" for k in sorted_keys if k != "sign" and params[k] is not None)
        digest = hmac.new(
            app_secret.encode("utf-8"),
            concatenated.encode("utf-8"),
            hashlib.md5,
        ).hexdigest()
        return digest.upper()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.5, min=2.0, max=12.0),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    )
    async def search_keyword_deals(
        self,
        client: httpx.AsyncClient,
        clone: BotClone,
        keyword: str,
        sys_settings: Optional[SystemSettings] = None,
    ) -> List[HarvestedCandidate]:
        app_key = settings.aliexpress_app_key
        app_secret = settings.aliexpress_app_secret
        if not app_key or not app_secret:
            return []

        tracking_id = (
            sys_settings.aliexpress_tracking_id
            if sys_settings and sys_settings.aliexpress_tracking_id
            else settings.aliexpress_tracking_id
        )

        params: Dict[str, str] = {
            "app_key": app_key,
            "method": "aliexpress.affiliate.product.query",
            "sign_method": "hmac",
            "timestamp": str(int(time.time() * 1000)),
            "format": "json",
            "v": "2.0",
            "keywords": keyword,
            "page_size": "30",
            "sort": "DISCOUNT_ASC",
            "target_currency": "USD",
            "target_language": "ES",
            "tracking_id": tracking_id,
        }
        params["sign"] = self._sign_aliexpress_request(params, app_secret)

        response = await client.post(self.API_ENDPOINT, data=params)
        response.raise_for_status()
        data = response.json()

        resp_root = (
            data.get("aliexpress_affiliate_product_query_response", {})
            .get("resp_result", {})
            .get("result", {})
        )
        products = resp_root.get("products", {}).get("product", [])
        if not isinstance(products, list):
            return []

        candidates: List[HarvestedCandidate] = []
        for prod in products:
            if not isinstance(prod, dict):
                continue

            title = str(prod.get("product_title") or "").strip()
            if not title or contains_negative_keyword(title, clone.negative_keywords):
                continue

            raw_orig = prod.get("target_original_price") or prod.get("original_price")
            raw_sale = prod.get("target_sale_price") or prod.get("sale_price")
            if raw_orig is None or raw_sale is None:
                continue

            try:
                orig_price = Decimal(str(raw_orig))
                sale_price = Decimal(str(raw_sale))
            except Exception:
                continue

            if orig_price <= sale_price or orig_price <= Decimal("0"):
                continue

            discount_float = round(
                float(((orig_price - sale_price) / orig_price) * Decimal("100")),
                1,
            )
            if discount_float < float(clone.min_discount_percent):
                continue

            prod_id = str(prod.get("product_id") or "").strip()
            detail_url = str(prod.get("promotion_link") or prod.get("product_detail_url") or "").strip()
            image_url = str(prod.get("product_main_image_url") or "").strip()
            currency = str(prod.get("target_sale_price_currency") or "USD").strip()

            if not prod_id or not detail_url or not image_url:
                continue

            candidates.append(
                HarvestedCandidate(
                    external_item_id=f"ALI_{prod_id}",
                    source_marketplace=MarketplaceEnum.ALIEXPRESS,
                    title=title,
                    original_price=orig_price.quantize(Decimal("0.01")),
                    sale_price=sale_price.quantize(Decimal("0.01")),
                    discount_percent=Decimal(str(discount_float)),
                    currency=currency,
                    raw_product_url=detail_url,
                    image_url=image_url,
                )
            )

        return candidates


class AmazonHarvester(BaseMarketplaceHarvester):
    """
    Cosechador para Amazon Product Advertising API 5.0 (PA-API 5.0)
    con firma criptográfica AWS Signature Version 4 (HMAC-SHA256).
    """

    marketplace = MarketplaceEnum.AMAZON

    @staticmethod
    def _sign_aws_v4_headers(
        access_key: str,
        secret_key: str,
        host: str,
        region: str,
        payload_str: str,
    ) -> Dict[str, str]:
        """Genera las cabeceras de autorización AWS4-HMAC-SHA256 requeridas por Amazon PA-API 5.0."""
        service = "ProductAdvertisingAPI"
        target = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems"
        uri = "/paapi5/searchitems"

        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        canonical_headers = (
            f"content-encoding:amz-1.0\n"
            f"content-type:application/json; charset=utf-8\n"
            f"host:{host}\n"
            f"x-amz-date:{amz_date}\n"
            f"x-amz-target:{target}\n"
        )
        signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"
        canonical_request = f"POST\n{uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
        string_to_sign = (
            f"{algorithm}\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        def _hmac_sha256(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), datestamp)
        k_region = _hmac_sha256(k_date, region)
        k_service = _hmac_sha256(k_region, service)
        k_signing = _hmac_sha256(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization_header = (
            f"{algorithm} Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        return {
            "content-encoding": "amz-1.0",
            "content-type": "application/json; charset=utf-8",
            "host": host,
            "x-amz-date": amz_date,
            "x-amz-target": target,
            "Authorization": authorization_header,
        }

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.5, min=2.0, max=12.0),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    )
    async def search_keyword_deals(
        self,
        client: httpx.AsyncClient,
        clone: BotClone,
        keyword: str,
        sys_settings: Optional[SystemSettings] = None,
    ) -> List[HarvestedCandidate]:
        access_key = settings.amazon_access_key
        secret_key = settings.amazon_secret_key
        partner_tag = (
            sys_settings.amazon_partner_tag
            if sys_settings and sys_settings.amazon_partner_tag
            else settings.amazon_partner_tag
        )
        if not access_key or not secret_key or not partner_tag:
            return []

        payload = {
            "Keywords": keyword,
            "ItemCount": 10,
            "Condition": "New",
            "PartnerTag": partner_tag,
            "PartnerType": "Associates",
            "Resources": [
                "Images.Primary.Large",
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "Offers.Listings.Condition",
            ],
        }
        payload_str = json.dumps(payload, separators=(",", ":"))
        aws_headers = self._sign_aws_v4_headers(
            access_key=access_key,
            secret_key=secret_key,
            host=settings.amazon_host,
            region=settings.amazon_region,
            payload_str=payload_str,
        )

        endpoint = f"https://{settings.amazon_host}/paapi5/searchitems"
        response = await client.post(endpoint, content=payload_str.encode("utf-8"), headers=aws_headers)
        response.raise_for_status()
        body = response.json()

        items = body.get("SearchResult", {}).get("Items", [])
        if not isinstance(items, list):
            return []

        candidates: List[HarvestedCandidate] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            asin = str(item.get("ASIN") or "").strip()
            detail_url = str(item.get("DetailPageURL") or "").strip()
            title = str(item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue") or "").strip()
            image_url = str(item.get("Images", {}).get("Primary", {}).get("Large", {}).get("URL") or "").strip()

            if not asin or not detail_url or not title or not image_url:
                continue
            if contains_negative_keyword(title, clone.negative_keywords):
                continue

            listings = item.get("Offers", {}).get("Listings", [])
            if not listings or not isinstance(listings, list):
                continue

            first_listing = listings[0]
            price_info = first_listing.get("Price", {})
            saving_basis = first_listing.get("SavingBasis", {})

            raw_sale = price_info.get("Amount")
            raw_orig = saving_basis.get("Amount")
            currency = str(price_info.get("Currency") or "USD")

            if raw_sale is None or raw_orig is None:
                continue

            try:
                sale_price = Decimal(str(raw_sale))
                orig_price = Decimal(str(raw_orig))
            except Exception:
                continue

            if orig_price <= sale_price or orig_price <= Decimal("0"):
                continue

            discount_float = round(
                float(((orig_price - sale_price) / orig_price) * Decimal("100")),
                1,
            )
            if discount_float < float(clone.min_discount_percent):
                continue

            candidates.append(
                HarvestedCandidate(
                    external_item_id=asin,
                    source_marketplace=MarketplaceEnum.AMAZON,
                    title=title,
                    original_price=orig_price.quantize(Decimal("0.01")),
                    sale_price=sale_price.quantize(Decimal("0.01")),
                    discount_percent=Decimal(str(discount_float)),
                    currency=currency,
                    raw_product_url=detail_url,
                    image_url=image_url,
                )
            )

        return candidates


class MultiStoreHarvesterOrchestrator:
    """
    Orquestador que ejecuta la cosecha priorizando Hotmart (`HotmartHarvester`) en modo `HYBRID` o `HOTMART`,
    y complementando con MercadoLibre, AliExpress y Amazon según configuración.
    """

    def __init__(self) -> None:
        self.hotmart_harvester = HotmartHarvester()
        self.ml_harvester = MercadoLibreHarvester()
        self.ali_harvester = AliExpressHarvester()
        self.amz_harvester = AmazonHarvester()

    async def harvest_best_deals_for_clone(
        self,
        session: AsyncSession,
        clone: BotClone,
        sys_settings: Optional[SystemSettings] = None,
        limit: int = 3,
    ) -> List[HarvestedCandidate]:
        """Devuelve las mejores ofertas multi-tienda ordenadas por descuento real verificado."""
        all_candidates: List[HarvestedCandidate] = []
        mode = (
            sys_settings.preferred_marketplace_mode
            if sys_settings and sys_settings.preferred_marketplace_mode
            else settings.preferred_marketplace_mode
        ).upper()

        # 1. Cosechar primero productos de Hotmart asignados a este clon
        if mode in {"HYBRID", "HOTMART"}:
            hotmart_deals = await self.hotmart_harvester.harvest_from_pool_for_clone(
                session=session,
                clone=clone,
                max_candidates=limit,
            )
            all_candidates.extend(hotmart_deals)
            if mode == "HOTMART":
                return all_candidates[:limit]

        # 2. Cosechar en AliExpress si está activo
        if settings.aliexpress_app_key and settings.aliexpress_app_secret:
            ali_deals = await self.ali_harvester.harvest_for_clone(
                session=session,
                clone=clone,
                sys_settings=sys_settings,
                max_candidates=2,
            )
            all_candidates.extend(ali_deals)

        # 3. Cosechar en MercadoLibre si el modo es HYBRID o MERCADOLIBRE
        if mode in {"HYBRID", "MERCADOLIBRE"}:
            ml_deals = await self.ml_harvester.harvest_for_clone(
                session=session,
                clone=clone,
                sys_settings=sys_settings,
                max_candidates=limit,
            )
            all_candidates.extend(ml_deals)

        # 4. Cosechar en Amazon si está activo
        if settings.amazon_access_key and settings.amazon_secret_key:
            amz_deals = await self.amz_harvester.harvest_for_clone(
                session=session,
                clone=clone,
                sys_settings=sys_settings,
                max_candidates=2,
            )
            all_candidates.extend(amz_deals)

        all_candidates.sort(
            key=lambda x: (
                1 if x.source_marketplace == MarketplaceEnum.HOTMART else 0,
                x.discount_percent,
                x.original_price - x.sale_price,
            ),
            reverse=True,
        )
        return all_candidates[:limit]
