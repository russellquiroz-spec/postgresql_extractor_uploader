postgres_local_client

Libreria interna y CLI opcional para explotar un PostgreSQL alojado en una VM desde maquinas locales, por medio de un tunel SSH: extraer, cargar y modificar informacion. Soporta multiples conexiones por alias, carga un env propio (`.env.postgres_local_client`) y evita depender del `.env` del proyecto host.

Toma el modelo de conexion de `redshift_extractor` (tunel SSH, aliases, `secret_loader`) y el motor de `postgres_local_extractor` (PostgreSQL, SQLAlchemy Core), y agrega la mitad que ninguna de las existentes tiene: **escritura**.

--------------------------------------------------------------------------------
QUE HACE
--------------------------------------------------------------------------------

- Abre un tunel SSH hacia la VM y lo gestiona de forma transparente (abrir, reusar, cerrar).
- Conecta a PostgreSQL usando SQLAlchemy Core via `localhost:<puerto_del_tunel>`.
- Ejecuta SQL y devuelve un `pandas.DataFrame`.
- Carga DataFrames a tablas (append / replace / upsert) con `COPY`.
- Modifica datos via DML y DDL controlado, con transacciones explicitas.
- Opcionalmente guarda resultados a CSV y/o Parquet sin dejar de devolver el DataFrame.
- Permite definir varias bases o usuarios con aliases, por ejemplo `local` y `local_rw`.
- Emite eventos de estado estructurados para que el proyecto host los imprima, registre o muestre en UI.

--------------------------------------------------------------------------------
PRINCIPIOS DE DISENO
--------------------------------------------------------------------------------

- Library-first: API limpia para ser llamada desde otros proyectos; la CLI es un extra.
- Env aislado: carga solo `.env.postgres_local_client`, nunca el `.env` del proyecto host.
- Credenciales fuera del repo: el env guarda configuracion no sensible y apunta a secretos externos.
- Multiples conexiones: seleccion por alias.
- Estado sin acoplamiento: la libreria no configura logging global; emite eventos.
- Fail-fast: errores explicitos y tempranos.
- Windows-friendly: aliases normalizados a lowercase, lectura de variables persistidas en registro.
- **Tunel transparente**: llamas `extract_sql(...)` y la libreria se encarga del tunel. Nunca hace falta abrir una terminal aparte. Todo tunel que abre queda cerrado al terminar el proceso, incluso ante excepcion o `Ctrl+C`.
- **Escrituras seguras por defecto**: a diferencia de las hermanas, esta libreria puede destruir datos. Un alias sin `READ_ONLY` explicito es de solo lectura, y toda operacion destructiva requiere opt-in.
- **Convivencia sin efectos secundarios**: no modifica estado global del proceso. Se puede importar junto a las otras del ecosistema sin alterar su comportamiento ni el suyo. Ver `docs/compatibilidad.md`.

--------------------------------------------------------------------------------
INSTALACION
--------------------------------------------------------------------------------

Plug-and-play con el instalador local. Crea el venv, instala el paquete editable con sus dependencias y genera `.env.postgres_local_client` desde el ejemplo si no existe:

```powershell
python install.py
```

Con dependencias de desarrollo:

```powershell
python install.py --dev
```

Luego activa el entorno y verifica:

```powershell
.\.venv\Scripts\activate
postgres-local-client ls
postgres-local-client ping --db local
```

