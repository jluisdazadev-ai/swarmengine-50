import base64
from io import BytesIO
import json
import logging
import re
import unicodedata
from typing import Any, Dict, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.models import BotClone, ProductOffer, SystemSettings

logger = logging.getLogger(__name__)
settings = get_settings()


def slugify_offer_title(title: str, max_length: int = 75) -> str:
    """Genera un slug URL-safe limpio a partir del título del producto para `/oferta/{short_code}-{slug}`."""
    normalized = unicodedata.normalize("NFD", title.lower())
    ascii_text = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    slug = re.sub(r"[^a-z0-9\s-]", "", ascii_text)
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return (slug[:max_length].rstrip("-")) or "oferta-verificada"


def build_offer_pseo_url(
    short_code: str,
    title: str,
    sys_settings: Optional[SystemSettings] = None,
) -> str:
    """Construye la URL canónica pública de la página individual de la oferta en el portal pSEO."""
    base_domain = (
        sys_settings.base_shortlink_domain
        if sys_settings and sys_settings.base_shortlink_domain
        else settings.base_shortlink_domain
    ).rstrip("/")
    slug = slugify_offer_title(title)
    return f"{base_domain}/oferta/{short_code}-{slug}"


def build_category_pseo_url(
    category_slug: str,
    sys_settings: Optional[SystemSettings] = None,
) -> str:
    """Construye la URL canónica pública de la categoría del micro-nicho en el portal pSEO."""
    base_domain = (
        sys_settings.base_shortlink_domain
        if sys_settings and sys_settings.base_shortlink_domain
        else settings.base_shortlink_domain
    ).rstrip("/")
    return f"{base_domain}/c/{category_slug}"


