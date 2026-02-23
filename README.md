# 🏘️ Vecinito v4

**Bot de Telegram con RAG para encontrar comercios y servicios de la zona**

Vecinito es un asistente inteligente que ayuda a vecinos de **City Bell**, **Gonnet** y **Villa Elisa** a encontrar comercios y servicios cerca de ellos, usando búsqueda semántica con Supabase + pgvector y GPT-4o-mini.

---

## ✨ Características

- 🔍 **Búsqueda semántica (RAG)** — Supabase + pgvector con embeddings de OpenAI
- 🧠 **Sinónimos inteligentes** — ~150+ mapeos ("pizza" → pizzería, "remedio" → farmacia)
- 📍 **Ordenar por cercanía** — Envía tu ubicación y ordena por distancia (Haversine)
- 🕐 **Horarios en tiempo real** — Muestra ABIERTO ✅ / CERRADO ❌ sin depender del LLM
- 🗺️ **Links a Google Maps** — Inyección automática de links en la respuesta
- 🎙️ **Audio** — Transcripción de notas de voz con Whisper
- 💬 **Contexto de conversación** — Recuerda el historial de la última hora
- 🏘️ **Selección de zona** — Botones inline para filtrar por City Bell, Gonnet o Villa Elisa
- ⚡ **Debouncing** — Agrupa mensajes rápidos y procesa solo la última versión
- 🛡️ **Rate limiting** — Máximo 10 mensajes por minuto por usuario
- 🗄️ **Redis** — Persistencia de historiales (2h) y ubicaciones (24h), con fallback a memoria
- 📦 **Fallback JSON** — Funciona sin Supabase usando búsqueda local con scoring ponderado

---

## 🚀 Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/tu-usuario/vecinito.git
cd vecinito
```

### 2. Crear entorno virtual

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno

Crear archivo `.env` en la raíz del proyecto:

```env
# === Requeridas ===
TELEGRAM_TOKEN=tu_token_de_botfather
OPENAI_API_KEY=tu_api_key_de_openai

# === Opcionales ===
SUPABASE_URL=tu_url_de_supabase
SUPABASE_KEY=tu_key_de_supabase
REDIS_URL=redis://localhost:6379
```

### 5. Configurar Supabase (recomendado)

Si usás Supabase con pgvector para búsqueda semántica:

```bash
python setup_database.py
```

Esto genera embeddings con `text-embedding-3-small` y los carga a la tabla de Supabase.

> Sin Supabase el bot funciona igual usando el archivo JSON local con búsqueda por scoring.

### 6. Iniciar Redis (opcional)

```bash
# Docker
docker run -d -p 6379:6379 redis

# Ubuntu/Debian
sudo apt install redis-server && sudo systemctl start redis

# Mac
brew install redis && brew services start redis
```

> ⚠️ Si Redis no está disponible, el bot usa memoria local (se pierde al reiniciar).

### 7. Ejecutar el bot

```bash
python bot.py
```

---

## 📁 Estructura del Proyecto

```
vecinito/
├── bot.py                  # 🤖 Bot principal (ejecutar este)
├── setup_database.py       # 🗄️ Carga comercios a Supabase con embeddings
├── regenerar_json.py       # 🔄 Regenera comercios.json desde el CSV
├── requirements.txt        # 📋 Dependencias
├── data/
│   ├── comercios.json      # 📦 Base de datos de comercios (fallback)
│   ├── comercios.csv       # 📊 Fuente original (CSV con ;)
│   ├── logs_busquedas.csv  # 📝 Log de búsquedas (auto-generado)
│   └── logs.csv            # 📝 Log general (auto-generado)
├── test_agente.py          # 🧪 Tests del agente
├── test_carga.py           # 🧪 Test de carga CSV
├── test_contexto.py        # 🧪 Test de contexto
├── .env                    # 🔐 Variables de entorno (no commitear)
└── .gitignore
```

---

## 🔧 Arquitectura

```
Usuario (Telegram)
       │
       ▼
   Handlers ──► Debounce Queue (5s, generation counter)
                      │
                      ▼
              obtener_respuesta()
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
    Cache check   RAG Search    JSON Fallback
    (LRU+TTL)    (Supabase     (scoring ponderado
                  pgvector)     + sinónimos)
                      │
                      ▼
              GPT-4o-mini (temp=0.3)
              + system prompt (~180 líneas)
              + historial (última hora)
              + datos de comercios
              + horarios (ABIERTO/CERRADO)
              + distancias (si hay ubicación)
                      │
                      ▼
              Post-procesamiento
              (inyección links Maps)
                      │
                      ▼
              Respuesta al usuario