Instalacion manual equivalente:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[parquet]"        # o: pip install -e ".[dev,parquet]"
```

`pyarrow` va en el extra `parquet` para no imponerlo en el install base. Sin el, todo funciona salvo `save_parquet=True`.

--------------------------------------------------------------------------------
ENTORNO DE DESTINO
--------------------------------------------------------------------------------

| Item | Valor |
|---|---|
| Motor | PostgreSQL 17 |
| Host / Puerto **vistos desde la VM** | `localhost` / `9553` |
| Base | `<nombre-base>` |
| Usuario de trabajo | `<usuario-bd>` |
| Permisos | `CONNECT` + RW completo sobre el esquema `public` |
| Host SSH | `<ip-del-bastion>:22`, OpenSSH sobre Windows Server 2019 |
| Forward | `-L <puerto_local>:localhost:9553` |

El puerto `9553` **no** esta abierto al exterior: PostgreSQL solo acepta conexiones desde `localhost` de la VM. El unico camino de entrada es el tunel SSH y la libreria no intenta ningun fallback directo.

`<usuario-bd>` no es superusuario: no puede crear bases, roles ni esquemas. Cualquier operacion fuera de `public` falla por permisos, y es intencional.

Equivalente manual que la libreria automatiza:

```powershell
ssh -L 9553:localhost:9553 <usuario-ssh>@<ip-del-bastion>
```

--------------------------------------------------------------------------------
CONFIGURACION: .env.postgres_local_client
--------------------------------------------------------------------------------

La libreria carga configuracion solo desde su env propio, en este orden:

1. `POSTGRES_LOCAL_CLIENT_ENV_FILE` si esta definida (ruta absoluta).
2. Busqueda hacia arriba desde el package instalado hasta encontrar `.env.postgres_local_client`.

Nunca carga el `.env` del proyecto host. Si no encuentra el archivo, o si no hay ningun alias configurado, falla de inmediato indicando ambas rutas intentadas.

**Precedencia de configuracion**, de mayor a menor:

1. argumentos explicitos de la funcion
2. variables de proceso con prefijo `PGC_` (ej. `PGC_SSH_HOST`)
3. valores del archivo `.env.postgres_local_client`

Dentro del archivo los nombres van **sin** prefijo: se lee aislado y no puede colisionar. Al leer de `os.environ` se exige el prefijo `PGC_`, para que un `SSH_HOST` suelto en el sistema —puesto por otra libreria o por el usuario— nunca sea consumido por esta.

El archivo debe guardarse en **UTF-8 sin BOM**. PowerShell 5.1 (`Set-Content`, `>`, `Out-File`) agrega BOM y `python-dotenv` no lo maneja; la libreria detecta el BOM y falla con un mensaje explicito en vez de leer la primera variable vacia.

### SSH (global)

```env
SSH_HOST=<ip-del-bastion>
SSH_PORT=22
SSH_LOCAL_PORT=0                 # 0 = puerto libre automatico (recomendado)
SSH_AUTO_OPEN=true               # abre el tunel solo si no hay uno vivo
SSH_KEEPALIVE_S=30
SSH_CONNECT_TIMEOUT_S=15
SSH_COMPRESSION=true             # duplica el throughput de las cargas grandes
# SSH_KNOWN_HOSTS_PATH=...       # default: ~/.ssh/known_hosts
```

`SSH_COMPRESSION` viene activado a proposito: `sshtunnel` reenvia el trafico en trozos
de 1 KB, asi que el throughput depende de cuantos bytes pasen por ese bucle. Medido
sobre un `COPY` de 100k filas contra la VM: ~4.1 s sin compresion, ~2.1 s con. Ponlo en
`false` si la CPU local resulta ser el cuello.

Autenticacion: elige **una** de las tres opciones.

```env
# A (la que usa el equipo hoy): usuario y password en una variable de sistema,
#    con el mismo contrato de parseo que CREDENTIALS_ENV.
SSH_CREDENTIALS_ENV=VM_SSH_CREDENTIALS

# B (recomendada a futuro): llave privada.
SSH_USER=<usuario-ssh>
SSH_PKEY_PATH=C:\Users\<usuario>\.ssh\id_ed25519
SSH_PKEY_PASSPHRASE_ENV=RABBIT_VM_SSH_PASSPHRASE   # NOMBRE de la variable, no el valor

