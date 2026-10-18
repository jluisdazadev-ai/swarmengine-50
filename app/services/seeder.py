import asyncio
from datetime import date
from decimal import Decimal
import logging
import re
from typing import Any, Dict, List

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.models import BotClone, DailyCloneMetrics, SystemSettings

logger = logging.getLogger(__name__)
settings = get_settings()


class GeneratedNicheBlueprint(BaseModel):
    """Esquema estructurado para un micro-nicho generado por Gemini 2.5 Flash."""

    niche_name: str = Field(..., description="Nombre comercial atractivo del micro-nicho de Hotmart")
    search_keywords: List[str] = Field(
        ..., min_length=4, max_length=8, description="Palabras clave específicas de alta conversión en Hotmart"
    )
    negative_keywords: List[str] = Field(
        ..., min_length=4, max_length=10, description="Palabras negativas para excluir contenido irrelevante"
    )
    min_discount_percent: int = Field(..., ge=20, le=70, description="Porcentaje mínimo de descuento exigido")
    min_price: float = Field(..., ge=15.0, description="Precio mínimo en USD para asegurar buena comisión")
    max_price: float = Field(..., gt=25.0, description="Precio máximo en USD del nicho")
    pseo_category_slug: str = Field(..., description="Slug URL SEO friendly en minúsculas separado por guiones")
    persona_prompt_style: str = Field(
        ..., description="Personalidad y tono de comunicación del micro-bot redactor"
    )


class GeneratedNicheBatchResponse(BaseModel):
    """Respuesta estructurada de Gemini con el lote de micro-nichos."""

    niches: List[GeneratedNicheBlueprint]


