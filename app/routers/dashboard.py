from datetime import date
from decimal import Decimal
import hashlib
import hmac
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import BASE_DIR, get_settings
from app.database import get_db_session
from app.models import BotClone, DailyCloneMetrics, HotmartProductPool, ProductOffer
from app.scheduler import execute_clone_pipeline, schedule_single_clone_job
from app.services.darwin import run_darwinian_evolution_cycle
from app.services.harvester import extract_live_hotmart_metadata
from app.services.seeder import ensure_system_settings, generate_new_clone_batch_with_gemini

logger = logging.getLogger(__name__)
settings = get_settings()

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
session_serializer = URLSafeTimedSerializer(settings.secret_key, salt="swarmengine-admin-auth")
SESSION_COOKIE_NAME = "swarm_admin_session"
SESSION_MAX_AGE_SECONDS = 86400 * 7  # 7 días

router = APIRouter(prefix="/admin", tags=["Panel de Control Administrativo"])


def is_admin_authenticated(request: Request) -> bool:
    """Verifica si la cookie firmada del administrador es válida."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    try:
        data = session_serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return data.get("user") == settings.admin_username
    except BadSignature:
        return False


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request) -> Response:
    """Muestra el formulario de acceso protegido por contraseña para `/admin`."""
    if is_admin_authenticated(request):
        return RedirectResponse(url="/admin", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={"error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    """Valida credenciales con comparación en tiempo constante y emite cookie firmada."""
    user_ok = hmac.compare_digest(username.strip(), settings.admin_username)
    pass_ok = hmac.compare_digest(password, settings.admin_password)

    if not (user_ok and pass_ok):
        return templates.TemplateResponse(
            request=request,
            name="admin_login.html",
            context={"error": "Credenciales inválidas. Verifica usuario y contraseña."},
            status_code=401,
        )

    signed_token = session_serializer.dumps({"user": settings.admin_username})
    response = RedirectResponse(url="/admin", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed_token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def admin_logout() -> RedirectResponse:
    """Cierra la sesión administrativa eliminando la cookie."""
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_dashboard_view(
    request: Request,
    msg: Optional[str] = None,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """
    Dashboard Administrativo principal en `/admin`:
    - Tabla de los 50 clones con estado (Activo/Pausado), ofertas publicadas hoy,
      clics acumulados, gasto en Gemini USD, comisiones registradas y botones de acción en 1 clic.
    - Gestor de HotLinks de Hotmart con extracción automática de metadatos en vivo.
    - Métricas financieras globales y gráficas Chart.js.
    """
    if not is_admin_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    sys_settings = await ensure_system_settings(session)
    today = date.today()

    today_stmt = select(DailyCloneMetrics).where(DailyCloneMetrics.date == today)
    today_rows = list((await session.execute(today_stmt)).scalars().all())
    today_by_clone: Dict[int, DailyCloneMetrics] = {m.clone_id: m for m in today_rows}

    agg_stmt = (
        select(
            BotClone,
            func.coalesce(func.sum(DailyCloneMetrics.offers_published), 0).label("total_offers"),
            func.coalesce(func.sum(DailyCloneMetrics.total_clicks), 0).label("total_clicks"),
            func.coalesce(func.sum(DailyCloneMetrics.ai_cost_usd), Decimal("0.000000")).label("total_ai_cost"),
            func.coalesce(
                func.sum(DailyCloneMetrics.confirmed_commissions_usd),
                Decimal("0.00"),
            ).label("total_commissions"),
        )
        .outerjoin(DailyCloneMetrics, DailyCloneMetrics.clone_id == BotClone.id)
        .group_by(BotClone.id)
        .order_by(BotClone.id.asc())
    )
    agg_rows = (await session.execute(agg_stmt)).all()

    clone_table: List[Dict[str, Any]] = []
    global_clicks = 0
    global_offers_today = 0
    global_ai_cost_today = Decimal("0.000000")
    global_ai_cost_total = Decimal("0.000000")
    global_commissions_total = Decimal("0.00")
    active_bots_count = 0

    for clone, total_offers, total_clicks, total_ai_cost, total_commissions in agg_rows:
        today_metric = today_by_clone.get(clone.id)
        offers_today = int(today_metric.offers_published) if today_metric else 0
        clicks_today = int(today_metric.total_clicks) if today_metric else 0
        ai_cost_today = Decimal(str(today_metric.ai_cost_usd)) if today_metric else Decimal("0.000000")

        clicks_accum = int(total_clicks or 0)
        ai_cost_accum = Decimal(str(total_ai_cost or "0.000000"))
        comm_accum = Decimal(str(total_commissions or "0.00"))

        global_clicks += clicks_accum
        global_offers_today += offers_today
        global_ai_cost_today += ai_cost_today
        global_ai_cost_total += ai_cost_accum
        global_commissions_total += comm_accum
        if clone.is_active:
            active_bots_count += 1

        clone_table.append(
            {
                "clone": clone,
                "offers_today": offers_today,
                "clicks_today": clicks_today,
                "total_offers": int(total_offers or 0),
                "total_clicks": clicks_accum,
                "ai_cost_today_usd": ai_cost_today,
                "ai_cost_total_usd": ai_cost_accum,
                "commissions_usd": comm_accum,
            }
        )

    top_chart_clones = sorted(clone_table, key=lambda x: x["total_clicks"], reverse=True)[:10]
    chart_labels = [item["clone"].clone_code for item in top_chart_clones]
    chart_clicks = [item["total_clicks"] for item in top_chart_clones]
    chart_commissions = [float(item["commissions_usd"]) for item in top_chart_clones]

    total_offers_db = (await session.execute(select(func.count(ProductOffer.id)))).scalar_one() or 0

    hotmart_stmt = (
        select(HotmartProductPool)
        .options(selectinload(HotmartProductPool.clone))
        .order_by(desc(HotmartProductPool.id))
        .limit(20)
    )
    hotmart_items = list((await session.execute(hotmart_stmt)).scalars().all())

    recent_offers_stmt = (
        select(ProductOffer)
        .options(selectinload(ProductOffer.clone))
        .order_by(desc(ProductOffer.detected_at))
        .limit(15)
    )
    recent_offers = list((await session.execute(recent_offers_stmt)).scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "sys_settings": sys_settings,
            "clone_table": clone_table,
            "hotmart_items": hotmart_items,
            "recent_offers": recent_offers,
            "total_clones": len(clone_table),
            "active_bots_count": active_bots_count,
            "global_clicks": global_clicks,
            "global_offers_today": global_offers_today,
            "total_offers_db": int(total_offers_db),
            "global_ai_cost_today": global_ai_cost_today,
            "global_ai_cost_total": global_ai_cost_total,
            "global_commissions_total": global_commissions_total,
            "chart_labels": chart_labels,
            "chart_clicks": chart_clicks,
            "chart_commissions": chart_commissions,
            "flash_msg": msg,
        },
    )


@router.post("/hotmart/add")
async def add_hotmart_product_to_pool(
    request: Request,
    background_tasks: BackgroundTasks,
    clone_id: int = Form(...),
    hotlink_url: str = Form(...),
    title: str = Form(""),
    image_url: str = Form(""),
    original_price: float = Form(99.0),
    sale_price: float = Form(49.0),
    currency: str = Form("USD"),
    coupon_code: str = Form(""),
    publish_now: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """
    Agrega un HotLink de Hotmart (`https://go.hotmart.com/...` o página de oferta) al pool de un Micro-Bot.
    Si el usuario deja el título o la imagen en blanco, extrae los datos reales en vivo desde la página de Hotmart.
    Si marca `publish_now`, dispara de inmediato la redacción con Gemini 2.5 Flash y la publicación.
    """
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    clean_hotlink = hotlink_url.strip()
    final_title = title.strip()
    final_image = image_url.strip()

    if not final_title or not final_image:
        try:
            meta = await extract_live_hotmart_metadata(clean_hotlink)
            if not final_title and meta.get("title"):
                final_title = str(meta["title"])
            if not final_image and meta.get("image_url"):
                final_image = str(meta["image_url"])
        except Exception as exc:
            logger.warning("No se pudo extraer metadata automática de %s: %s", clean_hotlink, exc)

    if not final_title:
        final_title = "Programa Completo Certificado con Descuento Especial"
    if not final_image:
        final_image = "https://static-media.hotmart.com/img/hotmart-logo-share.png"

    orig_dec = Decimal(str(max(1.0, round(original_price, 2))))
    sale_dec = Decimal(str(max(0.5, round(sale_price, 2))))
    if orig_dec <= sale_dec:
        orig_dec = (sale_dec * Decimal("2.0")).quantize(Decimal("0.01"))

    disc_float = round(float(((orig_dec - sale_dec) / orig_dec) * Decimal("100")), 1)
    disc_dec = Decimal(str(disc_float))

    url_hash = hashlib.sha1(clean_hotlink.encode("utf-8")).hexdigest()[:10].upper()
    external_id = f"HOT_{url_hash}"

    existing = (
        await session.execute(
            select(HotmartProductPool).where(HotmartProductPool.external_item_id == external_id).limit(1)
        )
    ).scalar_one_or_none()

    if existing is None:
        pool_entry = HotmartProductPool(
            clone_id=clone_id,
            external_item_id=external_id,
            title=final_title[:500],
            hotlink_url=clean_hotlink,
            image_url=final_image,
            original_price=orig_dec,
            sale_price=sale_dec,
            discount_percent=disc_dec,
            currency=currency.strip().upper() or "USD",
            coupon_code=coupon_code.strip(),
            is_active=True,
        )
        session.add(pool_entry)
    else:
        existing.clone_id = clone_id
        existing.title = final_title[:500]
        existing.hotlink_url = clean_hotlink
        existing.image_url = final_image
        existing.original_price = orig_dec
        existing.sale_price = sale_dec
        existing.discount_percent = disc_dec
        existing.currency = currency.strip().upper() or "USD"
        existing.coupon_code = coupon_code.strip()
        existing.is_active = True

    await session.commit()

    if publish_now == "on":
        background_tasks.add_task(execute_clone_pipeline, clone_id, True)
        return RedirectResponse(
            url="/admin?msg=Producto+Hotmart+guardado+y+publicandose+con+Gemini+2.5+Flash",
            status_code=302,
        )

    return RedirectResponse(
        url="/admin?msg=Producto+Hotmart+agregado+al+pool+correctamente",
        status_code=302,
    )


@router.post("/hotmart/bulk-auto-assign")
async def admin_bulk_auto_assign_hotlinks(
    request: Request,
    raw_urls_text: str = Form(...),
    default_coupon: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """
    Recibe múltiples enlaces de Hotmart en bloque sin elegir nicho;
    extrae título, foto y precio de cada uno y usa Gemini 2.5 Flash para asignarlos
    automáticamente a los Micro-Bots (`bot_01` a `bot_50`) más afines.
    """
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    from app.services.hotmart_sync import bulk_auto_assign_hotlinks_with_ai

    assigned = await bulk_auto_assign_hotlinks_with_ai(
        session=session,
        raw_urls_text=raw_urls_text,
        default_coupon=default_coupon,
    )
    return RedirectResponse(
        url=f"/admin?msg=IA+asigno+automaticamente+{len(assigned)}+productos+de+Hotmart+a+sus+Micro-Bots",
        status_code=302,
    )


@router.post("/hotmart/auto-discover")
async def admin_auto_discover_hotmart_catalog(
    request: Request,
    max_clones: int = Form(15),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """
    Entra automáticamente al catálogo de Hotmart Marketplace, busca productos ganadores
    para los Micro-Bots y los asigna sin intervención manual.
    """
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    from app.services.hotmart_sync import auto_discover_hotmart_catalog_for_clones

    count = await auto_discover_hotmart_catalog_for_clones(
        session=session,
        max_clones_to_scan=max(1, min(max_clones, 50)),
    )
    return RedirectResponse(
        url=f"/admin?msg=Auto-Explorador+Hotmart+asigno+{count}+productos+nuevos+a+los+Micro-Bots",
        status_code=302,
    )


@router.post("/clones/{clone_id}/toggle")
async def toggle_clone_active_state(
    clone_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Pausa o reactiva un Micro-Bot en 1 clic y actualiza inmediatamente su tarea en APScheduler."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    clone = (await session.execute(select(BotClone).where(BotClone.id == clone_id).limit(1))).scalar_one_or_none()
    if clone is None:
        raise HTTPException(status_code=404, detail="Clon no encontrado.")

    clone.is_active = not clone.is_active
    await session.commit()
    await session.refresh(clone)

    schedule_single_clone_job(clone)

    if request.headers.get("HX-Request") == "true":
        badge_class = (
            "bg-emerald-500/20 text-emerald-400 border-emerald-500/40"
            if clone.is_active
            else "bg-rose-500/20 text-rose-400 border-rose-500/40"
        )
        label = "ACTIVO" if clone.is_active else "PAUSADO"
        btn_label = "Pausar" if clone.is_active else "Activar"
        html_snippet = (
            f'<div id="clone-status-{clone.id}" class="flex items-center gap-2">'
            f'<span class="px-2.5 py-1 text-xs font-semibold rounded-full border {badge_class}">{label}</span>'
            f'<button hx-post="/admin/clones/{clone.id}/toggle" hx-target="#clone-status-{clone.id}" hx-swap="outerHTML" '
            f'class="px-2.5 py-1 text-xs bg-slate-800 hover:bg-slate-700 text-slate-200 rounded border border-slate-700">'
            f"{btn_label}</button></div>"
        )
        return HTMLResponse(content=html_snippet)

    return RedirectResponse(
        url=f"/admin?msg=Estado+de+{clone.clone_code}+actualizado",
        status_code=302,
    )


@router.post("/clones/{clone_id}/run")
async def trigger_clone_manual_execution(
    clone_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Dispara en 1 clic la cosecha, redacción con Gemini y publicación inmediata de un clon, devolviendo el resultado real."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    clone = (await session.execute(select(BotClone).where(BotClone.id == clone_id).limit(1))).scalar_one_or_none()
    if clone is None:
        raise HTTPException(status_code=404, detail="Clon no encontrado.")

    try:
        result = await execute_clone_pipeline(clone.id, True)
    except Exception as exc:
        logger.exception("Error ejecutando manualmente %s: %s", clone.clone_code, exc)
        result = {"status": "error", "reason": str(exc)}

    status = str(result.get("status", ""))
    if request.headers.get("HX-Request") == "true":
        if status == "published":
            short_code = result.get("short_code", "")
            tg_ok = bool(result.get("published_telegram"))
            tg_err = str(result.get("telegram_error", ""))[:65]
            tg_badge = (
                '<span class="px-2 py-0.5 text-[10px] font-bold bg-sky-500/20 text-sky-300 border border-sky-500/40 rounded">✈️ Telegram OK ✅</span>'
                if tg_ok
                else f'<span class="px-2 py-0.5 text-[10px] font-medium bg-amber-500/20 text-amber-300 border border-amber-500/40 rounded" title="{tg_err}">⚠️ TG: {tg_err or "Pendiente"}</span>'
            )
            return HTMLResponse(
                content=(
                    f'<div class="flex flex-col items-center gap-1">'
                    f'<span class="px-2 py-0.5 text-[11px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 rounded">✅ Web Publicada</span>'
                    f'{tg_badge}'
                    f'<a href="/c/{clone.pseo_category_slug}" target="_blank" class="text-[11px] text-sky-400 hover:underline">Ver en Portal ↗</a>'
                    f'<span class="text-[10px] text-slate-400 font-mono">/go/{short_code}</span>'
                    f'</div>'
                )
            )
        if status == "no_deals":
            return HTMLResponse(
                content=(
                    '<span class="px-2 py-1 text-[11px] font-medium bg-amber-500/20 text-amber-300 '
                    'border border-amber-500/40 rounded" title="Agrega un HotLink para este bot o usa el Auto-Explorador">⚠️ Sin links nuevos en cola</span>'
                )
            )
        reason = str(result.get("reason", status))[:50]
        return HTMLResponse(
            content=(
                f'<span class="px-2 py-1 text-[11px] font-medium bg-rose-500/20 text-rose-300 '
                f'border border-rose-500/40 rounded">⚠️ {reason}</span>'
            )
        )

    return RedirectResponse(
        url=f"/admin?msg=Resultado+{clone.clone_code}:+{status}",
        status_code=302,
    )


@router.post("/offers/{offer_id}/send-telegram")
async def send_existing_offer_to_telegram(
    offer_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Reenvía en 1 clic una oferta ya publicada en la web hacia el Canal de Telegram del bot."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    from app.services.affiliate import build_public_shortlink
    from app.services.image_card import generate_offer_card
    from app.services.publisher import publish_offer_to_telegram

    sys_settings = await ensure_system_settings(session)
    offer = (
        await session.execute(
            select(ProductOffer)
            .options(selectinload(ProductOffer.clone))
            .where(ProductOffer.id == offer_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if offer is None or offer.clone is None:
        return RedirectResponse(url="/admin?msg=Oferta+no+encontrada", status_code=302)

    card_buffer, _ = await generate_offer_card(
        image_url=offer.image_url,
        hook_headline=f"OFERTA -{int(round(offer.discount_percent))}% HOY",
        original_price=offer.original_price,
        sale_price=offer.sale_price,
        discount_percent=offer.discount_percent,
        currency=offer.currency,
        niche_name=offer.clone.niche_name,
        short_code=offer.short_code,
    )
    short_link = build_public_shortlink(offer.short_code, sys_settings=sys_settings)

    try:
        await publish_offer_to_telegram(
            card_buffer=card_buffer,
            caption_html=offer.ai_telegram_caption,
            short_link=short_link,
            clone=offer.clone,
            sys_settings=sys_settings,
        )
        offer.published_telegram = True
        await session.commit()
        return RedirectResponse(
            url=f"/admin?msg=Oferta+enviada+exitosamente+a+Telegram+({offer.clone.telegram_channel_id})",
            status_code=302,
        )
    except Exception as exc:
        err_clean = str(exc).replace(" ", "+")[:120]
        return RedirectResponse(
            url=f"/admin?msg=Error+Telegram:+{err_clean}",
            status_code=302,
        )


@router.post("/clones/{clone_id}/commission")
async def register_clone_commission(
    clone_id: int,
    request: Request,
    amount_usd: float = Form(...),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Registra comisiones confirmadas en USD para un clon específico en la fecha actual."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    dec_amount = Decimal(str(round(amount_usd, 2)))
    today = date.today()

    upsert_stmt = (
        pg_insert(DailyCloneMetrics)
        .values(
            clone_id=clone_id,
            date=today,
            offers_published=0,
            total_clicks=0,
            gemini_input_tokens=0,
            gemini_output_tokens=0,
            ai_cost_usd=Decimal("0.000000"),
            confirmed_commissions_usd=dec_amount,
        )
        .on_conflict_do_update(
            constraint="uq_daily_clone_metrics_clone_date",
            set_={
                "confirmed_commissions_usd": DailyCloneMetrics.confirmed_commissions_usd + dec_amount,
            },
        )
    )
    await session.execute(upsert_stmt)
    await session.commit()

    return RedirectResponse(url="/admin?msg=Comision+registrada+correctamente", status_code=302)


@router.post("/settings")
async def update_system_settings(
    request: Request,
    gemini_api_key: str = Form(""),
    telegram_bot_token: str = Form(""),
    admin_telegram_chat_id: str = Form(""),
    evolution_api_url: str = Form(""),
    evolution_api_key: str = Form(""),
    base_shortlink_domain: str = Form(""),
    preferred_marketplace_mode: str = Form("HYBRID"),
    hotmart_hottok: str = Form(""),
    mercadolibre_country_site: str = Form("MCO"),
    mercadolibre_affiliate_matt_tool: str = Form(""),
    amazon_partner_tag: str = Form(""),
    aliexpress_tracking_id: str = Form(""),
    daily_global_budget_usd: float = Form(1.50),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Actualiza en caliente las credenciales y parámetros de `SystemSettings` en PostgreSQL."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    sys_settings = await ensure_system_settings(session)
    if gemini_api_key.strip():
        sys_settings.gemini_api_key = gemini_api_key.strip()
    if telegram_bot_token.strip():
        sys_settings.telegram_bot_token = telegram_bot_token.strip()
    clean_tg_channel = admin_telegram_chat_id.strip()
    if clean_tg_channel and not clean_tg_channel.startswith("@") and not clean_tg_channel.startswith("-") and not clean_tg_channel.isdigit():
        clean_tg_channel = f"@{clean_tg_channel}"
    sys_settings.admin_telegram_chat_id = clean_tg_channel
    if clean_tg_channel:
        all_clones = list((await session.execute(select(BotClone))).scalars().all())
        for clone in all_clones:
            clone.telegram_channel_id = clean_tg_channel

    if evolution_api_url.strip():
        sys_settings.evolution_api_url = evolution_api_url.strip()
    if evolution_api_key.strip():
        sys_settings.evolution_api_key = evolution_api_key.strip()
    if base_shortlink_domain.strip():
        sys_settings.base_shortlink_domain = base_shortlink_domain.strip()
    sys_settings.preferred_marketplace_mode = preferred_marketplace_mode.strip().upper() or "HOTMART"
    if hotmart_hottok.strip():
        sys_settings.hotmart_hottok = hotmart_hottok.strip()
    sys_settings.mercadolibre_country_site = mercadolibre_country_site.strip().upper() or "MCO"
    sys_settings.mercadolibre_affiliate_matt_tool = mercadolibre_affiliate_matt_tool.strip()
    sys_settings.amazon_partner_tag = amazon_partner_tag.strip()
    sys_settings.aliexpress_tracking_id = aliexpress_tracking_id.strip()
    sys_settings.daily_global_budget_usd = Decimal(str(round(daily_global_budget_usd, 2)))

    await session.commit()
    return RedirectResponse(url="/admin?msg=Configuracion+global+y+Canal+de+Telegram+actualizados", status_code=302)


@router.post("/clones/generate-batch")
async def admin_generate_new_batch(
    request: Request,
    batch_size: int = Form(10),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Genera un nuevo lote de micro-nichos (por defecto 10) usando Gemini 2.5 Flash."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    sys_settings = await ensure_system_settings(session)
    size = max(1, min(batch_size, 20))
    created = await generate_new_clone_batch_with_gemini(
        session=session,
        batch_size=size,
        country_site=sys_settings.mercadolibre_country_site,
    )
    for clone in created:
        schedule_single_clone_job(clone)

    return RedirectResponse(
        url=f"/admin?msg=Generados+{len(created)}+nuevos+micro-bots+con+Gemini+2.5+Flash",
        status_code=302,
    )


@router.post("/clones/pause-all")
async def pause_all_clones(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Pausa los 50 Micro-Bots en 1 clic para poder cargar enlaces en cola sin que ninguno publique aún."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    clones = list((await session.execute(select(BotClone))).scalars().all())
    for clone in clones:
        clone.is_active = False
    await session.commit()

    for clone in clones:
        schedule_single_clone_job(clone)

    return RedirectResponse(
        url="/admin?msg=Todos+los+Micro-Bots+estan+PAUSADOS.+Puedes+cargar+tus+enlaces+tranquilamente",
        status_code=302,
    )


@router.post("/clones/activate-all")
async def activate_all_clones(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Activa todos los Micro-Bots en 1 clic e inicia su programación automática en APScheduler."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    clones = list((await session.execute(select(BotClone))).scalars().all())
    for clone in clones:
        clone.is_active = True
    await session.commit()

    for clone in clones:
        schedule_single_clone_job(clone)

    return RedirectResponse(
        url="/admin?msg=Todos+los+Micro-Bots+han+sido+ACTIVADOS+y+programados",
        status_code=302,
    )


@router.post("/hotmart/{pool_id}/delete")
async def delete_hotmart_pool_item(
    pool_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Elimina un producto guardado de la cola HotmartProductPool."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    item = (
        await session.execute(select(HotmartProductPool).where(HotmartProductPool.id == pool_id).limit(1))
    ).scalar_one_or_none()
    if item is not None:
        await session.delete(item)
        await session.commit()

    return RedirectResponse(
        url="/admin?msg=Enlace+eliminado+de+la+cola+de+Hotmart",
        status_code=302,
    )


@router.post("/clones/set-master-telegram")
async def set_master_telegram_channel(
    request: Request,
    master_channel: str = Form(...),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Asigna en 1 clic un mismo Canal de Telegram (ej. @OfertasVeloz) a los 50 Micro-Bots."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    clean_channel = master_channel.strip()
    if clean_channel and not clean_channel.startswith("@") and not clean_channel.startswith("-"):
        clean_channel = f"@{clean_channel}"

    if clean_channel:
        sys_settings = await ensure_system_settings(session)
        sys_settings.admin_telegram_chat_id = clean_channel
        clones = list((await session.execute(select(BotClone))).scalars().all())
        for clone in clones:
            clone.telegram_channel_id = clean_channel
        await session.commit()

    return RedirectResponse(
        url=f"/admin?msg=Canal+de+Telegram+{clean_channel}+asignado+a+los+50+Micro-Bots",
        status_code=302,
    )


@router.post("/seo/ping-all")
async def ping_all_search_engines(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Fuerza en 1 clic el envío masivo de todas las ofertas y categorías a IndexNow (Bing/Yahoo/Yandex) y Google WebSub."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    from app.services.indexer import submit_to_google_websub_hub, submit_urls_to_indexnow
    from app.services.publisher import build_category_pseo_url, build_offer_pseo_url

    sys_settings = await ensure_system_settings(session)
    offers = list((await session.execute(select(ProductOffer).order_by(desc(ProductOffer.detected_at)).limit(200))).scalars().all())
    clones = list((await session.execute(select(BotClone).where(BotClone.is_active.is_(True)))).scalars().all())

    urls: List[str] = [f"{sys_settings.base_shortlink_domain.rstrip('/')}/"]
    for off in offers:
        urls.append(build_offer_pseo_url(off.short_code, off.title, sys_settings=sys_settings))
        off.indexed_google = True
    for clone in clones:
        urls.append(build_category_pseo_url(clone.pseo_category_slug, sys_settings=sys_settings))

    await submit_urls_to_indexnow(urls[:500], sys_settings=sys_settings)
    await submit_to_google_websub_hub(sys_settings=sys_settings)
    await session.commit()

    return RedirectResponse(
        url=f"/admin?msg=Se+notificaron+{len(urls)}+URLs+a+Google+WebSub+e+IndexNow+(Bing/Yahoo/DuckDuckGo)",
        status_code=302,
    )


@router.post("/darwin/run")
async def admin_trigger_darwin_cycle(
    request: Request,
) -> JSONResponse:
    """Ejecuta bajo demanda la evaluación del Motor Darwiniano de Auto-Poda."""
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="No autorizado.")

    summary = await run_darwinian_evolution_cycle(force_evaluation=False)
    return JSONResponse(content=summary)
