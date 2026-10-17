import asyncio
from decimal import Decimal
from io import BytesIO
import logging
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.services.gemini_engine import format_currency_amount

logger = logging.getLogger(__name__)
settings = get_settings()

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1080
PRODUCT_BOX_SIZE = 700
PRODUCT_BOX_RADIUS = 36


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Carga fuentes TrueType DejaVuSans instaladas en el sistema Linux/Docker,
    con fallback seguro a la fuente vectorial interna de Pillow.
    """
    font_candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )

    for path in font_candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue

    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.2, min=1.0, max=8.0),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
)
async def download_product_image_bytes(image_url: str) -> bytes:
    """Descarga los bytes reales de la imagen del producto usando httpx con reintento exponencial."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    timeout = httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        response = await client.get(image_url)
        response.raise_for_status()
        return response.content


def _create_dark_gradient_background(width: int, height: int) -> Image.Image:
    """
    Genera un lienzo RGB de 1080x1080 con fondo degradado oscuro moderno
    (azul medianoche profundo a carbón espacial con acento radial sutil).
    """
    base = Image.new("RGB", (width, height), (11, 15, 29))
    draw = ImageDraw.Draw(base)

    top_color = (15, 23, 42)     # Slate 900
    bottom_color = (7, 10, 20)   # Deep Onyx

    for y in range(height):
        ratio = y / float(height - 1)
        r = int(top_color[0] * (1.0 - ratio) + bottom_color[0] * ratio)
        g = int(top_color[1] * (1.0 - ratio) + bottom_color[1] * ratio)
        b = int(top_color[2] * (1.0 - ratio) + bottom_color[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    return base


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    """Devuelve el ancho y alto en píxeles de un texto renderizado."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_text_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 2,
) -> List[str]:
    """Ajusta un texto en múltiples líneas sin exceder `max_width` píxeles."""
    words = text.strip().split()
    if not words:
        return []

    lines: List[str] = []
    current_line = words[0]

    for word in words[1:]:
        test_line = f"{current_line} {word}"
        w, _ = _measure_text(draw, test_line, font)
        if w <= max_width:
            current_line = test_line
        else:
            lines.append(current_line)
            current_line = word
            if len(lines) >= max_lines - 1:
                break

    lines.append(current_line)
    return lines[:max_lines]


def _render_card_sync(
    product_image_bytes: bytes,
    hook_headline: str,
    original_price: Decimal,
    sale_price: Decimal,
    discount_percent: Decimal,
    currency: str,
    niche_name: str,
    short_code: str,
    output_dir: Optional[Path] = None,
) -> Tuple[BytesIO, Path]:
    """
    Construye en memoria la tarjeta promocional de 1080x1080px con Pillow:
    - Lienzo RGB 1080x1080 con degradado oscuro moderno.
    - Recuadro blanco limpio de 700x700 con bordes redondeados y sombra suave centrado arriba.
    - Foto del producto centrada y escalada con filtro LANCZOS dentro del recuadro de 700x700.
    - Insignia roja brillante en la esquina superior derecha con `-{discount_percent}% OFF`.
    - Franja inferior con `hook_headline`, precio anterior tachado en gris y precio de oferta en verde/amarillo.
    - Exporta BytesIO en formato JPEG calidad 90 y guarda copia en `/static/cards/{short_code}.jpg`.
    """
    canvas = _create_dark_gradient_background(CANVAS_WIDTH, CANVAS_HEIGHT).convert("RGBA")

    # Coordenadas del recuadro blanco de 700x700 centrado horizontalmente
    box_x = (CANVAS_WIDTH - PRODUCT_BOX_SIZE) // 2  # 190
    box_y = 65
    box_rect = (box_x, box_y, box_x + PRODUCT_BOX_SIZE, box_y + PRODUCT_BOX_SIZE)

    # Sombra suave detrás del recuadro de 700x700
    shadow_layer = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.rounded_rectangle(
        (box_x - 6, box_y + 10, box_x + PRODUCT_BOX_SIZE + 6, box_y + PRODUCT_BOX_SIZE + 18),
        radius=PRODUCT_BOX_RADIUS + 4,
        fill=(0, 0, 0, 140),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=16))
    canvas = Image.alpha_composite(canvas, shadow_layer)

    # Crear el recuadro blanco limpio de 700x700 con bordes redondeados
    white_box = Image.new("RGBA", (PRODUCT_BOX_SIZE, PRODUCT_BOX_SIZE), (0, 0, 0, 0))
    mask = Image.new("L", (PRODUCT_BOX_SIZE, PRODUCT_BOX_SIZE), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (0, 0, PRODUCT_BOX_SIZE, PRODUCT_BOX_SIZE),
        radius=PRODUCT_BOX_RADIUS,
        fill=255,
    )

    box_surface = Image.new("RGBA", (PRODUCT_BOX_SIZE, PRODUCT_BOX_SIZE), (255, 255, 255, 255))

    # Decodificar y centrar la imagen del producto dentro del área de 700x700 (con margen interno de 40px)
    with Image.open(BytesIO(product_image_bytes)) as raw_img:
        prod_img = raw_img.convert("RGBA")
        # Si tiene transparencia, componer sobre blanco
        white_bg = Image.new("RGBA", prod_img.size, (255, 255, 255, 255))
        prod_img = Image.alpha_composite(white_bg, prod_img)

        max_inner_dim = PRODUCT_BOX_SIZE - 80  # 620x620 área útil
        prod_img.thumbnail((max_inner_dim, max_inner_dim), Image.Resampling.LANCZOS)

        offset_x = (PRODUCT_BOX_SIZE - prod_img.width) // 2
        offset_y = (PRODUCT_BOX_SIZE - prod_img.height) // 2
        box_surface.paste(prod_img, (offset_x, offset_y))

    white_box.paste(box_surface, (0, 0), mask=mask)
    canvas.paste(white_box, (box_x, box_y), mask=white_box)

    draw = ImageDraw.Draw(canvas)

    # Borde sutil alrededor del contenedor de 700x700
    draw.rounded_rectangle(
        box_rect,
        radius=PRODUCT_BOX_RADIUS,
        outline=(56, 189, 248, 95),
        width=3,
    )

    # Etiqueta superior izquierda con el nombre del micro-nicho
    font_niche = _load_font(size=22, bold=True)
    niche_label = niche_name.upper()[:34]
    niche_w, niche_h = _measure_text(draw, niche_label, font_niche)
    pill_x1, pill_y1 = 54, 24
    pill_x2, pill_y2 = pill_x1 + niche_w + 36, pill_y1 + niche_h + 20
    draw.rounded_rectangle(
        (pill_x1, pill_y1, pill_x2, pill_y2),
        radius=18,
        fill=(30, 41, 59, 235),
        outline=(71, 85, 105, 180),
        width=2,
    )
    draw.text((pill_x1 + 18, pill_y1 + 8), niche_label, font=font_niche, fill=(148, 163, 184, 255))

    # Insignia roja brillante en la esquina superior derecha con `-{discount_percent}% OFF`
    disc_int = int(round(float(discount_percent)))
    badge_text = f"-{disc_int}% OFF"
    font_badge = _load_font(size=44, bold=True)
    bw, bh = _measure_text(draw, badge_text, font_badge)

    badge_pad_x = 34
    badge_pad_y = 22
    badge_w = bw + badge_pad_x * 2
    badge_h = bh + badge_pad_y * 2
    badge_x2 = CANVAS_WIDTH - 48
    badge_x1 = badge_x2 - badge_w
    badge_y1 = 38
    badge_y2 = badge_y1 + badge_h

    # Sombra de la insignia roja
    draw.rounded_rectangle(
        (badge_x1 - 3, badge_y1 + 6, badge_x2 + 3, badge_y2 + 6),
        radius=28,
        fill=(0, 0, 0, 150),
    )
    # Fondo rojo brillante (#EF4444 / RGB 239, 35, 60) con borde blanco
    draw.rounded_rectangle(
        (badge_x1, badge_y1, badge_x2, badge_y2),
        radius=28,
        fill=(239, 35, 60, 255),
        outline=(255, 255, 255, 245),
        width=4,
    )
    draw.text(
        (badge_x1 + badge_pad_x, badge_y1 + badge_pad_y - 4),
        badge_text,
        font=font_badge,
        fill=(255, 255, 255, 255),
    )

    # Franja inferior (y = 790 a 1050)
    footer_top = 790
    draw.rounded_rectangle(
        (48, footer_top, CANVAS_WIDTH - 48, 1044),
        radius=30,
        fill=(17, 24, 39, 235),
        outline=(55, 65, 81, 180),
        width=2,
    )

    # 1. Hook Headline en blanco/amarillo en la parte superior de la franja inferior
    font_hook = _load_font(size=40, bold=True)
    hook_clean = hook_headline.strip().upper() or "OFERTA VERIFICADA HOY"
    hook_lines = _wrap_text_to_width(draw, hook_clean, font_hook, max_width=920, max_lines=1)
    hook_line = hook_lines[0] if hook_lines else hook_clean
    hw, hh = _measure_text(draw, hook_line, font_hook)
    hook_x = (CANVAS_WIDTH - hw) // 2
    hook_y = footer_top + 26
    draw.text((hook_x, hook_y), hook_line, font=font_hook, fill=(250, 204, 21, 255))

    # 2. Precio anterior tachado en gris
    orig_str = f"Antes: {format_currency_amount(original_price, currency)}"
    font_orig = _load_font(size=32, bold=False)
    ow, oh = _measure_text(draw, orig_str, font_orig)
    orig_x = (CANVAS_WIDTH - ow) // 2
    orig_y = hook_y + hh + 24
    gray_color = (156, 163, 175, 255)
    draw.text((orig_x, orig_y), orig_str, font=font_orig, fill=gray_color)

    # Línea de tachado roja/gris sobre el precio anterior
    strike_y = orig_y + (oh // 2) + 4
    draw.line(
        [(orig_x - 6, strike_y), (orig_x + ow + 6, strike_y)],
        fill=(239, 68, 68, 230),
        width=4,
    )

    # 3. Precio de oferta actual en verde neón brillante de gran tamaño
    sale_str = format_currency_amount(sale_price, currency)
    font_sale = _load_font(size=68, bold=True)
    sw, sh = _measure_text(draw, sale_str, font_sale)
    sale_x = (CANVAS_WIDTH - sw) // 2
    sale_y = orig_y + oh + 18
    # Verde esmeralda brillante (#22C55E -> RGB 34, 197, 94)
    draw.text((sale_x, sale_y), sale_str, font=font_sale, fill=(74, 222, 128, 255))

    # Convertir a RGB y exportar a BytesIO en formato JPEG calidad 90
    final_rgb = canvas.convert("RGB")
    buffer = BytesIO()
    final_rgb.save(buffer, format="JPEG", quality=90, optimize=True)
    buffer.seek(0)

    target_dir = output_dir if output_dir is not None else settings.static_cards_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"{short_code}.jpg"
    file_path.write_bytes(buffer.getvalue())
    buffer.seek(0)

    return buffer, file_path


async def generate_offer_card(
    image_url: str,
    hook_headline: str,
    original_price: Decimal,
    sale_price: Decimal,
    discount_percent: Decimal,
    currency: str,
    niche_name: str,
    short_code: str,
) -> Tuple[BytesIO, Path]:
    """
    Descarga asíncronamente la foto original del producto con `httpx`,
    genera la tarjeta de 1080x1080px en un hilo separado para no bloquear el event loop,
    guarda el archivo en `/static/cards/{short_code}.jpg` y retorna `(BytesIO, Path)`.
    """
    product_bytes = await download_product_image_bytes(image_url)
    card_buffer, saved_path = await asyncio.to_thread(
        _render_card_sync,
        product_bytes,
        hook_headline,
        original_price,
        sale_price,
        discount_percent,
        currency,
        niche_name,
        short_code,
        settings.static_cards_dir,
    )
    logger.info("Tarjeta promocional 1080x1080 generada para short_code=%s en %s", short_code, saved_path)
    return card_buffer, saved_path