# C: usuario en el archivo, password en una variable aparte.
SSH_USER=<usuario-ssh>
SSH_PASSWORD_ENV=RABBIT_VM_SSH_PASSWORD
```

`SSH_CREDENTIALS_ENV` tiene prioridad sobre `SSH_USER` / `SSH_PASSWORD_ENV`. El password y la passphrase nunca van inline en el archivo: solo el **nombre** de la variable de sistema que los contiene.

### App opcional

```env
LOG_LEVEL=INFO
OUTPUT_DIR=./output
DEFAULT_DB=local                 # alias usado cuando no se pasa `db`
```

La libreria no configura logging por si sola. `OUTPUT_DIR` es util para flujos locales o CLI; para la API se recomienda pasar `save_dir` explicitamente.

### PostgreSQL por alias

Cada alias necesita `HOST`, `PORT` y `DBNAME`. `HOST`/`PORT` son los del **destino visto desde el otro extremo del tunel** (dentro de la VM), no los del puerto local.

Opcion A (recomendada): apuntar a una variable de sistema con `CREDENTIALS_ENV`.

```env
POSTGRES__local__HOST=localhost
POSTGRES__local__PORT=9553
POSTGRES__local__DBNAME=<nombre-base>
POSTGRES__local__CREDENTIALS_ENV=VM_DB_CREDENTIALS
POSTGRES__local__READ_ONLY=true
```

Opcion B (solo uso local controlado): credenciales inline con `USER`/`PASSWORD`. Si `CREDENTIALS_ENV` esta definido, tiene prioridad.

Claves por alias soportadas: `HOST`, `PORT`, `DBNAME`, `USER`, `PASSWORD`, `CREDENTIALS_ENV`, `READ_ONLY`, `ALLOW_DDL`, `SCHEMA` (default `public`), `STATEMENT_TIMEOUT_S`, y override de SSH: `SSH_HOST`, `SSH_PORT`, `SSH_USER`, `SSH_PKEY_PATH`.

Un campo desconocido es un error, no un valor ignorado: un typo como `READONLY=false` fallaria en silencio dejando el alias en modo lectura, asi que se rechaza.

**Defaults seguros:** un alias sin `READ_ONLY` explicito es `READ_ONLY=true`, y sin `ALLOW_DDL` explicito es `ALLOW_DDL=false`.

Aliases: permiten letras, numeros, `_` y `-`; se normalizan a lowercase internamente.

**Patron recomendado — dos aliases sobre la misma base.** `local` read-only para notebooks de analisis y `local_rw` para los procesos que escriben. Es la forma mas barata de evitar que un `DELETE` accidental en exploracion toque la base. Los dos comparten un unico tunel.

### Resolucion de credenciales (secret_loader)

Contrato identico al de `redshift_extractor`. Formatos soportados para la variable de sistema:

```text
{"user":"db_user","password":"db_password"}
USER=db_user;PASSWORD=db_password
db_user:db_password
```

Tambien se soportan JSON con campos extra, anidado y escapado o envuelto como string. Si existe `%APPDATA%\KeyringManager\credentials.json`, se resuelve primero una entrada cuyo `env_var` coincida con `CREDENTIALS_ENV`.

--------------------------------------------------------------------------------
GESTION DEL TUNEL
--------------------------------------------------------------------------------

El caso primario es implicito: no tocas el tunel.

```python
from postgres_local_client import extract_sql

df = extract_sql("select 1 as test;")   # abre el tunel si hace falta, lo reusa despues
```

Para control explicito del ciclo de vida:

```python
from postgres_local_client import tunnel, extract_sql, load_dataframe

with tunnel(db="local_rw") as t:
    print(t.local_port)
    df = extract_sql("select * from public.ventas", db="local_rw")
    load_dataframe(df2, "staging_ventas", db="local_rw")
