# 🏘️ Vecinito

**Bot de Telegram para encontrar comercios locales en el Corredor Norte de La Plata**

Vecinito es un asistente virtual que ayuda a vecinos de City Bell, Gonnet y Villa Elisa a encontrar comercios y servicios cerca de ellos.

---

## ✨ Características

- 🔍 **Búsqueda inteligente** - Entiende lenguaje natural ("dónde compro carbón para el asado")
- 📍 **Ordenar por cercanía** - Envía tu ubicación y te muestra los más cercanos
- 🗺️ **Links a Google Maps** - Cada comercio tiene su ubicación exacta
- 💬 **Contexto de conversación** - Recuerda lo que hablaste en la última hora
- 🧠 **Razonamiento** - Si buscás un producto, deduce en qué tipo de comercio encontrarlo

---

## 🚀 Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/tu-usuario/vecinito.git
cd vecinito
```

### 2. Crear entorno virtual

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/Mac
source .venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno

Crear archivo `.env` en la raíz del proyecto:

```env
TELEGRAM_TOKEN=tu_token_de_botfather
OPENAI_API_KEY=tu_api_key_de_openai
REDIS_URL=redis://localhost:6379
```

### 5. Iniciar Redis (opcional pero recomendado)

**Windows (con Docker):**
```bash
docker run -d -p 6379:6379 redis
```

**Linux/Mac:**
```bash
# Ubuntu/Debian
sudo apt install redis-server
sudo systemctl start redis

# Mac con Homebrew
brew install redis
brew services start redis
```

> ⚠️ Si Redis no está disponible, el bot funciona igual pero usa memoria local (se pierde al reiniciar).

### 6. Ejecutar el bot

```bash
python bot.py
```

---

## 📁 Estructura del Proyecto

```
Vecinito/
├── bot.py                  # 🤖 Bot principal (ejecutar este)
├── data/
│   ├── comercios.json      # 📦 Base de datos de comercios
│   └── comercios.csv       # 📊 Fuente original (Excel/CSV)
├── regenerar_json.py       # 🔄 Script para actualizar JSON desde CSV
├── requirements.txt        # 📋 Dependencias
├── .env                    # 🔐 Variables de entorno (no commitear)
├── .env.example            # 📝 Ejemplo de configuración
└── .gitignore
```

### Archivos Legacy (no usados actualmente)
```
├── main.py                 # ⚠️ Versión anterior con LangGraph
├── test_agente.py          # ⚠️ Tests de versión anterior
├── test_carga.py           # ⚠️ Test de carga CSV
└── data/memoria.db         # ⚠️ SQLite de versión anterior
```

---

## 📊 Base de Datos de Comercios

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

1. Editar `data/comercios.csv` (Excel)
2. Ejecutar:
```bash
python regenerar_json.py
```

---

## 🔧 Cómo Funciona

### Arquitectura Simple

```
Usuario (Telegram)
       ↓
    bot.py
       ↓
   Redis (persistencia)
       ↓
   OpenAI GPT-4o-mini
       ↓
  comercios.json
       ↓
   Respuesta
```

### Persistencia con Redis

- **Historiales**: Se guardan en Redis con TTL de 2 horas
- **Ubicaciones**: Se guardan en Redis con TTL de 24 horas
- **Fallback**: Si Redis no está disponible, usa memoria local

### Flujo de Conversación

1. Usuario envía mensaje
2. Se agrega al historial (con timestamp)
3. Se filtran mensajes de la última hora
4. Se envía a GPT-4o-mini con:
   - Prompt del sistema
   - Base de datos completa de comercios
   - Historial de conversación
5. GPT genera respuesta
6. Se envía al usuario

### Historial con Expiración

- Cada mensaje tiene timestamp
- Solo se envían al LLM mensajes de la última hora
- Los mensajes viejos se limpian automáticamente

---

## 📱 Uso del Bot

### Comandos

| Comando | Descripción |
|---------|-------------|
| `/start` | Inicia el bot y muestra ayuda |

### Ejemplos de Búsqueda

```
👤 "Pizzerías en City Bell"
👤 "Farmacia 24 horas"
👤 "Dónde compro carbón para el asado?"
👤 "Carnicerías cerca" (después de enviar ubicación)
👤 "Hay más opciones?"
👤 "¿Cuál está más cerca?"
```

### Botones del Teclado

- 📍 **Enviar ubicación** - Ordena resultados por cercanía
- 🏘️ **City Bell / Gonnet / Villa Elisa** - Filtrar por zona

---

## ⚙️ Configuración Avanzada

### Variables de Entorno

| Variable | Descripción | Requerida |
|----------|-------------|-----------|
| `TELEGRAM_TOKEN` | Token del bot de BotFather | ✅ |
| `OPENAI_API_KEY` | API Key de OpenAI | ✅ |
| `REDIS_URL` | URL de conexión Redis (default: redis://localhost:6379) | ❌ |

### Modelo de IA

El bot usa `gpt-4o-mini` por defecto. Para cambiar:

```python
# En bot.py, línea ~165
response = client.chat.completions.create(
    model="gpt-4o-mini",  # Cambiar aquí
    ...
)
```

### Tiempo de Contexto

Por defecto, el historial dura 1 hora. Para modificar:

```python
# En bot.py, función obtener_respuesta()
hace_una_hora = ahora - timedelta(hours=1)  # Cambiar hours=X
```

---

## 📈 Categorías de Comercios

| Categoría | Ejemplos |
|-----------|----------|
| Gastronomía | Pizzerías, Heladerías, Cafés, Restaurantes |
| Salud | Farmacias, Ópticas |
| Comercio | Kioscos, Ferreterías |
| Almacén | Carnicerías, Verdulerías |
| Servicios | Veterinarias, Gimnasios, Peluquerías |

---

## 🛠️ Desarrollo

### Requisitos

- Python 3.10+
- Cuenta de Telegram (para crear bot con BotFather)
- API Key de OpenAI

### Dependencias Principales

```
python-telegram-bot>=20.0   # Bot de Telegram
openai>=1.0.0               # API de OpenAI
python-dotenv>=1.0.0        # Variables de entorno
redis>=5.0.0                # Persistencia de historiales
```

### Dependencias Opcionales

```
pandas>=2.0.0               # Solo para regenerar_json.py (CSV → JSON)
```

---

## 📝 Licencia

MIT

---

## 👥 Contribuir

1. Fork del repositorio
2. Crear rama (`git checkout -b feature/nueva-funcionalidad`)
3. Commit (`git commit -m 'Agrega nueva funcionalidad'`)
4. Push (`git push origin feature/nueva-funcionalidad`)
5. Crear Pull Request

---

## 📞 Soporte

¿Problemas? Abrí un issue en GitHub.

---

Desarrollado con ❤️ para el Corredor Norte de La Plata