def html_to_whatsapp_markdown(telegram_html: str, short_link: str) -> str:
    """
    Convierte las etiquetas HTML de Telegram (`<b>`, `<i>`, `<s>`)
    al formato nativo de WhatsApp (`*negrita*`, `_cursiva_`, `~tachado~`)
    y anexa el enlace corto de compra.
    """
    text = telegram_html
    text = re.sub(r"</?(?:b|strong)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:i|em)>", "_", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:s|strike|del)>", "~", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.strip()
    return f"{text}\n\n🔥 *Ver Oferta Real Aquí:* {short_link}"


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2.0, max=12.0),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
)
async def publish_offer_to_telegram(
    card_buffer: BytesIO,
    caption_html: str,
    short_link: str,
    clone: BotClone,
    sys_settings: Optional[SystemSettings] = None,
) -> Dict[str, Any]:
    """
    Publica la tarjeta generada en `BytesIO` en el canal de Telegram del clon llamando a
    `https://api.telegram.org/bot{token}/sendPhoto` con `parse_mode=HTML` y `reply_markup`
    conteniendo el botón inline `🔥 Ver Oferta Real Aquí`.
    """
    bot_token = (
        sys_settings.telegram_bot_token
        if sys_settings and sys_settings.telegram_bot_token
        else settings.telegram_bot_token
    ).strip()

    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN no está configurado.")

    chat_id = clone.telegram_channel_id.strip()
    if not chat_id:
        raise ValueError(f"El clon {clone.clone_code} no tiene telegram_channel_id configurado.")

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "🔥 Ver Oferta Real Aquí",
                    "url": short_link,
                }
            ]
        ]
    }

    card_buffer.seek(0)
    files = {
        "photo": (f"{clone.clone_code}_offer.jpg", card_buffer.getvalue(), "image/jpeg"),
    }
    data = {
        "chat_id": chat_id,
        "caption": caption_html[:1024],
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup),
    }

    candidate_chat_ids = [chat_id]
    if chat_id.startswith("-100") and len(chat_id) > 4:
        candidate_chat_ids.append("-" + chat_id[4:])
    elif chat_id.startswith("-") and not chat_id.startswith("-100"):
        candidate_chat_ids.append("-100" + chat_id[1:])

    if sys_settings and sys_settings.admin_telegram_chat_id.strip():
        master_chat = sys_settings.admin_telegram_chat_id.strip()
        if master_chat not in candidate_chat_ids:
            candidate_chat_ids.append(master_chat)
            if master_chat.startswith("-100") and len(master_chat) > 4:
                candidate_chat_ids.append("-" + master_chat[4:])
            elif master_chat.startswith("-") and not master_chat.startswith("-100"):
                candidate_chat_ids.append("-100" + master_chat[1:])

    timeout = httpx.Timeout(connect=10.0, read=25.0, write=20.0, pool=10.0)
    last_error_desc = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = None
        for target_chat in candidate_chat_ids:
            data["chat_id"] = target_chat
            files = {
                "photo": (f"{clone.clone_code}_offer.jpg", card_buffer.getvalue(), "image/jpeg"),
            }
            response = await client.post(url, data=data, files=files)
            if response.status_code == 200:
                break
            try:
                err_json = response.json()
                last_error_desc = str(err_json.get("description") or response.text)
            except Exception:
                last_error_desc = response.text
            logger.warning(
                "[%s] Intento Telegram chat_id=%s devolvió %d: %s",
                clone.clone_code,
                target_chat,
                response.status_code,
                last_error_desc,
            )

        if response is None or response.status_code != 200:
            raise RuntimeError(f"Telegram API ({response.status_code if response else 'ERR'}): {last_error_desc}")
        result = response.json()

    logger.info(
        "[%s] Oferta publicada en Telegram (%s) message_id=%s",
        clone.clone_code,
        data["chat_id"],
        result.get("result", {}).get("message_id"),
    )
    return result


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2.0, max=12.0),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
)
async def publish_offer_to_whatsapp(
    card_buffer: BytesIO,
    caption_html: str,
    short_link: str,
    clone: BotClone,
    sys_settings: Optional[SystemSettings] = None,
) -> Dict[str, Any]:
    """
    Publica la oferta en WhatsApp mediante Evolution API (`POST {evolution_url}/message/sendMedia/{instance}`)
    enviando la tarjeta en base64, el texto formateado y el enlace corto al `clone.whatsapp_group_jid`.
    """
    evolution_url = (
        sys_settings.evolution_api_url
        if sys_settings and sys_settings.evolution_api_url
        else settings.evolution_api_url
    ).rstrip("/")
    evolution_key = (
        sys_settings.evolution_api_key
        if sys_settings and sys_settings.evolution_api_key
        else settings.evolution_api_key
    ).strip()

    if not evolution_url or not evolution_key:
        raise ValueError("Credenciales de Evolution API (URL / API Key) no configuradas.")

    if not clone.whatsapp_group_jid:
        raise ValueError(f"El clon {clone.clone_code} no tiene whatsapp_group_jid configurado.")

    instance = settings.evolution_instance_name.strip() or "swarm_master"
    endpoint = f"{evolution_url}/message/sendMedia/{instance}"

    card_buffer.seek(0)
    b64_image = base64.b64encode(card_buffer.getvalue()).decode("utf-8")
    whatsapp_caption = html_to_whatsapp_markdown(caption_html, short_link)

    payload = {
        "number": clone.whatsapp_group_jid.strip(),
        "mediatype": "image",
        "mimetype": "image/jpeg",
        "caption": whatsapp_caption,
        "media": b64_image,
        "fileName": f"{clone.clone_code}_deal.jpg",
    }
    headers = {
        "apikey": evolution_key,
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(connect=10.0, read=25.0, write=20.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(endpoint, json=payload, headers=headers)
        if response.status_code >= 500 or response.status_code == 429:
            response.raise_for_status()
        response.raise_for_status()
        result = response.json()

    logger.info(
        "[%s] Oferta enviada a grupo WhatsApp (%s) vía Evolution API.",
        clone.clone_code,
        clone.whatsapp_group_jid,
    )
    return result
