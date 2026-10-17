from datetime import date, datetime, timezone
from decimal import Decimal
import enum
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MarketplaceEnum(str, enum.Enum):
    """Marketplaces soportados por el cosechador multi-tienda."""

    MERCADOLIBRE = "MERCADOLIBRE"
    AMAZON = "AMAZON"
    ALIEXPRESS = "ALIEXPRESS"


class SystemSettings(Base):
    """Configuración global persistida en PostgreSQL para administración dinámica en caliente."""

    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gemini_api_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    telegram_bot_token: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    admin_telegram_chat_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    evolution_api_url: Mapped[str] = mapped_column(String(255), nullable=False, default="http://localhost:8080")
    evolution_api_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    base_shortlink_domain: Mapped[str] = mapped_column(
        String(255), nullable=False, default="https://ofertas.swarmengine.pro"
    )
    mercadolibre_country_site: Mapped[str] = mapped_column(String(10), nullable=False, default="MCO")
    mercadolibre_affiliate_matt_tool: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    amazon_partner_tag: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    aliexpress_tracking_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    daily_global_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("1.50")
    )


class BotClone(Base):
    """Representa cada uno de los 50 Micro-Bots especializados por nicho."""

    __tablename__ = "bot_clones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clone_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    niche_name: Mapped[str] = mapped_column(String(150), nullable=False)
    search_keywords: Mapped[List[str]] = mapped_column(ARRAY(String(120)), nullable=False, default=list)
    negative_keywords: Mapped[List[str]] = mapped_column(ARRAY(String(120)), nullable=False, default=list)
    min_discount_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    min_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    max_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("99999999.00"))
    country_site: Mapped[str] = mapped_column(String(10), nullable=False, default="MCO")
    affiliate_sub_id: Mapped[str] = mapped_column(String(100), nullable=False)
    telegram_channel_id: Mapped[str] = mapped_column(String(120), nullable=False)
    whatsapp_group_jid: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    pseo_category_slug: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    persona_prompt_style: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # Relaciones
    offers: Mapped[List["ProductOffer"]] = relationship(
        "ProductOffer", back_populates="clone", cascade="all, delete-orphan"
    )
    click_events: Mapped[List["ClickEvent"]] = relationship(
        "ClickEvent", back_populates="clone", cascade="all, delete-orphan"
    )
    daily_metrics: Mapped[List["DailyCloneMetrics"]] = relationship(
        "DailyCloneMetrics", back_populates="clone", cascade="all, delete-orphan"
    )


class ProductOffer(Base):
    """Ofertas reales detectadas, redactadas con IA y publicadas por un Micro-Bot."""

    __tablename__ = "product_offers"
    __table_args__ = (
        UniqueConstraint("clone_id", "external_item_id", name="uq_clone_external_item"),
        Index("ix_product_offers_detected_at", "detected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clone_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bot_clones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_item_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source_marketplace: Mapped[MarketplaceEnum] = mapped_column(
        SqlEnum(MarketplaceEnum, name="marketplace_enum", native_enum=False),
        nullable=False,
        default=MarketplaceEnum.MERCADOLIBRE,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sale_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="COP")
    raw_product_url: Mapped[str] = mapped_column(Text, nullable=False)
    affiliate_full_url: Mapped[str] = mapped_column(Text, nullable=False)
    short_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    image_url: Mapped[str] = mapped_column(Text, nullable=False)
    ai_telegram_caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_seo_article_html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_telegram: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_whatsapp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    indexed_google: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # Relaciones
    clone: Mapped["BotClone"] = relationship("BotClone", back_populates="offers")
    clicks: Mapped[List["ClickEvent"]] = relationship(
        "ClickEvent", back_populates="offer", cascade="all, delete-orphan"
    )


class ClickEvent(Base):
    """Telemetría individual en tiempo real de cada clic en /go/{short_code}."""

    __tablename__ = "click_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    offer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("product_offers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    clone_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bot_clones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    visitor_ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    referer: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # Relaciones
    offer: Mapped["ProductOffer"] = relationship("ProductOffer", back_populates="clicks")
    clone: Mapped["BotClone"] = relationship("BotClone", back_populates="click_events")


class DailyCloneMetrics(Base):
    """Contabilidad financiera y de tráfico diaria por cada Micro-Bot."""

    __tablename__ = "daily_clone_metrics"
    __table_args__ = (
        UniqueConstraint("clone_id", "date", name="uq_daily_clone_metrics_clone_date"),
        Index("ix_daily_clone_metrics_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clone_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bot_clones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    offers_published: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gemini_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gemini_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0.000000")
    )
    confirmed_commissions_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )

    # Relaciones
    clone: Mapped["BotClone"] = relationship("BotClone", back_populates="daily_metrics")
