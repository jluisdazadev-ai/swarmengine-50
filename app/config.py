import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Configuración global del sistema SwarmEngine 50 cargada desde variables de entorno o archivo .env."""

    model_config = SettingsConfigDict(
        env_file=os.path.join(BASE_DIR, ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Identificación de la aplicación
    app_name: str = Field(default="SwarmEngine 50", description="Nombre de la plataforma")
    environment: str = Field(default="production", description="Entorno de ejecución")
    debug: bool = Field(default=False, description="Modo de depuración")
    secret_key: str = Field(
        default="swarmengine50-super-secret-signing-key-change-in-prod-2026",
        description="Clave secreta para firmar sesiones del panel administrativo",
    )
    admin_username: str = Field(default="admin", description="Usuario del panel administrativo")
    admin_password: str = Field(default="SwarmAdmin2026!", description="Contraseña del panel administrativo")

    # Base de Datos PostgreSQL 16 (SQLAlchemy 2.0 Async + asyncpg)
    database_url: str = Field(
        default="postgresql+asyncpg://swarm_admin:SwarmSecurePass2026!@localhost:5432/swarmengine50",
        description="DSN asíncrono de conexión a PostgreSQL",
    )
    db_pool_size: int = Field(default=10, description="Tamaño del pool de conexiones SQLAlchemy")
    db_max_overflow: int = Field(default=10, description="Conexiones adicionales máximas en picos")

    # Redis para caché de redirecciones y bloqueo distribuido de tareas
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="URL de conexión a Redis",
    )

    # Credenciales por defecto para inicialización de SystemSettings
    gemini_api_key: str = Field(default="", description="API Key de Google Gemini (google-genai)")
    gemini_model_name: str = Field(default="gemini-2.5-flash", description="Modelo Gemini para generación estructurada")
    gemini_input_cost_per_token: float = Field(default=0.00000015, description="Costo USD por token de entrada")
    gemini_output_cost_per_token: float = Field(default=0.00000060, description="Costo USD por token de salida")

    telegram_bot_token: str = Field(default="", description="Token HTTP de BotFather para Telegram")
    admin_telegram_chat_id: str = Field(default="", description="Chat ID del administrador para alertas")

    evolution_api_url: str = Field(default="http://localhost:8080", description="URL base de Evolution API para WhatsApp")
    evolution_api_key: str = Field(default="", description="Global API Key de Evolution API")
    evolution_instance_name: str = Field(default="swarm_master", description="Nombre de instancia en Evolution API")

    base_shortlink_domain: str = Field(
        default="https://ofertas.swarmengine.pro",
        description="Dominio público base para enlaces cortos /go/{short_code} y portal pSEO",
    )

    # Parámetros de Marketplaces y Afiliados
    mercadolibre_country_site: str = Field(default="MCO", description="Sitio de MercadoLibre por defecto (MCO, MLM, MLA, MLC)")
    mercadolibre_affiliate_matt_tool: str = Field(default="matt_tool_swarm50", description="Parámetro matt_tool de afiliado MercadoLibre")
    mercadolibre_affiliate_matt_word: str = Field(default="swarmengine", description="Parámetro matt_word de afiliado MercadoLibre")
    mercadolibre_access_token: Optional[str] = Field(default=None, description="Token OAuth opcional de MercadoLibre")

    amazon_partner_tag: str = Field(default="swarm50-20", description="Partner Tag de Amazon Associates")
    amazon_access_key: str = Field(default="", description="Access Key de Amazon PA-API 5.0")
    amazon_secret_key: str = Field(default="", description="Secret Key de Amazon PA-API 5.0")
    amazon_host: str = Field(default="webservices.amazon.com", description="Host regional de Amazon PA-API 5.0")
    amazon_region: str = Field(default="us-east-1", description="Región de Amazon PA-API 5.0")

    aliexpress_app_key: str = Field(default="", description="App Key de AliExpress Open Platform")
    aliexpress_app_secret: str = Field(default="", description="App Secret de AliExpress Open Platform")
    aliexpress_tracking_id: str = Field(default="swarm50_ali", description="Tracking ID de AliExpress Portals")

    # Indexación en Buscadores (Google Indexing API + IndexNow)
    google_service_account_json_path: str = Field(
        default=str(BASE_DIR / "credentials" / "google_service_account.json"),
        description="Ruta al archivo JSON de Service Account para Google Indexing API",
    )
    indexnow_api_key: str = Field(
        default="f89b2d3c4e5a6f708192a3b4c5d6e7f8",
        description="Clave hexadecimal de verificación para IndexNow (Bing/Yandex)",
    )

    # Presupuesto y límites operativos
    daily_global_budget_usd: float = Field(default=1.50, description="Presupuesto diario máximo en USD para Gemini API")
    duplicate_cooldown_days: int = Field(default=15, description="Días mínimos antes de republicar un mismo producto")
    static_cards_dir: Path = Field(default=BASE_DIR / "static" / "cards", description="Directorio local para tarjetas generadas")


@lru_cache()
def get_settings() -> Settings:
    """Devuelve una instancia en caché de la configuración global."""
    settings = Settings()
    settings.static_cards_dir.mkdir(parents=True, exist_ok=True)
    return settings