# aqui el tunel ya esta cerrado
```

API:

```python
open_tunnel(db=None, *, force_new=False, on_event=None) -> TunnelInfo
close_tunnel(db=None, *, on_event=None) -> None
close_all_tunnels(*, on_event=None) -> None
tunnel(db=None, *, on_event=None) -> ContextManager[TunnelInfo]
tunnel_status() -> list[TunnelInfo]
```

`TunnelInfo` expone `local_port`, `remote_host`, `remote_port`, `ssh_host`, `ssh_port`, `ssh_user`, `is_alive`, `opened_at` y `owned`.

Comportamiento:

1. **Idempotencia.** `open_tunnel()` con un tunel vivo devuelve el existente. `force_new=True` fuerza uno nuevo en otro puerto local (siempre efimero: dos tuneles no pueden compartir puerto).
2. **Un tunel por destino SSH, no por alias.** Dos aliases que apuntan al mismo `SSH_HOST` + `HOST:PORT` remoto comparten un solo tunel.
3. **Reuso entre procesos.** Si hay un tunel externo valido (un `ssh -L` que dejaste abierto), se reusa, se marca `owned=False` y **nunca se cierra**. Solo es detectable si fijas `SSH_LOCAL_PORT`, porque un puerto efimero no se puede adivinar; se emite un `WARNING` porque no hay forma de verificar a que base apunta sin conectarse (confirma con `ping()`).
4. **Verificacion real, no optimista.** "Tunel vivo" significa handshake TCP contra el puerto local **y** respuesta del servidor PostgreSQL del otro lado. Que el proceso SSH exista no basta: el caso comun de falla es un tunel zombie cuya sesion SSH ya murio.
5. **Cierre garantizado.** `atexit` mas manejo de `KeyboardInterrupt` / `SIGTERM`. Al cerrar se dispone primero el pool de SQLAlchemy y luego el tunel, en ese orden.
6. **Reconexion.** Si el tunel cae a media sesion se reabre una vez de forma transparente y se reintenta la operacion, emitiendo `tunnel_retry`. Nunca reintenta mas de una vez. `transaction()` no reintenta: si la conexion muere el servidor ya aborto todo y reintentar a ciegas podria repetir operaciones.
7. **Keepalive** cada `SSH_KEEPALIVE_S`, para que un notebook abierto todo el dia no se corte por inactividad.
8. **Errores distinguibles.** `TunnelNetworkError` (no hay ruta al 22: Security Group o IP cambiada), `TunnelAuthError` (llave o password invalidos), `TunnelBindError` (puerto local ocupado) y `TunnelHostKeyError` (host desconocido o host key que no coincide, con los dos fingerprints en el mensaje).

**Verificacion de host key.** Nunca se deshabilita. Se usa `~/.ssh/known_hosts` (o `SSH_KNOWN_HOSTS_PATH`); si el host no esta registrado, falla con las instrucciones para agregarlo en vez de aceptarlo automaticamente.

--------------------------------------------------------------------------------
USO COMO LIBRERIA
--------------------------------------------------------------------------------

Todas las funciones aceptan `db: str | None = None` (alias; default `DEFAULT_DB`) y `on_event: Callable[[dict], None] | None = None`.

> **Nota de firma.** `db` es **keyword-only con default**, a diferencia de `redshift_extractor` donde es el primer posicional. Es una divergencia deliberada: hace que `extract_sql("select 1")` funcione sin ceremonia y permite migrar los scripts de `postgres_local_extractor` sin editar cada llamada.

### Descubrimiento

```python
from postgres_local_client import list_databases, describe_database, ping

