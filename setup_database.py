import os
import json
import time
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    print("❌ Falta: pip install openai")
    sys.exit(1)

try:
    from supabase import create_client
except ImportError:
    print("❌ Falta: pip install supabase")
    sys.exit(1)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")

if not all([OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("❌ Faltan variables en .env: OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY")
    sys.exit(1)

openai_client  = OpenAI(api_key=OPENAI_API_KEY)
supabase       = create_client(SUPABASE_URL, SUPABASE_KEY)

BASE_DIR        = Path(__file__).resolve().parent
COMERCIOS_PATH  = BASE_DIR / "data" / "comercios.json"
EMBEDDING_MODEL = "text-embedding-3-small"


def generar_texto_embedding(raw: dict) -> str:
    """
    Texto representativo para el embedding.
    Servicios: nombre + rubro (simple y limpio)
    Comercios: nombre + categoria + zona + direccion
    """
    if "rubro" in raw:
        partes = [raw["nombre"], raw.get("rubro", "")]
    else:
        partes = [
            raw["nombre"],
            raw.get("categoria", ""),
            raw.get("zona", ""),
            raw.get("direccion", ""),
        ]
    return " | ".join(p for p in partes if p)


def normalizar_entrada(raw: dict) -> dict:
    """Convierte una entrada del JSON al formato de la tabla Supabase."""
    es_servicio = "rubro" in raw

    if es_servicio:
        return {
            "nombre":      raw["nombre"],
            "tipo":        "servicio",
            "rubro":       raw.get("rubro"),
            "experiencia": raw.get("experiencia"),  # int
            "contacto":    raw.get("contacto"),
            # Campos de comercio → null
            "categoria": None, "zona": None, "direccion": None,
            "horarios":  None, "lat":  None, "lon":       None, "maps": None,
        }
    else:
        return {
            "nombre":    raw["nombre"],
            "tipo":      "comercio",
            "categoria": raw.get("categoria"),
            "zona":      raw.get("zona"),
            "direccion": raw.get("direccion"),
            "horarios":  raw.get("horarios"),
            "lat":       raw.get("lat"),
            "lon":       raw.get("lon"),
            "maps":      raw.get("maps"),
            "contacto":  raw.get("contacto"),
            # Campos de servicio → null
            "rubro": None, "experiencia": None,
        }


def obtener_embedding(texto: str) -> list[float]:
    response = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texto
    )
    return response.data[0].embedding


def cargar_comercios():
    with open(COMERCIOS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    n_comercios = sum(1 for x in data if "rubro" not in x)
    n_servicios = sum(1 for x in data if "rubro" in x)

    print(f"📦 {len(data)} entradas en comercios.json")
    print(f"   🏪 Comercios: {n_comercios}")
    print(f"   🔧 Servicios: {n_servicios}")
    print()

    resp = input("⚠️  ¿Borrar datos existentes en Supabase antes de cargar? (s/n): ")
    if resp.lower() == "s":
        supabase.table("comercios").delete().neq("id", 0).execute()
        print("🗑️  Tabla limpiada\n")

    cargados = errores = 0

    for i, raw in enumerate(data):
        nombre = raw.get("nombre", f"entrada_{i}")
        tipo   = "servicio" if "rubro" in raw else "comercio"
        try:
            entrada             = normalizar_entrada(raw)
            entrada["embedding"] = obtener_embedding(generar_texto_embedding(raw))

            supabase.table("comercios").insert(entrada).execute()
            cargados += 1
            print(f"  ✅ [{i+1:3d}/{len(data)}] [{tipo:8}] {nombre}")

            if (i + 1) % 50 == 0:
                time.sleep(1)

        except Exception as e:
            errores += 1
            print(f"  ❌ [{i+1:3d}/{len(data)}] {nombre}: {e}")

    print()
    print("=" * 50)
    print(f"✅ Cargados: {cargados}")
    print(f"❌ Errores:  {errores}")
    print("\n🎉 Listo! Podés arrancar el bot.")


if __name__ == "__main__":
    cargar_comercios()
