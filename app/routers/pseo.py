from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import BASE_DIR, get_settings
from app.database import get_db_session
from app.models import BotClone, ProductOffer
from app.services.gemini_engine import format_currency_amount
from app.services.publisher import (
    build_category_pseo_url,
    build_offer_pseo_url,
    slugify_offer_title,
)
from app.services.seeder import ensure_system_settings

logger = logging.getLogger(__name__)
settings = get_settings()

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals["format_currency"] = format_currency_amount
templates.env.globals["slugify_offer"] = slugify_offer_title

router = APIRouter(tags=["Portal Web pSEO"])


@router.get("/", response_class=HTMLResponse)
async def pseo_home_directory(
    request: Request,
    q: Optional[str] = Query(default=None, description="Filtro de búsqueda de ofertas o nichos"),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """
    Directorio general público de las 50 categorías pSEO y últimas ofertas verificadas en tiempo real.
    """
    sys_settings = await ensure_system_settings(session)

    # Consultar los micro-nichos activos junto con su conteo de ofertas publicadas
    cat_stmt = (
        select(
            BotClone,
            func.count(ProductOffer.id).label("offers_count"),
        )
        .outerjoin(ProductOffer, ProductOffer.clone_id == BotClone.id)
        .where(BotClone.is_active.is_(True))
        .group_by(BotClone.id)
        .order_by(BotClone.id.asc())
    )
    cat_rows = (await session.execute(cat_stmt)).all()
    categories = [{"clone": row[0], "offers_count": int(row[1] or 0)} for row in cat_rows]

    # Consultar las últimas ofertas detectadas (con filtro opcional de búsqueda)
    offers_stmt = (
        select(ProductOffer)
        .options(selectinload(ProductOffer.clone))
        .order_by(desc(ProductOffer.detected_at))
        .limit(24)
    )
    if q and q.strip():
        search_term = f"%{q.strip()}%"
        offers_stmt = (
            select(ProductOffer)
            .join(BotClone, ProductOffer.clone_id == BotClone.id)
            .options(selectinload(ProductOffer.clone))
            .where(
                or_(
                    ProductOffer.title.ilike(search_term),
                    BotClone.niche_name.ilike(search_term),
                )
            )
            .order_by(desc(ProductOffer.detected_at))
            .limit(36)
        )

    latest_offers = list((await session.execute(offers_stmt)).scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "categories": categories,
            "latest_offers": latest_offers,
            "search_query": q or "",
            "base_domain": sys_settings.base_shortlink_domain.rstrip("/"),
            "total_clones": len(categories),
        },
    )


