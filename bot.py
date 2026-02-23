"""
Vecinito v4 — Bot con RAG (Supabase + pgvector)
Servicios: nombre, rubro, experiencia, contacto (sin zona).
Comercios: estructura completa con dirección, horarios, coordenadas.

CAMBIOS v4:
- [RAG] Diccionario de sinónimos (pizza→pizzería, remedio→farmacia, etc.)
- [RAG] Scoring ponderado por campo (nombre>rubro/categoría>tags)
- [RAG] Matching parcial de palabras (plom→plomero)
- [RAG] Expansión de query con sinónimos para Supabase también
- [UX] Bienvenida automática al primer mensaje de un usuario nuevo
- [PROMPT] Instrucción de zona en system prompt

Incluye todos los fixes de v3:
- Debouncing con generation counter
- Cola protegida con lock global
- Historial limitado a N mensajes
- Caché personal con hash de ubicación
- LRU para embeddings y memorias
- Limpieza de caché cada 1 hora
- CSV asincrónico
- Normalización de acentos
- Límite de audio
- System prompt v2
"""

import os
import re
import csv
import json
import hashlib
import logging
import sys
import asyncio
import tempfile
import unicodedata
from collections import OrderedDict
from datetime import datetime, timedelta
from math import radians, sin, cos, sqrt, atan2
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, filters,
)

# ══════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("vecinito")

# ══════════════════════════════════════════════════════════
# DEPENDENCIAS OPCIONALES
# ══════════════════════════════════════════════════════════

try:
    import redis
    REDIS_DISPONIBLE = True
except ImportError:
    REDIS_DISPONIBLE = False
    logger.warning("Redis no instalado. Usando memoria local.")

try:
    from supabase import create_client, Client as SupabaseClient
    SUPABASE_DISPONIBLE = True
except ImportError:
    SUPABASE_DISPONIBLE = False
    logger.warning("Supabase no instalado. Usando JSON como fallback.")

load_dotenv()

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")

if not TELEGRAM_TOKEN:
    logger.critical("TELEGRAM_TOKEN no configurado.")
    sys.exit(1)
if not OPENAI_API_KEY:
    logger.critical("OPENAI_API_KEY no configurado.")
    sys.exit(1)

client   = AsyncOpenAI(api_key=OPENAI_API_KEY)
BASE_DIR = Path(__file__).resolve().parent

# ── Límites ──
MAX_USUARIOS_MEMORIA    = 500
MAX_HISTORIAL_MENSAJES  = 20
MAX_CACHE_EMBEDDINGS    = 2000
CACHE_TTL_MINUTOS       = 5
MAX_CACHE_RESPUESTAS    = 1000
DEBOUNCE_SEGUNDOS       = 5.0
MAX_MENSAJES_POR_MINUTO = 10
MAX_AUDIO_MB            = 10

# ══════════════════════════════════════════════════════════
# REDIS
# ══════════════════════════════════════════════════════════

redis_client = None
if REDIS_DISPONIBLE:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        logger.info("✅ Redis conectado")
    except Exception as e:
        logger.warning(f"Redis no disponible ({e}). Usando memoria.")

# ══════════════════════════════════════════════════════════
# SUPABASE
# ══════════════════════════════════════════════════════════

supabase: "SupabaseClient | None" = None
if SUPABASE_DISPONIBLE and SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Supabase conectado (modo RAG)")
    except Exception as e:
        logger.warning(f"Supabase no disponible ({e}). Usando JSON.")

# ══════════════════════════════════════════════════════════
# FALLBACK JSON
# ══════════════════════════════════════════════════════════

COMERCIOS          = []
COMERCIOS_COMPACTO = []

if not supabase:
    json_path = BASE_DIR / "data" / "comercios.json"
    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            COMERCIOS = json.load(f)
        for i, c in enumerate(COMERCIOS):
            comp = {k: v for k, v in c.items() if k not in ("lat", "lon", "maps")}
            comp["id"] = i
            COMERCIOS_COMPACTO.append(comp)
        logger.info(f"📦 Fallback JSON: {len(COMERCIOS)} entradas")
    else:
        logger.error("Sin Supabase ni comercios.json. El bot no puede funcionar.")
        sys.exit(1)


# ══════════════════════════════════════════════════════════
# LRU DICT
# ══════════════════════════════════════════════════════════

class LRUDict(OrderedDict):
    """OrderedDict con tamaño máximo. Expulsa el más viejo al superar el límite."""

    def __init__(self, max_size: int, *args, **kwargs):
        self._max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._max_size:
            self.popitem(last=False)


# ══════════════════════════════════════════════════════════
# MEMORIA (LRU)
# ══════════════════════════════════════════════════════════

historiales_memoria = LRUDict(MAX_USUARIOS_MEMORIA)
ubicaciones_memoria = LRUDict(MAX_USUARIOS_MEMORIA)

# Set para trackear usuarios que ya recibieron bienvenida (en esta sesión)
_usuarios_bienvenida: set[str] = set()

# ══════════════════════════════════════════════════════════
# CACHÉS
# ══════════════════════════════════════════════════════════

_cache_lock = asyncio.Lock()

cache_respuestas_global:  dict[str, dict] = {}
cache_respuestas_usuario: dict[str, dict] = {}

_cache_embeddings: LRUDict = LRUDict(MAX_CACHE_EMBEDDINGS)

# ══════════════════════════════════════════════════════════
# COLA DE MENSAJES — generation counter
# ══════════════════════════════════════════════════════════

_cola_lock_global = asyncio.Lock()
cola_mensajes: dict[str, dict] = {}

# ══════════════════════════════════════════════════════════
# RATE LIMITING
# ══════════════════════════════════════════════════════════

_rate_limit: dict[str, list[datetime]] = {}

# ══════════════════════════════════════════════════════════
# LOGS
# ══════════════════════════════════════════════════════════

LOG_BUSQUEDAS = BASE_DIR / "data" / "logs_busquedas.csv"


# ══════════════════════════════════════════════════════════
# SINÓNIMOS Y EXPANSIÓN DE CONSULTA                  v4 NEW
# ══════════════════════════════════════════════════════════

