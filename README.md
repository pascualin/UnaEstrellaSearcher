# Humorous Review Scout

Herramienta para descubrir, recopilar, revisar y preparar reseñas graciosas o llamativas de Google Maps, con una UI local para moderación por sitio y una integración con Notion para dejar las reseñas aceptadas listas para el show.

## Qué puede hacer ahora

- Buscar sitios en Google Maps usando SerpApi.
- Buscar reseñas para un episodio a partir de su fecha y las celebraciones que selecciones.
- Limitar la búsqueda por país, regiones, categorías, volumen de reseñas y frescura.
- Recoger reseñas recientes y de baja puntuación por sitio.
- Puntuar el potencial de humor y marcar señales de seguridad.
- Guardar todo en SQLite para poder revisar, filtrar y seguir trabajando más tarde.
- Mostrar una UI local para configurar el proyecto, lanzar procesos y revisar reseñas.
- Detener una búsqueda en curso desde la ejecución en vivo.
- Agrupar las reseñas por sitio y ordenarlas de más a menos graciosas en su detalle.
- Moderar reseñas con un flujo simple de estado:
  - `Vacío`
  - `Aceptada`
  - `Rechazada`
- Navegar reseña a reseña sin volver al listado.
- Copiar desde la vista de detalle:
  - la URL de la reseña
  - texto formateado para Notion
  - una imagen de la reseña con estilo tipo Google Maps
- Exportar todas las reseñas aceptadas de un sitio a una única página de Notion, con una captura detrás de cada reseña.
- Marcar sitios como procesados para excluirlos de nuevas evaluaciones, sin salir de su detalle.
- Importar una reseña por enlace y analizar también otras reseñas del mismo sitio.
- Registrar en logs las llamadas a SerpApi y OpenAI para depuración.

## Flujo general

El proyecto está pensado para este flujo:

1. Configuras criterios de discovery.
2. Descubres sitios en Google Maps.
3. Recoges reseñas de esos sitios.
4. El sistema puntúa el humor y etiqueta riesgos.
5. Abres el sitio desde el listado y revisas sus reseñas ordenadas por humor.
6. Aceptas o rechazas cada reseña de forma independiente. Aceptar no exporta a Notion automáticamente.
7. Pulsas `Exportar a Notion` en el detalle del sitio para guardar juntas todas las aceptadas, con sus textos y capturas.
8. Marcas el sitio como procesado cuando terminas. Sigues en el detalle hasta decidir salir; puedes reabrirlo más adelante.

## Requisitos

- Python 3.9+
- `pip`
- una cuenta de SerpApi con cuota disponible
- una clave de OpenAI o TypeSafe si quieres scoring automático
- una integración de Notion si quieres exportar las reseñas seleccionadas

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

- `TYPESAFE_API_KEY`
  Necesaria para el scoring con TypeSafe Jev.

- `NOTION_ACCESS_TOKEN`
  Necesaria para crear páginas en Notion.

- `NOTION_DATABASE_ID`
  Base de datos de Notion donde se crean las páginas.

- `NOTION_AREA_PAGE_ID`
  Opcional. Si no se define, el proyecto usa por defecto la página de `Una Estrella` que se configuró durante el desarrollo.

- `OPENAI_PLANNING_MODEL`
  Opcional. Modelo para planificar las búsquedas temáticas y evaluar la relevancia cuando se usa OpenAI.

- `CONFIG_UI_USERNAME` y `CONFIG_UI_PASSWORD`
  Opcionales. Si se definen ambas, la UI exige autenticación HTTP Basic.

- `CONFIG_UI_HOST`
  Opcional. Dirección de escucha de la UI; por defecto `127.0.0.1`.

Ejemplo:

```bash
SERPAPI_API_KEY=...
OPENAI_API_KEY=...
TYPESAFE_API_KEY=...
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

### Elegir el modelo de puntuación

La UI permite elegir el proveedor, el modelo y la variable de entorno que contiene la API key. Para usar OpenAI:

```yaml
scoring:
  provider: openai
  model: gpt-5.4
  api_key_env: OPENAI_API_KEY
  reasoning_effort: none
  reasoning_mode: standard
  verbosity: low
  service_tier: auto