list_databases()                  # ['local', 'local_rw']
describe_database("local")        # host, port, dbname, read_only, allow_ddl... sin credenciales
ping("local")                     # ok, server_version, database, user, tunnel_port, latency_ms
```

`ping()` reporta `database`, `user` y `tunnel_port` **reales, tal como los ve el servidor**. Es la forma de detectar un puerto local colisionado con otro PostgreSQL.

### Lectura

```python
extract_sql(
    query: str | None = None,
    *,
    db: str | None = None,
    query_file: str | None = None,
    params: dict | None = None,
    save_dir: str | None = None,
    base_name: str | None = None,
    save_csv: bool = False,
    save_parquet: bool = False,
    chunksize: int | None = None,
    on_event=None,
) -> pandas.DataFrame
```

- `query` tiene prioridad sobre `query_file` si se pasan ambos.
- `query_file`: UTF-8, se lee completo, expande `~`, `FileNotFoundError` si no existe.
- `params` se enlaza con bindparams (`:nombre`), nunca por interpolacion de texto.
- Si `save_dir` es `None` o vacio, solo devuelve el DataFrame.
- `base_name` por defecto: `alias_dbname_timestamp`.
- `chunksize` activa lectura por lotes para resultados grandes (devuelve el DataFrame completo igual).
- `extract_sql` es solo lectura: si el SQL trae DML/DDL, falla indicando que uses `execute_sql`.

```python
df = extract_sql(
    "select * from public.ventas where fecha >= :desde",
    params={"desde": "2026-01-01"},
    save_dir=r"C:\Users\TuUsuario\Documents\salidas",
    base_name="ventas_enero",
    save_csv=True,
    save_parquet=True,
)
```

Nota sobre `params`: no uses `:n::int` (el `::` de cast confunde al parser de bindparams de SQLAlchemy y el parametro queda literal). Usa `cast(:n as int)`.

### Carga

```python
load_dataframe(
    df, table, *, db=None, schema=None,
    if_exists: Literal["append","replace","fail"] = "append",
    chunksize: int = 10_000,
    method: Literal["multi","copy"] = "copy",
    confirm: bool = False,
    on_event=None,
) -> int
```

- `method="copy"` usa `COPY ... FROM STDIN`. Referencias medidas para 100k filas x 3
  columnas: ~0.15 s contra un PostgreSQL local, ~2 s a traves del tunel a la VM. La
  diferencia es transferencia, no codigo: la conversion de pandas son ~10 ms y el limite
  lo pone el reenvio en trozos de 1 KB de `sshtunnel` (ver `SSH_COMPRESSION`).
- Las columnas del DataFrame se validan contra la tabla destino **antes** de escribir la primera fila; el error reporta las sobrantes y las faltantes.
- `if_exists="replace"` es destructivo: requiere `confirm=True` o falla. Vacia con `TRUNCATE` si el alias tiene `ALLOW_DDL=true`, y con `DELETE` si no, para no saltarse la guarda de DDL.
- Si la tabla no existe se crea a partir de los dtypes del DataFrame, pero eso es DDL: requiere `ALLOW_DDL=true`. Se emite un `WARNING`, porque los tipos que infiere pandas rara vez son los que querrias en produccion.
- Nulos: `NaN`, `NaT` y `pd.NA` se cargan como `NULL`; un string vacio se preserva como `''` y no se confunde con `NULL`.

```python
upsert_dataframe(
    df, table, conflict_cols: list[str], *,
    update_cols: list[str] | None = None,   # None = todas menos conflict_cols
    db=None, schema=None, chunksize=10_000, on_event=None,
) -> dict   # {"inserted": int, "updated": int}
```

- `INSERT ... ON CONFLICT (cols) DO UPDATE SET ...`, contando insertadas y actualizadas por separado.
- Verifica que exista PK o indice unico sobre `conflict_cols`; si no, falla listando los indices unicos que si existen.
- Si `update_cols` queda vacio, degenera en `ON CONFLICT DO NOTHING` (insert idempotente).

### Modificacion

```python
execute_sql(sql, params=None, *, db=None, allow_full_table=False, on_event=None) -> int
delete_where(table, where: str, params: dict, *, db=None, schema=None, on_event=None) -> int
```

Guardas:

- Un alias con `READ_ONLY=true` rechaza cualquier sentencia que no sea lectura.
- `UPDATE` o `DELETE` sin `WHERE` fallan salvo `allow_full_table=True`.
- `DROP`, `TRUNCATE`, `ALTER`, `CREATE` fallan salvo `ALLOW_DDL=true` en el alias.
- En `delete_where`, `where` es obligatorio y no puede estar vacio.

Las guardas se evaluan sobre el SQL **parseado** con `sqlglot`, no por `str.contains`: un `WHERE` dentro de un comentario o de un string literal no cuenta como `WHERE` valido. Si el SQL no se puede parsear, se rechaza (falla cerrado). Ademas, en un alias read-only la sesion va con `default_transaction_read_only=on`, asi que el servidor tambien lo hace cumplir.

### Transacciones

```python
from postgres_local_client import transaction

with transaction(db="local_rw") as tx:
    tx.execute_sql("update public.metas set valor = :v where id = :id", {"v": 10, "id": 3})
    tx.load_dataframe(df, "staging_ventas")
    # commit al salir sin excepcion; rollback si algo falla