# Mapa: lo que el usuario dice → términos que deberían matchear en los datos
# Se usa tanto para el filtro JSON como para expandir la query de Supabase
SINONIMOS: dict[str, list[str]] = {
    # ── Gastronomía (matchea categorías y tags reales) ──
    "pizza":        ["pizzeria", "pizzería"],
    "pizzas":       ["pizzeria", "pizzería"],
    "fugazzeta":    ["pizzeria", "pizzería"],
    "muzzarella":   ["pizzeria", "pizzería"],
    "empanada":     ["empanadas", "gastronomia", "gastronomía"],
    "empanadas":    ["gastronomia", "gastronomía", "pizzeria", "pizzería"],
    "hamburguesa":  ["hamburgueseria", "hamburguesería", "hamburguesas"],
    "hamburguesas": ["hamburgueseria", "hamburguesería"],
    "burger":       ["hamburgueseria", "hamburguesería", "hamburguesas"],
    "sushi":        ["sushi", "japones", "japonés", "rolls"],
    "rolls":        ["sushi", "japones"],
    "helado":       ["heladeria", "heladería"],
    "helados":      ["heladeria", "heladería"],
    "facturas":     ["panaderia", "panadería"],
    "pan":          ["panaderia", "panadería"],
    "medialunas":   ["panaderia", "panadería", "cafeteria", "cafetería"],
    "torta":        ["panaderia", "panadería", "pasteleria"],
    "tortas":       ["panaderia", "panadería", "pasteleria"],
    "cafe":         ["cafeteria", "cafetería", "cafe", "café"],
    "cafecito":     ["cafeteria", "cafetería", "cafe", "café"],
    "desayuno":     ["cafeteria", "cafetería", "cafe", "café", "brunch"],
    "merienda":     ["cafeteria", "cafetería", "cafe", "café"],
    "brunch":       ["cafeteria", "cafetería", "cafe", "café", "brunch"],
    "alfajor":      ["alfajores", "havanna", "kiosco"],
    "alfajores":    ["havanna", "kiosco", "chocolate"],
    "chocolate":    ["havanna", "kiosco", "cafeteria"],
    "carne":        ["carniceria", "carnicería", "asado"],
    "asado":        ["carniceria", "carnicería", "parrilla", "restaurante"],
    "achuras":      ["carniceria", "carnicería"],
    "vacio":        ["carniceria", "carnicería"],
    "pollo":        ["carniceria", "carnicería", "granja"],
    "verdura":      ["verduleria", "verdulería"],
    "verduras":     ["verduleria", "verdulería"],
    "fruta":        ["verduleria", "verdulería", "frutas"],
    "frutas":       ["verduleria", "verdulería"],
    "organico":     ["verduleria", "verdulería", "organico"],
    "comer":        ["gastronomia", "gastronomía", "restaurante"],
    "comida":       ["gastronomia", "gastronomía", "restaurante"],
    "cenar":        ["gastronomia", "gastronomía", "restaurante"],
    "almorzar":     ["gastronomia", "gastronomía", "restaurante", "almuerzo"],
    "morfi":        ["gastronomia", "gastronomía", "restaurante"],
    "parrilla":     ["parrilla", "restaurante", "asado", "carne"],
    "pastas":       ["restaurante", "pastas"],
    "milanesa":     ["restaurante", "milanesas"],
    "milanesas":    ["restaurante"],
    "birra":        ["cerveceria", "cervecería", "bar", "cerveza"],
    "cerveza":      ["cerveceria", "cervecería", "bar", "cerveza artesanal"],
    "trago":        ["bar", "cerveceria"],
    "tragos":       ["bar", "cerveceria"],
    "picada":       ["bar", "cerveceria", "picadas"],
    "picadas":      ["bar", "cerveceria"],

    # ── Salud ──
    "remedio":      ["farmacia", "medicamentos"],
    "remedios":     ["farmacia", "medicamentos"],
    "medicamento":  ["farmacia", "medicamentos"],
    "medicamentos": ["farmacia"],
    "pastilla":     ["farmacia", "medicamentos"],
    "pastillas":    ["farmacia", "medicamentos"],
    "perfumeria":   ["farmacia", "farmacity", "cosmeticos"],
    "cosmeticos":   ["farmacia", "farmacity", "perfumeria"],
    "oculista":     ["optica", "óptica", "anteojos", "lentes"],
    "anteojos":     ["optica", "óptica"],
    "lentes":       ["optica", "óptica"],

    # ── Mascotas (matchea datos reales) ──
    "veterinario":  ["veterinaria", "mascotas"],
    "perro":        ["veterinaria", "petshop", "pet shop", "mascotas"],
    "gato":         ["veterinaria", "petshop", "pet shop", "mascotas"],
    "mascota":      ["veterinaria", "petshop", "pet shop", "mascotas"],
    "mascotas":     ["veterinaria", "petshop", "pet shop"],
    "alimento":     ["petshop", "pet shop", "veterinaria"],

    # ── Servicios del hogar (matchea rubros reales) ──
    "plomero":      ["plomeria", "plomería"],
    "caño":         ["plomeria", "plomería", "plomero"],
    "cañeria":      ["plomeria", "plomería", "plomero"],
    "agua":         ["plomeria", "plomería", "plomero"],
    "electricista": ["electricidad", "electrico"],
    "enchufe":      ["electricidad", "electricista"],
    "luz":          ["electricidad", "electricista"],
    "cortocircuito":["electricidad", "electricista"],
    "albañil":      ["albañileria", "albañilería", "construccion"],
    "obra":         ["albañileria", "albañilería", "construccion"],
    "reforma":      ["albañileria", "albañilería", "construccion"],
    "construccion": ["albañileria", "albañilería"],
    "llave":        ["cerrajeria", "cerrajería", "cerrajero"],
    "cerradura":    ["cerrajeria", "cerrajería", "cerrajero"],
    "cerrajero":    ["cerrajeria", "cerrajería"],
    "pintar":       ["pintura", "pintor"],
    "pintor":       ["pintura"],
    "pasto":        ["jardineria", "jardinería", "jardinero"],
    "jardin":       ["jardineria", "jardinería", "jardinero"],
    "poda":         ["jardineria", "jardinería", "jardinero"],
    "jardinero":    ["jardineria", "jardinería"],
    "gas":          ["gasista"],
    "estufa":       ["gasista"],
    "calefon":      ["gasista", "plomeria"],
    "calefaccion":  ["gasista"],
    "aire":         ["aire acondicionado"],
    "split":        ["aire acondicionado"],
    "acondicionado":["aire acondicionado"],
    "mudanza":      ["flete", "fletes", "mudanza"],
    "mudanzas":     ["flete", "fletes"],
    "flete":        ["fletes", "mudanza"],

    # ── Compras / Comercio ──
    "coca":         ["kiosco", "bebidas"],
    "golosinas":    ["kiosco"],
    "cigarrillos":  ["kiosco"],
    "snacks":       ["kiosco"],
    "galletitas":   ["kiosco", "almacen"],
    "bebida":       ["kiosco", "bebidas"],
    "bebidas":      ["kiosco"],
    "ferreteria":   ["ferreteria", "herramientas"],
    "herramienta":  ["ferreteria", "herramientas"],
    "herramientas": ["ferreteria"],
    "tornillo":     ["ferreteria", "tornillos"],
    "tornillos":    ["ferreteria"],
    "clavo":        ["ferreteria"],
    "clavos":       ["ferreteria"],
    "pintura":      ["ferreteria", "pintura"],   # como producto en ferretería
    "materiales":   ["ferreteria", "construccion", "corralon"],
    "corralon":     ["ferreteria", "construccion", "materiales"],
    "arena":        ["ferreteria", "construccion", "materiales"],

    # ── Fitness / Deporte ──
    "gimnasio":     ["gimnasio", "fitness", "musculacion"],
    "gym":          ["gimnasio", "fitness", "musculacion"],
    "crossfit":     ["crossfit", "funcional", "gimnasio"],
    "entrenar":     ["gimnasio", "fitness", "crossfit"],
    "spinning":     ["gimnasio", "spinning"],
    "yoga":         ["gimnasio", "yoga"],
    "pileta":       ["gimnasio", "pileta", "natacion"],
    "natacion":     ["gimnasio", "pileta"],
    "paddle":       ["paddle", "tenis"],
    "tenis":        ["tenis", "paddle"],

    # ── Estética / Cuidado personal ──
    "peluqueria":   ["peluqueria", "peluquería", "corte"],
    "peluquero":    ["peluqueria", "peluquería"],
    "corte":        ["peluqueria", "peluquería", "barberia", "barbería"],
    "tintura":      ["peluqueria", "peluquería", "color"],
    "barberia":     ["barberia", "barbería", "barba"],
    "barbero":      ["barberia", "barbería", "barba"],
    "barba":        ["barberia", "barbería"],
    "uñas":         ["estetica", "estética"],
    "depilacion":   ["estetica", "estética"],

    # ── Lavadero ──
    "lavadero":     ["lavadero", "lavanderia"],
    "lavanderia":   ["lavadero", "lavanderia"],
    "lavar":        ["lavadero", "lavanderia"],

    # ── Vehículos ──
    "auto":         ["mecanico", "mecánico", "taller"],
    "mecanico":     ["mecanico", "mecánico", "taller"],
    "rueda":        ["gomeria", "gomería"],
    "goma":         ["gomeria", "gomería"],
    "pinchada":     ["gomeria", "gomería"],
    "nafta":        ["estacion de servicio", "ypf", "shell"],
}