```

Al abrir la configuración, el selector consulta `GET /v1/models` con la clave indicada y muestra los modelos de texto disponibles para esa cuenta. Si no puede consultar la cuenta, usa el catálogo general como respaldo. No admite texto libre, para evitar errores al escribir el identificador.

Los controles de ejecución se adaptan al modelo elegido: esfuerzo y modo de razonamiento, verbosidad, nivel de servicio, temperatura y límite de tokens. Las combinaciones incompatibles se ocultan; por ejemplo, la temperatura solo aparece en modelos sin razonamiento o cuando el esfuerzo es `none`. El scorer usa la [Responses API](https://developers.openai.com/api/reference/responses/create) y las capacidades publicadas en el [catálogo oficial de modelos](https://developers.openai.com/api/docs/models/all).

Para usar TypeSafe Jev:

```yaml
scoring:
  provider: typesafe
  model: jev-latest
  api_key_env: TYPESAFE_API_KEY
```

Jev usa el mismo texto de `prompt` como contexto de evaluación. Su respuesta `score` emplea once niveles ordenados (0–10), que el proyecto convierte a la escala 0–100. Jev también elige una etiqueta principal de humor y guarda la confianza del score en `humor_notes`. Ningún proveedor genera ya resúmenes de las reseñas.

Puedes crear la clave y consultar los modelos disponibles en la [documentación oficial de TypeSafe](https://docs.typesafe.ai/) y en su endpoint `GET /v1/models`.

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

Las búsquedas de episodio usan las regiones configuradas o, si están vacías, el país.
Si no indicas ninguno, no se añade una localidad por defecto ni se limita la búsqueda a Madrid.
Dejar el país vacío no anula una región que siga configurada.

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

### Buscar reseñas para un episodio por fecha

El flujo consulta las celebraciones de la fecha en
[Día Internacional de](https://www.diainternacionalde.com/). En la interfaz se
muestran primero para que el usuario elija cuáles quiere investigar; la búsqueda
no comienza hasta confirmar al menos una. Después busca únicamente reseñas nuevas
en Google Maps hasta reunir el objetivo o agotar el plan de búsquedas; las reseñas que ya
están guardadas no se vuelven a evaluar. Cuando hay varias celebraciones
seleccionadas, el objetivo global se considera un mínimo y se buscan al menos 3
reseñas nuevas por celebración. Las descartadas por sensibilidad o por no
producir búsquedas útiles no generan cupo.

```bash
PYTHONPATH=. .venv/bin/python -m humor_reviews.run episode-search \
  --date 2026-09-13 \
  --target 5 \
  --humor-threshold 60 \
  --relevance-threshold 60 \
  --observance "Día Internacional del Chocolate"
