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

    niche_name: str = Field(..., description="Nombre comercial atractivo del micro-nicho")
    search_keywords: List[str] = Field(
        ..., min_length=4, max_length=8, description="Palabras clave específicas de alta conversión"
    )
    negative_keywords: List[str] = Field(
        ..., min_length=5, max_length=12, description="Palabras negativas para excluir repuestos, rotos o accesorios baratos"
    )
    min_discount_percent: int = Field(..., ge=15, le=60, description="Porcentaje mínimo de descuento exigido")
    min_price: float = Field(..., ge=10.0, description="Precio mínimo en moneda local para filtrar baratijas")
    max_price: float = Field(..., gt=20.0, description="Precio máximo razonable del nicho")
    pseo_category_slug: str = Field(..., description="Slug URL SEO friendly en minúsculas separado por guiones")
    persona_prompt_style: str = Field(
        ..., description="Personalidad y tono de comunicación del micro-bot redactor"
    )


class GeneratedNicheBatchResponse(BaseModel):
    """Respuesta estructurada de Gemini con el lote de micro-nichos."""

    niches: List[GeneratedNicheBlueprint]


# Catálogo real completo de los 50 Micro-Bots iniciales (bot_01 a bot_50)
INITIAL_50_CLONES: List[Dict[str, Any]] = [
    {
        "clone_code": "bot_01",
        "niche_name": "Herramientas Eléctricas Profesionales",
        "search_keywords": ["taladro percutor dewalt", "pulidora bosch", "rotomartillo makita", "llave impacto milwaukee", "sierra circular einhell"],
        "negative_keywords": ["repuesto", "carbones", "caja vacia", "estuche solo", "usado", "para reparar", "maletin vacio", "interruptor"],
        "min_discount_percent": 25,
        "min_price": Decimal("120000.00"),
        "max_price": Decimal("2500000.00"),
        "pseo_category_slug": "herramientas-electricas-profesionales",
        "persona_prompt_style": "Maestro de obra y técnico industrial directo al grano, enfocado en torque, potencia real, durabilidad en trabajo pesado y ahorro neto.",
        "telegram_channel_id": "@OfertasTallerPro",
    },
    {
        "clone_code": "bot_02",
        "niche_name": "Monitores Gamer Alta Tasa de Refresco",
        "search_keywords": ["monitor gamer 144hz", "monitor 165hz ips", "monitor curvo 240hz", "monitor lg ultragear", "monitor asus tuf gaming"],
        "negative_keywords": ["cable", "soporte", "brazo", "base sola", "fuente poder", "adaptador", "roto", "linea en pantalla", "repuesto"],
        "min_discount_percent": 20,
        "min_price": Decimal("450000.00"),
        "max_price": Decimal("4500000.00"),
        "pseo_category_slug": "monitores-gamer-alta-frecuencia",
        "persona_prompt_style": "Gamer competitivo experto en hardware y eSports; destaca tiempo de respuesta en ms, panel IPS/VA, FreeSync/G-Sync y relación costo por hercio.",
        "telegram_channel_id": "@MonitoresGamerDeals",
    },
    {
        "clone_code": "bot_03",
        "niche_name": "Cafeteras Espresso y Barismo en Casa",
        "search_keywords": ["cafetera espresso oster", "cafetera delonghi dedica", "molino cafe krups", "cafetera nespresso essenza", "prensa francesa bodum"],
        "negative_keywords": ["vaso repuesto", "filtro papel", "descalcificador", "capsula vacia", "cuchara", "empaque silicona", "usada"],
        "min_discount_percent": 25,
        "min_price": Decimal("90000.00"),
        "max_price": Decimal("3200000.00"),
        "pseo_category_slug": "cafeteras-espresso-barismo",
        "persona_prompt_style": "Barista apasionado y cálido que explica bares de presión, extracción de crema perfecta y ahorro frente a cafeterías comerciales.",
        "telegram_channel_id": "@BaristaEnCasaOfertas",
    },
    {
        "clone_code": "bot_04",
        "niche_name": "Sillas Ergonómicas y Home Office",
        "search_keywords": ["silla ergonomica oficina malla", "silla presidencial lumbar", "silla gamer corsair", "escritorio elevable electrico", "silla ergonomica cabecero"],
        "negative_keywords": ["ruachas", "ruedas repuesto", "piston gas", "forro silla", "cojin suelto", "base estrella", "brazos repuesto"],
        "min_discount_percent": 28,
        "min_price": Decimal("220000.00"),
        "max_price": Decimal("2800000.00"),
        "pseo_category_slug": "sillas-ergonomicas-home-office",
        "persona_prompt_style": "Especialista en ergonomía postural y productividad para programadores y trabajadores remotos; resalta soporte lumbar y jornadas de 8+ horas.",
        "telegram_channel_id": "@HomeOfficeErgonomico",
    },
    {
        "clone_code": "bot_05",
        "niche_name": "Almacenamiento SSD NVMe y RAM PC",
        "search_keywords": ["ssd nvme m2 1tb", "ssd kingston nv2 2tb", "ssd samsung 980 pro", "memoria ram ddr5 32gb", "memoria ram ddr4 16gb 3200"],
        "negative_keywords": ["disipador solo", "tornillo m2", "adaptador usb", "carcasa externa", "cable sata", "usado", "defectuoso"],
        "min_discount_percent": 22,
        "min_price": Decimal("110000.00"),
        "max_price": Decimal("1800000.00"),
        "pseo_category_slug": "almacenamiento-ssd-nvme-ram",
        "persona_prompt_style": "Entusiasta de PC Building enfocado en velocidades de lectura/escritura MB/s, latencia CL, generación PCIe 4.0 y costo por gigabyte.",
        "telegram_channel_id": "@ChollosHardwarePC",
    },
    {
        "clone_code": "bot_06",
        "niche_name": "Freidoras de Aire y Cocina Saludable",
        "search_keywords": ["freidora de aire oster", "air fryer ninja", "freidora aire imusa 5 litros", "horno freidora aire black decker", "freidora digital xiaomi"],
        "negative_keywords": ["molde silicona", "papel parafinado", "rejilla repuesto", "recetario", "spray aceite", "resistencia repuesto"],
        "min_discount_percent": 30,
        "min_price": Decimal("160000.00"),
        "max_price": Decimal("1500000.00"),
        "pseo_category_slug": "freidoras-de-aire-cocina-saludable",
        "persona_prompt_style": "Chef práctico del hogar que destaca capacidad en litros, potencia en vatios, facilidad de limpieza antiadherente y ahorro de aceite.",
        "telegram_channel_id": "@CocinaSmartOfertas",
    },
    {
        "clone_code": "bot_07",
        "niche_name": "Robots Aspiradores y Limpieza Inteligente",
        "search_keywords": ["robot aspirador xiaomi", "aspiradora robot roborock", "roomba irobot", "aspiradora inalambrica dyson", "aspiradora vertical karcher"],
        "negative_keywords": ["cepillo repuesto", "filtro hepa suelto", "mopa repuesto", "bolsa polvo", "bateria reemplazo", "rueda repuesto"],
        "min_discount_percent": 25,
        "min_price": Decimal("350000.00"),
        "max_price": Decimal("4200000.00"),
        "pseo_category_slug": "robots-aspiradores-limpieza-smart",
        "persona_prompt_style": "Asesor de domótica práctica; enfatiza potencia de succión en Pascales (Pa), mapeo láser LiDAR y tiempo libre ganado en casa.",
        "telegram_channel_id": "@DomoticaLimpiezaDeals",
    },
    {
        "clone_code": "bot_08",
        "niche_name": "Audífonos Cancelación de Ruido y Hi-Fi",
        "search_keywords": ["audifonos sony wh-1000xm5", "sony wf-1000xm5", "bose quietcomfort", "airpods pro 2", "audifonos sennheiser momentum", "soundcore space q45"],
        "negative_keywords": ["estuche silicona", "almohadillas repuesto", "cable auxiliar", "forro protector", "caja vacia", "clon", "replica", "oem"],
        "min_discount_percent": 22,
        "min_price": Decimal("180000.00"),
        "max_price": Decimal("2400000.00"),
        "pseo_category_slug": "audifonos-cancelacion-ruido-hifi",
        "persona_prompt_style": "Audiófilo viajero exigente; analiza decibeles de cancelación activa ANC, códecs LDAC/aptX, autonomía real de batería y comodidad.",
        "telegram_channel_id": "@AudioHiFiDescuentos",
    },
    {
        "clone_code": "bot_09",
        "niche_name": "Cascos y Equipamiento Certificado para Motos",
        "search_keywords": ["casco agv k1", "casco ls2 certificado", "casco mt helmets", "chaqueta protecciones moto", "intercomunicador moto ejeas"],
        "negative_keywords": ["tornillo visor", "calcomania", "sticker", "llavero", "malla casco", "almohadilla interna", "usado"],
        "min_discount_percent": 25,
        "min_price": Decimal("150000.00"),
        "max_price": Decimal("2600000.00"),
        "pseo_category_slug": "cascos-equipamiento-motos",
        "persona_prompt_style": "Motero experimentado enfocado en seguridad vial, norma ECE 22.06, aerodinámica en carretera, Pinlock incluido y ventilación.",
        "telegram_channel_id": "@MoterosEquipadosPro",
    },
    {
        "clone_code": "bot_10",
        "niche_name": "Relojes Inteligentes y Deporte GPS",
        "search_keywords": ["garmin forerunner", "apple watch series 9", "galaxy watch 6", "amazfit gtr 4", "huawei watch gt 4"],
        "negative_keywords": ["pulso repuesto", "correa silicona", "vidrio templado", "cargador cable", "protector pantalla", "carcasa plastica"],
        "min_discount_percent": 22,
        "min_price": Decimal("250000.00"),
        "max_price": Decimal("3800000.00"),
        "pseo_category_slug": "smartwatch-relojes-deportivos-gps",
        "persona_prompt_style": "Entrenador de running y triatlón; destaca precisión GPS doble banda, métricas de VO2 Max, duración de batería en días y resistencia al agua.",
        "telegram_channel_id": "@SmartwatchFitnessDeals",
    },
    {
        "clone_code": "bot_11",
        "niche_name": "Tarjetas Gráficas y GPUs para Gaming/IA",
        "search_keywords": ["rtx 4060 ti", "rtx 4070 super", "radeon rx 7700 xt", "rtx 4060 8gb", "radeon rx 7800 xt"],
        "negative_keywords": ["soporte gpu", "cable riser", "ventilador repuesto", "caja vacia", "backplate", "para repuestos", "mineria dañada"],
        "min_discount_percent": 18,
        "min_price": Decimal("950000.00"),
        "max_price": Decimal("6500000.00"),
        "pseo_category_slug": "tarjetas-graficas-gpu-gaming",
        "persona_prompt_style": "Analista de rendimiento gráfico; compara VRAM, FPS en 1080p/1440p, DLSS 3, consumo energético TGP y costo por fotograma.",
        "telegram_channel_id": "@GPURadarOfertas",
    },
    {
        "clone_code": "bot_12",
        "niche_name": "Portátiles Gamer y Ultrabooks Profesionales",
        "search_keywords": ["portatil asus tuf rtx", "lenovo loq rtx", "acer nitro 5", "macbook air m2", "portatil hp victus"],
        "negative_keywords": ["cargador portatil", "teclado repuesto", "pantalla repuesto", "bateria reemplazo", "forro funda", "bisagra", "carcasa"],
        "min_discount_percent": 20,
        "min_price": Decimal("1800000.00"),
        "max_price": Decimal("8500000.00"),
        "pseo_category_slug": "portatiles-gamer-ultrabooks",
        "persona_prompt_style": "Consultor tecnológico universitario y profesional; desglosa procesador, RAM expandible, tarjeta de video dedicada y peso real.",
        "telegram_channel_id": "@PortatilesAlCosto",
    },
    {
        "clone_code": "bot_13",
        "niche_name": "Teclados Mecánicos Custom y Ratones Pro",
        "search_keywords": ["teclado mecanico logitech g pro", "mouse logitech g502 x", "teclado keychron", "mouse razer viper", "teclado hyperx alloy"],
        "negative_keywords": ["keycaps sueltas", "switches bolsa", "pads teflon", "cable paracord", "apoyamuñecas solo", "funda"],
        "min_discount_percent": 25,
        "min_price": Decimal("120000.00"),
        "max_price": Decimal("1200000.00"),
        "pseo_category_slug": "teclados-mecanicos-ratones-gamer",
        "persona_prompt_style": "Especialista en periféricos de precisión; detalla tipo de switch, sensor óptico, peso en gramos, conectividad Lightspeed y polling rate.",
        "telegram_channel_id": "@PerifericosProDeals",
    },
    {
        "clone_code": "bot_14",
        "niche_name": "Cámaras de Seguridad WiFi y Videovigilancia",
        "search_keywords": ["camara ezviz exterior", "camara tapo tp link", "kit dahua 4 camaras", "camara imou 360", "timbre inteligente ring"],
        "negative_keywords": ["caja paso", "balun", "conector bnc", "cable utp metro", "fuente 12v sola", "aviso disuasivo", "soporte plastico"],
        "min_discount_percent": 28,
        "min_price": Decimal("85000.00"),
        "max_price": Decimal("1800000.00"),
        "pseo_category_slug": "camaras-seguridad-wifi-vigilancia",
        "persona_prompt_style": "Experto en seguridad residencial y de negocios; destaca visión nocturna a color, detección humana por IA, protección IP66 y audio bidireccional.",
        "telegram_channel_id": "@SeguridadHogarOfertas",
    },
    {
        "clone_code": "bot_15",
        "niche_name": "Suplementos Deportivos y Proteínas Whey",
        "search_keywords": ["proteina whey gold standard", "creatina monohidratada micronizada", "proteina isolada", "bipro classic", "pre entreno c4"],
        "negative_keywords": ["shaker vacio", "pastillero", "cuchara medidora", "tarro vacio", "muestra sobre", "vencido"],
        "min_discount_percent": 25,
        "min_price": Decimal("80000.00"),
        "max_price": Decimal("650000.00"),
        "pseo_category_slug": "suplementos-deportivos-proteinas",
        "persona_prompt_style": "Nutricionista deportivo enfocado en gramos de proteína por servicio, pureza de creatina, número de servicios por envase y registro sanitario.",
        "telegram_channel_id": "@FitnessSuplementosDeals",
    },
    {
        "clone_code": "bot_16",
        "niche_name": "Equipos de Gimnasio y Pesas en Casa",
        "search_keywords": ["mancuernas ajustables", "banco multifuncional ejercicio", "bicicleta spinning", "barra olimpica", "kettlebell pesa rusa"],
        "negative_keywords": ["guantes", "cuerda saltar barata", "pinza seguro", "llave allen", "calcomania", "repuesto pedal"],
        "min_discount_percent": 28,
        "min_price": Decimal("140000.00"),
        "max_price": Decimal("3500000.00"),
        "pseo_category_slug": "equipos-gimnasio-pesas-casa",
        "persona_prompt_style": "Entrenador de fuerza en casa; analiza calibre del acero, peso máximo soportado, ahorro de espacio y retorno frente a pagar mensualidad de gimnasio.",
        "telegram_channel_id": "@GymEnCasaOfertas",
    },
    {
        "clone_code": "bot_17",
        "niche_name": "Camping, Carpas Impermeables y Senderismo",
        "search_keywords": ["carpa coleman impermeable", "carpa naturehike", "saco dormir termico", "morral trekking 60 litros", "linterna frontal recargable"],
        "negative_keywords": ["estaca suelta", "parche reparacion", "cuerda viento", "varilla repuesto", "bolsa empaque"],
        "min_discount_percent": 26,
        "min_price": Decimal("95000.00"),
        "max_price": Decimal("2200000.00"),
        "pseo_category_slug": "camping-carpas-senderismo",
        "persona_prompt_style": "Montañista y guía outdoor; explica columna de agua en mm, costuras termoselladas, peso en mochila y resistencia al viento.",
        "telegram_channel_id": "@CampingAventuraDeals",
    },
    {
        "clone_code": "bot_18",
        "niche_name": "Ciclismo de Ruta y MTB Componentes",
        "search_keywords": ["grupo shimano deore", "ciclocomputador igpsport", "casco giro ciclismo", "rodillo entrenamiento bicicleta", "luces bicicleta recargables"],
        "negative_keywords": ["parche neumatico", "tapavalvula", "balin", "cable guaya", "tornillo plato", "usado"],
        "min_discount_percent": 24,
        "min_price": Decimal("90000.00"),
        "max_price": Decimal("3200000.00"),
        "pseo_category_slug": "ciclismo-ruta-mtb-componentes",
        "persona_prompt_style": "Mecánico y ciclista de fondo; habla de gramos ahorrados, precisión de cambios Shimano/SRAM, lúmenes reales y seguridad en ruta.",
        "telegram_channel_id": "@CiclismoProDescuentos",
    },
    {
        "clone_code": "bot_19",
        "niche_name": "Hidrolavadoras y Detailing Automotriz",
        "search_keywords": ["hidrolavadora karcher k2", "hidrolavadora karcher k4", "polichadora orbital auto", "aspiradora carro black decker", "arrancador bateria portatil"],
        "negative_keywords": ["acople rapido", "empaque oring", "manguera sola", "boquilla suelta", "toalla microfibra suelta"],
        "min_discount_percent": 25,
        "min_price": Decimal("150000.00"),
        "max_price": Decimal("2400000.00"),
        "pseo_category_slug": "hidrolavadoras-detailing-automotriz",
        "persona_prompt_style": "Especialista en Car Detailing; resalta presión en PSI, caudal de agua por minuto, ahorro en lavaderos y cuidado de la pintura.",
        "telegram_channel_id": "@AutoDetailingOfertas",
    },
    {
        "clone_code": "bot_20",
        "niche_name": "Proyectores Smart y Cine en Casa",
        "search_keywords": ["proyector epson", "proyector samsung freestyle", "proyector full hd nativo", "barra de sonido lg", "barra sonido jbl"],
        "negative_keywords": ["control remoto", "cable hdmi", "soporte techo", "lampara repuesto", "telon solo"],
        "min_discount_percent": 25,
        "min_price": Decimal("280000.00"),
        "max_price": Decimal("4500000.00"),
        "pseo_category_slug": "proyectores-barras-sonido-cine",
        "persona_prompt_style": "Experto en Home Theater; evalúa lúmenes ANSI reales, resolución nativa 1080p/4K, potencia RMS de subwoofer y Dolby Atmos.",
        "telegram_channel_id": "@CineEnCasaDeals",
    },
    {
        "clone_code": "bot_21",
        "niche_name": "Televisores 4K QLED y OLED",
        "search_keywords": ["televisor samsung qled 55", "smart tv lg oled", "televisor tcl qled 65", "smart tv hisense 4k", "televisor sony bravia"],
        "negative_keywords": ["control remoto", "base patas", "tarjeta main", "tiras led repuesto", "pantalla rota", "soporte pared"],
        "min_discount_percent": 24,
        "min_price": Decimal("1100000.00"),
        "max_price": Decimal("7500000.00"),
        "pseo_category_slug": "televisores-4k-qled-oled",
        "persona_prompt_style": "Revisor de pantallas de cine; compara brillo en nits, contraste local dimming, HDMI 2.1 para consolas y sistema operativo Google TV/webOS.",
        "telegram_channel_id": "@SmartTVOfertasVIP",
    },
    {
        "clone_code": "bot_22",
        "niche_name": "Consolas PlayStation, Xbox y Nintendo Switch",
        "search_keywords": ["consola playstation 5 slim", "nintendo switch oled", "xbox series x", "xbox series s", "control dualsense ps5"],
        "negative_keywords": ["forro silicona", "gomitas joystick", "calcomania skin", "caja vacia", "cable carga", "soporte plastico"],
        "min_discount_percent": 18,
        "min_price": Decimal("220000.00"),
        "max_price": Decimal("3600000.00"),
        "pseo_category_slug": "consolas-videojuegos-ps5-switch-xbox",
        "persona_prompt_style": "Cazador de ofertas gamer; distingue versiones con lector vs digital, bundles con juegos incluidos y mínimos históricos reales.",
        "telegram_channel_id": "@ConsolasAlMinimo",
    },
    {
        "clone_code": "bot_23",
        "niche_name": "Drones con Cámara 4K y Estabilizadores",
        "search_keywords": ["dji mini 3", "dji mini 4 pro", "dji osmo mobile 6", "camara gopro hero 12", "insta360 x3"],
        "negative_keywords": ["helices repuesto", "protector lente", "correa cuello", "estuche solo", "cable otg", "filtro suelto"],
        "min_discount_percent": 20,
        "min_price": Decimal("380000.00"),
        "max_price": Decimal("5800000.00"),
        "pseo_category_slug": "drones-camaras-accion-estabilizadores",
        "persona_prompt_style": "Filmmaker aéreo y creador audiovisual; detalla rango de transmisión, estabilización de 3 ejes, tiempo de vuelo por batería y peso bajo 249g.",
        "telegram_channel_id": "@DronesYCamarasDeals",
    },
    {
        "clone_code": "bot_24",
        "niche_name": "Micrófonos e Interfaces para Streaming y Podcast",
        "search_keywords": ["microfono shure mv7", "microfono hyperx quadcast", "interfaz focusrite scarlett", "microfono elgato wave", "microfono inalambrico dji mic"],
        "negative_keywords": ["espuma antipop", "cable xlr barato", "adaptador rosca", "funda", "brazo barato"],
        "min_discount_percent": 22,
        "min_price": Decimal("160000.00"),
        "max_price": Decimal("2200000.00"),
        "pseo_category_slug": "microfonos-interfaces-streaming-podcast",
        "persona_prompt_style": "Productor de podcast e ingeniero de sonido; explica patrón polar cardioide, rechazo de ruido de fondo, conexión USB/XLR y preamplificadores.",
        "telegram_channel_id": "@StreamerAudioPro",
    },
    {
        "clone_code": "bot_25",
        "niche_name": "Impresoras 3D y Resina Creality/Anycubic",
        "search_keywords": ["impresora 3d creality ender", "creality k1", "impresora 3d bambu lab", "impresora resina anycubic", "filamento pla creality pack"],
        "negative_keywords": ["boquilla nozzle", "tubo teflon", "termistor", "correa gt2", "resorte cama", "cuchilla"],
        "min_discount_percent": 22,
        "min_price": Decimal("350000.00"),
        "max_price": Decimal("4800000.00"),
        "pseo_category_slug": "impresoras-3d-filamento-resina",
        "persona_prompt_style": "Maker e ingeniero de prototipado 3D; destaca velocidad en mm/s, autonivelación CR-Touch, extrusor directo y volumen de impresión.",
        "telegram_channel_id": "@Impresion3DOfertas",
    },
    {
        "clone_code": "bot_26",
        "niche_name": "Routers WiFi 6 y Redes Mesh de Alta Velocidad",
        "search_keywords": ["sistema mesh tp link deco", "router wifi 6 asus", "router mercusys wifi 6", "ubiquiti unifi ap", "switch gigabit poe"],
        "negative_keywords": ["conector rj45", "cable patch cord", "antena suelta", "fuente reemplazo", "capuchon"],
        "min_discount_percent": 25,
        "min_price": Decimal("140000.00"),
        "max_price": Decimal("2100000.00"),
        "pseo_category_slug": "routers-wifi-6-redes-mesh",
        "persona_prompt_style": "Ingeniero de telecomunicaciones; explica cobertura en metros cuadrados, puertos Gigabit reales, MU-MIMO y eliminación de zonas muertas.",
        "telegram_channel_id": "@RedesWiFiProDeals",
    },
    {
        "clone_code": "bot_27",
        "niche_name": "Energía Solar, Estaciones Portátiles y UPS",
        "search_keywords": ["estacion energia ecoflow", "ups interactiva apc", "ups forza 1000va", "inversor onda pura", "panel solar monocristalino"],
        "negative_keywords": ["conector mc4", "fusible", "cable metro", "terminal bateria", "tornillo soporte"],
        "min_discount_percent": 22,
        "min_price": Decimal("180000.00"),
        "max_price": Decimal("5500000.00"),
        "pseo_category_slug": "energia-solar-estaciones-ups",
        "persona_prompt_style": "Consultor energético; detalla capacidad en Wh/VA, química LiFePO4, protección contra picos de voltaje y autonomía en apagones.",
        "telegram_channel_id": "@EnergiaYRespaldoDeals",
    },
    {
        "clone_code": "bot_28",
        "niche_name": "Cerraduras Digitales Biométricas y Domótica",
        "search_keywords": ["cerradura digital huella", "cerradura inteligente yale", "cerradura wifi ezviz", "interruptor inteligente sonoff", "alexa echo show"],
        "negative_keywords": ["llavero rfid suelto", "pila alcalina", "caja chazo", "protector acrilico", "forro"],
        "min_discount_percent": 26,
        "min_price": Decimal("140000.00"),
        "max_price": Decimal("2200000.00"),
        "pseo_category_slug": "cerraduras-digitales-domotica",
        "persona_prompt_style": "Arquitecto de hogares inteligentes; enfatiza apertura con huella en 0.5s, códigos temporales para Airbnb y compatibilidad con Alexa/Tuya.",
        "telegram_channel_id": "@CasaInteligenteOfertas",
    },
    {
        "clone_code": "bot_29",
        "niche_name": "Baterías de Cocina Acero Quirúrgico y Hierro Fundido",
        "search_keywords": ["bateria cocina tramontina acero inoxidable", "olla presion universal", "sarten hierro fundido victoria", "bateria cocina tefal", "juego ollas kitchenaid"],
        "negative_keywords": ["empaque olla", "valvula repuesto", "mango suelto", "espatula plastica", "esponja"],
        "min_discount_percent": 28,
        "min_price": Decimal("130000.00"),
        "max_price": Decimal("2400000.00"),
        "pseo_category_slug": "baterias-cocina-ollas-sartenes",
        "persona_prompt_style": "Gastrónomo experto en utensilios eternos; destaca triple fondo difusor, compatibilidad con inducción y calibre del acero inoxidable.",
        "telegram_channel_id": "@CocinaGourmetDeals",
    },
    {
        "clone_code": "bot_30",
        "niche_name": "Licuadoras de Alto Rendimiento y Procesadores",
        "search_keywords": ["nutribullet pro", "licuadora ninja profesional", "batidora pedestal kitchenaid", "procesador alimentos oster", "licuadora reversible oster"],
        "negative_keywords": ["cuchilla repuesto", "empaque caucho", "vaso plastico solo", "tapa sola", "acople cuadrante"],
        "min_discount_percent": 26,
        "min_price": Decimal("160000.00"),
        "max_price": Decimal("2800000.00"),
        "pseo_category_slug": "licuadoras-batidoras-procesadores",
        "persona_prompt_style": "Especialista en repostería y cocina rápida; evalúa potencia del motor en caballos/vatios, cuchillas trituradoras de hielo y acople metálico.",
        "telegram_channel_id": "@ElectroCocinaOfertas",
    },
    {
        "clone_code": "bot_31",
        "niche_name": "Colchones En Caja y Descanso Premium",
        "search_keywords": ["colchon emma original", "colchon spring ortopedico", "colchon comodisimos", "almohada viscoelastica memory foam", "topper colchon"],
        "negative_keywords": ["funda almohada", "sabana suelta", "protector impermeable barato", "usado"],
        "min_discount_percent": 32,
        "min_price": Decimal("120000.00"),
        "max_price": Decimal("3800000.00"),
        "pseo_category_slug": "colchones-almohadas-descanso",
        "persona_prompt_style": "Especialista en higiene del sueño; analiza densidad de espumas, independencia de lechos, alivio de puntos de presión y años de garantía.",
        "telegram_channel_id": "@DescansoPremiumDeals",
    },
    {
        "clone_code": "bot_32",
        "niche_name": "Perfumería Árabe y Fragancias de Diseñador Originales",
        "search_keywords": ["perfume lattafa khamrah", "afnan 9pm original", "armaf club de nuit intense", "perfume versace eros", "jean paul gaultier le male"],
        "negative_keywords": ["decant", "muestra 5ml", "atomizador recargable", "replica", "aaa", "tester sin caja", "locion imitacion"],
        "min_discount_percent": 25,
        "min_price": Decimal("110000.00"),
        "max_price": Decimal("950000.00"),
        "pseo_category_slug": "perfumes-arabes-disenador-originales",
        "persona_prompt_style": "Reseñador experto en alta perfumería; describe notas de salida/fondo, estela, proyección en horas y autenticidad garantizada.",
        "telegram_channel_id": "@PerfumesOriginalesDeals",
    },
    {
        "clone_code": "bot_33",
        "niche_name": "Dermocosmética y Skincare Clínico",
        "search_keywords": ["bloqueador isdin fusion water", "serum la roche posay effaclar", "cerave limpiador hidratante", "eucerin antipigment dual", "vichy mineral 89"],
        "negative_keywords": ["bolsa regalo", "cosmetiquera vacia", "algodon", "banda cabello", "vencido"],
        "min_discount_percent": 25,
        "min_price": Decimal("65000.00"),
        "max_price": Decimal("450000.00"),
        "pseo_category_slug": "dermocosmetica-skincare-clinico",
        "persona_prompt_style": "Especialista en cuidado dermatológico; explica principios activos (niacinamida, ácido hialurónico, SPF 50+) y ahorro en reposición mensual.",
        "telegram_channel_id": "@SkincareClinicoOfertas",
    },
    {
        "clone_code": "bot_34",
        "niche_name": "Cuidado Capilar Profesional y Estilizado",
        "search_keywords": ["plancha babyliss pro nano titanium", "secador remington profesional", "cepillo secador revlon", "tratamiento olaplex", "mascarilla kerastase"],
        "negative_keywords": ["pinza pelo", "peinilla plastica", "gorro baño", "forro termico", "repuesto cable"],
        "min_discount_percent": 28,
        "min_price": Decimal("85000.00"),
        "max_price": Decimal("1400000.00"),
        "pseo_category_slug": "planchas-secadores-cuidado-capilar",
        "persona_prompt_style": "Estilista profesional de salón; destaca placas de titanio genuino, control iónico anti-frizz, temperatura constante y protección de la fibra.",
        "telegram_channel_id": "@CabelloProDescuentos",
    },
    {
        "clone_code": "bot_35",
        "niche_name": "Zapatillas Running y Sneakers Originales",
        "search_keywords": ["tenis adidas ultraboost", "nike pegasus running", "asics gel nimbus", "puma velocity nitro", "new balance 574 originales"],
        "negative_keywords": ["cordones", "plantillas sueltas", "limpiador espuma", "horma plastica", "replica", "aaa"],
        "min_discount_percent": 30,
        "min_price": Decimal("180000.00"),
        "max_price": Decimal("1200000.00"),
        "pseo_category_slug": "zapatillas-running-sneakers-originales",
        "persona_prompt_style": "Corredor y coleccionista de calzado; analiza amortiguación reactiva, drop, tracción de suela Continental y tiendas oficiales verificadas.",
        "telegram_channel_id": "@SneakersYRunningDeals",
    },
    {
        "clone_code": "bot_36",
        "niche_name": "Coches, Sillas de Auto y Mundo Bebé",
        "search_keywords": ["silla carro bebe joie", "coche bebe compacto", "monitor bebe camara", "extractor leche electrico", "corral cuna bebe"],
        "negative_keywords": ["chupo suelto", "babero", "medias", "juguete sonajero", "repuesto llanta"],
        "min_discount_percent": 26,
        "min_price": Decimal("140000.00"),
        "max_price": Decimal("2500000.00"),
        "pseo_category_slug": "coches-sillas-auto-mundo-bebe",
        "persona_prompt_style": "Asesor de maternidad y seguridad infantil; enfatiza anclaje ISOFIX, certificaciones de impacto lateral, plegado con una mano y confort.",
        "telegram_channel_id": "@BebesSegurosOfertas",
    },
    {
        "clone_code": "bot_37",
        "niche_name": "LEGO Sets de Colección y Juegos de Mesa",
        "search_keywords": ["lego technic", "lego star wars", "lego icons", "juego mesa catan", "lego speed champions"],
        "negative_keywords": ["llavero lego", "pieza suelta", "figura individual", "caja vacia", "instrucciones solas", "generico"],
        "min_discount_percent": 24,
        "min_price": Decimal("90000.00"),
        "max_price": Decimal("2600000.00"),
        "pseo_category_slug": "lego-coleccion-juegos-de-mesa",
        "persona_prompt_style": "Coleccionista AFOL y entusiasta geek; calcula precio por pieza, valor de colección a futuro y horas de armado garantizadas.",
        "telegram_channel_id": "@LegoYColeccionables",
    },
    {
        "clone_code": "bot_38",
        "niche_name": "Mascotas: Comederos Automáticos y Alimento Super Premium",
        "search_keywords": ["comedero automatico wifi mascotas", "fuente agua gatos acero", "concentrado hills perro", "royal canin gatos", "gimnasio rascador gatos grande"],
        "negative_keywords": ["collar barato", "bolsitas popo", "pelota goma", "filtro suelto", "plato plastico"],
        "min_discount_percent": 24,
        "min_price": Decimal("95000.00"),
        "max_price": Decimal("950000.00"),
        "pseo_category_slug": "mascotas-comederos-alimento-premium",
        "persona_prompt_style": "Veterinario práctico y amante de las mascotas; destaca programación de porciones por app, higiene antibacteriana y costo por kilo de alimento.",
        "telegram_channel_id": "@MascotasFelicesDeals",
    },
    {
        "clone_code": "bot_39",
        "niche_name": "Instrumentos Musicales y Estudio Home Studio",
        "search_keywords": ["piano digital yamaha", "guitarra electroacustica cort", "bateria electronica alesis", "monitores estudio mackie", "controlador midi arturia"],
        "negative_keywords": ["plumilla", "cuerda individual", "forro simple", "correa guitarra", "cable plug barato"],
        "min_discount_percent": 22,
        "min_price": Decimal("220000.00"),
        "max_price": Decimal("4500000.00"),
        "pseo_category_slug": "instrumentos-musicales-home-studio",
        "persona_prompt_style": "Músico de sesión y productor; evalúa sensibilidad de teclas contrapesadas, maderas de resonancia, respuesta plana en frecuencia y versatilidad.",
        "telegram_channel_id": "@MusicosYStudioOfertas",
    },
    {
        "clone_code": "bot_40",
        "niche_name": "Maletas de Viaje Cabina/Bodega y Mochilas Antirrobo",
        "search_keywords": ["maleta viaje samsonite", "maleta cabina totto", "mochila antirrobo bange", "juego maletas rigidas abs", "morral porta pc impermeable"],
        "negative_keywords": ["candado suelto", "identificador maleta", "forro elastico", "correa cinta", "rueda repuesto"],
        "min_discount_percent": 30,
        "min_price": Decimal("120000.00"),
        "max_price": Decimal("1800000.00"),
        "pseo_category_slug": "maletas-viaje-mochilas-antirrobo",
        "persona_prompt_style": "Viajero frecuente internacional; verifica medidas exactas de aerolínea, ruedas dobles 360°, candado TSA integrado y policarbonato resistente.",
        "telegram_channel_id": "@ViajerosSmartDeals",
    },
    {
        "clone_code": "bot_41",
        "niche_name": "Aires Acondicionados Inverter y Climatización",
        "search_keywords": ["aire acondicionado inverter 12000 btu", "minisplit lg dual inverter", "aire acondicionado samsung windfree", "deshumidificador ambiental", "purificador aire filtro hepa"],
        "negative_keywords": ["control remoto", "soporte escuadra", "cinta vinilo", "filtro repuesto", "tubo cobre"],
        "min_discount_percent": 22,
        "min_price": Decimal("320000.00"),
        "max_price": Decimal("4200000.00"),
        "pseo_category_slug": "aires-acondicionados-inverter-clima",
        "persona_prompt_style": "Ingeniero en climatización; calcula ahorro energético del compresor Inverter (hasta 70%), nivel de ruido en decibeles y capacidad BTU.",
        "telegram_channel_id": "@ClimaYConfortOfertas",
    },
    {
        "clone_code": "bot_42",
        "niche_name": "Lavadoras, Secadoras y Neveras No Frost",
        "search_keywords": ["lavadora lg carga frontal", "lavadora samsung digital inverter", "nevera no frost haceb", "nevecon samsung", "lavavajillas electrico"],
        "negative_keywords": ["manguera desague", "forro lavadora", "filtro pelusas", "bomba agua repuesto", "base rodachinas"],
        "min_discount_percent": 24,
        "min_price": Decimal("1100000.00"),
        "max_price": Decimal("6800000.00"),
        "pseo_category_slug": "lavadoras-neveras-linea-blanca",
        "persona_prompt_style": "Experto en línea blanca eficiente; compara clasificación energética A, capacidad real en kilos/litros y garantía de 10 años en motor.",
        "telegram_channel_id": "@LineaBlancaAlCosto",
    },
    {
        "clone_code": "bot_43",
        "niche_name": "Equipos de Soldadura Inverter y Compresores",
        "search_keywords": ["soldador inversor 200 amperios", "careta fotosensible soldar", "compresor aire 50 litros", "cortadora plasma", "soldador mig sin gas"],
        "negative_keywords": ["electrodo suelto", "vidrio careta", "porta electrodo solo", "acople", "manometro suelto"],
        "min_discount_percent": 26,
        "min_price": Decimal("140000.00"),
        "max_price": Decimal("2800000.00"),
        "pseo_category_slug": "soldadores-inverter-compresores",
        "persona_prompt_style": "Soldador industrial; analiza ciclo de trabajo al 60%, tecnología IGBT, arco estable en 110V/220V y caudal en CFM.",
        "telegram_channel_id": "@SoldaduraYTallerPro",
    },
    {
        "clone_code": "bot_44",
        "niche_name": "Instrumentación de Medición Láser y Multímetros",
        "search_keywords": ["nivel laser verde bosch", "multimetro digital fluke", "medidor distancia laser", "camara termografica", "pinza amperimetrica uni-t"],
        "negative_keywords": ["puntas prueba solas", "estuche tela", "fusible", "pila 9v", "tripode barato"],
        "min_discount_percent": 23,
        "min_price": Decimal("95000.00"),
        "max_price": Decimal("2500000.00"),
        "pseo_category_slug": "niveles-laser-multimetros-medicion",
        "persona_prompt_style": "Ingeniero eléctrico y residente de obra; destaca precisión milimétrica, True RMS, visibilidad de rayo verde en exteriores y norma CAT III/IV.",
        "telegram_channel_id": "@MedicionYPrecision",
    },
    {
        "clone_code": "bot_45",
        "niche_name": "Llantas para Auto y Camioneta de Marca",
        "search_keywords": ["llanta michelin", "llanta bridgestone", "llanta pirelli", "llanta goodyear", "llanta hankook"],
        "negative_keywords": ["copa rin", "valvula", "perno tuerca", "usada", "reencauchada", "forro llanta"],
        "min_discount_percent": 22,
        "min_price": Decimal("220000.00"),
        "max_price": Decimal("1600000.00"),
        "pseo_category_slug": "llantas-auto-camioneta-oferta",
        "persona_prompt_style": "Especialista en serviteca y seguridad vial; detalla índice de desgaste Treadwear, agarre en lluvia AA y ahorro comprando el juego completo.",
        "telegram_channel_id": "@LlantasEnOfertaHoy",
    },
    {
        "clone_code": "bot_46",
        "niche_name": "Parrillas BBQ, Ahumadores y Asados",
        "search_keywords": ["asador barril acero inoxidable", "parrilla gas weber", "asador carbon char broil", "termometro carne inalambrico", "caja china asadora"],
        "negative_keywords": ["pinza sola", "cepillo alambre", "bolsa carbon", "soplador manual", "delantal"],
        "min_discount_percent": 25,
        "min_price": Decimal("130000.00"),
        "max_price": Decimal("3200000.00"),
        "pseo_category_slug": "parrillas-bbq-asadores-barriles",
        "persona_prompt_style": "Maestro parrillero; resalta calibre del acero inoxidable 304, capacidad en libras de carne, reducción de humo y retención térmica.",
        "telegram_channel_id": "@MaestroParrilleroDeals",
    },
    {
        "clone_code": "bot_47",
        "niche_name": "Tablets para Diseño, Estudio y Lectores Kindle",
        "search_keywords": ["ipad 10 generacion", "samsung galaxy tab s9 fe", "xiaomi pad 6", "kindle paperwhite", "tableta grafica wacom"],
        "negative_keywords": ["funda estuche", "vidrio templado", "punta lapiz", "guante dibujo", "cable usb"],
        "min_discount_percent": 20,
        "min_price": Decimal("280000.00"),
        "max_price": Decimal("3900000.00"),
        "pseo_category_slug": "tablets-ipad-kindle-diseno",
        "persona_prompt_style": "Ilustrador digital y lector voraz; compara niveles de presión del stylus, pantalla antirreflejo, almacenamiento y autonomía de semanas.",
        "telegram_channel_id": "@TabletsYKindleDeals",
    },
    {
        "clone_code": "bot_48",
        "niche_name": "Celulares Gama Media-Alta y Flagships Liberados",
        "search_keywords": ["xiaomi poco x6 pro", "samsung galaxy s24", "motorola edge 50", "iphone 15 128gb", "redmi note 13 pro plus"],
        "negative_keywords": ["forro", "funda", "hidrogel", "vidrio", "bandeja sim", "display repuesto", "tapa trasera", "usado"],
        "min_discount_percent": 20,
        "min_price": Decimal("750000.00"),
        "max_price": Decimal("6200000.00"),
        "pseo_category_slug": "celulares-smartphones-liberados-oferta",
        "persona_prompt_style": "Reviewer experto en telefonía móvil; analiza procesador Snapdragon/Dimensity, cámara con OIS, carga rápida en vatios y precio por debajo del mercado.",
        "telegram_channel_id": "@CelularesCholloPro",
    },
    {
        "clone_code": "bot_49",
        "niche_name": "Gafas de Sol Originales Polarizadas y Relojería Clásica",
        "search_keywords": ["gafas ray ban originales", "gafas oakley holbrook", "reloj casio edifice", "reloj seiko 5 automatico", "reloj orient automatico"],
        "negative_keywords": ["estuche vacio", "paño microfibra", "cordon gafas", "eslabon", "correa suelta", "replica"],
        "min_discount_percent": 28,
        "min_price": Decimal("160000.00"),
        "max_price": Decimal("1800000.00"),
        "pseo_category_slug": "gafas-polarizadas-relojes-automaticos",
        "persona_prompt_style": "Coleccionista horológico y asesor de estilo masculino; destaca calibre automático japonés, cristal de zafiro, filtro UV400 polarizado y elegancia.",
        "telegram_channel_id": "@RelojesYGafasVIP",
    },
    {
        "clone_code": "bot_50",
        "niche_name": "Patinetas Eléctricas y Movilidad Urbana",
        "search_keywords": ["patineta electrica xiaomi scooter", "scooter electrico segway ninebot", "bicicleta electrica plegable", "monopatin electrico 800w", "casco urbano led"],
        "negative_keywords": ["neumatico solido", "guardabarros", "manilar", "tornillo bisagra", "cargador generico", "timbre"],
        "min_discount_percent": 22,
        "min_price": Decimal("950000.00"),
        "max_price": Decimal("6500000.00"),
        "pseo_category_slug": "patinetas-scooters-movilidad-electrica",
        "persona_prompt_style": "Especialista en micromovilidad sostenible; detalla potencia nominal y pico en vatios, autonomía real en kilómetros, subida de pendientes y ahorro en transporte.",
        "telegram_channel_id": "@MovilidadElectricaDeals",
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

    return sys_settings


async def seed_initial_clones(session: AsyncSession) -> List[BotClone]:
    """
    Carga los 50 micro-nichos iniciales en la tabla BotClone si está vacía,
    o inserta los clones faltantes preservando los existentes.
    También inicializa las métricas diarias en DailyCloneMetrics para el día actual.
    """
    await ensure_system_settings(session)

    existing_stmt = select(BotClone.clone_code)
    existing_result = await session.execute(existing_stmt)
    existing_codes = set(existing_result.scalars().all())

    inserted_clones: List[BotClone] = []
    today = date.today()

    for idx, blueprint in enumerate(INITIAL_50_CLONES, start=1):
        code = blueprint["clone_code"]
        if code in existing_codes:
            continue

        clone = BotClone(
            clone_code=code,
            niche_name=blueprint["niche_name"],
            search_keywords=blueprint["search_keywords"],
            negative_keywords=blueprint["negative_keywords"],
            min_discount_percent=blueprint["min_discount_percent"],
            min_price=blueprint["min_price"],
            max_price=blueprint["max_price"],
            country_site=settings.mercadolibre_country_site,
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

    if inserted_clones:
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
        logger.info("Sembrados %d micro-bots iniciales en PostgreSQL.", len(inserted_clones))
    else:
        logger.info("Los 50 micro-bots iniciales ya se encuentran registrados en PostgreSQL.")

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
        else "Explora micro-nichos de comercio electrónico con alta intención de compra y ticket medio-alto."
    )

    prompt = (
        f"Actúa como un Director de E-Commerce y Afiliados para el mercado {country_site}. "
        f"Genera exactamente {batch_size} nuevos micro-nichos rentables para bots cazadores de ofertas. "
        f"{context_hint}\n"
        f"PROHIBIDO repetir o solapar con estos nichos ya existentes: {excluded_str}.\n"
        "Para cada micro-nicho define:\n"
        "- niche_name: Nombre claro y específico.\n"
        "- search_keywords: Entre 4 y 8 términos de búsqueda con marca o especificación real.\n"
        "- negative_keywords: Entre 5 y 10 palabras para filtrar repuestos, accesorios baratos, cajas vacías o usados.\n"
        "- min_discount_percent: Entre 20 y 35.\n"
        "- min_price y max_price coherentes con la moneda del sitio (ej. COP para MCO, MXN para MLM, ARS para MLA).\n"
        "- pseo_category_slug: Slug único en español separado por guiones.\n"
        "- persona_prompt_style: Instrucción de voz experta para redactar reseñas y posts de Telegram."
    )

    response = client.models.generate_content(
        model=settings.gemini_model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GeneratedNicheBatchResponse,
            temperature=0.7,
        ),
    )
    return response


async def generate_new_clone_batch_with_gemini(
    session: AsyncSession,
    batch_size: int = 10,
    country_site: str = "MCO",
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
            country_site=country_site,
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

        # Contabilizar tokens consumidos en el primer clon del lote generado
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