@router.get("/c/{pseo_category_slug}", response_class=HTMLResponse)
async def pseo_category_page(
    pseo_category_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """
    Página dedicada del micro-nicho con todas sus ofertas activas y botones directos
    de suscripción al canal de Telegram y grupo de WhatsApp del bot.
    """
    sys_settings = await ensure_system_settings(session)
    slug_clean = pseo_category_slug.strip().lower()

    clone_stmt = select(BotClone).where(BotClone.pseo_category_slug == slug_clean).limit(1)
    clone = (await session.execute(clone_stmt)).scalar_one_or_none()
    if clone is None:
        raise HTTPException(status_code=404, detail="Categoría de micro-nicho no encontrada.")

    offers_stmt = (
        select(ProductOffer)
        .where(ProductOffer.clone_id == clone.id)
        .order_by(desc(ProductOffer.detected_at))
        .limit(60)
    )
    offers = list((await session.execute(offers_stmt)).scalars().all())

    tg_handle = clone.telegram_channel_id.lstrip("@")
    telegram_url = f"https://t.me/{tg_handle}" if tg_handle else ""

    canonical_url = build_category_pseo_url(clone.pseo_category_slug, sys_settings=sys_settings)

    return templates.TemplateResponse(
        request=request,
        name="category.html",
        context={
            "clone": clone,
            "offers": offers,
            "telegram_url": telegram_url,
            "canonical_url": canonical_url,
            "base_domain": sys_settings.base_shortlink_domain.rstrip("/"),
        },
    )


@router.get("/oferta/{short_code_and_slug}", response_class=HTMLResponse)
async def pseo_offer_detail_page(
    short_code_and_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """
    Página individual de la oferta (`/oferta/{short_code}-{slug}`) con:
    - `seo_mini_review_html` redactado por Gemini 2.5 Flash.
    - Datos estructurados `Schema.org/Product` en JSON-LD para Rich Snippets en Google Search.
    - Botón de compra rápida apuntando a `/go/{short_code}`.
    """
    sys_settings = await ensure_system_settings(session)
    parts = short_code_and_slug.strip().split("-", 1)
    short_code = parts[0].strip()

    stmt = (
        select(ProductOffer)
        .options(selectinload(ProductOffer.clone))
        .where(ProductOffer.short_code == short_code)
        .limit(1)
    )
    offer = (await session.execute(stmt)).scalar_one_or_none()
    if offer is None:
        raise HTTPException(status_code=404, detail="Oferta no encontrada.")

    related_stmt = (
        select(ProductOffer)
        .where(
            ProductOffer.clone_id == offer.clone_id,
            ProductOffer.id != offer.id,
        )
        .order_by(desc(ProductOffer.detected_at))
        .limit(6)
    )
    related_offers = list((await session.execute(related_stmt)).scalars().all())

    canonical_url = build_offer_pseo_url(offer.short_code, offer.title, sys_settings=sys_settings)
    base_domain = sys_settings.base_shortlink_domain.rstrip("/")
    card_file = Path(settings.static_cards_dir) / f"{offer.short_code}.jpg"
    card_image_url = (
        f"{base_domain}/static/cards/{offer.short_code}.jpg"
        if card_file.is_file()
        else offer.image_url
    )

    price_valid_until = (offer.detected_at + timedelta(days=15)).strftime("%Y-%m-%d")
    savings_value = offer.original_price - offer.sale_price

    # Estructura oficial Schema.org/Product + FAQPage en JSON-LD para Google Rich Results y Buscadores IA
    schema_org_graph = [
        {
            "@context": "https://schema.org/",
            "@type": "Product",
            "name": offer.title,
            "image": [card_image_url, offer.image_url],
            "description": (
                f"Oferta verificada en {offer.clone.niche_name}: {offer.title} con "
                f"-{offer.discount_percent}% de descuento real. Ahorras {format_currency_amount(savings_value, offer.currency)}."
            ),
            "sku": offer.external_item_id,
            "mpn": offer.external_item_id,
            "brand": {
                "@type": "Brand",
                "name": offer.clone.niche_name,
            },
            "aggregateRating": {
                "@type": "AggregateRating",
                "ratingValue": "4.8",
                "bestRating": "5",
                "ratingCount": "37",
            },
            "offers": {
                "@type": "Offer",
                "url": canonical_url,
                "priceCurrency": offer.currency,
                "price": str(offer.sale_price),
                "priceValidUntil": price_valid_until,
                "itemCondition": "https://schema.org/NewCondition",
                "availability": "https://schema.org/InStock",
                "seller": {
                    "@type": "Organization",
                    "name": offer.source_marketplace.value,
                },
            },
        },
        {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": f"¿Cómo accedo a {offer.title} con el {int(round(offer.discount_percent))}% de descuento?",
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": f"Al hacer clic en el botón oficial de oferta en esta página, el sistema aplica automáticamente el precio rebajado de {format_currency_amount(offer.sale_price, offer.currency)} (ahorras {format_currency_amount(savings_value, offer.currency)}) con acceso inmediato.",
                    },
                },
                {
                    "@type": "Question",
                    "name": "¿Desde qué países puedo inscribirme y qué métodos de pago aceptan?",
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": "Está disponible en EE.UU., México, España, Colombia, Chile y toda Latinoamérica con conversión automática a tu moneda local (Tarjeta, PayPal, OXXO/SPEI, PSE/Nequi, WebPay) y garantía oficial de 7 días.",
                    },
                },
            ],
        },
    ]

    tg_handle = offer.clone.telegram_channel_id.lstrip("@")
    telegram_url = f"https://t.me/{tg_handle}" if tg_handle else ""

    return templates.TemplateResponse(
        request=request,
        name="offer.html",
        context={
            "offer": offer,
            "clone": offer.clone,
            "related_offers": related_offers,
            "canonical_url": canonical_url,
            "card_image_url": card_image_url,
            "savings_formatted": format_currency_amount(savings_value, offer.currency),
            "schema_json_ld": json.dumps(schema_org_graph, ensure_ascii=False, indent=2),
            "telegram_url": telegram_url,
            "base_domain": base_domain,
        },
    )