def expandir_consulta(consulta: str) -> str:
    """
    Expande la consulta con sinónimos. Ej:
    "quiero pizza en City Bell" → "quiero pizza pizzeria pizzería en City Bell"
    """
    palabras = normalizar_texto(consulta).split()
    extras   = set()
    for p in palabras:
        if p in SINONIMOS:
            for s in SINONIMOS[p]:
                extras.add(normalizar_texto(s))
    if extras:
        expandida = consulta + " " + " ".join(extras)
        logger.info(f"Query expandida: +{extras}")
        return expandida
    return consulta


# ══════════════════════════════════════════════════════════
# NORMALIZACIÓN
# ══════════════════════════════════════════════════════════

def normalizar_texto(texto: str) -> str:
    """Minúsculas + quitar acentos + strip."""
    texto = texto.lower().strip()
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ══════════════════════════════════════════════════════════
# ALMACENAMIENTO
# ══════════════════════════════════════════════════════════

def obtener_historial(user_id: str) -> list:
    if redis_client:
        try:
            data = redis_client.get(f"historial:{user_id}")
            if data:
                return json.loads(data)
        except Exception as e:
            logger.warning(f"Redis get: {e}")
    return list(historiales_memoria.get(user_id, []))


def guardar_historial(user_id: str, historial: list):
    if redis_client:
        try:
            redis_client.setex(f"historial:{user_id}", 7200, json.dumps(historial))
            return
        except Exception as e:
            logger.warning(f"Redis set: {e}")
    historiales_memoria[user_id] = historial


def eliminar_historial(user_id: str):
    if redis_client:
        try:
            redis_client.delete(f"historial:{user_id}")
        except Exception:
            pass
    historiales_memoria.pop(user_id, None)


def obtener_ubicacion(user_id: str) -> tuple | None:
    if redis_client:
        try:
            data = redis_client.get(f"ubicacion:{user_id}")
            if data:
                c = json.loads(data)
                return (c["lat"], c["lon"])
        except Exception as e:
            logger.warning(f"Redis get ubicación: {e}")
    return ubicaciones_memoria.get(user_id)


def guardar_ubicacion(user_id: str, lat: float, lon: float):
    if redis_client:
        try:
            redis_client.setex(
                f"ubicacion:{user_id}", 86400,
                json.dumps({"lat": lat, "lon": lon}),
            )
            return
        except Exception as e:
            logger.warning(f"Redis set ubicación: {e}")
    ubicaciones_memoria[user_id] = (lat, lon)


def es_usuario_nuevo(user_id: str) -> bool:
    """True si el usuario nunca interactuó (sin historial ni bienvenida previa)."""
    if user_id in _usuarios_bienvenida:
        return False
    historial = obtener_historial(user_id)
    if historial:
        _usuarios_bienvenida.add(user_id)
        return False
    return True


def marcar_bienvenida(user_id: str):
    _usuarios_bienvenida.add(user_id)


# ══════════════════════════════════════════════════════════
# RATE LIMITING
# ══════════════════════════════════════════════════════════

def verificar_rate_limit(user_id: str) -> bool:
    ahora   = datetime.now()
    ventana = ahora - timedelta(minutes=1)

    if user_id not in _rate_limit:
        _rate_limit[user_id] = []

    _rate_limit[user_id] = [t for t in _rate_limit[user_id] if t > ventana]

    if len(_rate_limit[user_id]) >= MAX_MENSAJES_POR_MINUTO:
        logger.warning(f"Rate limit alcanzado para {user_id}")
        return False

    _rate_limit[user_id].append(ahora)
    return True


# ══════════════════════════════════════════════════════════
# LIMPIEZA PERIÓDICA (cada 1 hora)
# ══════════════════════════════════════════════════════════

async def limpiar_cache_periodico():
    while True:
        await asyncio.sleep(3600)
        ahora        = datetime.now()
        ventana_rate = ahora - timedelta(minutes=1)

        async with _cache_lock:
            eliminados = {}
            for cache, label in [
                (cache_respuestas_global,  "global"),
                (cache_respuestas_usuario, "usuario"),
            ]:
                vencidos = [
                    k for k, v in cache.items()
                    if (ahora - v["timestamp"]).total_seconds() >= CACHE_TTL_MINUTOS * 60
                ]
                for k in vencidos:
                    del cache[k]
                eliminados[label] = len(vencidos)

        inactivos = [
            uid for uid, ts in _rate_limit.items()
            if not any(t > ventana_rate for t in ts)
        ]
        for uid in inactivos:
            del _rate_limit[uid]

        total = eliminados["global"] + eliminados["usuario"]
        if total > 0 or inactivos:
            logger.info(
                f"🧹 Limpieza: {eliminados['global']} caché global, "
                f"{eliminados['usuario']} caché usuario, "
                f"{len(inactivos)} rate-limit purgados"
            )


# ══════════════════════════════════════════════════════════
# RAG — EMBEDDINGS
# ══════════════════════════════════════════════════════════

async def obtener_embedding(texto: str) -> list[float]:
    key = hashlib.md5(texto.encode()).hexdigest()
    if key in _cache_embeddings:
        return _cache_embeddings[key]
    response = await client.embeddings.create(
        model="text-embedding-3-small",
        input=texto,
    )
    emb = response.data[0].embedding
    _cache_embeddings[key] = emb
    return emb


# ══════════════════════════════════════════════════════════
# DETECCIÓN DE ZONA
# ══════════════════════════════════════════════════════════

_ZONAS_MAP = {
    "city bell": "City Bell",
    "citybell":  "City Bell",
    "gonnet":    "Gonnet",
    "villa elisa": "Villa Elisa",
    "villaelisa":  "Villa Elisa",
}


def detectar_zona(texto: str) -> str | None:
    t = normalizar_texto(texto)
    for clave, zona in _ZONAS_MAP.items():
        if clave in t:
            return zona
    return None


# ══════════════════════════════════════════════════════════
# PARSER DE HORARIOS — PRECÁLCULO                    v4 NEW
# ══════════════════════════════════════════════════════════
# El LLM es MALO calculando si "Mar-Dom 18-24" está abierto a las 23:50.
# Solución: precalcular en Python e inyectar el resultado como campo.

_DIAS_NOMBRES: dict[str, int] = {
    "lunes": 0, "lun": 0, "lu": 0,
    "martes": 1, "mar": 1, "ma": 1,
    "miercoles": 2, "mie": 2, "mi": 2,
    "jueves": 3, "jue": 3, "ju": 3,
    "viernes": 4, "vie": 4, "vi": 4,
    "sabado": 5, "sab": 5, "sa": 5,
    "domingo": 6, "dom": 6, "do": 6,
}
# Single letters — solo para rangos claros tipo "L-V"
_DIAS_LETRA: dict[str, int] = {
    "l": 0, "m": 1, "x": 2, "j": 3, "v": 4, "s": 5, "d": 6,
}


def _normalizar_dia(s: str) -> int | None:
    """Convierte nombre/abreviatura de día a weekday (0=Lun, 6=Dom)."""
    s = normalizar_texto(s.strip().rstrip("."))
    if s in _DIAS_NOMBRES:
        return _DIAS_NOMBRES[s]
    if len(s) == 1 and s in _DIAS_LETRA:
        return _DIAS_LETRA[s]
    return None


def _parsear_hora(h_str: str) -> float | None:
    """Convierte '8', '8:30', '20', '24' → horas decimales."""
    h_str = h_str.strip().replace(".", ":")
    if ":" in h_str:
        parts = h_str.split(":")
        try:
            return int(parts[0]) + int(parts[1]) / 60
        except (ValueError, IndexError):
            return None
    try:
        val = float(h_str)
        return val if 0 <= val <= 24 else None
    except ValueError:
        return None


