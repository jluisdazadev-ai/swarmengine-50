import asyncio
from datetime import date
from decimal import Decimal
import html
import logging
import re
from typing import Optional, Tuple

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.models import BotClone, DailyCloneMetrics, SystemSettings
from app.services.harvester import HarvestedCandidate
from app.services.seeder import ensure_system_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Costos oficiales por token de Gemini 2.5 Flash
GEMINI_INPUT_TOKEN_COST_USD = Decimal("0.00000015")
GEMINI_OUTPUT_TOKEN_COST_USD = Decimal("0.00000060")


class BudgetExceededException(RuntimeError):
    """Excepción lanzada cuando el gasto diario acumulado en Gemini supera daily_global_budget_usd."""


class GeneratedOfferContent(BaseModel):
    """Esquema Pydantic para Structured Outputs de Gemini 2.5 Flash."""

    hook_headline: str = Field(
        ...,
        description="Titular de máximo 5 palabras de alto impacto visual para la tarjeta gráfica 1080x1080.",
    )
    telegram_post_html: str = Field(
        ...,
        description=(
            "Post para Telegram usando únicamente etiquetas HTML válidas (<b>, <i>, <s>), "
            "emojis moderados, precio antes vs. ahora, ahorro en dinero real y llamado a la acción directo. "
            "Máximo 55 palabras, tono experto humano, prohibido sonar robótico."
        ),
    )
    seo_meta_description: str = Field(
        ...,
        description="Meta descripción SEO persuasiva de hasta 150 caracteres para Google Search.",
    )
    seo_mini_review_html: str = Field(
        ...,
        description=(
            "Mini artículo reseña de aproximadamente 250 palabras en HTML limpio (<h3>, <p>, <ul>, <li>, <strong>) "
            "explicando por qué vale la pena el descuento, especificaciones clave y para quién es ideal."
        ),
    )


def format_currency_amount(amount: Decimal, currency: str) -> str:
    """Formatea precios según convenciones de moneda latinoamericana o USD."""
    curr = currency.upper().strip()
    if curr in {"COP", "CLP", "ARS", "PYG"}:
        formatted_int = f"{int(round(amount)):,}".replace(",", ".")
        return f"${formatted_int} {curr}"
    return f"${amount:,.2f} {curr}"


def sanitize_telegram_html(raw_html: str) -> str:
    """
    Limpia el HTML generado para garantizar compatibilidad 100% con el parser HTML de Telegram.
    Telegram solo permite <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>, <code>, <pre>, <a href="">.
    """
    text = raw_html.strip()
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "", text, flags=re.IGNORECASE)

    allowed_tags_pattern = re.compile(
        r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre)(?:\s+[^>]*)?>",
        flags=re.IGNORECASE,
    )

    placeholders = {}

    def _preserve_tag(match: re.Match) -> str:
        key = f"__TG_TAG_{len(placeholders)}__"
        tag_str = match.group(0).lower()
        if tag_str.startswith("<b"):
            normalized = "<b>"
        elif tag_str.startswith("</b") or tag_str.startswith("</strong"):
            normalized = "</b>"
        elif tag_str.startswith("<strong"):
            normalized = "<b>"
        elif tag_str.startswith("<i") or tag_str.startswith("<em"):
            normalized = "<i>"
        elif tag_str.startswith("</i") or tag_str.startswith("</em"):
            normalized = "</i>"
        elif tag_str.startswith("<s") or tag_str.startswith("<strike") or tag_str.startswith("<del"):
            normalized = "<s>"
        elif tag_str.startswith("</s") or tag_str.startswith("</strike") or tag_str.startswith("</del"):
            normalized = "</s>"
        elif tag_str.startswith("<u") or tag_str.startswith("<ins"):
            normalized = "<u>"
        elif tag_str.startswith("</u") or tag_str.startswith("</ins"):
            normalized = "</u>"
        elif tag_str.startswith("<code"):
            normalized = "<code>"
        elif tag_str.startswith("</code"):
            normalized = "</code>"
        else:
            normalized = tag_str
        placeholders[key] = normalized
        return key

    temp_text = allowed_tags_pattern.sub(_preserve_tag, text)
    temp_text = re.sub(r"<[^>]+>", "", temp_text)
    escaped_text = html.escape(temp_text, quote=False)

    for key, tag_val in placeholders.items():
        escaped_text = escaped_text.replace(key, tag_val)

    escaped_text = re.sub(r"\n{3,}", "\n\n", escaped_text).strip()
    return escaped_text