```

---

## 📊 Base de Datos

### Tipos de entrada

El bot maneja dos tipos de datos:

| Tipo | Campos | Ejemplo |
|------|--------|---------|
| **Comercio** | nombre, categoría, zona, dirección, horarios, contacto, lat/lon, maps | Pizzería, Farmacia |
| **Servicio** | nombre, rubro, experiencia, contacto | Plomero, Electricista |

### Formato JSON (`data/comercios.json`)

```json
{
  "nombre": "Pizzería Los Tíos",
  "tags": "pizza empanadas delivery",
  "categoria": "Gastronomía",
  "zona": "City Bell",
  "direccion": "Calle 13 nro 456",
  "contacto": "https://wa.me/5492214567890",
  "horarios": "Lun-Vie 18-23 | Sab-Dom 12-24",
  "lat": -34.8721,
  "lon": -58.0132,
  "maps": "https://www.google.com/maps?q=-34.8721,-58.0132"
}
```

### Actualizar comercios

1. Editar `data/comercios.csv` (separador `;`)
2. Regenerar el JSON:
   ```bash
   python regenerar_json.py
   ```
3. Si usás Supabase, recargar embeddings:
   ```bash
   python setup_database.py
   ```

---

## 🔍 Sistema de Búsqueda

### Modo RAG (Supabase + pgvector)

1. La consulta del usuario se expande con sinónimos
2. Se genera un embedding con `text-embedding-3-small`
3. Se busca en Supabase usando similitud vectorial
4. Si no hay resultados, se usa el fallback JSON

### Modo JSON (fallback)

1. Se expande la query con sinónimos
2. Se filtran stopwords (~40 palabras)
3. Se calcula score ponderado por campo:
   - `nombre` = peso 4
   - `categoría/rubro` = peso 3
   - `tags` = peso 1
   - Bonus zona = +5
4. Matching parcial: palabras ≥4 caracteres matchean como prefijo (50% del peso)
5. Se devuelven los top 12 resultados

### Sinónimos

~150+ mapeos de lenguaje coloquial a términos de la base de datos:

```
"pizza"    → pizzería
"remedio"  → farmacia, medicamentos
"plomero"  → plomería, cañerías
"asado"    → carnicería, parrilla, carbón
```

---

## 📱 Uso del Bot

### Comandos

| Comando | Descripción |
|---------|-------------|
| `/start` | Bienvenida e instrucciones |
| `/reset` | Borra el historial de conversación |

### Ejemplos de Búsqueda

```
👤 "Pizzerías en City Bell"
👤 "Farmacia 24 horas"
👤 "Dónde compro carbón para el asado?"
👤 "Carnicerías cerca" (después de enviar ubicación)
👤 "Hay más opciones?"
👤 "¿Cuál está más cerca?"
👤 🎙️ (nota de voz con la consulta)
```

### Botones del Teclado

- 📍 **Enviar ubicación** — Ordena resultados por cercanía
- 🏘️ **City Bell / Gonnet / Villa Elisa** — Filtrar por zona
- **Botones inline de zona** — Aparecen automáticamente si no tenés ubicación ni zona definida

---

## ⚙️ Configuración

### Variables de Entorno

| Variable | Descripción | Requerida |
|----------|-------------|-----------|
| `TELEGRAM_TOKEN` | Token del bot de BotFather | ✅ |
| `OPENAI_API_KEY` | API Key de OpenAI | ✅ |
| `SUPABASE_URL` | URL del proyecto Supabase | ❌ |
| `SUPABASE_KEY` | Key del proyecto Supabase | ❌ |
| `REDIS_URL` | URL de Redis (default: `redis://localhost:6379`) | ❌ |

### Límites Configurables

| Parámetro | Valor | Descripción |
|-----------|-------|-------------|
| `MAX_USUARIOS_MEMORIA` | 500 | Máx. usuarios en cache LRU |
| `MAX_HISTORIAL_MENSAJES` | 20 | Máx. mensajes por historial |
| `MAX_CACHE_EMBEDDINGS` | 2000 | Máx. embeddings cacheados |
| `CACHE_TTL_MINUTOS` | 5 | TTL del cache de respuestas |
| `DEBOUNCE_SEGUNDOS` | 5.0 | Ventana de debounce |
| `MAX_MENSAJES_POR_MINUTO` | 10 | Rate limit por usuario |
| `MAX_AUDIO_MB` | 10 | Tamaño máximo de audio |

### Modelos de IA

| Uso | Modelo |
|-----|--------|
| Chat | `gpt-4o-mini` (temp=0.3, max_tokens=1000) |
| Embeddings | `text-embedding-3-small` |
| Audio | `whisper-1` |

---

## 🛡️ Resiliencia

- **Redis caído** → Fallback a memoria local (LRU con límite de 500 usuarios)
- **Supabase caído** → Fallback a búsqueda local en JSON con scoring ponderado
- **Markdown inválido** → Reintento con texto plano
- **Errores generales** → Mensaje amigable al usuario + log del error
- **Cache periódica** → Limpieza automática cada 1 hora

---

## 📋 Dependencias

### Requeridas

```
python-telegram-bot==22.6     # Bot de Telegram
openai==2.20.0                # API de OpenAI (chat, embeddings, whisper)
python-dotenv==1.2.1          # Variables de entorno
redis==7.1.1                  # Persistencia de historiales
```

### Opcionales

```
supabase                      # RAG con pgvector
pandas==3.0.0                 # Solo para regenerar_json.py (CSV → JSON)
```

---

## 📝 Licencia

MIT

---


