import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from googleapiclient.discovery import build
import httpx
from oauth2client.service_account import ServiceAccountCredentials
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.models import SystemSettings

logger = logging.getLogger(__name__)
settings = get_settings()

GOOGLE_INDEXING_SCOPES = ["https://www.googleapis.com/auth/indexing"]
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2.0, max=12.0),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
)
async def submit_urls_to_indexnow(
    urls: List[str],
    sys_settings: Optional[SystemSettings] = None,
) -> bool:
    """
    Notifica instantáneamente a IndexNow API (`https://api.indexnow.org/indexnow`)
    para forzar el rastreo inmediato en Bing, Yandex, Seznam y Naver.
    """
    if not urls:
        return False

    base_domain_url = (
        sys_settings.base_shortlink_domain
        if sys_settings and sys_settings.base_shortlink_domain
        else settings.base_shortlink_domain
    ).rstrip("/")
    parsed_domain = urlparse(base_domain_url)
    host = parsed_domain.netloc or parsed_domain.path
    key = settings.indexnow_api_key.strip()

    if not host or not key:
        return False

    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"{base_domain_url}/{key}.txt",
        "urlList": urls,
    }

    timeout = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            INDEXNOW_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if response.status_code in (200, 202):
            logger.info("IndexNow aceptó %d URLs para indexación inmediata.", len(urls))
            return True
        if response.status_code >= 500 or response.status_code == 429:
            response.raise_for_status()
        logger.warning(
            "IndexNow devolvió HTTP %d: %s",
            response.status_code,
            response.text[:300],
        )
        return False


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2.0, max=15.0),
    retry=retry_if_exception_type(Exception),
)
def _publish_google_indexing_sync(url: str, notification_type: str = "URL_UPDATED") -> Dict[str, Any]:
    """
    Llama a Google Indexing API (`https://indexing.googleapis.com/v3/urlNotifications:publish`)
    usando `google-api-python-client` y `oauth2client.service_account.ServiceAccountCredentials`.
    """
    sa_path = Path(settings.google_service_account_json_path)
    if not sa_path.is_file():
        raise FileNotFoundError(
            f"No existe el archivo de credenciales de Google Service Account en: {sa_path}"
        )

    credentials = ServiceAccountCredentials.from_json_keyfile_name(
        str(sa_path),
        scopes=GOOGLE_INDEXING_SCOPES,
    )
    service = build("indexing", "v3", credentials=credentials, cache_discovery=False)
    body = {
        "url": url,
        "type": notification_type,
    }
    response = service.urlNotifications().publish(body=body).execute()
    return response


async def submit_url_to_google_indexing(
    url: str,
    notification_type: str = "URL_UPDATED",
) -> bool:
    """
    Envía de forma asíncrona una notificación `URL_UPDATED` a Google Indexing API
    para forzar el rastreo inmediato en Google Search.
    """
    sa_path = Path(settings.google_service_account_json_path)
    if not sa_path.is_file():
        logger.debug(
            "Omitiendo Google Indexing API porque %s no está presente en el servidor.",
            sa_path,
        )
        return False

    try:
        result = await asyncio.to_thread(_publish_google_indexing_sync, url, notification_type)
        logger.info("Google Indexing API indexó URL=%s respuesta=%s", url, result)
        return True
    except Exception as exc:
        logger.warning("Fallo al notificar Google Indexing API para %s: %s", url, exc)
        return False


async def notify_search_engines_for_offer(
    offer_url: str,
    category_url: str,
    sys_settings: Optional[SystemSettings] = None,
) -> bool:
    """
    Dispara en paralelo la notificación a IndexNow API y Google Indexing API
    para la URL individual de la oferta y la página del micro-nicho.
    Devuelve True si al menos uno de los motores confirmó recepción.
    """
    urls_to_index = [offer_url, category_url]

    indexnow_task = asyncio.create_task(
        submit_urls_to_indexnow(urls_to_index, sys_settings=sys_settings)
    )
    google_offer_task = asyncio.create_task(
        submit_url_to_google_indexing(offer_url, notification_type="URL_UPDATED")
    )

    results = await asyncio.gather(indexnow_task, google_offer_task, return_exceptions=True)
    success = any(r is True for r in results)
    return success
