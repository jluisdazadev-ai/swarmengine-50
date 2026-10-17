from datetime import date
from decimal import Decimal
import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db_session
from app.models import BotClone, DailyCloneMetrics
from app.services.seeder import ensure_system_settings

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/webhooks", tags=["Webhooks de Comisiones (Hotmart)"])


def _extract_affiliate_commission_usd(payload: Dict[str, Any]) -> Decimal:
    """
    Extrae el monto real en USD de la comisión del afiliado desde el payload oficial
    de Hotmart Webhook v2.0.0 (`data.commissions` donde `source == 'AFFILIATE'`).
    """
    data = payload.get("data") or {}
    commissions = data.get("commissions") or []

    if isinstance(commissions, list):
        for comm in commissions:
            if not isinstance(comm, dict):
                continue
            source = str(comm.get("source") or "").upper()
            if source == "AFFILIATE":
                val = comm.get("value")
                if val is not None:
                    return Decimal(str(round(float(val), 2)))

    # Fallback: leer el valor en `purchase.price.value` si es comisión única
    purchase = data.get("purchase") or {}
    price_node = purchase.get("price") or {}
    if price_node.get("value") is not None:
        return Decimal(str(round(float(price_node["value"]), 2)))

    return Decimal("0.00")


def _extract_tracking_origin(payload: Dict[str, Any]) -> Optional[str]:
    """
    Extrae el código de rastreo `src` o `sck` enviado por Hotmart en el nodo `origin`
    de la compra (ej. `swarm-bot-01` o `bot_01`).
    """
    data = payload.get("data") or {}
    purchase = data.get("purchase") or {}
    origin = purchase.get("origin") or {}

    sck = str(origin.get("sck") or "").strip()
    if sck:
        return sck

    src = str(origin.get("src") or "").strip()
    if src:
        return src

    tracking = data.get("affiliates")
    if isinstance(tracking, list) and tracking:
        first_aff = tracking[0]
        if isinstance(first_aff, dict) and first_aff.get("name"):
            return str(first_aff["name"]).strip()

    return None


@router.post("/hotmart")
async def hotmart_postback_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """
    Receptor oficial de Webhooks de Hotmart (`POST /webhooks/hotmart` - Versión 2.0.0):
    1. Verifica la cabecera `X-Hotmart-Hottok` contra `SystemSettings.hotmart_hottok`.
    2. Procesa eventos `PURCHASE_APPROVED` y `PURCHASE_COMPLETE` sumando la comisión real en USD
       en `DailyCloneMetrics.confirmed_commissions_usd` del Micro-Bot identificado por `src` / `sck`.
    3. Procesa eventos `PURCHASE_REFUNDED` y `PURCHASE_CHARGEBACK` descontando la comisión revertida.
    """
    sys_settings = await ensure_system_settings(session)
    expected_hottok = (sys_settings.hotmart_hottok or settings.hotmart_hottok).strip()

    incoming_hottok = (
        request.headers.get("X-Hotmart-Hottok")
        or request.headers.get("x-hotmart-hottok")
        or ""
    ).strip()

    if expected_hottok and not hmac.compare_digest(incoming_hottok, expected_hottok):
        logger.warning("Intento de acceso a /webhooks/hotmart con Hottok inválido.")
        raise HTTPException(status_code=401, detail="Hottok inválido.")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"JSON inválido: {exc}")

    event_type = str(payload.get("event") or "").upper().strip()
    tracking_tag = _extract_tracking_origin(payload)
    commission_usd = _extract_affiliate_commission_usd(payload)

    # Buscar el clon propietario de la venta por clone_code (sck) o affiliate_sub_id (src)
    clone: Optional[BotClone] = None
    if tracking_tag:
        stmt = (
            select(BotClone)
            .where(
                or_(
                    BotClone.clone_code == tracking_tag,
                    BotClone.affiliate_sub_id == tracking_tag,
                )
            )
            .limit(1)
        )
        clone = (await session.execute(stmt)).scalar_one_or_none()

    if clone is None:
        # Si la venta vino sin parámetro src/sck, se asigna al bot_01 para no perder el registro contable
        fallback_stmt = select(BotClone).order_by(BotClone.id.asc()).limit(1)
        clone = (await session.execute(fallback_stmt)).scalar_one_or_none()

    if clone is None:
        return {"status": "ignored", "reason": "no_clones_in_db"}

    today = date.today()

    if event_type in {"PURCHASE_APPROVED", "PURCHASE_COMPLETE"} and commission_usd > Decimal("0.00"):
        upsert_stmt = (
            pg_insert(DailyCloneMetrics)
            .values(
                clone_id=clone.id,
                date=today,
                offers_published=0,
                total_clicks=0,
                gemini_input_tokens=0,
                gemini_output_tokens=0,
                ai_cost_usd=Decimal("0.000000"),
                confirmed_commissions_usd=commission_usd,
            )
            .on_conflict_do_update(
                constraint="uq_daily_clone_metrics_clone_date",
                set_={
                    "confirmed_commissions_usd": DailyCloneMetrics.confirmed_commissions_usd + commission_usd,
                },
            )
        )
        await session.execute(upsert_stmt)
        await session.commit()

        logger.info(
            "💰 [Hotmart Webhook] Venta aprobada para %s | Comisión: +$%s USD",
            clone.clone_code,
            commission_usd,
        )
        return {
            "status": "credited",
            "event": event_type,
            "clone_code": clone.clone_code,
            "commission_usd": float(commission_usd),
        }

    if event_type in {"PURCHASE_REFUNDED", "PURCHASE_CHARGEBACK"} and commission_usd > Decimal("0.00"):
        upsert_stmt = (
            pg_insert(DailyCloneMetrics)
            .values(
                clone_id=clone.id,
                date=today,
                offers_published=0,
                total_clicks=0,
                gemini_input_tokens=0,
                gemini_output_tokens=0,
                ai_cost_usd=Decimal("0.000000"),
                confirmed_commissions_usd=Decimal("0.00"),
            )
            .on_conflict_do_update(
                constraint="uq_daily_clone_metrics_clone_date",
                set_={
                    "confirmed_commissions_usd": DailyCloneMetrics.confirmed_commissions_usd - commission_usd,
                },
            )
        )
        await session.execute(upsert_stmt)
        await session.commit()

        logger.info(
            "↩️ [Hotmart Webhook] Reembolso descontado para %s | -$%s USD",
            clone.clone_code,
            commission_usd,
        )
        return {
            "status": "debited",
            "event": event_type,
            "clone_code": clone.clone_code,
            "commission_usd": float(commission_usd),
        }

    return {"status": "received", "event": event_type}