def _expandir_rango_dias(inicio: int, fin: int) -> list[int]:
    """Lun-Vie → [0,1,2,3,4]. Sab-Mar → [5,6,0,1]."""
    if fin >= inicio:
        return list(range(inicio, fin + 1))
    return list(range(inicio, 7)) + list(range(0, fin + 1))


def esta_abierto_ahora(horario_str: str | None, ahora: datetime) -> bool | None:
    """
    Determina si un comercio está abierto en este momento.
    Returns: True (abierto), False (cerrado), None (no se pudo determinar).

    Formatos soportados:
    - "24hs", "24 horas"
    - "Lun-Vie 8-20", "L-V 8-20"
    - "Mar-Dom 18-24"
    - "L-V 8-13 y 16-20" (turno partido)
    - "Lun-Sab 8-20 | Dom 9-13" (múltiples segmentos)
    - "Lun a Vie 8 a 20" (con "a" en vez de "-")
    - "Sab 9-13" (día suelto)
    """
    if not horario_str or not horario_str.strip():
        return None

    h = horario_str.strip()

    # 24hs / 24 horas → siempre abierto
    if re.search(r"24\s*(?:hs|horas?)", h, re.IGNORECASE):
        return True

    dia_actual  = ahora.weekday()  # 0=Lun, 6=Dom
    hora_actual = ahora.hour + ahora.minute / 60

    # Separar en segmentos por | ; o saltos de línea
    segmentos = re.split(r"[|;\n]", h)

    for seg in segmentos:
        seg = seg.strip()
        if not seg:
            continue

        seg_lower = seg.lower()

        # Saltar segmentos que dicen "cerrado"
        if "cerrado" in seg_lower:
            continue

        # Pre-procesar: "lun a vie" → "lun-vie", "8 a 20" → "8-20"
        seg_proc = re.sub(r"(\w+)\s+a\s+(\w+)", r"\1-\2", seg_lower)

        # ── Detectar días ──
        dias_validos = None

        # Rango de días: "lun-vie", "mar-dom", "l-v"
        day_range = re.search(
            r"\b([a-záéíóú]+)\s*[-–]\s*([a-záéíóú]+)\b",
            seg_proc,
        )
        if day_range:
            d1 = _normalizar_dia(day_range.group(1))
            d2 = _normalizar_dia(day_range.group(2))
            if d1 is not None and d2 is not None:
                dias_validos = _expandir_rango_dias(d1, d2)

        # Día suelto: "sab 9-13"
        if dias_validos is None:
            first_word = re.match(r"([a-záéíóú]+)", seg_proc)
            if first_word:
                d = _normalizar_dia(first_word.group(1))
                if d is not None:
                    dias_validos = [d]

        # Sin info de días → asumir todos los días
        if dias_validos is None:
            dias_validos = list(range(7))

        if dia_actual not in dias_validos:
            continue

        # ── Detectar rangos horarios ──
        # Usar seg_proc donde "9 a 21" ya fue convertido a "9-21"
        time_ranges = re.findall(
            r"(\d{1,2}(?:[:.]\d{2})?)\s*[-–]\s*(\d{1,2}(?:[:.]\d{2})?)",
            seg_proc,
        )

        for open_str, close_str in time_ranges:
            open_h  = _parsear_hora(open_str)
            close_h = _parsear_hora(close_str)
            if open_h is None or close_h is None:
                continue

            if close_h > open_h:
                # Turno normal: 8-20, 18-24
                if open_h <= hora_actual < close_h:
                    return True
            elif close_h < open_h:
                # Turno nocturno (cruza medianoche): 22-6, 18-2
                if hora_actual >= open_h or hora_actual < close_h:
                    return True
            # close_h == open_h → dato raro, ignorar

    # Si llegamos acá y procesamos al menos un segmento, está cerrado
    return False


def inyectar_estado_horario(comercios: list[dict], ahora: datetime) -> list[dict]:
    """
    Agrega campo 'estado_actual' a cada comercio basado en sus horarios.
    Los servicios (sin horarios) no se tocan.
    """
    for c in comercios:
        horario = c.get("horarios") or c.get("horario") or ""
        if not horario:
            continue
        estado = esta_abierto_ahora(horario, ahora)
        if estado is True:
            c["estado_actual"] = "ABIERTO AHORA ✅"
        elif estado is False:
            c["estado_actual"] = "CERRADO AHORA ❌"
        # None → no se pudo determinar, no se agrega campo
    return comercios


# ══════════════════════════════════════════════════════════
# FILTRO JSON LOCAL — MEJORADO                       v4
# ══════════════════════════════════════════════════════════

_STOPWORDS = frozenset({
    "hay", "busco", "quiero", "necesito", "me", "un", "una", "unos", "unas",
    "en", "de", "que", "por", "para", "los", "las", "el", "la", "lo",
    "con", "sin", "del", "al", "y", "o", "a", "es", "si", "no",
    "mas", "muy", "bien", "como", "donde", "cerca", "algun", "alguno",
    "alguna", "tiene", "tenes", "ahora", "abierto", "abierta", "abiertos",
    "abiertas", "hoy", "buen", "buenas", "buena", "buenos",
})

# Pesos por campo: matchear en nombre vale más que en tags
_PESO_CAMPO = {
    "nombre":    4,
    "categoria": 3,
    "rubro":     3,
    "tags":      1,
    "zona":      0,   # zona se maneja aparte con bonus
}


def filtrar_json_local(consulta: str, zona: str | None = None, top_k: int = 12) -> list[dict]:
    """
    Filtro mejorado con:
    - Scoring ponderado por campo
    - Matching parcial (mínimo 4 chars)
    - Expansión de sinónimos ya aplicada a la consulta
    Si no matchea nada, devuelve lista vacía.
    """
    consulta_expandida = expandir_consulta(consulta)

    palabras = {
        p for p in normalizar_texto(consulta_expandida).split()
        if p not in _STOPWORDS and len(p) > 2
    }

    if not palabras and not zona:
        logger.warning("Filtro JSON: sin keywords útiles ni zona.")
        return []

    scored = []
    for c in COMERCIOS_COMPACTO:
        score = 0.0

        for campo, peso in _PESO_CAMPO.items():
            valor = normalizar_texto(str(c.get(campo, "")))
            if not valor:
                continue
            for p in palabras:
                # Match exacto (substring)
                if p in valor:
                    score += peso
                # Match parcial: si la palabra tiene 4+ chars y es prefijo
                elif len(p) >= 4 and any(
                    word.startswith(p) for word in valor.split()
                ):
                    score += peso * 0.5

        # Bonus por zona
        if zona:
            zona_comercio = normalizar_texto(str(c.get("zona", "")))
            if normalizar_texto(zona) in zona_comercio:
                score += 5

        if score > 0:
            scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    resultado = [c for _, c in scored[:top_k]]

    if not resultado:
        logger.warning("Filtro JSON sin matches.")
        return []

    logger.info(f"Filtro JSON local: {len(resultado)} resultados (zona={zona})")
    return resultado


