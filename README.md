# Humorous Review Scout

Herramienta para descubrir, recopilar, revisar y preparar reseñas graciosas o llamativas de Google Maps, con una UI local para moderación y una integración con Notion para dejar cada reseña aceptada lista para el show.

## Qué puede hacer ahora

- Buscar sitios en Google Maps usando SerpApi.
- Limitar la búsqueda por país, regiones, categorías, volumen de reseñas y frescura.
- Recoger reseñas recientes y de baja puntuación por sitio.
- Puntuar el potencial de humor y marcar señales de seguridad.
- Guardar todo en SQLite para poder revisar, filtrar y seguir trabajando más tarde.
- Mostrar una UI local para configurar el proyecto, lanzar procesos y revisar reseñas.
- Moderar reseñas con un flujo simple de estado:
  - `Vacío`
  - `Aceptada`
  - `Rechazada`
- Navegar reseña a reseña sin volver al listado.
- Copiar desde la vista de detalle:
  - la URL de la reseña
  - texto formateado para Notion
  - una imagen de la reseña con estilo tipo Google Maps
- Sincronizar automáticamente una reseña aceptada a Notion.
- Añadir también a Notion la captura de la reseña al final del body.
- Registrar en logs las llamadas a SerpApi y OpenAI para depuración.

## Flujo general

El proyecto está pensado para este flujo:

1. Configuras criterios de discovery.
2. Descubres sitios en Google Maps.
3. Recoges reseñas de esos sitios.
4. El sistema puntúa el humor y etiqueta riesgos.
5. Revisas las reseñas desde la UI.
6. Cuando aceptas una reseña:
   - pasa a `Aceptada`
   - se crea una página en Notion
   - se rellenan propiedades clave
   - se añade el texto de la reseña
   - se sube la captura de pantalla
7. Cuando rechazas una reseña:
   - pasa a `Rechazada`
   - deja de aparecer como pendiente

## Requisitos

- Python 3.9+
- `pip`
- una cuenta de SerpApi con cuota disponible
- una clave de OpenAI si quieres scoring con LLM
- una integración de Notion si quieres sincronización automática

## Instalación

Desde la raíz del proyecto:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Variables de entorno

El proyecto usa un `.env` en la raíz.

### Necesarias para el flujo base

- `SERPAPI_API_KEY`

### Opcionales

- `OPENAI_API_KEY`
  Necesaria para el scoring con OpenAI.

- `NOTION_ACCESS_TOKEN`
  Necesaria para crear páginas en Notion.

- `NOTION_DATABASE_ID`
  Base de datos de Notion donde se crean las páginas.

- `NOTION_AREA_PAGE_ID`
  Opcional. Si no se define, el proyecto usa por defecto la página de `Una Estrella` que se configuró durante el desarrollo.

Ejemplo:

```bash
SERPAPI_API_KEY=...
OPENAI_API_KEY=...
NOTION_ACCESS_TOKEN=...
NOTION_DATABASE_ID=...
NOTION_AREA_PAGE_ID=...
```

Para cargarlo en la shell actual:

```bash
set -a
source .env
set +a
```

## Configuración

La configuración vive en `config.yaml`.

Desde la UI puedes ajustar, entre otras cosas:

- número objetivo semanal
- país
- regiones
- categorías
- filtro por nombre
- mínimo de reseñas totales por sitio
- antigüedad máxima de actividad reciente

Ejemplos de uso real:

- buscar restaurantes en toda España
- limitar discovery a Madrid
- lanzar búsquedas temáticas por celebraciones

## Ejecución por línea de comandos

### Pipeline semanal completo

```bash
PYTHONPATH=. .venv/bin/python -m humor_reviews.run weekly
```

### Ensayo sin llamadas externas

```bash
PYTHONPATH=. .venv/bin/python -m humor_reviews.run weekly --no-api
```

### Pasos individuales

```bash
PYTHONPATH=. .venv/bin/python -m humor_reviews.run discover
PYTHONPATH=. .venv/bin/python -m humor_reviews.run collect
PYTHONPATH=. .venv/bin/python -m humor_reviews.run shortlist
```

### Saltar llamadas externas en un paso concreto

```bash
PYTHONPATH=. .venv/bin/python -m humor_reviews.run discover --no-api
PYTHONPATH=. .venv/bin/python -m humor_reviews.run collect --no-api
```

### Añadir un sitio manualmente

```bash
PYTHONPATH=. .venv/bin/python -m humor_reviews.run add-place <place_id>
```

### Reintentar reseñas con error de scoring LLM

```bash
PYTHONPATH=. .venv/bin/python -m humor_reviews.run rescore-llm-errors --limit 20
```

### Búsquedas temáticas para celebraciones

```bash
PYTHONPATH=. .venv/bin/python -m humor_reviews.run themed-celebrations \
  --celebrations "Día del Padre, Semana Santa" \
  --target 10 \
  --threshold 60 \
  --max-searches 15 \
  --max-places 12 \
  --max-reviews-per-place 10
```

## Interfaz web local

Lanza la UI con:

```bash
PYTHONPATH=. .venv/bin/python scripts/config_ui.py
```

Luego abre:

- [http://127.0.0.1:5173](http://127.0.0.1:5173)

## Qué incluye la UI

### 1. Pantalla de configuración

- edición de `config.yaml`
- lanzamiento del pipeline semanal
- vista del progreso

### 2. Vista de base de datos

- resumen de métricas:
  - reseñas totales
  - vacías
  - aceptadas
  - rechazadas
- orden por:
  - puntuación de humor
  - última actualización
- filtro por estado:
  - vacías
  - aceptadas
  - rechazadas
  - todas

### 3. Vista de detalle de reseña

- navegación `Anterior` / `Siguiente`
- acciones laterales:
  - `Copiar URL`
  - `Copiar texto`
  - `Copiar imagen`
  - `Rechazar`
  - `Aceptar`
- enlaces del lugar:
  - Google Maps
  - Notion, si ya existe sincronización

## Estados de reseña

El modelo actual de moderación usa un único campo `status`.

Valores:

- vacío: pendiente de revisión
- `accepted`: aceptada
- `rejected`: rechazada

Comportamiento en la UI:

- si está aceptada:
  - el botón `Aceptar` pasa a gris
  - cambia el texto a `Aceptada`
  - deja de ser clicable
- si está rechazada:
  - el botón `Rechazar` pasa a gris
  - cambia el texto a `Rechazada`
  - deja de ser clicable

La base migró automáticamente los datos anteriores:

- seleccionadas antiguas -> aceptadas
- revisadas no seleccionadas -> rechazadas
- no revisadas ni seleccionadas -> vacías

## Copiado y exportación

### Copiar texto

Genera texto preparado para pegar en Notion, con este formato:

- nombre de usuario
- cita con el texto de la reseña
- si existe respuesta:
  - título `Respuesta de propietario`
  - cita con la respuesta

### Copiar imagen

Genera un PNG vertical, legible y optimizado para leer en directo en dispositivos como iPad mini.

Incluye:

- nombre y datos básicos del lugar
- autor de la reseña
- estrellas y fecha
- texto de la reseña
- respuesta del propietario, si existe

### Copiar URL

Copia la URL real de la reseña de Google Maps.

## Integración con Notion

Cuando aceptas una reseña desde la UI:

- se crea una página en la base de datos configurada
- se rellena el icono de la página con `⭐`
- se asignan propiedades del registro
- se escribe el body
- se sube la captura de la reseña al final del body

### Mapeo actual a Notion

- `Título` -> `Nombre del sitio - Nombre del reviewer`
- `URL` -> URL de la reseña
- `Type` -> `Review`
- `Scope` -> `personal`
- `Area` -> relación a `Una Estrella`
- `Tags` -> añade `Respuesta del propietario` si existe owner reply

### Contenido del body

Se usa el mismo formato que el botón `Copiar texto`:

- reviewer
- bloque de cita con la reseña
- si existe:
  - encabezado `Respuesta de propietario`
  - bloques de cita con la respuesta
- imagen generada desde la captura de la reseña

### Requisitos de Notion

Para que funcione bien:

- la integración debe estar compartida con la base de datos destino
- la integración también debe tener acceso a la página usada en la relación `Area`

## Logs y depuración de APIs

El proyecto puede registrar las llamadas a APIs en `data/progress.log`.

Ahí aparecen eventos como:

- `api_request`
- `api_response`
- `api_cache_hit`
- `region_filtered_out`

Esto es útil para comprobar:

- qué parámetros se enviaron a SerpApi
- qué devolvió la API
- si una búsqueda fue servida desde caché
- si el filtro local descartó resultados por región

## Almacenamiento local

Los datos viven en:

- base SQLite: `data/humor_reviews.db`
- log de progreso: `data/progress.log`

La UI, el pipeline y la sincronización con Notion trabajan sobre esa base.

## Notas importantes

- SerpApi puede responder correctamente pero fallar por cuota si la cuenta no tiene búsquedas disponibles.
- El filtrado por región se hace tanto en la query como en una validación local posterior.
- Las llamadas a OpenAI son opcionales y dependen de `OPENAI_API_KEY`.
- La UI local es la forma recomendada de revisión manual.
- El comando CLI `set-status` existe todavía con nomenclatura antigua y no es el flujo recomendado para moderación manual; la UI refleja mejor el modelo actual.

## Test rápido de scoring OpenAI

```bash
PYTHONPATH=. .venv/bin/python scripts/test_score.py
```

## Estructura útil del proyecto

- `humor_reviews/run.py`
  Punto de entrada CLI.
- `humor_reviews/discover.py`
  Discovery de sitios con SerpApi.
- `humor_reviews/collect.py`
  Recogida de reseñas.
- `humor_reviews/humor.py`
  Scoring de humor y seguridad.
- `humor_reviews/storage.py`
  Persistencia SQLite.
- `humor_reviews/notion_sync.py`
  Creación y enriquecimiento de páginas en Notion.
- `scripts/config_ui.py`
  Servidor HTTP local de la UI.
- `scripts/config_view.html`
  Vista de configuración.
- `scripts/db_view.html`
  Listado y resumen de reseñas.
- `scripts/review_detail.html`
  Vista de detalle y acciones de moderación.
