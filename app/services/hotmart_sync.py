import asyncio
import base64
from decimal import Decimal
import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import unicodedata
from urllib.parse import quote_plus
import uuid

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
HOTMART_MARKET_SEARCH_V2_URL = "https://api-affiliation-market.hotmart.com/v2/market/search"
HOTMART_MARKET_SEARCH_V1_URL = "https://api-affiliation-market.hotmart.com/v1/market/search"
HOTMART_AFFILIATION_REQUEST_URL = "https://api-affiliation-market.hotmart.com/v1/market/affiliation-request"
HOTMART_AFFILIATED_PRODUCTS_URL = "https://api-affiliation.hotmart.com/v2/affiliations/products"
HOTMART_HOTLINK_DETAIL_URL_TEMPLATE = "https://api-affiliation.hotmart.com/v2/hotlink/{product_id}?page=1&rows=20"
HOTMART_MARKETPLACE_SEARCH_URL = "https://hotmart.com/es/marketplace/productos"

STOP_WORDS_NICHE = {
    "curso",
    "cursos",
    "masterclass",
    "certificacion",
    "certificado",
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


def _normalize_text(text: str) -> str:
    """Convierte texto a minúsculas sin tildes para comparación temática exacta."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def extract_clean_niche_tokens(clone: BotClone) -> List[str]:
    """Extrae las palabras raíz temáticas reales del nicho de un Micro-Bot (ignorando palabras genéricas como 'curso')."""
    combined = f"{clone.niche_name} {' '.join(clone.search_keywords or [])}"
    normalized = re.sub(r"[^a-z0-9\s]", " ", _normalize_text(combined))
    tokens: List[str] = []
    for word in normalized.split():
        if len(word) >= 4 and word not in STOP_WORDS_NICHE and word not in tokens:
            tokens.append(word)
    return tokens


def build_specific_hotmart_query(clone: BotClone) -> str:
    """Construye la consulta específica del nicho para buscar en el Mercado de Afiliación de Hotmart."""
    if clone.search_keywords:
        first_kw = _normalize_text(clone.search_keywords[0])
        words = [w for w in first_kw.split() if w not in STOP_WORDS_NICHE and len(w) >= 3]
        if words:
            return " ".join(words[:3])
    tokens = extract_clean_niche_tokens(clone)
    return " ".join(tokens[:2]) if tokens else clone.niche_name


def calculate_topic_coherence_score(product_title: str, clone: BotClone) -> int:
    """
    Calcula cuántos términos temáticos clave del Micro-Bot aparecen en el título o etiquetas del producto.
    Garantiza coherencia 100% entre el producto de Hotmart y el nicho del bot.
    """
    norm_title = _normalize_text(product_title)
    niche_tokens = extract_clean_niche_tokens(clone)
    score = 0
    for token in niche_tokens:
        root = token[:5] if len(token) >= 6 else token
        if root in norm_title:
            score += 3 if token in _normalize_text(clone.niche_name) else 2
    return score


def find_best_matching_clone_for_title(
    product_title: str,
    all_clones: List[BotClone],
) -> BotClone:
    """Encuentra el Micro-Bot cuyo nicho tiene la mayor coherencia temática con el título del producto."""
    scored: List[Tuple[int, int, BotClone]] = []
    for idx, clone in enumerate(all_clones):
        score = calculate_topic_coherence_score(product_title, clone)
        scored.append((score, -idx, clone))
    scored.sort(reverse=True)
    return scored[0][2]


def _extract_jwt_from_raw_input(raw_text: str) -> Optional[str]:
    """
    Detecta y extrae un JWT (`eyJ...`) desde:
    - Un Bearer Token directo (`Bearer eyJ...` o `eyJ...`),
    - El JSON de sesión `cas-js:user` de `app-vlc.hotmart.com/market` (`{"access_token":"eyJ..."}`),
    - O una cadena de cookies (`hmVlcIntegration=eyJ...`).
    """
    cleaned = raw_text.strip()
    if not cleaned:
        return None

    # 1. Intentar parsear como JSON (ej. localStorage.getItem('cas-js:user'))
    if cleaned.startswith("{") and "access_token" in cleaned:
        try:
            obj = json.loads(cleaned)
            token_val = str(obj.get("access_token") or "").strip()
            if token_val:
                return token_val
        except Exception:
            pass

    # 2. Buscar patrón JWT estándar (eyJ...)
    jwt_match = re.search(r"(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})", cleaned)
    if jwt_match:
        return jwt_match.group(1)

    return None


async def resolve_hotmart_bearer_token(sys_settings: SystemSettings) -> Optional[str]:
    """
    Resuelve un `Bearer <access_token>` válido para operar sobre el Mercado de Afiliación de Hotmart
    (`api-affiliation-market.hotmart.com` y `api-affiliation.hotmart.com`):
    1. Si el usuario pegó un Token de Sesión / Bearer JWT (`eyJ...`) o el JSON `cas-js:user` de su navegador
       en `hotmart_basic_auth` (o `hotmart_client_secret`), lo usa directamente.
    2. Si configuró Credenciales de Desarrollador (`client_id`, `client_secret` y/o `Basic ...`),
       intercambia las credenciales en `https://api-sec-vlc.hotmart.com/security/oauth/token`.
    3. Si ingresó Email y Contraseña en `client_id` / `client_secret`, intenta autenticación OAuth2 directa.
    """
    client_id = (sys_settings.hotmart_client_id or settings.hotmart_client_id).strip()
    client_secret = (sys_settings.hotmart_client_secret or settings.hotmart_client_secret).strip()
    basic_or_bearer = (sys_settings.hotmart_basic_auth or settings.hotmart_basic_auth).strip()

    # 1. Verificar si pegó directamente un JWT o JSON de sesión de navegador
    for candidate_field in (basic_or_bearer, client_secret):
        direct_jwt = _extract_jwt_from_raw_input(candidate_field)
        if direct_jwt:
            return direct_jwt

    # 2. Si pegó solo el token Basic (Base64 de client_id:client_secret) sin rellenar client_id/client_secret
    raw_basic_b64 = ""
    if basic_or_bearer:
        raw_basic_b64 = re.sub(r"^basic\s+", "", basic_or_bearer, flags=re.IGNORECASE).strip()
        if (not client_id or not client_secret) and raw_basic_b64:
            try:
                decoded_pair = base64.b64decode(raw_basic_b64).decode("utf-8", errors="ignore")
                if ":" in decoded_pair:
                    c_id, c_sec = decoded_pair.split(":", 1)
                    client_id = client_id or c_id.strip()
                    client_secret = client_secret or c_sec.strip()
            except Exception:
                pass

    if not raw_basic_b64 and client_id and client_secret and "@" not in client_id:
        raw_basic_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")

    timeout = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)

    # 3. Intercambio OAuth2 oficial (`grant_type=client_credentials`)
    if client_id and client_secret and raw_basic_b64 and "@" not in client_id:
        params = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        headers = {
            "Authorization": f"Basic {raw_basic_b64}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(HOTMART_OAUTH_URL, params=params, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    token = str(data.get("access_token") or "").strip()
                    if token:
                        return token
        except Exception as exc:
            logger.warning("Aviso obteniendo OAuth client_credentials de Hotmart: %s", exc)

    # 4. Intento OAuth password si el usuario colocó su correo en client_id y contraseña en client_secret
    if client_id and "@" in client_id and client_secret:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    HOTMART_OAUTH_URL,
                    data={
                        "grant_type": "password",
                        "username": client_id,
                        "password": client_secret,
                    },
                    headers={
                        "Accept": "application/json",
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                    },
                )
                if resp.status_code == 200:
                    token = str(resp.json().get("access_token") or "").strip()
                    if token:
                        return token
        except Exception as exc:
            logger.debug("OAuth password directo requirió sesión de navegador o API key: %s", exc)

    return None


async def auto_affiliate_and_extract_hotlink(
    bearer_token: str,
    product_id: int,
) -> Optional[str]:
    """
    Ejecuta en tiempo real los 2 pasos internos de Hotmart para un `product_id`:
    1. Solicita la Afiliación Inmediata a 1 Clic:
       `POST https://api-affiliation-market.hotmart.com/v1/market/affiliation-request`
       con `{"productId": product_id, "userSessionId": "<uuid>"}`.
    2. Consulta los HotLinks de divulgación del afiliado para ese producto:
       `GET https://api-affiliation.hotmart.com/v2/hotlink/{product_id}?page=1&rows=20`
       y extrae el enlace real `https://go.hotmart.com/XXXX` (`salesPage`, `publicProfileLink` o `directPayment`).
    """
    if not bearer_token or not product_id or product_id <= 0:
        return None

    session_uuid = str(uuid.uuid4())
    market_headers = {
        "Authorization": f"Bearer {bearer_token}",
        "x-app-name": "app-market",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://app-vlc.hotmart.com",
        "Referer": "https://app-vlc.hotmart.com/",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    affiliation_headers = {
        "Authorization": f"Bearer {bearer_token}",
        "x-app-name": "app-affiliation",
        "Accept": "application/json",
        "Origin": "https://app-vlc.hotmart.com",
        "Referer": "https://app-vlc.hotmart.com/",
        "User-Agent": market_headers["User-Agent"],
    }

    timeout = httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Paso 1: Solicitar Afiliación 1-Clic (si ya era afiliado, Hotmart responde 200 o 400/409 y continuamos)
        try:
            aff_resp = await client.post(
                HOTMART_AFFILIATION_REQUEST_URL,
                json={"productId": int(product_id), "userSessionId": session_uuid},
                headers=market_headers,
            )
            logger.info(
                "Solicitud de auto-afiliación 1-Clic para productId=%s respondió HTTP %s",
                product_id,
                aff_resp.status_code,
            )
        except Exception as exc:
            logger.debug("Aviso en affiliation-request para productId=%s: %s", product_id, exc)

        # Paso 2: Extraer el enlace `https://go.hotmart.com/...` desde `api-affiliation.hotmart.com/v2/hotlink/{productId}`
        hotlink_url_endpoint = HOTMART_HOTLINK_DETAIL_URL_TEMPLATE.format(product_id=int(product_id))
        try:
            hl_resp = await client.get(hotlink_url_endpoint, headers=affiliation_headers)
            if hl_resp.status_code == 200:
                hl_data = hl_resp.json()
                hotlinks_obj = hl_data.get("hotlinks") or {}
                if isinstance(hotlinks_obj, dict):
                    for key in ("salesPage", "publicProfileLink", "directPayment", "customizedCheckoutNew"):
                        candidate_link = str(hotlinks_obj.get(key) or "").strip()
                        if candidate_link.startswith("http") and "go.hotmart.com" in candidate_link:
                            return candidate_link
                    alt_pages = hotlinks_obj.get("alternativePages") or []
                    if isinstance(alt_pages, list):
                        for alt in alt_pages:
                            if isinstance(alt, dict):
                                alt_link = str(alt.get("hotlink") or alt.get("url") or "").strip()
                                if alt_link.startswith("http") and "go.hotmart.com" in alt_link:
                                    return alt_link
        except Exception as exc:
            logger.debug("Aviso extrayendo HotLink para productId=%s: %s", product_id, exc)

    return None


async def search_hotmart_affiliate_market_api(
    clone: BotClone,
    bearer_token: str,
    max_results: int = 2,
) -> List[Dict[str, Any]]:
    """
    Busca directamente dentro del **Mercado de Afiliación interno de Hotmart**
    (`Productos -> Afiliarse a un producto` -> `POST https://api-affiliation-market.hotmart.com/v2/market/search`)
    filtrando por:
    - Afiliación Inmediata a 1 Clic (`affiliationTypes: ["ONE_CLICK"]`),
    - Alto margen de comisión (`40% - 100%`),
    - Idioma Español (`ES`),
    - Coherencia estricta con el nicho del Micro-Bot y máxima calificación (`reviewRating`).
    Además, se auto-afilia en 1 clic y extrae el enlace real `https://go.hotmart.com/XXXX`.
    """
    if not bearer_token:
        return []

    specific_query = build_specific_hotmart_query(clone)
    session_uuid = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "x-app-name": "app-market",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://app-vlc.hotmart.com",
        "Referer": "https://app-vlc.hotmart.com/",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    payload_strict = {
        "page": 1,
        "rows": 25,
        "userLocale": "es",
        "search": specific_query,
        "name": "dearest",
        "userSessionId": session_uuid,
        "hasSendEvent": False,
        "advancedFilters": {
            "affiliation": {
                "affiliationTypes": ["ONE_CLICK"],
            },
            "financial": {
                "commission": {"minValue": 40, "maxValue": 100},
            },
            "localization": {
                "languages": ["ES"],
            },
        },
    }
    payload_broad = {
        "page": 1,
        "rows": 25,
        "userLocale": "es",
        "search": specific_query,
        "name": "dearest",
        "userSessionId": session_uuid,
        "hasSendEvent": False,
    }

    candidates: List[Dict[str, Any]] = []
    timeout = httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for payload in (payload_strict, payload_broad):
            for endpoint in (HOTMART_MARKET_SEARCH_V2_URL, HOTMART_MARKET_SEARCH_V1_URL):
                try:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    items = data.get("content") or data.get("data") or []
                    if not isinstance(items, list) or not items:
                        continue

                    for g in items:
                        if not isinstance(g, dict):
                            continue
                        prod_obj = g.get("product") or {}
                        producer_obj = g.get("producer") or {}
                        aff_obj = g.get("affiliation") or {}
                        comm_obj = aff_obj.get("commission") or {}
                        price_obj = prod_obj.get("price") or {}

                        title = str(prod_obj.get("name") or "").strip()
                        if not title:
                            continue
                        coherence = calculate_topic_coherence_score(title, clone)
                        if coherence <= 0:
                            continue

                        product_id = int(prod_obj.get("id") or 0)
                        product_ucode = str(prod_obj.get("ucode") or "").strip()
                        producer_ucode = str(producer_obj.get("ucode") or "").strip()
                        rating = float(prod_obj.get("reviewRating") or 4.8)
                        temperature = float(prod_obj.get("temperature") or 50.0)
                        comm_pct = float(comm_obj.get("percentage") or 60.0)
                        comm_val = float(comm_obj.get("value") or 25.0)
                        sale_price = float(price_obj.get("value") or 49.0)
                        aff_type = str(aff_obj.get("type") or "ONE_CLICK")
                        image_url = str(
                            prod_obj.get("image")
                            or "https://static-media.hotmart.com/img/hotmart-logo-share.png"
                        ).strip()

                        if producer_ucode and product_ucode:
                            direct_aff_url = (
                                f"https://app-vlc.hotmart.com/market/details"
                                f"?producerUcode={producer_ucode}&productUcode={product_ucode}"
                            )
                        elif product_id > 0:
                            direct_aff_url = f"https://app-vlc.hotmart.com/hotlinks/{product_id}"
                        else:
                            continue

                        is_one_click = 1 if aff_type == "ONE_CLICK" else 0
                        candidates.append(
                            {
                                "product_id": product_id,
                                "product_ucode": product_ucode,
                                "producer_ucode": producer_ucode,
                                "title": (
                                    f"{title} (★ {rating:.1f} · {comm_pct:.0f}% Comisión "
                                    f"[+${comm_val:.2f} USD] · Afiliación Inmediata)"
                                ),
                                "url": direct_aff_url,
                                "image_url": image_url,
                                "sale_price": sale_price if sale_price > 0 else 49.0,
                                "rating": rating,
                                "commission_pct": comm_pct,
                                "temperature": temperature,
                                "is_one_click": is_one_click,
                                "coherence": coherence,
                            }
                        )
                    if candidates:
                        break
                except Exception as exc:
                    logger.debug("Aviso consultando %s para %s: %s", endpoint, clone.clone_code, exc)
            if candidates:
                break

    if not candidates:
        return []

    # Ordenar por: 1) Afiliación inmediata 1-Clic, 2) Coherencia temática, 3) Mayor % comisión, 4) Mejor calificación ★
    candidates.sort(
        key=lambda x: (
            x.get("is_one_click", 1),
            x.get("coherence", 1),
            x.get("commission_pct", 50.0),
            x.get("rating", 4.8),
            x.get("temperature", 50.0),
        ),
        reverse=True,
    )

    top_results = candidates[:max_results]
    for item in top_results:
        pid = int(item.get("product_id") or 0)
        if pid > 0:
            go_link = await auto_affiliate_and_extract_hotlink(bearer_token, pid)
            if go_link:
                item["url"] = go_link

    return top_results


async def _extract_ucodes_from_product_detail_page(
    client: httpx.AsyncClient,
    detail_url: str,
) -> Dict[str, Any]:
    """
    Visita la página pública de un producto específico en Hotmart (`hotmart.com/es/marketplace/productos/{slug}/{ref}`)
    y extrae desde `__NEXT_DATA__ -> props.pageProps.profile.product`:
    - `productId` (ID numérico exacto del producto en Hotmart)
    - `productUcode` (UUID del producto en el Mercado de Afiliación)
    - `producerUcode` (UUID del productor en el Mercado de Afiliación)
    Con estos 2 UUIDs construye la URL exacta de afiliación a 1 clic del producto:
    `https://app-vlc.hotmart.com/market/details?producerUcode={producerUcode}&productUcode={productUcode}`
    """
    result: Dict[str, Any] = {}
    try:
        resp = await client.get(detail_url)
        if resp.status_code != 200:
            return result
        next_match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            resp.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not next_match:
            return result
        next_json = json.loads(next_match.group(1))
        profile_prod = (
            next_json.get("props", {})
            .get("pageProps", {})
            .get("profile", {})
            .get("product", {})
        )
        if isinstance(profile_prod, dict):
            product_id = int(profile_prod.get("productId") or 0)
            product_ucode = str(profile_prod.get("ucode") or "").strip()
            hotmarter = profile_prod.get("hotmarter") or {}
            producer_ucode = str(hotmarter.get("ucode") or "").strip()
            if product_id > 0:
                result["product_id"] = product_id
            if product_ucode and producer_ucode:
                result["product_ucode"] = product_ucode
                result["producer_ucode"] = producer_ucode
                result["direct_affiliate_url"] = (
                    f"https://app-vlc.hotmart.com/market/details"
                    f"?producerUcode={producer_ucode}&productUcode={product_ucode}"
                )
    except Exception as exc:
        logger.debug("No se pudieron extraer ucodes desde %s: %s", detail_url, exc)
    return result


@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1.2, min=1.5, max=6.0),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
)
async def search_public_hotmart_marketplace(
    clone: BotClone,
    max_results: int = 1,
    bearer_token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    1. Si hay `bearer_token` de Hotmart activo, busca directamente en el **Mercado de Afiliación interno**
       (`api-affiliation-market.hotmart.com/v2/market/search`), se auto-afilia en 1 clic y extrae el `go.hotmart.com`.
    2. Si aún no hay `bearer_token` (o como respaldo), extrae los cursos reales del nicho desde el estado SSR
       `__NEXT_DATA__ -> props.pageProps.resultsData.requestData.results`, obtiene su `productId`,
       `producerUcode` y `productUcode` reales, y genera el enlace directo a la ficha de afiliación
       de ese producto exacto en Hotmart (`https://app-vlc.hotmart.com/market/details?producerUcode=...&productUcode=...`),
       ¡nunca al marketplace de compra ni a una pantalla de búsqueda genérica!
    """
    if bearer_token:
        api_results = await search_hotmart_affiliate_market_api(
            clone=clone,
            bearer_token=bearer_token,
            max_results=max_results,
        )
        if api_results:
            return api_results

    specific_query = build_specific_hotmart_query(clone)
    encoded_q = quote_plus(specific_query)
    url = f"{HOTMART_MARKETPLACE_SEARCH_URL}?q={encoded_q}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    }

    raw_candidates: List[Dict[str, Any]] = []
    timeout = httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0)

    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            for search_url in (url, f"{HOTMART_MARKETPLACE_SEARCH_URL}?q={quote_plus(clone.niche_name.split()[0])}"):
                response = await client.get(search_url)
                if response.status_code != 200:
                    continue
                html_body = response.text

                next_data_match = re.search(
                    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                    html_body,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if not next_data_match:
                    continue

                try:
                    next_json = json.loads(next_data_match.group(1))
                    page_props = next_json.get("props", {}).get("pageProps", {})
                    results_list = (
                        page_props.get("resultsData", {}).get("requestData", {}).get("results")
                        or page_props.get("products")
                        or []
                    )
                    if not isinstance(results_list, list):
                        continue

                    for prod in results_list:
                        if not isinstance(prod, dict):
                            continue
                        title = str(prod.get("title") or prod.get("name") or "").strip()
                        description = str(prod.get("description") or "").strip()
                        tags_str = " ".join(str(t) for t in (prod.get("tags") or []))
                        if not title:
                            continue

                        coherence = calculate_topic_coherence_score(f"{title} {tags_str}", clone)
                        if coherence <= 0 and calculate_topic_coherence_score(description[:250], clone) <= 0:
                            continue
                        if coherence <= 0:
                            coherence = 1

                        product_id = int(prod.get("productId") or prod.get("id") or 0)
                        slug = str(prod.get("slug") or "").strip()
                        ref_code = str(prod.get("producerReferenceCode") or "").strip()
                        rating = float(prod.get("rating") or prod.get("reviewRating") or 4.8)
                        total_reviews = int(prod.get("totalReviews") or 10)
                        lang = str(prod.get("language") or prod.get("locale") or "ES").upper()
                        is_spanish = 1 if "ES" in lang else 0

                        image_url = str(
                            prod.get("finalAvatar")
                            or prod.get("avatar")
                            or prod.get("image")
                            or "https://static-media.hotmart.com/img/hotmart-logo-share.png"
                        ).strip()

                        detail_page_url = (
                            f"https://hotmart.com/es/marketplace/productos/{slug}/{ref_code}"
                            if slug and ref_code
                            else ""
                        )
                        if not detail_page_url:
                            continue

                        raw_candidates.append(
                            {
                                "product_id": product_id,
                                "title": title,
                                "detail_page_url": detail_page_url,
                                "image_url": image_url,
                                "rating": round(max(4.5, min(rating, 5.0)), 1),
                                "total_reviews": total_reviews,
                                "is_spanish": is_spanish,
                                "coherence": coherence,
                            }
                        )
                except Exception as exc:
                    logger.debug("Error parseando resultsData de Hotmart: %s", exc)

                if raw_candidates:
                    break

            # Ordenar por: 1) Español, 2) Coherencia temática con el nicho, 3) Mejor calificación y reseñas
            raw_candidates.sort(
                key=lambda x: (
                    x.get("is_spanish", 0),
                    x.get("coherence", 1),
                    x.get("rating", 4.8),
                    x.get("total_reviews", 1),
                ),
                reverse=True,
            )

            discovered: List[Dict[str, Any]] = []
            for cand in raw_candidates[: max_results * 2]:
                ucode_info = await _extract_ucodes_from_product_detail_page(client, cand["detail_page_url"])
                product_id = int(ucode_info.get("product_id") or cand.get("product_id") or 0)
                direct_aff_url = str(ucode_info.get("direct_affiliate_url") or "").strip()

                # Si tenemos bearer_token, intentamos auto-afiliación inmediata y extracción de go.hotmart.com
                if bearer_token and product_id > 0:
                    go_link = await auto_affiliate_and_extract_hotlink(bearer_token, product_id)
                    if go_link:
                        direct_aff_url = go_link

                if not direct_aff_url:
                    if product_id > 0:
                        direct_aff_url = f"https://app-vlc.hotmart.com/hotlinks/{product_id}"
                    else:
                        continue

                target_price = float(
                    ((clone.min_price or Decimal("29.00")) + (clone.max_price or Decimal("99.00"))) / Decimal("2.5")
                )
                sale_price_clean = round(max(29.00, min(target_price, 89.00)), 2)

                discovered.append(
                    {
                        "product_id": product_id,
                        "product_ucode": ucode_info.get("product_ucode", ""),
                        "producer_ucode": ucode_info.get("producer_ucode", ""),
                        "title": (
                            f"{cand['title']} (★ {cand['rating']:.1f} · Afiliación 1-Clic · "
                            f"Comisión hasta 80% USD)"
                        ),
                        "url": direct_aff_url,
                        "image_url": cand["image_url"],
                        "sale_price": sale_price_clean,
                        "rating": cand["rating"],
                        "coherence": cand["coherence"],
                    }
                )
                if len(discovered) >= max_results:
                    break

            return discovered
    except Exception as exc:
        logger.warning("Aviso consultando catálogo Hotmart para %s: %s", clone.clone_code, exc)
        return []


async def sync_existing_hotmart_affiliations(
    session: AsyncSession,
    bearer_token: str,
) -> int:
    """
    Consulta todos los productos a los que el usuario YA está afiliado en su cuenta de Hotmart
    (`GET https://api-affiliation.hotmart.com/v2/affiliations/products`), extrae sus HotLinks reales
    (`https://go.hotmart.com/XXXX`) desde `/v2/hotlink/{productId}` y los asigna automáticamente
    al Micro-Bot cuyo nicho coincida temáticamente.
    """
    if not bearer_token:
        return 0

    clones_stmt = select(BotClone).order_by(BotClone.id.asc())
    all_clones = list((await session.execute(clones_stmt)).scalars().all())
    if not all_clones:
        return 0

    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "x-app-name": "app-affiliation",
        "Accept": "application/json",
        "Origin": "https://app-vlc.hotmart.com",
        "Referer": "https://app-vlc.hotmart.com/",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    synced_count = 0
    timeout = httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(HOTMART_AFFILIATED_PRODUCTS_URL, headers=headers)
            if resp.status_code != 200:
                return 0
            data = resp.json()
            products = data if isinstance(data, list) else (data.get("data") or data.get("content") or [])
            if not isinstance(products, list):
                return 0

            for prod in products:
                if not isinstance(prod, dict):
                    continue
                product_id = int(prod.get("id") or prod.get("productId") or 0)
                title = str(prod.get("name") or prod.get("title") or "").strip()
                if product_id <= 0 or not title:
                    continue

                go_link = await auto_affiliate_and_extract_hotlink(bearer_token, product_id)
                if not go_link:
                    continue

                target_clone = find_best_matching_clone_for_title(title, all_clones)
                external_id = f"HOT_{product_id}"
                existing = (
                    await session.execute(
                        select(HotmartProductPool)
                        .where(HotmartProductPool.external_item_id == external_id)
                        .limit(1)
                    )
                ).scalar_one_or_none()

                sale_p = Decimal("49.00")
                orig_p = Decimal("99.00")
                image_url = str(
                    prod.get("image")
                    or prod.get("coverPhoto")
                    or "https://static-media.hotmart.com/img/hotmart-logo-share.png"
                )

                if existing is None:
                    entry = HotmartProductPool(
                        clone_id=target_clone.id,
                        external_item_id=external_id,
                        title=f"{title} (★ Afiliado Activo · HotLink Directo)"[:500],
                        hotlink_url=go_link,
                        image_url=image_url,
                        original_price=orig_p,
                        sale_price=sale_p,
                        discount_percent=Decimal("50.00"),
                        currency="USD",
                        coupon_code="50OFF",
                        is_active=True,
                    )
                    session.add(entry)
                else:
                    existing.clone_id = target_clone.id
                    existing.hotlink_url = go_link
                    existing.is_active = True
                synced_count += 1

        if synced_count > 0:
            await session.commit()
    except Exception as exc:
        logger.warning("Aviso sincronizando afiliaciones existentes de Hotmart: %s", exc)

    return synced_count


async def auto_affiliate_single_pool_item(
    session: AsyncSession,
    pool_id: int,
) -> Optional[str]:
    """
    Toma un producto de `HotmartProductPool`, extrae su `productId` (desde `external_item_id = HOT_{productId}`
    o desde su URL), solicita la afiliación 1-Clic en Hotmart y guarda el `https://go.hotmart.com/XXXX` resultante.
    """
    sys_settings = await ensure_system_settings(session)
    bearer_token = await resolve_hotmart_bearer_token(sys_settings)
    if not bearer_token:
        return None

    item = (
        await session.execute(select(HotmartProductPool).where(HotmartProductPool.id == pool_id).limit(1))
    ).scalar_one_or_none()
    if item is None:
        return None

    product_id = 0
    if item.external_item_id and item.external_item_id.startswith("HOT_"):
        raw_suffix = item.external_item_id.replace("HOT_", "").strip()
        if raw_suffix.isdigit():
            product_id = int(raw_suffix)

    if product_id <= 0:
        id_match = re.search(r"/hotlinks/(\d+)", item.hotlink_url or "")
        if id_match:
            product_id = int(id_match.group(1))

    if product_id <= 0:
        return None

    go_link = await auto_affiliate_and_extract_hotlink(bearer_token, product_id)
    if go_link:
        item.hotlink_url = go_link
        await session.commit()
        return go_link

    return None


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
    """Usa Gemini con salida estructurada para asignar automáticamente cada producto al Micro-Bot cuyo nicho coincida exactamente."""
    client = genai.Client(api_key=api_key)
    prompt = (
        "Actúa como el Clasificador Maestro de SwarmEngine 50.\n"
        "Tienes la siguiente flota de 50 Micro-Bots especializados:\n"
        f"{json.dumps(clones_catalog, ensure_ascii=False)}\n\n"
        "Y tienes esta lista de productos/cursos extraídos de Hotmart:\n"
        f"{json.dumps(extracted_items, ensure_ascii=False)}\n\n"
        "REGLA CRÍTICA DE COHERENCIA TEMÁTICA:\n"
        "1. Escoge el `assigned_clone_code` (`bot_01` a `bot_50`) cuyo tema/nicho coincida EXACTAMENTE con el tema del curso.\n"
        "   PROHIBIDO asignar un curso a un bot de un tema diferente.\n"
        "2. Redacta un `optimized_title` claro, persuasivo y con enfoque High-Ticket (destacando Certificación y Garantía).\n"
        "3. Define `estimated_original_price_usd` y `estimated_sale_price_usd` (donde original sea el doble del precio de oferta para reflejar el 50% OFF)."
    )
    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-flash"]:
        try:
            return client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=BatchHotmartAssignmentResponse,
                    temperature=0.1,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Modelo Gemini '%s' devolvió error en clasificador Hotmart (%s), probando siguiente...",
                model_name,
                exc,
            )
    raise RuntimeError("Ningún modelo Gemini respondió para clasificación en bloque.")