async def buscar_relevantes(consulta: str, zona: str | None = None, top_k: int = 12) -> list[dict]:
    """Búsqueda semántica en Supabase. Fallback a filtro JSON."""
    if not supabase:
        return filtrar_json_local(consulta, zona=zona, top_k=top_k)

    try:
        # Expandir consulta con sinónimos para mejor embedding
        consulta_expandida = expandir_consulta(consulta)
        embedding = await obtener_embedding(consulta_expandida)

        result = supabase.rpc("buscar_comercios", {
            "query_embedding": embedding,
            "zona_filtro":     zona,
            "top_k":           top_k,
        }).execute()

        if result.data:
            for c in result.data:
                c.pop("embedding", None)
                c.pop("similarity", None)
            logger.info(f"RAG: {len(result.data)} resultados (zona={zona})")
            return result.data

        logger.warning("RAG sin resultados, usando filtro JSON fallback")
        return filtrar_json_local(consulta, zona=zona, top_k=top_k)

    except Exception as e:
        logger.error(f"Error RAG: {e}. Usando filtro JSON fallback.")
        return filtrar_json_local(consulta, zona=zona, top_k=top_k)


# ══════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════

def calcular_distancia(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def registrar_busqueda_sync(user_id: str, mensaje: str, tipo: str = "texto"):
    try:
        nuevo = not LOG_BUSQUEDAS.exists()
        LOG_BUSQUEDAS.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_BUSQUEDAS, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(["timestamp", "user_id", "tipo", "mensaje"])
            w.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                user_id, tipo, mensaje,
            ])
    except Exception as e:
        logger.warning(f"Error log: {e}")


async def registrar_busqueda(user_id: str, mensaje: str, tipo: str = "texto"):
    await asyncio.to_thread(registrar_busqueda_sync, user_id, mensaje, tipo)