# Catálogo Maestro Opción A: Los 50 Micro-Nichos de Hotmart más rentables para
# EE.UU. Hispano (US Latinos), México, España, Colombia y Chile (Precios base en USD)
INITIAL_50_CLONES: List[Dict[str, Any]] = [
    {
        "clone_code": "bot_01",
        "niche_name": "Instalación y Reparación de Aires Acondicionados (HVAC)",
        "search_keywords": ["aire acondicionado inverter", "refrigeracion y climatizacion", "tecnico minisplit", "reparacion aires acondicionados"],
        "negative_keywords": ["gratis", "pirata", "torrent", "pdf suelto", "usado"],
        "min_discount_percent": 40,
        "min_price": Decimal("25.00"),
        "max_price": Decimal("297.00"),
        "pseo_category_slug": "curso-instalacion-aires-acondicionados-hvac",
        "persona_prompt_style": "Ingeniero maestro en climatización HVAC; destaca cuánto cobra un técnico por instalación en EE.UU., México, España y Colombia y cómo recuperar la inversión en el primer trabajo.",
        "telegram_channel_id": "@ExpertosHVACDeals",
    },
    {
        "clone_code": "bot_02",
        "niche_name": "Instalación de Paneles Solares y Energía Fotovoltaica",
        "search_keywords": ["paneles solares", "energia solar fotovoltaica", "instalador solar", "dimensionamiento solar"],
        "negative_keywords": ["gratis", "pirata", "juguete", "miniatura", "usado"],
        "min_discount_percent": 40,
        "min_price": Decimal("29.00"),
        "max_price": Decimal("350.00"),
        "pseo_category_slug": "curso-instalacion-paneles-solares-fotovoltaicos",
        "persona_prompt_style": "Consultor en energía solar; explica cálculo de inversores, baterías LiFePO4, ahorro eléctrico y alta demanda laboral en EE.UU., México, España y Latinoamérica.",
        "telegram_channel_id": "@EnergiaSolarProDeals",
    },
    {
        "clone_code": "bot_03",
        "niche_name": "Mecánica, Electricidad e Inyección de Motos",
        "search_keywords": ["mecanica de motos", "electricidad de motos", "inyeccion electronica motos", "taller de motos"],
        "negative_keywords": ["gratis", "calcomania", "llavero", "usado"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-mecanica-electricidad-motos",
        "persona_prompt_style": "Jefe de taller de motocicletas; habla de diagnóstico con multímetro, sistema FI, reparación de motores 2T/4T y cómo montar un taller rentable.",
        "telegram_channel_id": "@MecanicaMotosPro",
    },
    {
        "clone_code": "bot_04",
        "niche_name": "Reparación de Celulares y Microelectrónica Avanzada",
        "search_keywords": ["reparacion de celulares", "microelectronica smartphones", "reballing iphone android", "curso tecnico celulares"],
        "negative_keywords": ["forro", "funda", "vidrio templado", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("247.00"),
        "pseo_category_slug": "curso-reparacion-celulares-microelectronica",
        "persona_prompt_style": "Especialista en microsoldadura y laboratorio móvil; resalta lectura de esquemáticos, cambio de IC de carga y márgenes de ganancia del 300% por reparación.",
        "telegram_channel_id": "@LaboratorioMovilPro",
    },
    {
        "clone_code": "bot_05",
        "niche_name": "Porcelanato Líquido, Resina Epóxica y Mesas Río",
        "search_keywords": ["resina epoxica", "porcelanato liquido", "pisos epoxicos 3d", "mesas con resina"],
        "negative_keywords": ["llavero barato", "molde suelto", "gratis"],
        "min_discount_percent": 50,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-resina-epoxica-porcelanato-liquido",
        "persona_prompt_style": "Contratista de acabados de lujo en EE.UU. y Latinoamérica; detalla proporción exacta de catalizador, técnicas sin burbujas y cobro en dólares por metro cuadrado.",
        "telegram_channel_id": "@ResinaEpoxicaNegocio",
    },
    {
        "clone_code": "bot_06",
        "niche_name": "Drywall, Pladur y Construcción en Seco Profesional",
        "search_keywords": ["curso drywall", "construccion en seco", "cielos rasos drywall", "instalacion pladur"],
        "negative_keywords": ["tornillo suelto", "espátula barata", "gratis"],
        "min_discount_percent": 40,
        "min_price": Decimal("20.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-drywall-pladur-construccion-seco",
        "persona_prompt_style": "Contratista de remodelaciones en USA, España y Latam; explica estructura metálica, encintado nivel 5, diseño de luces indirectas LED y presupuestos de obra.",
        "telegram_channel_id": "@DrywallMasterDeals",
    },
    {
        "clone_code": "bot_07",
        "niche_name": "Electricidad Residencial, Comercial y Automatización",
        "search_keywords": ["electricidad residencial", "instalaciones electricas", "tableros electricos", "domotica electricistas"],
        "negative_keywords": ["cinta aislante", "destornillador barato", "gratis"],
        "min_discount_percent": 40,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("220.00"),
        "pseo_category_slug": "curso-electricidad-residencial-industrial",
        "persona_prompt_style": "Ingeniero electricista certificado; enfatiza lectura de planos, normativa NEC/RETIE, cálculo de cargas y seguridad eléctrica.",
        "telegram_channel_id": "@ElectricistasProDeals",
    },
    {
        "clone_code": "bot_08",
        "niche_name": "Car Detailing, Pulido Espejo y Polarizado Automotriz",
        "search_keywords": ["car detailing", "detallado automotriz", "polarizado automotriz", "pintura hidroimpresion", "wrapping automotriz"],
        "negative_keywords": ["trapo microfibra", "ambientador", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-car-detailing-polarizado-wrapping",
        "persona_prompt_style": "Dueño de estudio de Detailing de alta gama; explica corrección de barniz, recubrimiento cerámico 9H y cómo facturar entre $150 y $600 USD por vehículo.",
        "telegram_channel_id": "@DetailingNegocioPro",
    },
    {
        "clone_code": "bot_09",
        "niche_name": "Soldadura Especializada MIG, TIG y Estructuras Metálicas",
        "search_keywords": ["curso soldadura tig", "soldadura mig mag", "estructuras metalicas", "soldador profesional"],
        "negative_keywords": ["electrodo suelto", "gratis", "usado"],
        "min_discount_percent": 40,
        "min_price": Decimal("20.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-soldadura-tig-mig-estructuras",
        "persona_prompt_style": "Inspector de soldadura industrial; detalla control de amperaje, gas argón, cordones limpios en acero inoxidable/aluminio y salidas laborales bien pagadas.",
        "telegram_channel_id": "@SoldadoresProDeals",
    },
    {
        "clone_code": "bot_10",
        "niche_name": "Carpintería en Melamina y Diseño de Cocinas Integrales",
        "search_keywords": ["muebles de melamina", "cocinas integrales", "carpinteria moderna", "closets en melamina"],
        "negative_keywords": ["bisagra suelta", "tornillo", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("20.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-muebles-melamina-cocinas-integrales",
        "persona_prompt_style": "Fabricante de mobiliario moderno; enseña cómo diseñar en SketchUp/Polyboard, pedir cortes optimizados sin tener maquinaria pesada y armar cocinas de alto margen.",
        "telegram_channel_id": "@MelaminaNegocioPro",
    },
    {
        "clone_code": "bot_11",
        "niche_name": "Automatización con Inteligencia Artificial y Agentes IA",
        "search_keywords": ["inteligencia artificial negocios", "automatizacion n8n make", "agentes ia", "chatgpt para empresas"],
        "negative_keywords": ["gratis", "basico", "pirata"],
        "min_discount_percent": 40,
        "min_price": Decimal("29.00"),
        "max_price": Decimal("297.00"),
        "pseo_category_slug": "curso-automatizacion-inteligencia-artificial-agentes",
        "persona_prompt_style": "Arquitecto de automatización IA; explica cómo crear bots de atención y ventas para negocios en EE.UU., España, México y Colombia cobrando igualas mensuales en USD.",
        "telegram_channel_id": "@AgentesIANegocios",
    },
    {
        "clone_code": "bot_12",
        "niche_name": "Excel Avanzado, Macros VBA y Análisis con Power BI",
        "search_keywords": ["excel avanzado", "power bi", "macros vba excel", "analisis de datos"],
        "negative_keywords": ["plantilla gratis", "pirata"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-excel-avanzado-power-bi- certificacion",
        "persona_prompt_style": "Consultor Senior de Business Intelligence; destaca creación de dashboards ejecutivos, tablas dinámicas, DAX y cómo conseguir ascensos o trabajo remoto en USD.",
        "telegram_channel_id": "@ExcelYPowerBIPro",
    },
    {
        "clone_code": "bot_13",
        "niche_name": "Inglés Acelerado para Trabajo Remoto y Latinos en EE.UU.",
        "search_keywords": ["curso de ingles conversacional", "ingles para trabajo", "ingles fluido rapido", "aprender ingles"],
        "negative_keywords": ["diccionario", "gratis", "infantil"],
        "min_discount_percent": 45,
        "min_price": Decimal("29.00"),
        "max_price": Decimal("297.00"),
        "pseo_category_slug": "curso-ingles-fluido-trabajo-estados-unidos",
        "persona_prompt_style": "Coach bilingüe para profesionales y latinos en USA; enfatiza fluidez auditiva sin gramática aburrida, preparación de entrevistas y duplicar ingresos hablando inglés.",
        "telegram_channel_id": "@InglesTrabajoUSD",
    },
    {
        "clone_code": "bot_14",
        "niche_name": "Creación de LLC en EE.UU., Taxes e Inversión para Hispanos",
        "search_keywords": ["crear llc estados unidos", "impuestos usa hispanos", "credito comercial usa", "finanzas e inversiones"],
        "negative_keywords": ["gratis", "estafa"],
        "min_discount_percent": 35,
        "min_price": Decimal("35.00"),
        "max_price": Decimal("397.00"),
        "pseo_category_slug": "curso-llc-estados-unidos-impuestos-inversion",
        "persona_prompt_style": "Asesor fiscal y financiero internacional; explica apertura de cuentas bancarias en USD, optimización legal de impuestos, ITIN/EIN y protección patrimonial.",
        "telegram_channel_id": "@FinanzasYLLCDeals",
    },
    {
        "clone_code": "bot_15",
        "niche_name": "Barbería Profesional, Cortes Fade y Negocio de BarberShop",
        "search_keywords": ["curso de barberia", "barberia profesional", "cortes fade degradado", "colorimetria barbershop"],
        "negative_keywords": ["peinilla", "capa suelta", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("179.00"),
        "pseo_category_slug": "curso-barberia-profesional-fade-barbershop",
        "persona_prompt_style": "Master Barber internacional; resalta técnicas de desvanecido perfecto, perfilado de barba con navaja, fidelización de clientes VIP y alta rentabilidad en USA, España y Latam.",
        "telegram_channel_id": "@MasterBarberDeals",
    },
    {
        "clone_code": "bot_16",
        "niche_name": "Uñas Acrílicas, Polygel y Manicure Ruso Profesional",
        "search_keywords": ["unas acrilicas", "manicure ruso", "curso polygel", "nail art profesional"],
        "negative_keywords": ["lima suelta", "esmalte barato", "gratis"],
        "min_discount_percent": 50,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("169.00"),
        "pseo_category_slug": "curso-unas-acrilicas-polygel-manicure-ruso",
        "persona_prompt_style": "Instructora Master Nail Artist; destaca encapsulados perfectos, cero desprendimiento, velocidad en mesa y cómo llenar la agenda de clientas.",
        "telegram_channel_id": "@UnasProAcademia",
    },
    {
        "clone_code": "bot_17",
        "niche_name": "Diseño de Cejas, Microblading y Extensiones de Pestañas",
        "search_keywords": ["extensiones de pestanas", "pestanas pelo a pelo", "microblading cejas", "laminado de cejas"],
        "negative_keywords": ["pinza barata", "pegante suelto", "gratis"],
        "min_discount_percent": 50,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("179.00"),
        "pseo_category_slug": "curso-cejas-microblading-pestanas-pelo-a-pelo",
        "persona_prompt_style": "Especialista en estética de la mirada; explica visagismo facial, volumen ruso, retención de 5 semanas y ganancias de $50 a $150 USD por sesión.",
        "telegram_channel_id": "@EsteticaMiradaPro",
    },
    {
        "clone_code": "bot_18",
        "niche_name": "Cosmetología, Limpieza Facial Profunda y Skincare",
        "search_keywords": ["limpieza facial profunda", "cosmetologia estetica", "dermapen peeling", "masajes reductores"],
        "negative_keywords": ["algodon", "banda cabello", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("220.00"),
        "pseo_category_slug": "curso-cosmetologia-limpieza-facial-estetica",
        "persona_prompt_style": "Cosmetóloga clínica; detalla protocolos de cabina, aparatología estética, activos dermatológicos y rentabilidad por paciente.",
        "telegram_channel_id": "@CosmetologiaCabinaPro",
    },
    {
        "clone_code": "bot_19",
        "niche_name": "Barismo Profesional, Arte Latte y Negocio de Cafeterías",
        "search_keywords": ["curso de barismo", "arte latte", "montar una cafeteria", "cafe de especialidad"],
        "negative_keywords": ["cuchara", "filtro papel", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("180.00"),
        "pseo_category_slug": "curso-barismo-arte-latte-negocio-cafeteria",
        "persona_prompt_style": "Barista campeón y consultor de cafeterías; explica calibración de molienda, texturización de leche, costeo de bebidas frías/calientes y márgenes del 400%.",
        "telegram_channel_id": "@BaristaNegocioDeals",
    },
    {
        "clone_code": "bot_20",
        "niche_name": "Maestro Parrillero, Cortes de Carne y Ahumados Texanos",
        "search_keywords": ["maestro parrillero", "carnes ahumadas brisket", "asados y parrilla", "hamburguesas artesanales negocio"],
        "negative_keywords": ["pinza", "delantal", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("179.00"),
        "pseo_category_slug": "curso-maestro-parrillero-ahumados-hamburguesas",
        "persona_prompt_style": "Pitmaster y chef parrillero; resalta control de brasas, términos exactos, rubs secretos, brisket ahumado 12h y cómo montar un negocio gastronómico rentable.",
        "telegram_channel_id": "@ParrillaYAhumadosPro",
    },
    {
        "clone_code": "bot_21",
        "niche_name": "Repostería Fina, Pastelería Creativa y Negocio desde Casa",
        "search_keywords": ["reposteria rentable", "pasteleria creativa", "tortas comerciales", "postres en vasito negocio"],
        "negative_keywords": ["molde barato", "vela", "gratis"],
        "min_discount_percent": 50,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("169.00"),
        "pseo_category_slug": "curso-reposteria-rentable-pasteleria-negocio",
        "persona_prompt_style": "Chef pastelera emprendedora; enseña bizcochos húmedos, coberturas estables al clima cálido, estandarización de costos y ventas por encargo en redes.",
        "telegram_channel_id": "@ReposteriaNegocioPro",
    },
    {
        "clone_code": "bot_22",
        "niche_name": "Pastelería Canina y Felina y Nutrición Natural de Mascotas",
        "search_keywords": ["pasteleria canina y felina", "alimentacion natural mascotas", "dieta barf perros", "premios caseros perros"],
        "negative_keywords": ["collar barato", "juguete goma", "gratis"],
        "min_discount_percent": 50,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("149.00"),
        "pseo_category_slug": "curso-pasteleria-canina-felina-nutricion",
        "persona_prompt_style": "Bio-nutricionista veterinario; explica ingredientes 100% seguros aprobados para perros y gatos, tortas de cumpleaños para mascotas y el boom del mercado Pet en USA y Latam.",
        "telegram_channel_id": "@PasteleriaCaninaVIP",
    },
    {
        "clone_code": "bot_23",
        "niche_name": "Adiestramiento Canino Profesional y Peluquería Canina",
        "search_keywords": ["adiestramiento canino", "educacion canina", "peluqueria canina grooming", "etologia canina"],
        "negative_keywords": ["correa barata", "bolsitas", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-adiestramiento-canino-peluqueria-grooming",
        "persona_prompt_style": "Adiestrador K9 y Master Groomer; destaca obediencia en positivo, corrección de ansiedad por separación, cortes por raza y tarifas profesionales por sesión.",
        "telegram_channel_id": "@AdiestramientoK9Pro",
    },
    {
        "clone_code": "bot_24",
        "niche_name": "Impresión 3D, Modelado CAD y Negocio de Piezas 3D",
        "search_keywords": ["impresion 3d negocio", "curso fusion 360 impresion 3d", "impresoras resina y filamento", "modelado 3d"],
        "negative_keywords": ["boquilla suelta", "gratis"],
        "min_discount_percent": 40,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-impresion-3d-modelado-negocio",
        "persona_prompt_style": "Ingeniero de manufactura aditiva; explica calibración de laminadores Orca/PrusaSlicer, diseño de repuestos industriales y cómo monetizar una granja 3D.",
        "telegram_channel_id": "@Impresion3DNegocio",
    },
    {
        "clone_code": "bot_25",
        "niche_name": "Fotografía, Fotogrametría e Inspección Técnica con Drones",
        "search_keywords": ["piloto de drones", "fotogrametria con drones", "topografia drones", "audiovisual con drones"],
        "negative_keywords": ["helice repuesto", "gratis"],
        "min_discount_percent": 40,
        "min_price": Decimal("29.00"),
        "max_price": Decimal("297.00"),
        "pseo_category_slug": "curso-piloto-drones-fotogrametria-topografia",
        "persona_prompt_style": "Piloto comercial certificado de RPAS; detalla levantamientos topográficos 3D, ortomosaicos con Pix4D/Agisoft e inspección de obras y agricultura.",
        "telegram_channel_id": "@DronesProfesionales",
    },
    {
        "clone_code": "bot_26",
        "niche_name": "Cámaras de Seguridad CCTV, Redes IP y Alarmas Inteligentes",
        "search_keywords": ["instalacion camaras de seguridad", "cctv ip dahua hikvision", "alarmas y control de acceso", "redes y cableado estructurado"],
        "negative_keywords": ["conector bnc suelto", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("20.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-instalacion-camaras-seguridad-cctv-ip",
        "persona_prompt_style": "Integrador de seguridad electrónica; enseña configuración de DVR/NVR, cámaras IP PoE, acceso remoto por celular y cotización de proyectos para empresas.",
        "telegram_channel_id": "@SeguridadCCTVNegocio",
    },
    {
        "clone_code": "bot_27",
        "niche_name": "Reparación de Lavadoras, Neveras Inverter y Línea Blanca",
        "search_keywords": ["reparacion de lavadoras", "reparacion de refrigeradores neveras", "tarjetas electronicas lavadoras", "linea blanca inverter"],
        "negative_keywords": ["manguera suelta", "forro", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("220.00"),
        "pseo_category_slug": "curso-reparacion-lavadoras-neveras-inverter",
        "persona_prompt_style": "Técnico senior en electrodomésticos Inverter; explica diagnóstico de placas electrónicas, carga de gas refrigerante y alta demanda de visitas técnicas a domicilio.",
        "telegram_channel_id": "@TecnicosLineaBlanca",
    },
    {
        "clone_code": "bot_28",
        "niche_name": "Diagnóstico Electrónico Automotriz, Escáner y ECUs",
        "search_keywords": ["electronica automotriz", "escaner automotriz", "reparacion ecu computadoras automotrices", "osciloscopio automotriz"],
        "negative_keywords": ["fusible suelto", "gratis"],
        "min_discount_percent": 40,
        "min_price": Decimal("25.00"),
        "max_price": Decimal("297.00"),
        "pseo_category_slug": "curso-electronica-automotriz-escaner-ecu",
        "persona_prompt_style": "Ingeniero mecatrónico automotriz; destaca lectura de redes CAN Bus, programación de llaves/inmovilizadores, prueba de sensores y diagnóstico avanzado.",
        "telegram_channel_id": "@MecatronicaAutoPro",
    },
    {
        "clone_code": "bot_29",
        "niche_name": "Programación Full-Stack, Python y Desarrollo de Software",
        "search_keywords": ["curso python profesional", "desarrollo web full stack", "programacion desde cero", "crear aplicaciones saas"],
        "negative_keywords": ["gratis", "pirata"],
        "min_discount_percent": 40,
        "min_price": Decimal("25.00"),
        "max_price": Decimal("297.00"),
        "pseo_category_slug": "curso-programacion-python-fullstack-saas",
        "persona_prompt_style": "Tech Lead Senior; enfocado en proyectos reales de portafolio, despliegue en producción y cómo conseguir empleos remotos en dólares.",
        "telegram_channel_id": "@DevFullStackDeals",
    },
    {
        "clone_code": "bot_30",
        "niche_name": "Edición de Video Viral (CapCut, Premiere, After Effects)",
        "search_keywords": ["edicion de video profesional", "capcut pro creadores", "premiere y after effects", "editor de videos para youtube"],
        "negative_keywords": ["gratis", "plantilla pirata"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-edicion-video-capcut-premiere-reels",
        "persona_prompt_style": "Director audiovisual y editor de creadores top; explica retención en los primeros 3 segundos, diseño sonoro, subtítulos dinámicos y cómo cobrar por paquetes de Reels.",
        "telegram_channel_id": "@EditoresProDeals",
    },
    {
        "clone_code": "bot_31",
        "niche_name": "Tráfico Pago, Meta Ads, Google Ads y Embudos de Venta",
        "search_keywords": ["meta ads facebook ads", "google ads profesional", "trafico pago", "embudos de venta"],
        "negative_keywords": ["seguidores falsos", "bots likes", "gratis"],
        "min_discount_percent": 40,
        "min_price": Decimal("25.00"),
        "max_price": Decimal("297.00"),
        "pseo_category_slug": "curso-meta-ads-google-ads-trafico-pago",
        "persona_prompt_style": "Media Buyer que gestiona presupuestos reales; habla de ROAS, costo por compra CPA, estructura de campañas y escalado rentable.",
        "telegram_channel_id": "@MediaBuyerProDeals",
    },
    {
        "clone_code": "bot_32",
        "niche_name": "Importaciones de China (Alibaba) y Comercio Electrónico",
        "search_keywords": ["importar de china", "importaciones alibaba", "casillero estados unidos china", "vender en ecommerce"],
        "negative_keywords": ["gratis", "estafa"],
        "min_discount_percent": 40,
        "min_price": Decimal("25.00"),
        "max_price": Decimal("247.00"),
        "pseo_category_slug": "curso-importaciones-china-alibaba-ecommerce",
        "persona_prompt_style": "Agente de carga y comercio exterior; explica verificación de proveedores Gold/Verified en Alibaba, cálculo de aranceles, agentes navieros y márgenes del 100%+.",
        "telegram_channel_id": "@ImportadoresProDeals",
    },
    {
        "clone_code": "bot_33",
        "niche_name": "Bienes Raíces, Remates Judiciales y Renta en Airbnb",
        "search_keywords": ["inversion bienes raices", "negocio airbnb", "remates hipotecarios", "flipping inmobiliario"],
        "negative_keywords": ["gratis", "piramide"],
        "min_discount_percent": 40,
        "min_price": Decimal("29.00"),
        "max_price": Decimal("350.00"),
        "pseo_category_slug": "curso-bienes-raices-airbnb-inversion",
        "persona_prompt_style": "Inversionista inmobiliario; analiza rentabilidad anual ROI, subarriendo legal en Airbnb sin ser propietario y apalancamiento inteligente.",
        "telegram_channel_id": "@InversionRaizDeals",
    },
    {
        "clone_code": "bot_34",
        "niche_name": "Producción Musical, Mezcla, Mastering y Home Studio",
        "search_keywords": ["produccion musical", "mezcla y masterizacion", "fl studio ableton", "aprender piano guitarra"],
        "negative_keywords": ["cable suelto", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-produccion-musical-mezcla-home-studio",
        "persona_prompt_style": "Productor musical e ingeniero de mezcla; destaca ecualización sustractiva, compresión vocal profesional y cómo lanzar música con calidad de estudio desde casa.",
        "telegram_channel_id": "@HomeStudioMasterPro",
    },
    {
        "clone_code": "bot_35",
        "niche_name": "Arquitectura BIM, AutoCAD, Revit y Renderizado 3D",
        "search_keywords": ["curso revit bim", "autocad completo", "lumion vray sketchup", "presupuestos de obra"],
        "negative_keywords": ["crack", "serial", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("247.00"),
        "pseo_category_slug": "curso-revit-bim-autocad-renders-arquitectura",
        "persona_prompt_style": "Arquitecto BIM Manager; explica coordinación de proyectos 3D, cómputos métricos automáticos y renders fotorrealistas que venden proyectos antes de construir.",
        "telegram_channel_id": "@ArquitecturaBIMDeals",
    },
    {
        "clone_code": "bot_36",
        "niche_name": "Hidroponía, Huertos Rentables y Agricultura Moderna",
        "search_keywords": ["cultivo hidroponico", "hidroponia comercial", "forraje verde hidroponico", "abonos organicos"],
        "negative_keywords": ["semilla suelta", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("179.00"),
        "pseo_category_slug": "curso-hidroponia-comercial-cultivos-rentables",
        "persona_prompt_style": "Ingeniero agrónomo especialista en hidroponía NFT; detalla soluciones nutritivas, ahorro del 80% de agua y producción intensiva en espacios pequeños.",
        "telegram_channel_id": "@HidroponiaRentable",
    },
    {
        "clone_code": "bot_37",
        "niche_name": "Jabonería Artesanal, Velas Aromáticas y Cosmética Natural",
        "search_keywords": ["jabones artesanales", "velas aromaticas de soja", "cosmetica natural", "shampoo solido"],
        "negative_keywords": ["mecha suelta", "gratis"],
        "min_discount_percent": 50,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("149.00"),
        "pseo_category_slug": "curso-jabones-artesanales-velas-cosmetica-natural",
        "persona_prompt_style": "Formuladora de cosmética botánica; explica saponificación en frío, aceites esenciales puros, moldes decorativos y creación de marca propia desde casa.",
        "telegram_channel_id": "@CosmeticaNaturalVIP",
    },
    {
        "clone_code": "bot_38",
        "niche_name": " Decoración con Globos, Eventos VIP y Wedding Planner",
        "search_keywords": ["decoracion con globos", "organizacion de eventos", "resina y flores eternas", "globoflexia profesional"],
        "negative_keywords": ["bolsa globos barata", "gratis"],
        "min_discount_percent": 50,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("169.00"),
        "pseo_category_slug": "curso-decoracion-globos-organizacion-eventos",
        "persona_prompt_style": "Event Planner de bodas y eventos corporativos; destaca arcos orgánicos, estructuras sin helio, cotización por evento en dólares y alta demanda todo el año.",
        "telegram_channel_id": "@EventosYGlobosPro",
    },
    {
        "clone_code": "bot_39",
        "niche_name": "Sublimación, Estampado DTF, Vinilo Textil y Personalizados",
        "search_keywords": ["curso sublimacion y estampado", "impresion dtf textil", "cricut y cameo", "negocio de personalizados"],
        "negative_keywords": ["taza suelta", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("169.00"),
        "pseo_category_slug": "curso-sublimacion-dtf-estampado-negocio",
        "persona_prompt_style": "Dueño de taller de estampado textil; explica perfiles de color ICC, tiempos y temperaturas exactas de plancha y producción de prendas personalizadas sin desperdicio.",
        "telegram_channel_id": "@SublimacionYDTFPro",
    },
    {
        "clone_code": "bot_40",
        "niche_name": "Coctelería Clásica, Mixología de Autor y Bartender Pro",
        "search_keywords": ["curso de cocteleria", "bartender profesional", "mixologia molecular", "cocteles para eventos"],
        "negative_keywords": ["pitillo", "servilleta", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("169.00"),
        "pseo_category_slug": "curso-cocteleria-bartender-mixologia",
        "persona_prompt_style": "Head Bartender internacional; detalla balance de almíbares artesanales, técnica de agitado, barras móviles para eventos y trabajo rápido en bares de USA, España y Latam.",
        "telegram_channel_id": "@BartenderMixologia",
    },
    {
        "clone_code": "bot_41",
        "niche_name": "Nutrición Deportiva, Hipertrofia y Entrenamiento Personal",
        "search_keywords": ["nutricion deportiva", "entrenador personal fitness", "hipertrofia muscular", "perdida de grasa"],
        "negative_keywords": ["tarro vacio", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-nutricion-deportiva-entrenamiento-hipertrofia",
        "persona_prompt_style": "Preparador físico y nutricionista basado en ciencia; explica cálculo de macronutrientes, sobrecarga progresiva y periodización real sin dietas extremas.",
        "telegram_channel_id": "@CienciaFitnessDeals",
    },
    {
        "clone_code": "bot_42",
        "niche_name": "Yoga, Pilates, Suelo Pélvico y Bienestar Postural",
        "search_keywords": ["instructor de yoga", "pilates terapeutico", "hipopresivos suelo pelvico", "alivio dolor de espalda"],
        "negative_keywords": ["medias", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-yoga-pilates-terapeutico-bienestar",
        "persona_prompt_style": "Fisioterapeuta e instructora de movimiento consciente; enfatiza rehabilitación postural, respiración, flexibilidad articular y rutinas guiadas en casa.",
        "telegram_channel_id": "@BienestarYPilatesPro",
    },
    {
        "clone_code": "bot_43",
        "niche_name": "Fisioterapia, Masoterapia Clínica y Quiropraxia",
        "search_keywords": ["masoterapia profesional", "masajes descontracturantes", "vendaje neuromuscular kinesiotape", "terapia manual"],
        "negative_keywords": ["aceite barato", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("220.00"),
        "pseo_category_slug": "curso-masoterapia-terapia-manual-fisioterapia",
        "persona_prompt_style": "Terapeuta físico clínico; explica anatomía palpatoria, liberación miofascial, descarga deportiva y cómo atender pacientes privados.",
        "telegram_channel_id": "@MasoterapiaClinica",
    },
    {
        "clone_code": "bot_44",
        "niche_name": "Maternidad, Estimulación Temprana y Sueño Infantil",
        "search_keywords": ["estimulacion temprana bebes", "alimentacion complementaria blw", "sueno infantil bebes", "crianza respetuosa"],
        "negative_keywords": ["babero", "chupo", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("19.00"),
        "max_price": Decimal("169.00"),
        "pseo_category_slug": "curso-estimulacion-temprana-alimentacion-blw-bebes",
        "persona_prompt_style": "Pediatra y asesora de maternidad; transmite tranquilidad, menús seguros sin riesgo de atragantamiento (BLW) y rutinas para que el bebé duerma toda la noche.",
        "telegram_channel_id": "@MaternidadConscientePro",
    },
    {
        "clone_code": "bot_45",
        "niche_name": "Cerrajería Automotriz, Programación de Llaves y Transponder",
        "search_keywords": ["cerrajeria automotriz", "programacion de llaves con chip", "apertura de autos", "inmovilizadores automotrices"],
        "negative_keywords": ["llavero plastico", "gratis"],
        "min_discount_percent": 40,
        "min_price": Decimal("25.00"),
        "max_price": Decimal("247.00"),
        "pseo_category_slug": "curso-cerrajeria-automotriz-programacion-llaves",
        "persona_prompt_style": "Maestro cerrajero automotriz; resalta programación de llaves Smart Key desde cero, lectura de memorias EEPROM y tarifas de $80 a $250 USD por servicio de emergencia.",
        "telegram_channel_id": "@CerrajeriaAutoPro",
    },
    {
        "clone_code": "bot_46",
        "niche_name": "Tapicería Automotriz, Restauración de Cuero y Muebles",
        "search_keywords": ["tapiceria automotriz", "tapizado de muebles", "restauracion de cuero automotriz", "forrado de timones"],
        "negative_keywords": ["aguja suelta", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("20.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-tapiceria-automotriz-restauracion-muebles",
        "persona_prompt_style": "Artesano tapicero de autos clásicos y modernos; enseña patronaje, costura doble diamante, reparación de tableros/airbags y tapizado capitoné.",
        "telegram_channel_id": "@TapiceriaMasterDeals",
    },
    {
        "clone_code": "bot_47",
        "niche_name": "Contabilidad, Impuestos, Nómina y Facturación Electrónica",
        "search_keywords": ["contabilidad para no contadores", "declaracion de renta e impuestos", "nomina y prestaciones", "finanzas para pymes"],
        "negative_keywords": ["calculadora barata", "gratis"],
        "min_discount_percent": 40,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("220.00"),
        "pseo_category_slug": "curso-contabilidad-impuestos-finanzas-pymes",
        "persona_prompt_style": "Contador Público Senior; explica flujo de caja, deducciones fiscales legales, estados financieros y cómo llevar la contabilidad de varios negocios.",
        "telegram_channel_id": "@ContabilidadPymePro",
    },
    {
        "clone_code": "bot_48",
        "niche_name": "Oratoria, Ventas Consultivas y Cierre High-Ticket",
        "search_keywords": ["cierre de ventas", "closer de ventas high ticket", "oratoria y hablar en publico", "ventas por whatsapp"],
        "negative_keywords": ["gratis", "spam"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("247.00"),
        "pseo_category_slug": "curso-closer-ventas-high-ticket-oratoria",
        "persona_prompt_style": "Director comercial y Closer High-Ticket; enseña manejo de objeciones, guiones psicológicos por WhatsApp/Zoom y cómo ganar comisiones cerrando ventas para terceros.",
        "telegram_channel_id": "@VentasHighTicketPro",
    },
    {
        "clone_code": "bot_49",
        "niche_name": "Reparación de Portátiles, Consolas y Soldadura de Placas",
        "search_keywords": ["reparacion de laptops portatiles", "reparacion consolas ps5 xbox", "electronica de placas madre", "lectura de esquematicos"],
        "negative_keywords": ["forro", "cable", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("22.00"),
        "max_price": Decimal("247.00"),
        "pseo_category_slug": "curso-reparacion-portatiles-consolas-microelectronica",
        "persona_prompt_style": "Ingeniero electrónico de laboratorio; explica medición de fuentes step-down, reprogramación de BIOS, cambio de puertos HDMI en PS5 y diagnóstico corto circuito.",
        "telegram_channel_id": "@LaboratorioPCPro",
    },
    {
        "clone_code": "bot_50",
        "niche_name": "Reparación de Bicicletas, Suspensiones y Patinetas Eléctricas",
        "search_keywords": ["mecanica de bicicletas", "reparacion patinetas electricas scooters", "baterias de litio movilidad", "mantenimiento suspensiones mtb"],
        "negative_keywords": ["calcomania", "timbre", "gratis"],
        "min_discount_percent": 45,
        "min_price": Decimal("20.00"),
        "max_price": Decimal("197.00"),
        "pseo_category_slug": "curso-reparacion-patinetas-electricas-bicicletas",
        "persona_prompt_style": "Especialista en electromovilidad urbana; destaca reparación de controladoras Brushless, armado de packs de baterías de litio con BMS y mantenimiento hidráulico.",
        "telegram_channel_id": "@MovilidadTallerPro",
    },
]


def _slugify(value: str) -> str:
    """Convierte cualquier texto en un slug SEO limpio y seguro para URLs."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s-]", "", value.lower())
    cleaned = re.sub(r"[\s_-]+", "-", cleaned).strip("-")
    return cleaned or "nicho-ofertas"


async def ensure_system_settings(session: AsyncSession) -> SystemSettings:
    """Verifica que exista el registro único de SystemSettings en PostgreSQL; si no existe, lo crea."""
    stmt = select(SystemSettings).limit(1)
    result = await session.execute(stmt)
    sys_settings = result.scalar_one_or_none()

    if sys_settings is None:
        sys_settings = SystemSettings(
            gemini_api_key=settings.gemini_api_key,
            telegram_bot_token=settings.telegram_bot_token,
            admin_telegram_chat_id=settings.admin_telegram_chat_id,
            evolution_api_url=settings.evolution_api_url,
            evolution_api_key=settings.evolution_api_key,
            base_shortlink_domain=settings.base_shortlink_domain,
            preferred_marketplace_mode="HOTMART",
            hotmart_hottok=settings.hotmart_hottok,
            hotmart_client_id=settings.hotmart_client_id,
            hotmart_client_secret=settings.hotmart_client_secret,
            hotmart_basic_auth=settings.hotmart_basic_auth,
            mercadolibre_country_site=settings.mercadolibre_country_site,
            mercadolibre_affiliate_matt_tool=settings.mercadolibre_affiliate_matt_tool,
            amazon_partner_tag=settings.amazon_partner_tag,
            aliexpress_tracking_id=settings.aliexpress_tracking_id,
            daily_global_budget_usd=Decimal(str(settings.daily_global_budget_usd)),
        )
        session.add(sys_settings)
        await session.commit()
        await session.refresh(sys_settings)
        logger.info("Registro inicial de SystemSettings creado en PostgreSQL.")
    elif sys_settings.base_shortlink_domain in {
        "https://ofertas.swarmengine.pro",
        "http://localhost:8000",
        "",
    }:
        sys_settings.base_shortlink_domain = settings.base_shortlink_domain
        await session.commit()
        await session.refresh(sys_settings)

    return sys_settings


async def seed_initial_clones(session: AsyncSession) -> List[BotClone]:
    """
    Carga o actualiza los 50 micro-nichos de la Opción A (Multi-País Hispano High-Ticket en Hotmart:
    EE.UU. Latino, México, España, Colombia y Chile) en la tabla BotClone.
    """
    await ensure_system_settings(session)

    existing_stmt = select(BotClone)
    existing_result = await session.execute(existing_stmt)
    existing_by_code: Dict[str, BotClone] = {c.clone_code: c for c in existing_result.scalars().all()}

    inserted_clones: List[BotClone] = []
    updated_count = 0
    today = date.today()

    for idx, blueprint in enumerate(INITIAL_50_CLONES, start=1):
        code = blueprint["clone_code"]
        if code in existing_by_code:
            existing_clone = existing_by_code[code]
            if existing_clone.pseo_category_slug != blueprint["pseo_category_slug"]:
                existing_clone.niche_name = blueprint["niche_name"]
                existing_clone.search_keywords = blueprint["search_keywords"]
                existing_clone.negative_keywords = blueprint["negative_keywords"]
                existing_clone.min_discount_percent = blueprint["min_discount_percent"]
                existing_clone.min_price = blueprint["min_price"]
                existing_clone.max_price = blueprint["max_price"]
                existing_clone.telegram_channel_id = blueprint["telegram_channel_id"]
                existing_clone.pseo_category_slug = blueprint["pseo_category_slug"]
                existing_clone.persona_prompt_style = blueprint["persona_prompt_style"]
                updated_count += 1
            continue

        clone = BotClone(
            clone_code=code,
            niche_name=blueprint["niche_name"],
            search_keywords=blueprint["search_keywords"],
            negative_keywords=blueprint["negative_keywords"],
            min_discount_percent=blueprint["min_discount_percent"],
            min_price=blueprint["min_price"],
            max_price=blueprint["max_price"],
            country_site="GLOBAL",
            affiliate_sub_id=f"swarm-{code.replace('_', '-')}",
            telegram_channel_id=blueprint["telegram_channel_id"],
            whatsapp_group_jid=f"1203630{idx:04d}999999@g.us",
            pseo_category_slug=blueprint["pseo_category_slug"],
            persona_prompt_style=blueprint["persona_prompt_style"],
            schedule_interval_minutes=60,
            is_active=True,
        )
        session.add(clone)
        inserted_clones.append(clone)

    if inserted_clones or updated_count > 0:
        await session.flush()
        for clone in inserted_clones:
            metric = DailyCloneMetrics(
                clone_id=clone.id,
                date=today,
                offers_published=0,
                total_clicks=0,
                gemini_input_tokens=0,
                gemini_output_tokens=0,
                ai_cost_usd=Decimal("0.000000"),
                confirmed_commissions_usd=Decimal("0.00"),
            )
            session.add(metric)

        await session.commit()
        logger.info(
            "Catálogo Opción A (Hotmart Multi-País Hispano High-Ticket) sincronizado: %d nuevos, %d actualizados.",
            len(inserted_clones),
            updated_count,
        )

    all_stmt = select(BotClone).order_by(BotClone.id.asc())
    all_result = await session.execute(all_stmt)
    return list(all_result.scalars().all())


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2, max=15),
    retry=retry_if_exception_type(Exception),
)
def _call_gemini_for_niche_batch(
    api_key: str,
    batch_size: int,
    country_site: str,
    existing_niche_names: List[str],
    seed_Hint: str = "",
) -> types.GenerateContentResponse:
    """Invoca de forma síncrona el SDK oficial google-genai con Structured Outputs y reintento exponencial."""
    client = genai.Client(api_key=api_key)
    excluded_str = ", ".join(existing_niche_names[-60:]) if existing_niche_names else "Ninguno"
    context_hint = (
        f"Enfócate especialmente en sub-nichos hermanos derivados de estos ganadores: {seed_Hint}."
        if seed_Hint
        else "Explora micro-nichos de cursos prácticos, oficios rentables y certificaciones en Hotmart con alta conversión en EE.UU. Hispano, México, España, Colombia y Chile."
    )

    prompt = (
        "Actúa como un Director de Marketing de Afiliados en Hotmart para el mercado Multi-País Hispano "
        "(EE.UU. Latino, México, España, Colombia y Chile). "
        f"Genera exactamente {batch_size} nuevos micro-nichos de alta rentabilidad en USD.\n"
        f"{context_hint}\n"
        f"PROHIBIDO repetir o solapar con estos nichos ya existentes: {excluded_str}.\n"
        "Para cada micro-nicho define:\n"
        "- niche_name: Nombre claro y específico del oficio, habilidad o negocio.\n"
        "- search_keywords: Entre 4 y 6 términos de búsqueda en Hotmart.\n"
        "- negative_keywords: Palabras negativas para filtrar contenido gratis o irrelevante.\n"
        "- min_discount_percent: Entre 35 y 55.\n"
        "- min_price y max_price en USD (ej. 19.0 a 297.0).\n"
        "- pseo_category_slug: Slug único en español separado por guiones.\n"
        "- persona_prompt_style: Instrucción de voz experta para redactar reseñas persuasivas en español neutro."
    )

    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-flash"]:
        try:
            return client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GeneratedNicheBatchResponse,
                    temperature=0.7,
                ),
            )
        except Exception as exc:
            logger.warning("Modelo Gemini '%s' devolvió error en generación de lote (%s), probando siguiente...", model_name, exc)
    raise RuntimeError("Ningún modelo Gemini estuvo disponible para generar el lote.")


async def generate_new_clone_batch_with_gemini(
    session: AsyncSession,
    batch_size: int = 10,
    country_site: str = "GLOBAL",
    seed_hint: str = "",
) -> List[BotClone]:
    """
    Genera e inserta en PostgreSQL un nuevo lote de micro-bots (por defecto de 10 en 10)
    utilizando Gemini 2.5 Flash con salida estructurada Pydantic.
    """
    sys_settings = await ensure_system_settings(session)
    api_key = sys_settings.gemini_api_key or settings.gemini_api_key
    if not api_key:
        raise ValueError(
            "No se encontró GEMINI_API_KEY configurada en SystemSettings ni en las variables de entorno."
        )

    existing_stmt = select(BotClone.clone_code, BotClone.niche_name, BotClone.pseo_category_slug)
    existing_rows = (await session.execute(existing_stmt)).all()
    existing_codes = {row[0] for row in existing_rows}
    existing_names = [row[1] for row in existing_rows]
    existing_slugs = {row[2] for row in existing_rows}

    count_stmt = select(func.count(BotClone.id))
    total_existing = (await session.execute(count_stmt)).scalar_one() or 0

    response = await asyncio.to_thread(
        _call_gemini_for_niche_batch,
        api_key,
        batch_size,
        country_site,
        existing_names,
        seed_hint,
    )

    parsed_batch = GeneratedNicheBatchResponse.model_validate_json(response.text)
    created_clones: List[BotClone] = []
    next_index = total_existing + 1
    today = date.today()

    for item in parsed_batch.niches:
        while f"bot_{next_index:02d}" in existing_codes:
            next_index += 1
        clone_code = f"bot_{next_index:02d}"
        existing_codes.add(clone_code)

        base_slug = _slugify(item.pseo_category_slug or item.niche_name)
        slug = base_slug
        slug_counter = 2
        while slug in existing_slugs:
            slug = f"{base_slug}-{slug_counter}"
            slug_counter += 1
        existing_slugs.add(slug)

        channel_clean = re.sub(r"[^a-zA-Z0-9]", "", item.niche_name.title())[:20]
        clone = BotClone(
            clone_code=clone_code,
            niche_name=item.niche_name,
            search_keywords=item.search_keywords,
            negative_keywords=item.negative_keywords,
            min_discount_percent=item.min_discount_percent,
            min_price=Decimal(str(round(item.min_price, 2))),
            max_price=Decimal(str(round(item.max_price, 2))),
            country_site="GLOBAL",
            affiliate_sub_id=f"swarm-{clone_code.replace('_', '-')}",
            telegram_channel_id=f"@{channel_clean}Deals",
            whatsapp_group_jid=f"1203630{next_index:04d}888888@g.us",
            pseo_category_slug=slug,
            persona_prompt_style=item.persona_prompt_style,
            schedule_interval_minutes=60,
            is_active=True,
        )
        session.add(clone)
        created_clones.append(clone)
        next_index += 1

    if created_clones:
        await session.flush()

        usage = response.usage_metadata
        in_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
        out_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
        batch_cost = Decimal(
            str(
                round(
                    (in_tokens * settings.gemini_input_cost_per_token)
                    + (out_tokens * settings.gemini_output_cost_per_token),
                    6,
                )
            )
        )

        for idx, clone in enumerate(created_clones):
            metric = DailyCloneMetrics(
                clone_id=clone.id,
                date=today,
                offers_published=0,
                total_clicks=0,
                gemini_input_tokens=in_tokens if idx == 0 else 0,
                gemini_output_tokens=out_tokens if idx == 0 else 0,
                ai_cost_usd=batch_cost if idx == 0 else Decimal("0.000000"),
                confirmed_commissions_usd=Decimal("0.00"),
            )
            session.add(metric)

        await session.commit()
        for clone in created_clones:
            await session.refresh(clone)

    logger.info("Lote de %d nuevos micro-bots generados con Gemini 2.5 Flash y guardados.", len(created_clones))
    return created_clones