@router.get("/rss.xml", response_class=Response)
async def dynamic_rss_feed_xml(
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """
    Canal RSS 2.0 + Media RSS con enlace a Google WebSub Hub (`https://pubsubhubbub.appspot.com/`).
    Permite:
    1. Indexación instantánea en Google vía WebSub.
    2. Auto-publicación automática en Pinterest, Facebook Pages, X/Twitter y Make/IFTTT con la tarjeta 1080x1080.
    """
    sys_settings = await ensure_system_settings(session)
    base_domain = sys_settings.base_shortlink_domain.rstrip("/")
    now_rfc = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    offers_stmt = (
        select(ProductOffer)
        .options(selectinload(ProductOffer.clone))
        .order_by(desc(ProductOffer.detected_at))
        .limit(100)
    )
    offers = list((await session.execute(offers_stmt)).scalars().all())

    rss_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">',
        "  <channel>",
        "    <title>Ofertas Verificadas y Cursos con Descuento | SwarmEngine 50</title>",
        f"    <link>{xml_escape(base_domain)}/</link>",
        "    <description>Descuentos en tiempo real en programas certificados, oficios rentables, tecnología y negocios.</description>",
        "    <language>es</language>",
        f"    <lastBuildDate>{now_rfc}</lastBuildDate>",
        f'    <atom:link href="{xml_escape(base_domain)}/rss.xml" rel="self" type="application/rss+xml"/>',
        '    <atom:link href="https://pubsubhubbub.appspot.com/" rel="hub"/>',
    ]

    for off in offers:
        offer_url = build_offer_pseo_url(off.short_code, off.title, sys_settings=sys_settings)
        card_file = Path(settings.static_cards_dir) / f"{off.short_code}.jpg"
        img_url = (
            f"{base_domain}/static/cards/{off.short_code}.jpg"
            if card_file.is_file()
            else off.image_url
        )
        pub_date = (
            off.detected_at.strftime("%a, %d %b %Y %H:%M:%S +0000")
            if off.detected_at
            else now_rfc
        )
        desc_text = (
            f"Oferta verificada (-{int(round(off.discount_percent))}% OFF): {off.title} "
            f"por ${off.sale_price:.2f} {off.currency} (Antes ${off.original_price:.2f})."
        )
        rss_lines.extend(
            [
                "    <item>",
                f"      <title>{xml_escape(off.title)} (-{int(round(off.discount_percent))}% OFF)</title>",
                f"      <link>{xml_escape(offer_url)}</link>",
                f"      <guid isPermaLink=\"true\">{xml_escape(offer_url)}</guid>",
                f"      <pubDate>{pub_date}</pubDate>",
                f"      <description>{xml_escape(desc_text)}</description>",
                f'      <enclosure url="{xml_escape(img_url)}" type="image/jpeg" length="150000"/>',
                f'      <media:content url="{xml_escape(img_url)}" medium="image" type="image/jpeg"/>',
                "    </item>",
            ]
        )

    rss_lines.extend(["  </channel>", "</rss>"])
    return Response(content="\n".join(rss_lines), media_type="application/rss+xml; charset=utf-8")


@router.get("/sitemap.xml", response_class=Response)
async def dynamic_sitemap_xml(
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """
    Genera dinámicamente el `sitemap.xml` estándar con todas las URLs de categorías
    y ofertas publicadas para indexación en Google Search Console y Bing Webmaster Tools.
    """
    sys_settings = await ensure_system_settings(session)
    base_domain = sys_settings.base_shortlink_domain.rstrip("/")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    clones_stmt = select(BotClone).where(BotClone.is_active.is_(True)).order_by(BotClone.id.asc())
    clones = list((await session.execute(clones_stmt)).scalars().all())

    offers_stmt = select(ProductOffer).order_by(desc(ProductOffer.detected_at)).limit(5000)
    offers = list((await session.execute(offers_stmt)).scalars().all())

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        "  <url>",
        f"    <loc>{xml_escape(base_domain)}/</loc>",
        f"    <lastmod>{now_iso}</lastmod>",
        "    <changefreq>hourly</changefreq>",
        "    <priority>1.0</priority>",
        "  </url>",
    ]

    for clone in clones:
        cat_url = build_category_pseo_url(clone.pseo_category_slug, sys_settings=sys_settings)
        xml_lines.extend(
            [
                "  <url>",
                f"    <loc>{xml_escape(cat_url)}</loc>",
                f"    <lastmod>{now_iso}</lastmod>",
                "    <changefreq>hourly</changefreq>",
                "    <priority>0.8</priority>",
                "  </url>",
            ]
        )

    for offer in offers:
        offer_url = build_offer_pseo_url(offer.short_code, offer.title, sys_settings=sys_settings)
        offer_date = (
            offer.detected_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if offer.detected_at
            else now_iso
        )
        xml_lines.extend(
            [
                "  <url>",
                f"    <loc>{xml_escape(offer_url)}</loc>",
                f"    <lastmod>{offer_date}</lastmod>",
                "    <changefreq>daily</changefreq>",
                "    <priority>0.9</priority>",
                "  </url>",
            ]
        )

    xml_lines.append("</urlset>")
    xml_body = "\n".join(xml_lines)
    return Response(content=xml_body, media_type="application/xml; charset=utf-8")


@router.get("/{indexnow_key}.txt", response_class=PlainTextResponse)
async def indexnow_verification_key_file(indexnow_key: str) -> PlainTextResponse:
    """Sirve el archivo de verificación pública exigido por el protocolo IndexNow."""
    if indexnow_key.strip() != settings.indexnow_api_key.strip():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    return PlainTextResponse(content=settings.indexnow_api_key.strip(), status_code=200)