async def transcribir_audio(voice_file, file_size: int | None = None) -> str | None:
    if file_size and file_size > MAX_AUDIO_MB * 1024 * 1024:
        logger.warning(f"Audio demasiado grande: {file_size / 1024 / 1024:.1f} MB")
        return None

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await voice_file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as audio:
            t = await client.audio.transcriptions.create(
                model="whisper-1", file=audio, language="es",
            )
        return t.text.strip()
    except Exception as e:
        logger.error(f"Error transcribiendo: {e}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════
# COLA DE MENSAJES — generation counter
# ══════════════════════════════════════════════════════════

async def agregar_mensaje_a_cola(user_id: str, mensaje: str, update: Update):
    async with _cola_lock_global:
        if user_id not in cola_mensajes:
            cola_mensajes[user_id] = {
                "mensajes": [], "update": None, "generation": 0,
            }
        cola_mensajes[user_id]["mensajes"].append(mensaje)
        cola_mensajes[user_id]["update"] = update
        cola_mensajes[user_id]["generation"] += 1
        gen = cola_mensajes[user_id]["generation"]

    asyncio.create_task(_esperar_y_procesar(user_id, gen))


async def _esperar_y_procesar(user_id: str, generation: int):
    try:
        await asyncio.sleep(DEBOUNCE_SEGUNDOS)

        async with _cola_lock_global:
            entry = cola_mensajes.get(user_id)
            if not entry or entry["generation"] != generation:
                return
            mensajes = entry["mensajes"]
            update   = entry["update"]
            del cola_mensajes[user_id]

        if not mensajes or not update:
            return

        mensaje_final = (
            mensajes[0] if len(mensajes) == 1
            else "\n".join(f"{i+1}. {m}" for i, m in enumerate(mensajes))
        )

        if len(mensajes) > 1:
            logger.info(f"{user_id}: {len(mensajes)} mensajes agrupados")

        await registrar_busqueda(
            user_id,
            mensaje_final if len(mensajes) == 1 else f"[{len(mensajes)} agrupados]",
        )
        await update.message.chat.send_action(ChatAction.TYPING)
        respuesta = await obtener_respuesta(user_id, mensaje_final, skip_log=True)
        logger.info(f"Respuesta: {respuesta[:100]}...")

        # Enviar respuesta
        await responder_seguro(update.message, respuesta, disable_web_page_preview=True)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Error cola {user_id}: {e}")


# ══════════════════════════════════════════════════════════
# PROMPT DEL SISTEMA v2
# ══════════════════════════════════════════════════════════

PROMPT_SISTEMA_BASE = """
=== IDENTIDAD ===
Sos "Vecinito" 🏘️, el guía local de City Bell, Gonnet y Villa Elisa.
Sos un vecino, no un robot. Hablás de igual a igual, con buena onda.
Tu único objetivo es ayudar a encontrar COMERCIOS y SERVICIOS de la zona.

=== TIPOS DE ENTRADA ===
Hay dos tipos de datos en "DATOS DISPONIBLES":
- COMERCIO: tiene campo "categoria". Tiene dirección, horarios, zona. Es un local físico.
- SERVICIO: tiene campo "rubro". NO tiene dirección, zona ni horarios. Es una persona que trabaja a domicilio.

Diferencia clave: los servicios son personas que van al domicilio del cliente, por eso no tienen local ni horarios fijos. NUNCA inventes dirección, zona ni horarios para un servicio.

=== LÍMITE DE TEMA ===
SOLO respondés sobre comercios, servicios, productos y oficios de la zona.

Excepciones que SÍ debés manejar naturalmente (NO son fuera de tema):
- Saludos y despedidas: "gracias", "genial", "chau", "dale", "ya lo llamo" → respondé con calidez breve ("De nada! Cualquier cosa acá estoy 😊").
- Contexto antes de un pedido: "está lloviendo y necesito un techista" → la intención es buscar un servicio, respondé normalmente.
- Feedback: "no me sirve", "muy lejos", "otro?" → ofrecé alternativas o pedí más contexto.
- Preguntas sobre vos: "qué sos?", "cómo funcionás?" → respondé brevemente que sos un bot vecinal que ayuda a encontrar comercios y servicios en City Bell, Gonnet y Villa Elisa.
- Pedido específico por nombre: "tenés el teléfono de Pizzería Los Tíos?" → si está en los datos, respondé con esa única entrada sin listar alternativas innecesarias.

Para temas genuinamente fuera de alcance (política, clima, recetas, matemática, etc.) respondé SOLO:
"Jaja, eso no es lo mío 😅 Yo te ayudo con comercios y servicios de la zona. ¿Necesitás algo?"

CLAVE: evaluá siempre la INTENCIÓN FINAL del mensaje. Si la intención es encontrar un comercio o servicio, respondé aunque haya contexto irrelevante de por medio.

=== REGLAS DE TONO ===
1. REACCIÓN ANTES DE ACCIÓN: Primero empatizá brevemente con lo que piden.
   - Comida: "Uhhh, se viene el antojo! 😋"
   - Problema urgente: "Uh, qué garrón. Pero tranqui, se soluciona 💪"
   - Búsqueda general: "Dale, te busco!"
2. LENGUAJE NATURAL: "Dale", "Fijate", "Te paso", "Buenísimo", "Che".
   NUNCA uses: "Su solicitud", "He encontrado", "Procesando", "A continuación".
3. Máximo 1-2 emojis por mensaje (fuera de las tarjetas de resultado).
4. Sé conciso. No repitas información ni agregues relleno.

=== INSTRUCCIONES DE RAZONAMIENTO ===

1. CONTEXTO CONVERSACIONAL:
   Leé el historial completo antes de responder. Si dicen "cuáles están abiertas" sin especificar qué, buscá en el historial qué estaban buscando antes. Si no hay contexto previo, preguntá: "¿Abiertas de qué? Contame qué buscás 😊"

2. INTERPRETACIÓN DE BÚSQUEDAS:
   - Marcas (Coca-Cola, Franui, Havanna) → buscar KIOSCOS o comercios que vendan esa marca.
   - Productos genéricos (pizza, clavos, pan) → buscar el RUBRO correspondiente (pizzería, ferretería, panadería).
   - Rubro directo (plomero, electricista) → buscar ese rubro en servicios o comercios.
   - Si la búsqueda es ambigua entre comercio y servicio (ej: "cerrajero"), mostrá ambos tipos si existen en los datos.
   - Si la búsqueda es amplia (ej: "comida"), mostrá variedad de rubros gastronómicos, no solo uno.

3. HORARIOS — REGLAS:
   El mensaje del usuario incluye automáticamente: [Hoy es {Día} {Fecha}, son las {Hora} hs]

   IMPORTANTE — CAMPO "estado_actual":
   Cada comercio PUEDE tener un campo "estado_actual" precalculado por el sistema:
   - "ABIERTO AHORA ✅" → el comercio ESTÁ abierto en este momento. Confiá en este dato.
   - "CERRADO AHORA ❌" → el comercio ESTÁ cerrado en este momento. Confiá en este dato.
   - Si NO tiene campo "estado_actual" → no se pudo determinar. Mostrá el horario tal cual sin afirmar si está abierto o cerrado.

   NUNCA intentes calcular horarios por tu cuenta. SIEMPRE usá el campo "estado_actual" si existe.

   Reglas según lo que pide el usuario:
   a) Si pide "ABIERTOS" o "ABIERTOS AHORA": SOLO mostrá los que tengan estado_actual = "ABIERTO AHORA ✅". Si ninguno está abierto, decí: "Uf, a esta hora están todos cerrados 😴 ¿Querés que te muestre los horarios para que vayas después?"
   b) Si pide comercios SIN especificar "abiertos": mostrá todos los relevantes. Si tiene estado_actual "ABIERTO AHORA ✅", incluilo en el campo horario. Si tiene "CERRADO AHORA ❌", mostrá solo el horario normal sin destacar.
   c) NUNCA ocultes un comercio relevante solo porque está cerrado, a menos que el usuario haya pedido explícitamente "abiertos".
   d) Si un comercio no tiene horarios cargados en los datos, mostrá "🕐 Consultar horarios" en vez de inventar.

4. RANKING Y ORDEN DE RESULTADOS:
   a) Si hay "UBICACIÓN DEL USUARIO" en el contexto:
      - Urgencias (farmacia, plomero, electricista, cerrajero, gasista): ordenar por CERCANÍA.
      - Gastronomía y experiencias (parrilla, heladería, "lugar lindo"): ordenar por ESPECIALIDAD/relevancia, pero mostrar distancia.
      - Resto: ordenar por CERCANÍA.
   b) Si NO hay ubicación del usuario:
      - Ordenar por RELEVANCIA al pedido (qué tan bien matchea con lo que busca).
      - Si detectás una zona en el mensaje ("en City Bell"), priorizá esa zona.
   c) Para SERVICIOS: priorizar por AÑOS DE EXPERIENCIA (mayor primero).

5. SIN RESULTADOS:
   - Si no hay nada en los datos: "Uh, no tengo [X] en mi base todavía 😅 Si conocés alguno, avisame y lo sumo!"
   - Si hay resultados pero ninguno abierto (y pidió abiertos): "Uf, a esta hora están todos cerrados 😴 Te paso los horarios así sabés cuándo ir:"

=== REGLA DE ORO: CERO INVENCIÓN ===
SOLO podés mostrar información que esté TEXTUALMENTE en los DATOS DISPONIBLES.
- Si un dato no está (teléfono, horario, dirección): NO lo inventes. Indicá que no está disponible.
- Si un comercio/servicio no aparece en DATOS DISPONIBLES: NO existe para vos, aunque lo conozcas del mundo real.
- El tip final (💡) SOLO puede contener información que se desprenda de los datos (ej: "es el más cercano" si la distancia lo confirma, "tiene más experiencia" si los años lo confirman). NUNCA inventes atributos como "tiene delivery", "es el mejor", "tiene estacionamiento" si no está en los datos.

=== MANEJO DE DATOS INCOMPLETOS ===
- Sin teléfono/contacto: no pongas el campo 📞 (excepto en servicios donde es crítico; en ese caso poné "📞 No disponible — consultá por redes").
- Sin horarios (en comercio): poné "🕐 Consultar horarios".
- Sin dirección (en comercio): poné "📫 Consultar dirección".
- Sin experiencia (en servicio): no pongas el campo ⭐.
- Campo "tags" es SOLO para tu razonamiento interno. NUNCA lo muestres al usuario.

=== FORMATO DE RESPUESTA ===

COMERCIO:
[Reacción empática breve]

📍 *[Nombre]*
🏷️ [Categoría]
📫 [Dirección]
🕐 [Horarios — agregar "ABIERTO AHORA ✅" si corresponde]
🚶 [X.X km / X metros] ← SOLO si hay "UBICACIÓN DEL USUARIO" en el contexto
📞 [Contacto] ← SOLO si existe en los datos

SERVICIO:
[Reacción empática breve]

🔧 *[Nombre]*
🏷️ [Rubro]
⭐ [X años de experiencia] ← SOLO si existe en los datos
📞 [Contacto] ← OBLIGATORIO, si no existe poné "No disponible"

REGLAS DE FORMATO:
- SIN líneas separadoras (no uses ---, ***, ===, etc.)
- Negrita SOLO para el nombre del comercio/servicio: *Nombre*
- Una línea vacía entre cada tarjeta
- Máximo 4 resultados por respuesta (si hay más, mostrá los 4 mejores y ofrecé: "¿Querés que te muestre más opciones?")
- Si el usuario pide uno específico por nombre, mostrá solo ese
- NO incluyas links de Google Maps (se agregan automáticamente después)
- Distancia 🚶 SOLO si ves "UBICACIÓN DEL USUARIO" en el contexto
- Tip final 💡 SOLO si podés decir algo útil basado en los datos reales. Si no, no pongas tip.

=== MANEJO DE FEEDBACK DEL USUARIO ===
- "No me sirve" / "Muy lejos" / "Otro" / "Alguno más?" → Ofrecé alternativas de los datos. Si no hay más, decilo: "No tengo más opciones de [X] cargadas 😅"
- "Gracias" / "Genial" / "Dale" / "Ya lo llamo" → "De nada! Cualquier cosa acá estoy 😊" (breve, sin forzar otra búsqueda)
- "No entiendo" / respuesta confusa del usuario → Pedí clarificación amablemente: "No te entendí bien 😅 ¿Qué estás buscando?"

=== EJEMPLOS ===

Ejemplo 1 — COMERCIO (búsqueda de pizza):
Uhhh, se viene la pizza! 🍕

📍 *Pizzería Los Tíos*
🏷️ Gastronomía
📫 Calle 13 nro 456, City Bell
🕐 ABIERTO AHORA ✅ · L-V 18-23 | S-D 12-24
📞 https://wa.me/5492214567890

📍 *Pizza Napoli*
🏷️ Gastronomía
📫 Calle 14 nro 890, City Bell
🕐 L-D 19-24
📞 +54 221 456 1001

💡 Los Tíos está abierto ahora si tenés hambre ya!

Ejemplo 2 — SERVICIO (búsqueda de plomero):
Uh, qué garrón. Pero se soluciona 💪

🔧 *Carlos Pérez*
🏷️ Plomero
⭐ 15 años de experiencia
📞 +54 221 555 1234

🔧 *Mario Gómez*
🏷️ Plomero
⭐ 8 años de experiencia
📞 https://wa.me/5492215551235

💡 Carlos es el que tiene más experiencia!

Ejemplo 3 — COMERCIO CON UBICACIÓN:
Dale, te busco lo más cercano 📍

📍 *Farmacia Santa Ana 24hs*
🏷️ Salud
📫 Calle 14 nro 1200, City Bell
🕐 ABIERTO AHORA ✅ · 24 horas
🚶 450 metros
📞 +54 221 456 7893

💡 Es la más cercana y está abierta las 24hs!

Ejemplo 4 — SIN RESULTADOS:
Uh, no tengo veterinarias cargadas en mi base todavía 😅
Si conocés alguna de la zona, avisame y la sumo!

Ejemplo 5 — TODOS CERRADOS (pidió "abiertos"):
Uf, a esta hora las panaderías están todas cerradas 😴
Te paso las opciones así sabés cuándo ir:

📍 *Panadería Don Juan*
🏷️ Panadería
📫 Calle 7 nro 300, Gonnet
🕐 L-S 7-13 | D cerrado

Ejemplo 6 — FEEDBACK "no me sirve":
Usuario: "no, esos no, algún otro?"
Dale, te busco más opciones! [muestra otros resultados de los datos]
— o si no hay más: "No tengo más [rubro] cargados por ahora 😅 ¿Te puedo ayudar con otra cosa?"
"""


# ══════════════════════════════════════════════════════════
# ENVÍO Y RESPUESTA
# ══════════════════════════════════════════════════════════

async def responder_seguro(message, texto: str, **kwargs):
    try:
        await message.reply_text(texto, parse_mode="Markdown", **kwargs)
    except Exception:
        try:
            await message.reply_text(texto, **kwargs)
        except Exception as e:
            logger.error(f"Error enviando: {e}")
            try:
                await message.reply_text(
                    "Ups, tuve un problema mostrando la respuesta 😅 ¿Podés intentar de nuevo?"
                )
            except Exception:
                pass


def inyectar_maps_links(respuesta: str, comercios: list[dict]) -> str:
    for c in comercios:
        nombre = c.get("nombre", "")
        maps   = c.get("maps", "")
        if not maps or nombre not in respuesta:
            continue
        marcador = f"*{nombre}*"
        if maps in respuesta or f"{marcador}\n   🗺️" in respuesta:
            continue
        respuesta = respuesta.replace(marcador, f"{marcador}\n   🗺️ {maps}", 1)
    return respuesta


async def obtener_respuesta(user_id: str, mensaje: str, skip_log: bool = False) -> str:
    historial = obtener_historial(user_id)
    ahora     = datetime.now()

    historial.append({
        "role": "user", "content": mensaje, "timestamp": ahora.isoformat(),
    })

    if not mensaje.startswith("Repetí la búsqueda") and not skip_log:
        await registrar_busqueda(user_id, mensaje)

    # Filtrar última hora
    hace_una_hora = ahora - timedelta(hours=1)
    historial_rec = [
        m for m in historial
        if datetime.fromisoformat(m.get("timestamp", ahora.isoformat())) > hace_una_hora
    ]

    # Límite de mensajes
    if len(historial_rec) > MAX_HISTORIAL_MENSAJES:
        historial_rec = historial_rec[-MAX_HISTORIAL_MENSAJES:]

    guardar_historial(user_id, historial_rec)

    # ── Caché ─────────────────────────────────────────────
    ubicacion       = obtener_ubicacion(user_id)
    tiene_ubicacion = ubicacion is not None

    if tiene_ubicacion:
        lat_u, lon_u = ubicacion
        loc_hash     = f"{lat_u:.4f},{lon_u:.4f}"
        cache_key    = hashlib.md5(f"{user_id}:{loc_hash}:{mensaje}".encode()).hexdigest()
        cache_activo = cache_respuestas_usuario
        cache_label  = "personal"
    else:
        cache_key    = hashlib.md5(normalizar_texto(mensaje).encode()).hexdigest()
        cache_activo = cache_respuestas_global
        cache_label  = "global"

    if cache_key in cache_activo:
        cached = cache_activo[cache_key]
        if (ahora - cached["timestamp"]).total_seconds() < CACHE_TTL_MINUTOS * 60:
            logger.info(f"Cache hit! (tipo={cache_label})")
            historial_rec.append({
                "role": "assistant", "content": cached["respuesta"],
                "timestamp": ahora.isoformat(),
            })
            guardar_historial(user_id, historial_rec)
            return cached["respuesta"]

    # ── Contexto dinámico ─────────────────────────────────
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    ctx  = (
        f"[Hoy es {dias[ahora.weekday()]} {ahora.strftime('%d/%m/%Y')}, "
        f"son las {ahora.strftime('%H:%M')} hs]\n"
    )

    # ── Búsqueda ──────────────────────────────────────────
    zona     = detectar_zona(mensaje)
    busqueda = f"{mensaje} {zona}" if zona else mensaje

    relevantes = await buscar_relevantes(busqueda, zona=zona, top_k=12)

    # Distancias
    if ubicacion:
        lat_u, lon_u = ubicacion
        distancias = {}
        for c in relevantes:
            if c.get("lat") and c.get("lon"):
                d = calcular_distancia(lat_u, lon_u, c["lat"], c["lon"])
                distancias[c["nombre"]] = d

        if distancias:
            def fmt(d):
                return f"{int(d * 1000)} metros" if d < 1.0 else f"{d:.1f} km"

            lista = "\n".join(
                f"- {n}: a {fmt(d)}"
                for n, d in sorted(distancias.items(), key=lambda x: x[1])
            )
            ctx += f"\nUBICACIÓN DEL USUARIO — Distancias REALES:\n{lista}\n"

    # Inyectar estado de horario precalculado (ABIERTO/CERRADO)
    inyectar_estado_horario(relevantes, ahora)

    # JSON para el LLM
    datos_llm = [
        {k: v for k, v in c.items() if k not in ("lat", "lon", "maps", "id")}
        for c in relevantes
    ]
    datos_json = json.dumps(datos_llm, ensure_ascii=False, separators=(",", ":"))

    prompt = PROMPT_SISTEMA_BASE + f"\n=== DATOS DISPONIBLES ===\n{datos_json}\n=== FIN DATOS ==="

    mensajes_llm = [{"role": m["role"], "content": m["content"]} for m in historial_rec]

    for idx in range(len(mensajes_llm) - 1, -1, -1):
        if mensajes_llm[idx]["role"] == "user":
            mensajes_llm[idx]["content"] = ctx + mensajes_llm[idx]["content"]
            break

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": prompt}, *mensajes_llm],
            temperature=0.3,
            max_tokens=1000,
        )

        respuesta = response.choices[0].message.content
        respuesta = inyectar_maps_links(respuesta, relevantes)

        u     = response.usage
        costo = ((u.prompt_tokens / 1_000_000) * 0.15) + \
                ((u.completion_tokens / 1_000_000) * 0.60)
        logger.info(
            f"Tokens → {u.prompt_tokens} in / {u.completion_tokens} out | "
            f"${costo:.6f} | RAG: {len(datos_llm)} resultados | caché: {cache_label}"
        )

        async with _cache_lock:
            cache_activo[cache_key] = {"respuesta": respuesta, "timestamp": ahora}
            while len(cache_activo) > MAX_CACHE_RESPUESTAS:
                oldest_key = min(cache_activo, key=lambda k: cache_activo[k]["timestamp"])
                del cache_activo[oldest_key]

        historial_rec.append({
            "role": "assistant", "content": respuesta,
            "timestamp": ahora.isoformat(),
        })
        guardar_historial(user_id, historial_rec)
        return respuesta

    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        return "Ups, tuve un problema técnico 😅 ¿Podés intentar de nuevo?"