```

`tx` expone `extract_sql`, `execute_sql`, `load_dataframe`, `upsert_dataframe` y `delete_where` sobre una unica conexion y transaccion. El rollback esta garantizado ante cualquier excepcion, y el evento `tx_rollback` incluye cual fue la operacion que fallo.

### Utilidades de esquema

```python
list_schemas(db=None) -> list[str]
list_tables(db=None, schema=None) -> pandas.DataFrame        # nombre, tipo, filas aprox, tamano
describe_table(table, db=None, schema=None) -> pandas.DataFrame
table_exists(table, db=None, schema=None) -> bool
```

--------------------------------------------------------------------------------
EVENTOS DE ESTADO
--------------------------------------------------------------------------------

Mismo contrato que las librerias hermanas. Cada evento es un dict con `ts`, `level` (`DEBUG` | `INFO` | `WARNING` | `ERROR`), `event`, `message` y extras segun la operacion: `db`, `rows`, `cols`, `path`, `elapsed_s`, `table`, `affected`, `local_port`, `owned`.

Eventos: `config_loaded`, `tunnel_open`, `tunnel_reused`, `tunnel_retry`, `tunnel_close`, `connect`, `query_start`, `query_done`, `write_start`, `write_progress` (por chunk), `write_done`, `tx_begin`, `tx_commit`, `tx_rollback`, `file_saved`, `error`.

Los eventos de tunel incluyen `local_port`, `ssh_host`, `owned` y `elapsed_s`. Abrir un tunel tarda segundos: sin estos eventos la primera query se percibe como "colgada".

```python
def printer(evt):
    extras = {k: v for k, v in evt.items() if k not in ("ts", "level", "event", "message")}
    print(f'{evt["ts"]} [{evt["level"]}] {evt["event"]}: {evt["message"]} | {extras}')