def enforce_hook_word_limit(headline: str, max_words: int = 5) -> str:
    """Garantiza que el hook_headline tenga como máximo 5 palabras de alto impacto."""
    cleaned = re.sub(r"\s+", " ", headline.strip())
    words = cleaned.split(" ")
    if len(words) <= max_words:
        return cleaned.upper()
    return " ".join(words[:max_words]).upper()


async def check_daily_budget_available(
    session: AsyncSession,
    sys_settings: Optional[SystemSettings] = None,
) -> Tuple[bool, Decimal, Decimal]:
    """
    Consulta el gasto acumulado de hoy en `DailyCloneMetrics.ai_cost_usd`
    y verifica si está por debajo de `daily_global_budget_usd`.
    Devuelve `(is_available, current_spend_usd, budget_limit_usd)`.
    """
    if sys_settings is None:
        sys_settings = await ensure_system_settings(session)

    budget_limit = Decimal(str(sys_settings.daily_global_budget_usd or settings.daily_global_budget_usd))
    today = date.today()

    stmt = select(func.coalesce(func.sum(DailyCloneMetrics.ai_cost_usd), Decimal("0.000000"))).where(
        DailyCloneMetrics.date == today
    )
    current_spend = Decimal(str((await session.execute(stmt)).scalar_one()))
    return (current_spend < budget_limit, current_spend, budget_limit)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2.0, max=15.0),
    retry=retry_if_exception_type(Exception),
)
def _invoke_gemini_structured_offer(
    api_key: str,
    prompt: str,
) -> types.GenerateContentResponse:
    """Ejecuta la llamada real al SDK `google-genai` con `gemini-2.5-flash` y `response_schema`."""
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=settings.gemini_model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GeneratedOfferContent,
            temperature=0.65,
        ),
    )
    return response


