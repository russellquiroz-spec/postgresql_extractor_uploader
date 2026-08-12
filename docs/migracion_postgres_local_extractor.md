# Migracion desde `postgres_local_extractor`

Resumen en una linea: **las llamadas a `extract_sql` no cambian**; lo que cambia es a
donde apunta la libreria (VM via tunel SSH, no PostgreSQL local) y como se configura.

---

## Lo que se mantiene igual

| | |
|---|---|
| Firma de `extract_sql` | `extract_sql("select ...")` sigue funcionando tal cual |
| `save_dir` / `base_name` / `save_csv` / `save_parquet` | mismos nombres y semantica |
| `on_event` | mismo contrato de eventos (`ts`, `level`, `event`, `message` + extras) |
| Devuelve | `pandas.DataFrame` |
| Motor | SQLAlchemy Core, sin ORM |
| Env aislado | archivo propio, nunca el `.env` del proyecto host |
| `install.py` | mismo flujo (`python install.py`, `--dev`) |

Fue una decision explicita mantener `db` como **keyword-only con default**, en vez de
copiar el `db` posicional de `redshift_extractor`, precisamente para que esta migracion
no obligue a editar cada llamada:

```python
# antes (postgres_local_extractor)
df = extract_sql("select 1")

# ahora (postgres_local_client)
df = extract_sql("select 1")          # identico: usa DEFAULT_DB
df = extract_sql("select 1", db="local_rw")   # solo si necesitas otro alias
```

---

## Lo que cambia

### 1. Configuracion: de una URL a aliases

`postgres_local_extractor` usa una sola variable con la URL completa:

```env
# .env.postgres_local_extractor
BD_ENGINE_RABBIT_LOCAL = 'postgresql://usuario:password@localhost:9558/<base-local>'
```

`postgres_local_client` usa aliases y no guarda credenciales en el archivo:

```env
# .env.postgres_local_client
SSH_HOST=<ip-del-bastion>
SSH_CREDENTIALS_ENV=<VAR_CREDENCIAL_SSH>
DEFAULT_DB=local

POSTGRES__local__HOST=localhost
POSTGRES__local__PORT=9553
POSTGRES__local__DBNAME=<nombre-base>
POSTGRES__local__CREDENTIALS_ENV=<VAR_CREDENCIAL_BD>
POSTGRES__local__READ_ONLY=true
```

La URL se arma internamente con `sqlalchemy.engine.URL.create(...)`, nunca por
concatenacion. Eso es lo que hace que el password de la VM (que trae `( ) + | $`) se
escape correctamente y que el nombre de base con guion (`<nombre-base>`) no necesite
tratamiento aparte.

La variable de override del archivo tambien cambia de nombre:
`POSTGRES_LOCAL_EXTRACTOR_ENV_FILE` -> `POSTGRES_LOCAL_CLIENT_ENV_FILE`.

### 2. Base de datos distinta

| | `postgres_local_extractor` | `postgres_local_client` |
|---|---|---|
| Base | `<base-local>` | `<nombre-base>` |
| Ubicacion | PostgreSQL local, `localhost:9558` | PostgreSQL en la VM, `localhost:9553` **dentro de la VM** |
| Acceso | directo | solo por tunel SSH |

No son la misma base. Si un script lee de `<base-local>`, migrarlo a esta libreria
lo apunta a otro lado. Las dos librerias pueden convivir instaladas si necesitas las dos
bases.

### 3. El tunel

No hay nada que hacer: la primera operacion abre el tunel y se reusa por el resto del
proceso. Pero implica dos cosas nuevas:

- La primera query tarda ~0.5 s extra (apertura del tunel). Pasa `on_event` para verlo
  en vez de percibirlo como colgado.
- Necesitas la host key de la VM en `known_hosts` una sola vez. Ver `docs/onboarding.md`.

### 4. Read-only por default

En `postgres_local_extractor` no habia guardas porque no escribia. Aca un alias sin
`READ_ONLY` explicito es de **solo lectura**, y `extract_sql` solo acepta lectura.

Si un script hacia `extract_sql("insert into ...")` aprovechando que la funcion solo
ejecutaba SQL, ahora falla con un mensaje que indica que use `execute_sql`. Eso es
intencional.

### 5. Driver

`psycopg2-binary` -> `psycopg` v3 (`postgresql+psycopg://`). Motivos: `COPY` con una API
limpia, soporte nativo de SQLAlchemy 2.0. No cambia nada en el codigo de usuario. Los dos
paquetes conviven en el mismo venv si tienes las dos librerias instaladas.

### 6. BOM en el `.env`

`postgres_local_extractor` toleraba el BOM en silencio (`encoding="utf-8-sig"`).
`postgres_local_client` **falla** con un error que menciona el BOM explicitamente y da el
comando para arreglarlo. Es preferible fallar claro que leer una variable vacia.

### 7. Errores

`postgres_local_extractor` envolvia todo en `RuntimeError`. Aca hay una jerarquia:
`ConfigError`, `TunnelNetworkError`, `TunnelAuthError`, `TunnelBindError`,
`TunnelHostKeyError`, `ReadOnlyError`, `DDLNotAllowedError`, `FullTableOperationError`,
`SchemaMismatchError`, `UpsertTargetError`. Todas derivan de
`PostgresLocalClientError`, asi que un `except PostgresLocalClientError` captura todo.

Si tu codigo hacia `except RuntimeError`, cambialo a `except PostgresLocalClientError`.

---

## Lo que gana

Cosas que antes tocaba hacer a mano:

```python
from postgres_local_client import (
    load_dataframe, upsert_dataframe, execute_sql, delete_where, transaction,
    list_tables, describe_table, table_exists, ping,
)

load_dataframe(df, "staging_ventas", db="local_rw")                 # COPY
upsert_dataframe(df, "metas", ["id"], db="local_rw")                # ON CONFLICT
delete_where("staging_ventas", "fecha < :d", {"d": "2026-01-01"}, db="local_rw")

with transaction(db="local_rw") as tx:                              # todo o nada
    tx.execute_sql("update public.metas set valor = :v where id = :id", {"v": 10, "id": 3})
    tx.load_dataframe(df, "staging_ventas")
```

---

## Checklist de migracion de un script

1. `pip install -e ".[parquet]"` en el proyecto (o `python install.py`).
2. Copiar `.env.example` a `.env.postgres_local_client` y llenar `SSH_*` y los aliases.
3. Registrar la host key de la VM (`docs/onboarding.md`).
4. `postgres-local-client ping --db local` para confirmar.
5. Cambiar el import: `from postgres_local_extractor import extract_sql` ->
   `from postgres_local_client import extract_sql`.
6. Revisar que el script apunte a la base correcta: `<nombre-base>` no es
   `<base-local>`.
7. Si el script escribe, pasarle `db="local_rw"` (o el alias de escritura que definas).
8. Cambiar `except RuntimeError` por `except PostgresLocalClientError` si aplica.