df = extract_sql("select 1 as test;", on_event=printer)
```

Un `on_event` que lanza excepcion no tumba la operacion en curso.

**Ningun evento, log, mensaje de error ni traceback contiene la password de base de datos, la password o passphrase SSH, ni la URL completa.** La conexion se referencia siempre como `host:port/dbname`. Los campos sensibles llevan `repr=False` para que un `print(cfg)` accidental no los exponga, y hay una red de seguridad que tacha secretos registrados si alguna vez aparecieran en un mensaje de una dependencia.

--------------------------------------------------------------------------------
CLI
--------------------------------------------------------------------------------

El paquete expone el comando `postgres-local-client`:

```powershell
postgres-local-client ls
postgres-local-client describe --db local
postgres-local-client ping --db local
postgres-local-client run      --db local --query "select 1 as test" --out .\output\r.parquet --fmt parquet
postgres-local-client run-file query.sql --db local
postgres-local-client tables   --db local --schema public
postgres-local-client load     --file .\data\ventas.csv --table ventas --db local_rw --if-exists append
postgres-local-client tunnel   status
postgres-local-client tunnel   open --keep-alive
postgres-local-client tunnel   close
```

Formatos soportados: `csv`, `parquet`. Los subcomandos de datos abren y cierran el tunel por si mismos; no requieren `tunnel open` previo. `--debug` en cualquier comando muestra los eventos `DEBUG`.

Codigos de salida: `0` exito, `1` error de negocio, `2` error de configuracion, `3` error de tunel.

### run-file

Por defecto envuelve el query con `LIMIT 10` para una prueba rapida (solo aplica a `SELECT`/`WITH`, decidido sobre el SQL parseado); `--full` ejecuta el query completo.

```powershell
postgres-local-client run-file query.sql --db local
postgres-local-client run-file query.sql --db local --full
postgres-local-client run-file query.sql --db local --print-sql --dry-run
postgres-local-client run-file query.sql --db local --full --output resultado.csv
```

Opciones: `--db`, `--limit` (default 10), `--full`, `--retries` / `--retry-wait` (default 3 intentos, 5 s), `--print-sql`, `--dry-run`, `--output`.

### load

- Pide confirmacion interactiva si `--if-exists replace`, salvo `--yes`.
- `--dry-run` valida columnas contra la tabla destino y reporta el plan sin escribir.
- Rechaza ejecutarse contra un alias con `READ_ONLY=true`, indicando cual usar en su lugar.

### tunnel open --keep-alive

Levanta el tunel en foreground e imprime el puerto local asignado, para trabajar desde un cliente externo (DBeaver, pgAdmin). Sin `--keep-alive` el tunel se cierra al terminar el proceso, asi que solo sirve como verificacion.

--------------------------------------------------------------------------------
ESTRUCTURA DEL PROYECTO
--------------------------------------------------------------------------------

- `config.py`: localiza el env propio, carga SSH y descubre conexiones por alias.
- `secret_loader.py`: resuelve credenciales desde KeyringManager, variables de sistema y registro de Windows. Copia sin modificar del de `redshift_extractor`.
- `types.py`: contratos (`SSHConfig`, `PostgresConfig`, `TunnelInfo`, `LoadResult`, `UpsertResult`).
- `errors.py`: jerarquia de excepciones (incluye las cuatro de tunel).
- `events.py`: emision de eventos y redaccion de secretos.
- `logging.py`: logger propio con `NullHandler`; configuracion de consola solo para la CLI.
- `tunnel.py`: manejo del tunel SSH — open/close/status, health check, host key, atexit, reconexion.
- `engine.py`: engine de SQLAlchemy por alias, pool, `statement_timeout`, reintento por caida de tunel.
- `guards.py`: validacion de SQL destructivo sobre el arbol parseado (read-only, WHERE, DDL).
- `extractor.py`: `list_databases`, `extract_sql`.
- `loader.py`: `load_dataframe`, `upsert_dataframe` (COPY).
- `mutator.py`: `execute_sql`, `delete_where`.
- `tx.py`: context manager `transaction()`.
- `schema.py`: `ping`, `describe_database`, `list_schemas`, `list_tables`, `describe_table`, `table_exists`.
- `io.py`: utilidades de escritura CSV/Parquet.
- `cli.py`: entrypoint de CLI.

> Ojo con un detalle de nombres: el paquete re-exporta el context manager `tunnel`, que sombrea el submodulo `postgres_local_client.tunnel`. Para llegar al modulo usa imports explicitos (`from postgres_local_client.tunnel import open_tunnel`).

--------------------------------------------------------------------------------
TESTS
--------------------------------------------------------------------------------

```powershell
pytest tests/
```

- Unitarios de `config`, `secret_loader`, `guards`, `types`, `events`, `io` y construccion de URL, sin necesidad de base.
- `tests/sshserver.py` levanta un **servidor SSH real en proceso** (paramiko en modo servidor, con forwarding `direct-tcpip`) y `tests/fakepg.py` un PostgreSQL falso que responde al health check. Con eso los tests de tunel cubren apertura, reuso, caida, zombie, cierre, concurrencia y los cuatro modos de fallo sin depender de la VM.
- Integracion contra la base real, marcada `@pytest.mark.integration`; se salta con mensaje si la VM no responde. Todo lo que escriben esos tests vive en un esquema desechable (`pytest_tmp`) que un fixture crea y destruye con `DROP SCHEMA ... CASCADE`: `public` no se toca ni por accidente.
- Convivencia (`tests/test_coexistencia.py`, marca `coexistence`): snapshots de `os.environ` y del root logger antes y despues del import y de la primera operacion, `SSH_HOST` inyectado con valor falso, y verificacion de que el `pyproject.toml` no declare techos sin justificar.
- Fuga de recursos: 50 ciclos de abrir/cerrar tunel contando hilos y puertos.

Marcas: `pytest -m "not integration"` para correr sin base; `-m sshserver` solo los de tunel.

--------------------------------------------------------------------------------
TROUBLESHOOTING
--------------------------------------------------------------------------------

- **SSH auth falla:** revisa `SSH_USER` / `SSH_CREDENTIALS_ENV`, `SSH_PKEY_PATH` y que la llave publica este en `authorized_keys` de la VM. En Windows Server, para una cuenta de administrador la ruta correcta es `C:\ProgramData\ssh\administrators_authorized_keys` (no `~\.ssh\authorized_keys`), y el archivo debe tener ACL restringida a `Administrators` y `SYSTEM`; si no, `sshd` la ignora en silencio. Es la causa numero uno de "la llave es correcta pero no entra".
- **Timeout al puerto 22:** Security Group de AWS o IP publica de tu maquina cambiada.
- **`TunnelHostKeyError`:** el host no esta en `known_hosts`. Agregalo con `ssh-keyscan <ip-del-bastion> >> ~/.ssh/known_hosts` y verifica el fingerprint con quien administra la VM. Ver `docs/onboarding.md`.
- **No conecta a PostgreSQL pero el tunel esta arriba:** verifica `POSTGRES__<alias>__HOST/PORT`; deben ser `localhost:9553` (destino visto desde la VM), no el puerto local del tunel.
- **Conecta pero la base "esta vacia" o tiene datos raros:** puerto local colisionado con otro PostgreSQL. Revisa `ping()`, que reporta `tunnel_port`, `database` y `user` reales.
- **La variable de credenciales no aparece:** abre una terminal nueva o valida el valor persistido en Windows (la libreria tambien lee el registro).
- **Password raro o con escapes:** usa JSON o revisa el parseo con `parse_credentials_secret`.
- **Alias no existe:** revisa con `list_databases()`.
- **`.env` con BOM:** recrealo con Python o un editor que guarde UTF-8 sin BOM. El error te da el comando exacto.
- **Parquet falla:** `pip install "postgres-local-client[parquet]"`.
- **`AttributeError: module 'paramiko' has no attribute 'DSSKey'`:** tienes `paramiko>=4` instalado, incompatible con `sshtunnel` 0.4.0. Reinstala respetando el rango del `pyproject.toml`. Ver `docs/compatibilidad.md`.
- **El tunel se abre contra el bastion equivocado, o la config "no se aplica":** sintoma clasico de contaminacion de `os.environ` por otra libreria. Verifica con `describe_database()` y `tunnel_status()`. Esta libreria es inmune por construccion; si te pasa, revisa `docs/compatibilidad.md`.

--------------------------------------------------------------------------------
SEGURIDAD
--------------------------------------------------------------------------------

- No commitear `.env.postgres_local_client`; esta en `.gitignore` desde el commit inicial.
- Preferir `CREDENTIALS_ENV` / KeyringManager sobre `USER`/`PASSWORD` inline.
- La libreria no imprime ni loggea credenciales, y el logger de paramiko queda acotado a un logger propio que no propaga.
- Privilegios minimos: `<usuario-bd>` limitado al esquema `public` de `<nombre-base>`.
- **Read-only por default:** el alias de uso general (`local`) va con `READ_ONLY=true`. La escritura se habilita en un alias aparte y explicito.
- **Autenticacion SSH por llave, no por password**, en cuanto el equipo genere y registre sus llaves `ed25519`. Hoy la VM solo tiene password, por eso se soportan las dos.
- Mientras el tunel esta abierto, **cualquier proceso local puede alcanzar la base sin autenticacion SSH**. Otra razon para usar puerto efimero y cerrar al terminar en vez de dejarlo abierto indefinidamente.
- Nunca se deshabilita la verificacion de host key.

--------------------------------------------------------------------------------
DOCUMENTACION ADICIONAL
--------------------------------------------------------------------------------

- `docs/compatibilidad.md`: resultado de la verificacion de resolucion conjunta con las hermanas, y las reglas que garantizan la convivencia.
- `docs/migracion_postgres_local_extractor.md`: que cambia y que se mantiene igual al migrar desde `postgres_local_extractor`.
- `docs/onboarding.md`: guia para que un usuario nuevo quede operando (llave SSH, `known_hosts`, `.env`, verificacion).

--------------------------------------------------------------------------------
ROADMAP SUGERIDO
--------------------------------------------------------------------------------

- Extraer `secret_loader` a un paquete interno compartido, si algun dia el modulo empieza a cambiar seguido.
- `COPY TO` para exportaciones grandes sin pasar por pandas.
- Streaming real en lectura (hoy `chunksize` lotea el fetch pero materializa el DataFrame completo).
- Checks de calidad de datos y metricas de operacion post-carga.
- Pool de tuneles persistente entre procesos (daemon local) si el costo de apertura molesta.