# ══════════════════════════════════════════════════════════
# SALUDOS SIN IA
# ══════════════════════════════════════════════════════════

_SALUDOS = frozenset([
    "hola", "buenas", "buen dia", "buen día", "holis", "hola vecinito",
    "que tal", "qué tal", "buenas tardes", "buenas noches",
    "buenos dias", "buenos días", "hey",
])


def _es_saludo(texto: str) -> bool:
    limpio = texto.lower().strip().rstrip("!. ")
    if limpio in _SALUDOS:
        return True
    return re.sub(r"(.)\1{2,}", r"\1", limpio) in _SALUDOS


# ══════════════════════════════════════════════════════════
# MENSAJE DE BIENVENIDA (primer mensaje)              v4 NEW
# ══════════════════════════════════════════════════════════

MENSAJE_BIENVENIDA = (
    "¡Hola{nombre}! 👋 Soy *Vecinito* 🏘️, tu guía de barrio.\n\n"
    "Te ayudo a encontrar *comercios y servicios* en "
    "*City Bell*, *Gonnet* y *Villa Elisa*.\n\n"
    "Podés preguntarme cosas como:\n"
    "🍕 _\"Quiero pedir pizza\"_\n"
    "🔧 _\"Necesito un plomero urgente\"_\n"
    "💊 _\"Farmacia abierta ahora\"_\n"
    "⚡ _\"Electricista en Gonnet\"_\n\n"
    "📍 *Tip:* Enviame tu ubicación y te muestro los más cercanos!\n\n"
    "Ahora sí, *¿en qué te puedo ayudar?* 😊"
)


