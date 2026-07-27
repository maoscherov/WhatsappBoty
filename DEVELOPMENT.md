# Pipeline de desarrollo

Este repo (rama `develop`) es el **ambiente de desarrollo**. Los cambios de
esquema de base de datos se versionan con **Alembic** y se promueven a los demás
ambientes aplicando las migraciones.

## Setup local

```bash
pip install -r requirements-dev.txt   # incluye deps de test (pgserver, pytest)
```

Variables (`.env`):
- `DATABASE_URL` — Postgres (opcional; sin ella el bot corre solo con Redis)
- `OPENAI_API_KEY` — embeddings del RAG (opcional)
- `ANTHROPIC_API_KEY`, `REDIS_URL`, `WHATSAPP_*`, `MP_ACCESS_TOKEN`, etc.

## Base de datos y migraciones (Alembic)

El esquema lo definen las migraciones en `migrations/versions/`. **Alembic es la
única fuente de verdad del esquema** — `db.py` solo abre el pool.

```bash
# Aplicar todas las migraciones al ambiente apuntado por DATABASE_URL
alembic upgrade head

# Ver estado / historial
alembic current
alembic history

# Crear una nueva migración (editar el archivo generado con SQL/pgvector)
alembic revision -m "descripcion del cambio"

# Revertir la última
alembic downgrade -1
```

En el arranque del server (`app/main.py`), si `DATABASE_URL` está seteada, se
corre `alembic upgrade head` automáticamente — así cada deploy queda migrado.

### Promover a otro ambiente (staging / prod)

1. Setear `DATABASE_URL` del ambiente destino.
2. `alembic upgrade head` (o dejar que el arranque lo haga).
3. Indexar el catálogo para el RAG (una vez):
   `POST /bo/rag/reindex?key=BO_KEY`

## Tests

```bash
pytest              # corre todo (levanta un Postgres+pgvector embebido)
pytest tests/test_logic.py       # solo lógica (sin DB)
pytest -k rag                    # solo RAG
```

- `tests/test_logic.py` — búsqueda de catálogo, receta por categoría, matchers.
- `tests/test_db_rag.py` — Postgres real (pgserver) + pgvector: esquema,
  historial de mensajes, búsqueda semántica, base de conocimiento.
- `tests/test_degradation.py` — sin Postgres/embeddings, todo no-opea.

Los tests de DB usan `pgserver` (Postgres embebido con pgvector) y embeddings
fake determinísticos, así corren sin conexión ni claves externas.
