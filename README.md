# SwarmEngine 50 — Plataforma de Enjambre de 50 Micro-Bots Automatizados de Afiliados y pSEO

Sistema autónomo en **Python 3.11 + FastAPI + SQLAlchemy 2.0 Async + PostgreSQL 16 + Redis 7.2 + APScheduler + Gemini 2.5 Flash + Pillow** diseñado para operar 50 micro-nichos de afiliados y posicionamiento programático (pSEO) en un solo VPS consumiendo **menos de 1.2 GB de RAM**.

---

## Arquitectura de Módulos

1. **Semilla Automática (`app/services/seeder.py`)**: Siembra los 50 micro-nichos iniciales (`bot_01` a `bot_50`) con sus palabras clave, palabras negativas, descuentos mínimos y estilos de redacción, o genera lotes de 10 nuevos micro-bots con Gemini 2.5 Flash.
2. **Cosechador Real Multi-Tienda (`app/services/harvester.py`)**: Consultas HTTP asíncronas reales a **MercadoLibre API**, **AliExpress Open Platform** (firma HMAC-MD5) y **Amazon PA-API 5.0** (firma AWSv4 HMAC-SHA256) con reintentos exponenciales (`tenacity`) y ventana anti-duplicados de 15 días.
3. **Motor de Afiliados y Redireccionador (`app/services/affiliate.py` & `app/routers/redirect.py`)**: Inyección de `affiliate_sub_id` por cada micro-bot, generación de códigos cortos de 7 caracteres (`/go/{short_code}`), caché en Redis y telemetría asíncrona de clics (`ClickEvent` y `DailyCloneMetrics`).
4. **Redactor Multimodal con Gemini 2.5 Flash (`app/services/gemini_engine.py`)**: Salidas estructuradas (`response_schema` Pydantic) con control de presupuesto diario global y contabilización exacta de tokens (`usage_metadata`).
5. **Generador Gráfico con Pillow (`app/services/image_card.py`)**: Tarjetas promocionales de `1080x1080px` en memoria (`BytesIO`) con foto del producto centrada en contenedor de `700x700`, badge `-XX% OFF`, titular de impacto y precios antes/ahora.
6. **Publicador Multicanal (`app/services/publisher.py`)**: Publicación automática en **Telegram** (`sendPhoto` + botón inline) y **WhatsApp** vía **Evolution API** (`sendMedia`).
7. **Indexador Instantáneo (`app/services/indexer.py`)**: Notificación automática a **Google Indexing API** y **IndexNow API** (Bing/Yandex).
8. **Motor Darwiniano y Panel Administrativo (`app/services/darwin.py`, `app/routers/dashboard.py`, `app/routers/pseo.py`)**: Auto-poda mensual de clones sin tracción, replicación de los Top 5 ganadores, portal público pSEO con `Schema.org/Product` JSON-LD (`/`, `/c/{slug}`, `/oferta/{short_code}-{slug}`, `/sitemap.xml`) y centro de comando en `/admin`.

---

## Despliegue con Docker Compose

```bash
# 1. (Opcional) Crear archivo .env personalizado
cp .env.example .env

# 2. Construir y levantar PostgreSQL 16, Redis 7.2 y SwarmEngine 50
docker compose up -d --build

# 3. Verificar estado de los servicios
curl http://localhost:8000/health
```

- **Portal Público pSEO:** `http://localhost:8000/`
- **Sitemap XML Dinámico:** `http://localhost:8000/sitemap.xml`
- **Panel de Control Administrativo:** `http://localhost:8000/admin` (Usuario por defecto: `admin` / Contraseña: `SwarmAdmin2026!`)