async def record_gemini_token_usage(
    session: AsyncSession,
    clone_id: int,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    """
    Calcula el costo exacto en USD (`input * $0.00000015 + output * $0.00000060`)
    y actualiza atómicamente `DailyCloneMetrics` para el `clone_id` en la fecha actual.
    """
    in_dec = Decimal(input_tokens) * GEMINI_INPUT_TOKEN_COST_USD
    out_dec = Decimal(output_tokens) * GEMINI_OUTPUT_TOKEN_COST_USD
    exact_cost = (in_dec + out_dec).quantize(Decimal("0.000001"))

    today = date.today()
    upsert_stmt = (
        pg_insert(DailyCloneMetrics)
        .values(
            clone_id=clone_id,
            date=today,
            offers_published=0,
            total_clicks=0,
            gemini_input_tokens=input_tokens,
            gemini_output_tokens=output_tokens,
            ai_cost_usd=exact_cost,
            confirmed_commissions_usd=Decimal("0.00"),
        )
        .on_conflict_do_update(
            constraint="uq_daily_clone_metrics_clone_date",
            set_={
                "gemini_input_tokens": DailyCloneMetrics.gemini_input_tokens + input_tokens,
                "gemini_output_tokens": DailyCloneMetrics.gemini_output_tokens + output_tokens,
                "ai_cost_usd": DailyCloneMetrics.ai_cost_usd + exact_cost,
            },
        )
    )
    await session.execute(upsert_stmt)
    await session.commit()
    return exact_cost


async def generate_offer_copy_with_gemini(
    session: AsyncSession,
    clone: BotClone,
    candidate: HarvestedCandidate,
    sys_settings: Optional[SystemSettings] = None,
) -> Tuple[GeneratedOfferContent, int, int, Decimal]:
    """
    Genera el contenido estructurado de una oferta (`GeneratedOfferContent`) con Gemini 2.5 Flash,
    contabiliza los tokens de entrada y salida desde `response.usage_metadata`
    y registra el costo real en `DailyCloneMetrics`.
    """
    if sys_settings is None:
        sys_settings = await ensure_system_settings(session)

    is_under_budget, current_spend, budget_limit = await check_daily_budget_available(
        session=session,
        sys_settings=sys_settings,
    )
    if not is_under_budget:
        raise BudgetExceededException(
            f"Presupuesto diario global de Gemini agotado (${current_spend:.4f} / ${budget_limit:.2f} USD)."
        )

    api_key = (sys_settings.gemini_api_key or settings.gemini_api_key).strip()
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY no está configurada en SystemSettings ni en variables de entorno."
        )

    savings_amount = candidate.original_price - candidate.sale_price
    orig_formatted = format_currency_amount(candidate.original_price, candidate.currency)
    sale_formatted = format_currency_amount(candidate.sale_price, candidate.currency)
    savings_formatted = format_currency_amount(savings_amount, candidate.currency)

    is_hotmart = candidate.source_marketplace.value == "HOTMART"
    market_context_rules = (
        "CONTEXTO DE MERCADO (ESTRATEGIA MULTI-PAÍS HISPANO HIGH-TICKET):\n"
        "- Tu audiencia compra desde EE.UU. (US Latinos), México, España, Colombia y Chile.\n"
        "- Usa español neutro internacional de alta conversión (evita modismos cerrados de un solo país).\n"
        "- Resalta que el programa incluye acceso online inmediato 24/7, Certificado Internacional de Finalización, "
        "Garantía de Satisfacción de 7 días respaldada por Hotmart y pago seguro en moneda local "
        "(Tarjeta Débito/Crédito, PayPal, OXXO/SPEI en México, PSE/Nequi en Colombia, WebPay/Sencillito en Chile).\n"
        "- Enfócate en el retorno de inversión (ROI): cómo esta habilidad u oficio permite generar ingresos reales o escalar un negocio.\n\n"
        if is_hotmart
        else ""
    )

    prompt = (
        f"Eres el redactor titular del canal especializado '{clone.niche_name}'.\n"
        f"Tu estilo y personalidad obligatoria: {clone.persona_prompt_style}\n\n"
        f"{market_context_rules}"
        "DATOS REALES VERIFICADOS DEL PRODUCTO EN OFERTA:\n"
        f"- Producto: {candidate.title}\n"
        f"- Plataforma / Tienda: {candidate.source_marketplace.value}\n"
        f"- Precio anterior: {orig_formatted}\n"
        f"- Precio de oferta hoy: {sale_formatted}\n"
        f"- Descuento verificado: -{candidate.discount_percent}%\n"
        f"- Ahorro neto en dinero real: {savings_formatted}\n\n"
        "REGLAS ESTRICTAS DE GENERACIÓN:\n"
        "1. `hook_headline`: Máximo 5 palabras vibrantes y directas para estampar en la tarjeta gráfica (ej. 'CERTIFÍCATE HOY A MITAD PRECIO' o 'DOMINA EL OFICIO MÁS RENTABLE').\n"
        "2. `telegram_post_html`: Máximo 55 palabras. Usa únicamente etiquetas HTML válidas en Telegram (`<b>`, `<i>`, `<s>`). "
        "Incluye el precio anterior tachado con `<s>`, el precio actual en `<b>`, el ahorro exacto en dinero real "
        f"({savings_formatted}) y un llamado a la acción directo al final. Prohibido sonar robótico o usar clichés como 'En el vertiginoso mundo de...'.\n"
        "3. `seo_meta_description`: Exactamente entre 120 y 150 caracteres optimizados para CTR orgánico en Google (EE.UU., México, España, Colombia y Chile).\n"
        "4. `seo_mini_review_html`: Alrededor de 250 palabras en HTML semántico limpio (`<h3>`, `<p>`, `<ul>`, `<li>`, `<strong>`) "
        "explicando por qué vale la pena este descuento, qué aprenderá o logrará el comprador, el retorno de inversión (ROI), "
        "la garantía de 7 días y para qué perfil de estudiante o profesional en EE.UU., México, España, Colombia o Chile es ideal."
    )

    response = await asyncio.to_thread(
        _invoke_gemini_structured_offer,
        api_key,
        prompt,
    )

    parsed_content = GeneratedOfferContent.model_validate_json(response.text)
    parsed_content.hook_headline = enforce_hook_word_limit(parsed_content.hook_headline, max_words=5)
    parsed_content.telegram_post_html = sanitize_telegram_html(parsed_content.telegram_post_html)
    parsed_content.seo_meta_description = parsed_content.seo_meta_description.strip()[:155]

    usage = response.usage_metadata
    prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    candidate_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0

    cost_usd = await record_gemini_token_usage(
        session=session,
        clone_id=clone.id,
        input_tokens=prompt_tokens,
        output_tokens=candidate_tokens,
    )

    logger.info(
        "[%s] Gemini 2.5 Flash redactó oferta '%s' | Tokens In=%d Out=%d | Costo=$%s USD",
        clone.clone_code,
        candidate.external_item_id,
        prompt_tokens,
        candidate_tokens,
        f"{cost_usd:.6f}",
    )

    return parsed_content, prompt_tokens, candidate_tokens, cost_usd