async def bulk_auto_assign_hotlinks_with_ai(
    session: AsyncSession,
    raw_urls_text: str,
    default_coupon: str = "",
) -> List[HotmartProductPool]:
    """
    Recibe una lista de enlaces de Hotmart (`go.hotmart.com/...` o `hotmart.com/...`),
    visita cada enlace en tiempo real con `httpx` para extraer su título, foto y precio,
    y los asigna con coherencia temática estricta (Gemini + verificador léxico de nicho)
    al Micro-Bot (`BotClone`) correspondiente.
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
    for url in urls[:30]:
        try:
            meta = await extract_live_hotmart_metadata(url)
            extracted_items.append(
                {
                    "hotlink_url": url,
                    "raw_title": meta.get("title") or url,
                    "image_url": meta.get("image_url") or "https://static-media.hotmart.com/img/hotmart-logo-share.png",
                    "detected_price": float(meta["sale_price"]) if meta.get("sale_price") else 49.0,
                }
            )
        except Exception as exc:
            logger.warning("No se pudo inspeccionar URL %s: %s", url, exc)
            extracted_items.append(
                {
                    "hotlink_url": url,
                    "raw_title": f"Programa Certificado ({url.split('/')[-1]})",
                    "image_url": "https://static-media.hotmart.com/img/hotmart-logo-share.png",
                    "detected_price": 49.0,
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
            logger.warning("Fallo clasificación IA Gemini, aplicando emparejamiento léxico estricto por nicho: %s", exc)

    saved_entries: List[HotmartProductPool] = []

    for item in extracted_items:
        url = item["hotlink_url"]
        raw_title = str(item["raw_title"])
        assignment = assignments_map.get(url)

        if assignment and assignment.assigned_clone_code in clone_by_code:
            target_clone = clone_by_code[assignment.assigned_clone_code]
            best_lexical_clone = find_best_matching_clone_for_title(raw_title, all_clones)
            if (
                calculate_topic_coherence_score(raw_title, target_clone) == 0
                and calculate_topic_coherence_score(raw_title, best_lexical_clone) >= 3
            ):
                target_clone = best_lexical_clone
            final_title = assignment.optimized_title
            orig_p = Decimal(str(round(assignment.estimated_original_price_usd, 2)))
            sale_p = Decimal(str(round(assignment.estimated_sale_price_usd, 2)))
        else:
            target_clone = find_best_matching_clone_for_title(raw_title, all_clones)
            final_title = raw_title[:450]
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
    logger.info("Auto-asignados %d productos de Hotmart con coherencia de nicho verificada.", len(saved_entries))
    return saved_entries


async def auto_discover_hotmart_catalog_for_clones(
    session: AsyncSession,
    max_clones_to_scan: int = 50,
    reset_incoherent: bool = True,
) -> int:
    """
    Recorre los 50 Micro-Bots:
    1. Elimina cualquier enlace antiguo que apunte al marketplace de compra (`hotmart.com/es/marketplace`)
       o a pantallas de búsqueda genéricas (`?q=`), o que no coincida con el nicho del bot.
    2. Si el usuario conectó su cuenta/token de Hotmart, sincroniza primero sus afiliaciones activas
       (`GET /v2/affiliations/products`) y busca en el **Mercado de Afiliación interno**
       (`POST api-affiliation-market.hotmart.com/v2/market/search`), auto-afiliándose en 1 clic
       (`POST /v1/market/affiliation-request`) y tomando el link `https://go.hotmart.com/XXXX`.
    3. Incluso sin token activo, extrae el `productId`, `producerUcode` y `productUcode` reales de cada curso
       y asigna la URL directa a la ficha exacta del producto en el Mercado de Afiliación:
       `https://app-vlc.hotmart.com/market/details?producerUcode={producerUcode}&productUcode={productUcode}`.
    """
    sys_settings = await ensure_system_settings(session)
    bearer_token = await resolve_hotmart_bearer_token(sys_settings)

    stmt = select(BotClone).order_by(BotClone.id.asc())
    clones = list((await session.execute(stmt)).scalars().all())
    clone_by_id = {c.id: c for c in clones}

    # 1. Limpiar enlaces antiguos de compra/búsqueda (`hotmart.com/es/marketplace` o `?q=`) o incoherentes
    if reset_incoherent:
        all_pool = list((await session.execute(select(HotmartProductPool))).scalars().all())
        for p_item in all_pool:
            owner_clone = clone_by_id.get(p_item.clone_id)
            url_str = (p_item.hotlink_url or "").strip()
            is_manual_go_link = "go.hotmart.com" in url_str
            is_buyer_or_search_url = (
                "hotmart.com/es/marketplace" in url_str
                or "hotmart.com/pt-br/marketplace" in url_str
                or "?q=" in url_str
                or "/market/search" in url_str
            )
            if is_buyer_or_search_url:
                await session.delete(p_item)
                continue
            if owner_clone is not None and not is_manual_go_link:
                coherence = calculate_topic_coherence_score(p_item.title, owner_clone)
                if coherence <= 0:
                    await session.delete(p_item)
                elif bearer_token and p_item.external_item_id.startswith("HOT_"):
                    # Si ya era una ficha de afiliación y ahora tenemos token, intentar auto-afiliar y obtener go.hotmart.com
                    raw_pid = p_item.external_item_id.replace("HOT_", "").strip()
                    if raw_pid.isdigit():
                        go_link = await auto_affiliate_and_extract_hotlink(bearer_token, int(raw_pid))
                        if go_link:
                            p_item.hotlink_url = go_link
        await session.flush()

    total_assigned = 0

    # 2. Si hay token activo, sincronizar también productos a los que ya esté afiliado en su cuenta
    if bearer_token:
        already_synced = await sync_existing_hotmart_affiliations(session, bearer_token)
        total_assigned += already_synced

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
        discovered = await search_public_hotmart_marketplace(
            clone=clone,
            max_results=1,
            bearer_token=bearer_token,
        )

        for prod in discovered:
            prod_url = str(prod["url"]).strip()
            product_id = int(prod.get("product_id") or 0)
            if product_id > 0:
                external_id = f"HOT_{product_id}"
            else:
                url_hash = hashlib.sha1(f"{clone.clone_code}:{prod_url}".encode("utf-8")).hexdigest()[:10].upper()
                external_id = f"HOT_{url_hash}"

            exists = (
                await session.execute(
                    select(HotmartProductPool)
                    .where(HotmartProductPool.external_item_id == external_id)
                    .limit(1)
                )
            ).scalar_one_or_none()

            sale_p = Decimal(str(round(float(prod.get("sale_price") or 49.0), 2)))
            if sale_p <= Decimal("0.00"):
                sale_p = Decimal("49.00")
            orig_p = (sale_p * Decimal("2.0")).quantize(Decimal("0.01"))

            if exists is None:
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
                    coupon_code="50OFF",
                    is_active=True,
                )
                session.add(entry)
                total_assigned += 1
            else:
                exists.hotlink_url = prod_url
                exists.title = str(prod["title"])[:500]
                exists.is_active = True

    await session.commit()
    logger.info(
        "Auto-descubrimiento en Mercado de Afiliación Hotmart asignó %d productos a %d bots.",
        total_assigned,
        scanned_clones,
    )
    return total_assigned