async def enviar_bienvenida_si_nuevo(user_id: str, update: Update) -> bool:
    """
    Si es la primera vez del usuario, envía bienvenida + teclado.
    Retorna True si envió la bienvenida (el caller sigue procesando el mensaje normal).
    """
    if not es_usuario_nuevo(user_id):
        return False

    marcar_bienvenida(user_id)
    user_name = update.effective_user.first_name

    keyboard = [
        [KeyboardButton("📍 Enviar ubicación", request_location=True)],
        [
            KeyboardButton("🏘️ City Bell"),
            KeyboardButton("🏘️ Gonnet"),
            KeyboardButton("🏘️ Villa Elisa"),
        ],
    ]

    nombre_fmt = f" {user_name}" if user_name else ""
    await responder_seguro(
        update.message,
        MENSAJE_BIENVENIDA.format(nombre=nombre_fmt),
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return True


# ══════════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = str(update.effective_user.id)
    user_name = update.effective_user.first_name
    eliminar_historial(user_id)
    marcar_bienvenida(user_id)

    mensaje = (
        f"¡Hola {user_name}! 👋 Soy *Vecinito* 🏘️\n\n"
        f"Tu guía de comercios y servicios en:\n"
        f"📍 City Bell  📍 Gonnet  📍 Villa Elisa\n\n"
        f"*Preguntame lo que necesites:*\n"
        f"• _\"Pizzerías en City Bell\"_\n"
        f"• _\"Necesito un plomero\"_\n"
        f"• _\"Farmacia 24hs\"_\n"
        f"• _\"Electricista urgente\"_\n\n"
        f"📍 *Tip:* Enviame tu ubicación y te muestro los más cercanos!\n"
        f"🔄 Escribí *reset* para borrar el historial"
    )
    keyboard = [
        [KeyboardButton("📍 Enviar ubicación", request_location=True)],
        [
            KeyboardButton("🏘️ City Bell"),
            KeyboardButton("🏘️ Gonnet"),
            KeyboardButton("🏘️ Villa Elisa"),
        ],
    ]
    await responder_seguro(
        update.message, mensaje,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def manejar_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    loc     = update.message.location
    guardar_ubicacion(user_id, loc.latitude, loc.longitude)
    marcar_bienvenida(user_id)  # Si mandó ubicación, ya no es nuevo
    logger.info(f"Ubicación {user_id}: ({loc.latitude}, {loc.longitude})")

    historial = obtener_historial(user_id)
    ultimo    = next(
        (m["content"] for m in reversed(historial) if m["role"] == "user"), None,
    )

    if ultimo:
        await update.message.reply_text("📍 ¡Ubicación recibida! Buscando los más cercanos...")
        await update.message.chat.send_action(ChatAction.TYPING)
        respuesta = await obtener_respuesta(user_id, f"Repetí la búsqueda de: {ultimo}")
        await responder_seguro(update.message, respuesta, disable_web_page_preview=True)
    else:
        await update.message.reply_text(
            "📍 ¡Listo! Ahora te puedo mostrar los comercios más cercanos.\n\n"
            "¿Qué estás buscando?"
        )


async def manejar_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    marcar_bienvenida(user_id)
    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("🎤 Escuchando tu audio...")

    voice     = await update.message.voice.get_file()
    file_size = update.message.voice.file_size

    texto = await transcribir_audio(voice, file_size=file_size)

    if texto is None:
        if file_size and file_size > MAX_AUDIO_MB * 1024 * 1024:
            await update.message.reply_text(
                f"El audio es muy largo 😅 Mandame uno de menos de {MAX_AUDIO_MB} MB o escribilo."
            )
        else:
            await update.message.reply_text(
                "No pude entender el audio 😅 ¿Podés intentar de nuevo o escribirlo?"
            )
        return

    await registrar_busqueda(user_id, texto, tipo="audio")
    await update.message.chat.send_action(ChatAction.TYPING)
    respuesta = await obtener_respuesta(user_id, texto, skip_log=True)
    await responder_seguro(update.message, respuesta, disable_web_page_preview=True)


async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    texto   = update.message.text

    if not texto:
        return

    logger.info(f"{user_id}: {texto}")

    # Reset
    if texto.lower().strip() in ("reset", "/reset", "resetear", "borrar historial"):
        eliminar_historial(user_id)
        await responder_seguro(
            update.message,
            "✅ Listo! Borré el historial.\nEmpecemos de nuevo 🔄 ¿Qué necesitás?",
        )
        return

    # Rate limiting
    if not verificar_rate_limit(user_id):
        await responder_seguro(
            update.message,
            "Che, bajá un cambio! 😅 Mandame los mensajes de a uno y esperá la respuesta.",
        )
        return

    # Bienvenida al primer mensaje (antes de todo)
    # Envía la bienvenida y TAMBIÉN procesa el mensaje del usuario
    bienvenida_enviada = await enviar_bienvenida_si_nuevo(user_id, update)

    # Saludo sin IA (solo si NO acabamos de enviar bienvenida, porque sería redundante)
    if not bienvenida_enviada and _es_saludo(texto):
        user_name = update.effective_user.first_name
        await responder_seguro(
            update.message,
            f"¡Hola {user_name}! 👋 Soy *Vecinito* 🏘️\n\n"
            f"Tu asistente de barrio para encontrar comercios y servicios en "
            f"*City Bell*, *Gonnet* y *Villa Elisa*.\n\n"
            f"Preguntame lo que necesites:\n"
            f"🍕 _\"Quiero pedir pizza\"_\n"
            f"🔧 _\"Necesito un plomero urgente\"_\n"
            f"💊 _\"Farmacia abierta ahora\"_\n"
            f"⚡ _\"Electricista en Gonnet\"_\n\n"
            f"📍 También podés enviarme tu *ubicación* y te muestro lo más cercano!",
        )
        return

    # Si fue bienvenida + saludo, no hace falta procesar más
    if bienvenida_enviada and _es_saludo(texto):
        return

    # Botones de zona
    if texto.startswith("🏘️"):
        texto = f"Qué comercios hay en {texto.replace('🏘️', '').strip()}?"

    await agregar_mensaje_a_cola(user_id, texto, update)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Excepción: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text(
                "Ups, algo salió mal 😅 ¿Podés intentar de nuevo?"
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

def main():
    logger.info(
        f"Iniciando Vecinito v4 — modo: "
        f"{'RAG (Supabase)' if supabase else 'JSON fallback'}"
    )

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.LOCATION, manejar_ubicacion))
    app.add_handler(MessageHandler(filters.VOICE, manejar_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    app.add_error_handler(error_handler)

    async def iniciar_tareas_background(app):
        asyncio.create_task(limpiar_cache_periodico())

    app.post_init = iniciar_tareas_background

    logger.info("Bot listo!")
    app.run_polling()


if __name__ == "__main__":
    main()