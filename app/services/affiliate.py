import hashlib
import logging
import secrets
import string
from typing import Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import redis_client
from app.models import BotClone, MarketplaceEnum, ProductOffer, SystemSettings

logger = logging.getLogger(__name__)
settings = get_settings()

SHORT_CODE_ALPHABET = string.ascii_letters + string.digits
SHORT_CODE_LENGTH = 7
REDIS_SHORTCODE_PREFIX = "swarm:go:"
REDIS_SHORTCODE_TTL_SECONDS = 86400 * 30  # 30 días en caché Redis


def build_affiliate_url(
    raw_url: str,
    marketplace: MarketplaceEnum,
    clone: BotClone,
    sys_settings: Optional[SystemSettings] = None,
) -> str:
    """
    Construye el enlace de afiliado real inyectando los parámetros de atribución oficiales
    de cada marketplace junto con el `clone.affiliate_sub_id` para trazabilidad por micro-bot.
    """
    parsed = urlparse(raw_url.strip())
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))

    matt_tool = (
        sys_settings.mercadolibre_affiliate_matt_tool
        if sys_settings and sys_settings.mercadolibre_affiliate_matt_tool
        else settings.mercadolibre_affiliate_matt_tool
    )
    amazon_tag = (
        sys_settings.amazon_partner_tag
        if sys_settings and sys_settings.amazon_partner_tag
        else settings.amazon_partner_tag
    )
    ali_tracking = (
        sys_settings.aliexpress_tracking_id
        if sys_settings and sys_settings.aliexpress_tracking_id
        else settings.aliexpress_tracking_id
    )

    if marketplace == MarketplaceEnum.MERCADOLIBRE:
        if matt_tool:
            query_params["matt_tool"] = matt_tool
        if settings.mercadolibre_affiliate_matt_word:
            query_params["matt_word"] = settings.mercadolibre_affiliate_matt_word
        query_params["matt_sub_id"] = clone.affiliate_sub_id
        query_params["utm_source"] = "swarmengine50"
        query_params["utm_medium"] = "affiliate_bot"
        query_params["utm_campaign"] = clone.clone_code

    elif marketplace == MarketplaceEnum.AMAZON:
        if amazon_tag:
            query_params["tag"] = amazon_tag
        query_params["ascsubtag"] = clone.affiliate_sub_id
        query_params["linkCode"] = "ll1"

    elif marketplace == MarketplaceEnum.ALIEXPRESS:
        if ali_tracking:
            query_params["aff_fcid"] = ali_tracking
        query_params["aff_fsk"] = clone.affiliate_sub_id
        query_params["aff_platform"] = "portals-tool"
        query_params["sk"] = clone.affiliate_sub_id
        query_params["utm_source"] = clone.clone_code

    encoded_query = urlencode(query_params, doseq=True)
    rebuilt_url = urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc,
            parsed.path,
            parsed.params,
            encoded_query,
            "",  # Limpiar fragmentos # para evitar conflictos con trackers de afiliado
        )
    )
    return rebuilt_url


async def generate_unique_short_code(
    session: AsyncSession,
    length: int = SHORT_CODE_LENGTH,
) -> str:
    """
    Genera un código corto único de 7 caracteres alfanuméricos verificando
    que no exista colisión en PostgreSQL (`ProductOffer.short_code`).
    """
    for _ in range(20):
        candidate = "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(length))
        stmt = select(ProductOffer.id).where(ProductOffer.short_code == candidate).limit(1)
        existing_id = (await session.execute(stmt)).scalar_one_or_none()
        if existing_id is None:
            return candidate

    raise RuntimeError("No fue posible generar un short_code único tras 20 intentos.")


def build_public_shortlink(
    short_code: str,
    sys_settings: Optional[SystemSettings] = None,
) -> str:
    """Devuelve la URL pública completa del enlace corto `/go/{short_code}`."""
    domain = (
        sys_settings.base_shortlink_domain
        if sys_settings and sys_settings.base_shortlink_domain
        else settings.base_shortlink_domain
    ).rstrip("/")
    return f"{domain}/go/{short_code}"


async def cache_shortlink_in_redis(offer: ProductOffer) -> None:
    """Almacena en Redis los metadatos de redirección de una oferta para respuesta en <2ms."""
    redis_key = f"{REDIS_SHORTCODE_PREFIX}{offer.short_code}"
    payload: Dict[str, str] = {
        "offer_id": str(offer.id),
        "clone_id": str(offer.clone_id),
        "affiliate_full_url": offer.affiliate_full_url,
    }
    try:
        await redis_client.hset(redis_key, mapping=payload)
        await redis_client.expire(redis_key, REDIS_SHORTCODE_TTL_SECONDS)
    except Exception as exc:
        logger.warning("No se pudo almacenar short_code %s en caché Redis: %s", offer.short_code, exc)


def hash_visitor_ip(raw_ip: str) -> str:
    """Calcula un hash SHA-256 irreversible con sal sobre la IP del visitante para privacidad GDPR/Habeas Data."""
    salted = f"{raw_ip.strip()}:{settings.secret_key}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()