```

El comando admite repetir `--observance "Nombre"` para limitar la ejecución a
celebraciones concretas de esa fecha. Si no se indica, mantiene el comportamiento
compatible de utilizar todas las celebraciones encontradas.

Para contar como candidata del episodio, una reseña debe superar los umbrales de
humor y relevancia y no estar marcada como no recomendada por seguridad.
No se buscan candidatas antiguas ni se usan para completar el objetivo.

El plan se amplía con consultas distintas relacionadas con las celebraciones y
usa hasta `--max-searches` búsquedas (30 por defecto). Cada sitio aporta hasta
`--max-reviews-per-place` reseñas para examinar (10 por defecto). El antiguo
`--max-places` se acepta por compatibilidad, pero no detiene la búsqueda de episodio
antes de alcanzar el objetivo: el límite de seguridad es el plan finito de consultas.
Si se agota sin suficientes candidatas, la UI muestra `Objetivo no alcanzado` y el
número encontrado, en lugar de indicar que se ha cumplido el objetivo.

La relevancia se guarda por ejecución y celebración. Una reseña que supere el
umbral de humor pero no el de relevancia permanece pendiente en la base de datos
para poder utilizarla en otro episodio. Con TypeSafe, la planificación y la
relevancia temática tienen un modo local que no requiere OpenAI. Con OpenAI se
usa `OPENAI_PLANNING_MODEL` para esas tareas cuando está disponible.

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

### 2. Ejecución en vivo

- consulta y selección de las celebraciones de la fecha antes de buscar
- objetivo de reseñas nuevas y umbrales de humor y relevancia
- botón `Detener búsqueda` y bloqueo de ejecuciones simultáneas
- estadísticas de esta ejecución, sin sumar las reseñas recibidas en cada página de API ni repetir eventos del log
- contador de reseñas graciosas nuevas: reseñas analizadas que alcanzan el umbral de humor y cumplen el filtro de seguridad del episodio
- hallazgos agrupados por sitio, con su mejor puntuación y una marca visible de `PROCESADO` o `PENDIENTE`
- enlaces al detalle del sitio en otra pestaña

Las reseñas graciosas que no encajan temáticamente se guardan para otros episodios;
puedes revisarlas en la base de datos. Entrar de nuevo en la ejecución no vuelve a
mostrar los avisos emergentes del historial.

### 3. Vista de base de datos

- resumen de métricas:
  - reseñas totales
  - vacías
  - aceptadas
  - rechazadas
- orden por:
  - mejor puntuación de humor de cada sitio
  - última actualización de las reseñas de cada sitio
- filtro por estado del sitio:
  - pendientes de procesar
  - procesados
  - todas
- tarjetas con el estado de procesado, su fecha y los recuentos de reseñas pendientes, aceptadas y rechazadas

### 4. Vista de detalle de sitio

- todas las reseñas analizadas, ordenadas de más a menos graciosas
- traducción al español de reseñas y respuestas del propietario de otros idiomas cuando está disponible `OPENAI_API_KEY`
- botones `Aceptar` y `Rechazar` para cada reseña
- exportación conjunta de las aceptadas a Notion
- acciones para marcar el sitio como procesado o reabrirlo, sin cambiar de pantalla

Los sitios procesados se omiten en discovery, recogida, importación por enlace y
reintentos de scoring. Sus reseñas tampoco entran en nuevas sugerencias del pipeline.

### 5. Importación manual

- análisis por enlace de Google Maps y recogida adicional del mismo sitio
- importación de una reseña desde una o varias capturas pegadas
- campo opcional para identificar a quien envió la reseña

Después de importar una captura se abre el detalle del sitio en una pestaña nueva,
con todas sus reseñas analizadas para seleccionar cuáles exportar a Notion.
Capturas de reseñas distintas no se sobrescriben aunque se reutilice el mismo
enlace. Reimportar la misma captura conserva su sitio, valoración y selección,
sin volver a puntuarla ni crear un sitio vacío.
Si se identifica el sitio por el enlace o por su nombre y dirección visibles,
se analizan también sus otras reseñas con los mismos límites y filtros que en
la importación por URL. Se agrupan en el mismo sitio, sin repetir reseñas ya
guardadas ni analizar sitios procesados. El análisis funciona en segundo plano
y muestra el progreso en la sección de capturas. El nombre del remitente sólo
se asigna a la reseña enviada, no a las adicionales.
Si hay varios sitios posibles o falla la búsqueda adicional, la captura queda
guardada y se muestra el motivo; no se elige un sitio ambiguo.

La lectura de capturas usa Responses de OpenAI y respeta las opciones del modelo
configurado. Omite `temperature` cuando el modelo o el nivel de razonamiento no
la admiten, y reserva tokens para extraer el texto completo. No guarda reseñas
cuando la lectura queda incompleta.
Si las estrellas no son visibles, la reseña se importa como `no rating`, sin
deducirlas del texto. Esta etiqueta aparece en los detalles y las capturas para
Notion, y la valoración de humor recibe la puntuación como desconocida.

### 6. Vista de detalle de reseña

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

## Importación por enlace

En `Importar reseña`, pega el enlace de Google Maps y pulsa `Analizar reseña y sitio`.
Se admiten enlaces completos y enlaces cortos de `maps.app.goo.gl` y `goo.gl/maps`.
El servidor resuelve las redirecciones y conserva la URL de la reseña aunque Google
devuelva su pantalla de consentimiento.
El sitio se identifica por su identificador de Google Maps y se consulta su ficha
para guardar el nombre, la dirección y la valoración media aunque la respuesta de
reseñas no los incluya. Si el sitio ya existe, se reutiliza en lugar de duplicarlo.
Cuando no se puede obtener su nombre (también al importar capturas), se asigna un
nombre descriptivo basado en el texto y un identificador corto para distinguir
sitios desconocidos; no se agrupan todos como `Importado manualmente`.
La reseña enlazada se incorpora junto con otras reseñas de una o dos estrellas del mismo sitio.
La recogida adicional examina hasta `Máx. reseñas por sitio` entradas y omite las
que no tienen texto o ya están guardadas. Las existentes conservan su puntuación,
traducciones y decisiones de moderación; no se crean duplicados por cambios de idioma en el enlace.
El progreso se muestra mientras continúa el análisis en segundo plano y se
recupera al volver a la pantalla. Solo se admite un análisis manual por enlace a la vez.
Si la recogida adicional falla, se muestra un aviso y las reseñas ya guardadas siguen disponibles.

`Revisar reseñas del sitio` abre el detalle conjunto en otra pestaña, ordenado por humor.
Desde ahí puedes aceptar o rechazar cada reseña y exportar todas las aceptadas a una única página de Notion, con sus capturas.
Si indicas quién envió la reseña, su sección en Notion incluye `Nos la envía: Nombre`
para poder mencionarle en el programa. La atribución se guarda solo en la reseña
del enlace, no en las otras reseñas encontradas automáticamente en el mismo sitio.
Los sitios marcados como procesados siguen excluidos del análisis.

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

Cuando exportas un sitio desde la UI:

- se crea o actualiza una única página en la base de datos configurada
- se rellena el icono de la página con `⭐`
- se asignan propiedades del registro
- se escriben consecutivamente todas las reseñas aceptadas
- se genera y sube una captura PNG detrás de cada reseña

### Mapeo actual a Notion

- `Título` -> nombre del sitio, localidad/provincia/país, puntuación media y número de reseñas seleccionadas
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
- captura PNG de cada reseña, colocada antes del separador de la siguiente

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
- Las llamadas de scoring son opcionales y dependen de `OPENAI_API_KEY` o `TYPESAFE_API_KEY`, según el proveedor elegido.
- La UI local es la forma recomendada de revisión manual.
- El comando CLI `set-status` existe todavía con nomenclatura antigua y no es el flujo recomendado para moderación manual; la UI refleja mejor el modelo actual.

## Test rápido de scoring

```bash
PYTHONPATH=. .venv/bin/python scripts/test_score.py
```

## Pruebas de regresión

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```

La suite usa datos temporales y dobles de prueba para los servicios externos.
Cubre la selección de celebraciones, objetivos de reseñas nuevas, filtros geográficos,
sitios procesados, parada de búsquedas, moderación por sitio e importación por enlace.
No exporta a Notion ni consume llamadas de scoring.

## Estructura útil del proyecto

- `humor_reviews/run.py`
  Punto de entrada CLI.
- `humor_reviews/discover.py`
  Discovery de sitios con SerpApi.
- `humor_reviews/celebration_calendar.py`
  Consulta y caché de celebraciones por fecha.
- `humor_reviews/celebration_strategy.py`
  Selección segura de temas y generación de consultas.
- `humor_reviews/celebration_relevance.py`
  Evaluación de la relación entre una reseña y las celebraciones.
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
  Listado de sitios con filtros de procesado y orden por puntuación o actualización.
- `scripts/place_detail.html`
  Moderación de reseñas, exportación a Notion y cierre o reapertura del sitio.
- `scripts/review_detail.html`
  Vista de detalle y acciones de moderación.
- `scripts/import_review_view.html`
  Importación por enlace y por capturas.
- `scripts/run_view.html`
  Selección de celebraciones y seguimiento de la ejecución.
