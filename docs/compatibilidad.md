# Compatibilidad y convivencia con el ecosistema

Fecha de la verificacion: **2026-08-12**
Maquina: Windows 11 Pro, Python 3.13.2, pip 25.3

Este documento cubre dos cosas distintas:

1. La **evidencia empirica** de que `postgres_local_client` resuelve junto a las
   librerias hermanas (criterio 30).
2. Las **reglas de construccion** que hacen cierta la convivencia contra cualquier
   combinacion presente y futura, sin depender de esa evidencia (criterios 25 a 29).

Lo segundo es lo que realmente garantiza la convivencia. Lo primero es una
comprobacion puntual contra las versiones que existian el dia de la verificacion.

---

## 1. Resolucion conjunta (criterio 30)

Metodo: venv limpio y `pip install --dry-run --ignore-installed --report`, que resuelve
el grafo completo sin instalar nada. Se probaron todas las combinaciones **por pares**
(esta libreria + cada hermana) y la de **todas juntas** como caso extremo.

Las cuatro hermanas estaban disponibles en la maquina, asi que se pudieron probar todas.

| Combinacion | Resultado |
|---|---|
| solo `postgres_local_client` | RESUELVE |
| esta + `redshift_extractor` | RESUELVE |
| esta + `mongo_extractor` | RESUELVE |
| esta + `netsuite_extractor` | RESUELVE |
| esta + `postgres_local_extractor` | RESUELVE |
| esta + las cuatro hermanas | RESUELVE |

Ningun `ResolutionImpossible`. No hubo que aflojar ningun rango para acomodarse.

### Versiones que gana el resolvedor con todas juntas

| Paquete | Version | Nota |
|---|---|---|
| `pandas` | 3.0.5 | compartida con las cuatro hermanas |
| `numpy` | 2.5.2 | transitiva de pandas |
| `sqlalchemy` | 2.0.52 | compartida con `postgres_local_extractor` |
| `psycopg` + `psycopg-binary` | 3.3.4 | **solo esta libreria** |
| `psycopg2-binary` | 2.9.12 | de `redshift_extractor` y `postgres_local_extractor` |
| `python-dotenv` | 1.0.1 | las hermanas la pinean exacta; nuestro `>=1.0` la acepta |
| `sshtunnel` | 0.4.0 | compartida con `redshift_extractor` y `mongo_extractor` |
| `paramiko` | 3.5.0 | las hermanas la pinean exacta; nuestro rango la acepta |
| `keyring` | 25.7.0 | compartida con tres hermanas |
| `typer` | 0.27.1 | compartida con las cuatro |
| `sqlglot` | 30.16.0 | **solo esta libreria** |
| `pyarrow` | 25.0.1 | de `redshift_extractor`; aca va en el extra `parquet` |
| `cryptography`, `bcrypt`, `pynacl` | 50.0.0 / 5.0.0 / 1.6.2 | transitivas de paramiko |

### Los puntos donde se esperaba conflicto

**`psycopg2` vs `psycopg` (v3).** Conviven sin problema, como se anticipaba: son
distribuciones distintas con nombres de import distintos. En la resolucion conjunta
aparecen las dos a la vez. Cada una vendorea su propio libpq y en Windows no se observo
choque de DLL: `ping()` de esta libreria y `extract_sql` de `redshift_extractor`
funcionan en el mismo venv.

**`pandas` / `numpy` / `pyarrow`.** Son las mas compartidas. Nuestro piso es
`pandas>=2.0`, mas bajo que el `>=2.2.3` de las hermanas, asi que nunca somos nosotros
los que fuerzan la version. Verificado empiricamente sobre pandas 3.0.5.

**`sqlglot`.** Es la unica dependencia que ninguna hermana tiene. No genero friccion:
no comparte transitivas relevantes con nadie. Si algun dia la genera, la alternativa es
un parseo propio acotado — pero nunca `str.contains`.

**`paramiko` / `sshtunnel`.** Aqui **si** aparecio un problema, y grave. Ver abajo.

---

## 2. El unico techo del proyecto: `paramiko<4`

Esta es la incompatibilidad concreta que justifica el unico techo de version del
`pyproject.toml`, y el hallazgo mas importante para el equipo.

**`sshtunnel` 0.4.0 no funciona con `paramiko>=4`.**

`sshtunnel.get_keys()` construye este diccionario:

```python
paramiko_key_types = {'rsa': paramiko.RSAKey,
                      'dsa': paramiko.DSSKey,      # <-- ya no existe
                      'ecdsa': paramiko.ECDSAKey}
```

`paramiko` elimino `DSSKey` en la version 4.0.0. El diccionario se construye
**incondicionalmente** en cada creacion del forwarder —no lo evitan
`host_pkey_directories=[]` ni `allow_agent=False`— asi que cualquier intento de abrir un
tunel truena con:

```
AttributeError: module 'paramiko' has no attribute 'DSSKey'
```

Verificado el 2026-08-12: falla con `paramiko` 4.0.0 y 5.0.0; funciona con 3.5.1.
`sshtunnel` 0.4.0 es la ultima release publicada (2021), asi que no hay version nueva a
la que subir: el techo tiene que ir en `paramiko`.

```toml
"paramiko>=2.7.2,<4",
```

Consecuencias:

- Es compatible con el `paramiko==3.5.0` que pinean `redshift_extractor` y
  `mongo_extractor`, asi que no rompe nada del ecosistema.
- Declararlo convierte un `AttributeError` confuso en tiempo de ejecucion en un error
  de resolucion claro en tiempo de instalacion.
- **Esto tambien afecta a `redshift_extractor` y `mongo_extractor`**: su pin exacto los
  protege hoy, pero si alguna vez lo aflojan a `paramiko>=3`, pip podria darles 4.x y su
  tunel dejaria de abrir. Vale la pena que ellas tambien declaren el techo.
- Se quita en cuanto exista una `sshtunnel` que soporte `paramiko>=4`.

---

## 3. Politica de dependencias

- **Rangos, no pins.** `>=` con el minimo real probado y **sin techo**, salvo la
  excepcion documentada arriba. Un pin exacto o un techo preventivo en una libreria
  interna se vuelve un conflicto irresoluble en cuanto otra pide algo distinto.
- **Minimo comun.** Ante la duda entre dos versiones que funcionan, la mas baja. Un piso
  alto excluye tanto como un techo bajo. Por eso `pandas>=2.0` y no `>=2.2.3`.
- **Superficie minima.** `pyarrow` va en el extra `.[parquet]` para no imponerlo en el
  install base.
- **Sin transitivas re-declaradas.** `paramiko` se declara solo porque el codigo lo
  importa directamente (excepciones, `HostKeys`, carga de llaves, verificacion de host
  key), y con el rango mas laxo que funciona.

`keyring` es una adicion a la tabla original de la solicitud: `secret_loader` lo importa
de forma perezosa cuando encuentra una entrada en `%APPDATA%\KeyringManager\credentials.json`,
y ese archivo **si existe** en la maquina del equipo. Sin declararlo, `CREDENTIALS_ENV`
fallaria con un `RuntimeError` pidiendo instalarlo. Se declara con piso `>=24`, mas bajo
que el `>=25.6.0` de las hermanas, asi que no aporta restriccion.

---

## 4. Aislamiento de `os.environ`: el bug que si estaba ahi

El aislamiento que documentan las hermanas es sobre **que archivo** carga cada una, no
sobre el entorno del proceso.

`python-dotenv` usa `override=False` por defecto, asi que `load_dotenv()` **no** pisa lo
que ya exista en `os.environ` — pero **si** copia el archivo al entorno. Con dos
librerias cargando su env, la primera en cargar gana y la segunda se queda en silencio
con los valores de la otra, sin ningun error.

Esto no era hipotetico. Al empezar este proyecto, las cuatro hermanas llamaban
`load_dotenv()`, y `redshift_extractor` usa nombres **planos** identicos a los que
necesita esta libreria:

```
SSH_HOST   SSH_PORT   SSH_USER   SSH_PKEY_PATH   LOG_LEVEL   OUTPUT_DIR
```

En un proyecto host con las dos instaladas, la segunda en cargar habria intentado
conectarse **al bastion de la otra**. Y el bug solo aparece en el proyecto host que
instala dos: en el venv de desarrollo de cada libreria es invisible.

### Como queda resuelto

**En esta libreria (inmunidad por construccion):**

1. Nunca se carga el env al entorno del proceso: se usa `dotenv_values(path)`, que
   devuelve un `dict`. Verificado por test con un grep literal sobre el paquete.
2. Al leer de `os.environ` se exige el prefijo `PGC_`. Un `SSH_HOST` suelto en el
   sistema nunca es consumido.
3. Excepciones deliberadas, ambas sin riesgo de colision porque el nombre ya esta
   acotado: `CREDENTIALS_ENV` / `SSH_CREDENTIALS_ENV` (que apuntan a variables de
   sistema, es el contrato de `secret_loader` compartido) y
   `POSTGRES_LOCAL_CLIENT_ENV_FILE`.

**En las hermanas (arreglado en este mismo trabajo).** Se migraron las cuatro de
`load_dotenv()` a `dotenv_values()`:

| Libreria | Archivo | Tests despues del cambio |
|---|---|---|
| `redshift_extractor` | `src/redshift_extractor/config.py` | 13 pasan |
| `mongo_extractor` | `src/mongo_extractor/config.py` | 39 pasan |
| `netsuite_extractor` | `src/netsuite_extractor/config.py` | 9 pasan |
| `postgres_local_extractor` | `src/postgres_local_extractor/config.py` | 9 pasan |

El cambio es acotado: cada `load_config()` lee su archivo a un `dict` y consulta ese
`dict` en vez de `os.environ`. Los secretos siguen resolviendose desde variables de
sistema reales via `secret_loader`, que no cambio. Se verifico ademas que cada una sigue
cargando su configuracion **real** y que `os.environ` queda intacto:

```
redshift_extractor        -> aliases ['<alias-prod>', 'dev'], os.environ intacto
postgres_local_extractor  -> localhost:9558/<base-local>,    os.environ intacto
```

Efecto secundario positivo: las tres que no lo tenian ganaron tolerancia a BOM
(`encoding="utf-8-sig"`), que era otra fuente de "la primera variable se lee vacia".

Cambio de comportamiento a tener en cuenta: antes, una variable ya presente en
`os.environ` ganaba sobre el archivo (`override=False`). Ahora el archivo propio es la
unica fuente de configuracion. Ninguno de sus tests dependia de lo primero, y no esta
documentado en sus READMEs como una capacidad.

Hay un test de regresion para todo el ecosistema en
`tests/test_coexistencia.py::test_ninguna_libreria_del_ecosistema_usa_load_dotenv`, que
revisa el codigo de las hermanas en disco y se salta con mensaje explicito si no estan.

---

## 5. Sin mutacion de estado global

- No se llama a la configuracion global de logging ni se toca el root logger. Solo se
  configura `logging.getLogger("postgres_local_client")` con `NullHandler`.
- No se modifica `warnings.filterwarnings`.
- **`sshtunnel` si muta estado global, y se revierte.** `sshtunnel.create_logger()` hace
  tres cosas que afectan al proceso entero, incluso si se le pasa un logger propio:
  1. `logging.captureWarnings(True)`, que redirige el modulo `warnings` a `logging`;
  2. `logging.getLogger('py.warnings').handlers.extend(...)`;
  3. `logging.getLogger('paramiko.transport').handlers = <nuestros handlers>`.

  Toda llamada a `sshtunnel` va envuelta en un context manager que toma snapshot de esas
  tres cosas y las restaura. El logger de `paramiko` no se silencia globalmente: se le
  pasa un logger propio con `NullHandler` y `propagate=False`, asi que su verbosidad no
  llega al root logger del host pero tampoco se le quita a otra libreria que lo use.
  Verificado por test comparando el estado antes y despues de abrir un tunel real.
- No se registran signal handlers que reemplacen los existentes: el de `SIGTERM` se
  encadena al previo, y `SIGINT` no se toca (su comportamiento por default es levantar
  `KeyboardInterrupt`, que es justo lo que se quiere). `atexit` se registra de forma
  perezosa al abrir el primer tunel, y el handler nunca lanza.

Tambien se encontro y se rodeo un **deadlock de `sshtunnel` 0.4.0**: si `start()` falla
(por ejemplo por autenticacion), el forward server local queda en `_server_list` sin que
su hilo `serve_forever` haya arrancado, y `stop()` llama `srv.shutdown()`, que espera
para siempre un evento que solo pone ese hilo. `force=True` no ayuda porque el
`shutdown()` es incondicional (`sshtunnel.py:1463`). En ese camino se cierran los
sockets directamente.

## 6. Throughput: `sshtunnel` reenvia en trozos de 1 KB

Hallazgo de rendimiento, no de convivencia, pero de la misma familia (limitacion de una
dependencia vieja que hay que rodear desde afuera).

`sshtunnel` reenvia el trafico con `recv(1024)` en su handler (`sshtunnel.py:309` y
`:332`). Para el payload de un `COPY` grande eso significa miles de iteraciones, cada una
con su `select()`, y el throughput queda alrededor de **0.7 MB/s** independientemente del
enlace.

Medicion del criterio 17 (100k filas x 3 columnas, ~3 MB de payload de texto), contra la
VM en Ohio:

| Escenario | Tiempo | Throughput |
|---|---|---|
| PostgreSQL local, sin tunel | 0.14 s | ~21 MB/s |
| Por el tunel, sin compresion | 4.1 s | 0.74 MB/s |
| Por el tunel, con compresion | 2.1 s | 1.43 MB/s |

Desglose de las fases, para descartar que el problema fuera del codigo propio:

| Fase | Tiempo |
|---|---|
| `astype(object).where(notna)` de 100k x 3 | 0.009 s |
| lo mismo por lotes de 10k, como en el loader | 0.012 s |
| `itertuples` sobre el frame convertido | 0.029 s |
| `load_config()` (incluye resolver credenciales via KeyringManager) | 0.137 s |
| `ensure_tunnel` abriendo | 0.479 s |
| `ensure_tunnel` reusando (incluye el health check) | 0.236 s |
| **el `COPY` en si** | **~2-5 s, segun el enlace** |

O sea: el codigo propio aporta decimas de segundo y el resto es transferencia.

Por eso `SSH_COMPRESSION` viene en `true` por default. No es gratis —cuesta CPU local— pero
para el caso de uso primario (cargas desde una maquina local a una VM en otra region) la
relacion es clara. Con compresion, el criterio 17 pasa con margen: 3.4 s medidos de punta
a punta, incluyendo apertura de tunel, creacion del esquema desechable y validacion de
columnas, contra un limite de 10 s.

Sin compresion el criterio pasaba a veces y fallaba a veces segun la variabilidad del
enlace, que es peor que fallar siempre. Si algun dia hace falta mas, las opciones son
`COPY TO`/`FROM` binario (menos bytes para datos numericos) o dejar de usar `sshtunnel`
para el forwarding y manejar el canal `direct-tcpip` de paramiko con un buffer grande.

## 7. Convivencia de tuneles

- Puerto local **efimero** (`SSH_LOCAL_PORT=0`) como default: si dos librerias fijaran el
  mismo puerto local, colisionarian.
- **Solo se cierra lo que se abrio.** Un tunel preexistente se marca `owned=False` y
  nunca se cierra.
- No se asume exclusividad sobre el puerto: `ping()` reporta `database`, `user` y
  `tunnel_port` reales, tal como los ve el servidor.

---

## 8. Lo que NO se pudo verificar

**Piso de `pandas>=2.0`.** Se declara por API (`read_sql`, `to_sql`, `itertuples`,
`astype`, `to_parquet` no cambiaron desde 2.0) pero se verifico empiricamente solo sobre
la 3.0.5, que es lo que resuelve pip hoy.

**Alias `dev` de `redshift_extractor`.** En la corrida del proyecto host dio
`Timeout opening channel` contra su cluster. El tunel al bastion abre bien y el otro alias
funciona por el mismo bastion, asi que es alcanzabilidad del target, no convivencia.

### Criterios 31 y 32: VERIFICADOS en un proyecto host real (2026-08-12)

Se verificaron en el venv de un proyecto que usa las librerias de verdad, con **cuatro**
instaladas a la vez: `postgres_local_client` (regular, en site-packages) mas
`postgres_local_extractor`, `redshift_extractor` y `mongo_extractor` (editables).

**Config (criterio 31).** Cargadas en el mismo proceso, en los dos ordenes de import y
cada orden en su propio subproceso, cada libreria conservo su propio bastion:

| | orden `plc, rs, mg` | orden `mg, rs, plc` |
|---|---|---|
| `postgres_local_client` | su bastion | su bastion |
| `redshift_extractor` | su bastion (otro) | su bastion (otro) |
| `mongo_extractor` | su jump host / target SSM | identico |

Con un control negativo adicional: contaminando el entorno del proceso con `SSH_HOST`,
`SSH_PORT`, `SSH_USER`, `SSH_PKEY_PATH`, `LOG_LEVEL` y `OUTPUT_DIR` falsos, las cuatro
siguieron resolviendo los valores de su archivo. Ninguna lee las variables planas del
proceso; esta libreria solo admite override con prefijo `PGC_`.

**Tuneles simultaneos (criterios 31 y 32).** Con el tunel de `redshift_extractor` vivo:

| | puerto local | bastion | destino remoto | vivo |
|---|---|---|---|---|
| `redshift_extractor` | 59336 | el suyo | su cluster:5439 | si (`select 1`) |
| `postgres_local_client` | 59345 | el suyo | `localhost:9553` | si (`select 1`) |

Puertos distintos, bastiones distintos, destinos correctos, los dos vivos a la vez con
consulta real sobre cada uno. Tras `close_all_tunnels()` de esta libreria: su puerto quedo
cerrado y `tunnel_status()` vacio, y el de la hermana **siguio vivo** — `is_alive=True` y un
`select 1` nuevo sobre su puerto devolvio 1. Al salir de su contexto, ambos puertos
cerrados, sin sockets ni threads residuales. Cada libreria cerro unicamente lo suyo.

**Proceso (criterio 26 y 28) en el mismo escenario.** `os.environ`, y `level` y `handlers`
del root logger, identicos antes de importar, despues de importar las cuatro, y despues de
la primera operacion de cada una.

Nota sobre `basicConfig`: las cuatro hermanas lo llaman dentro de su `configure_logging()`,
que mutaria el root logger. En la practica no afecta, porque solo lo invocan sus CLI y no
su API. Esta libreria **no lo llama en ningun caso**: su `configure_logging()` configura
unicamente el logger propio, y hay un test que falla si el token aparece en el paquete.

### La verificacion previa, en venv de laboratorio

Antes del proyecto host se corrio lo mismo en un venv limpio con tres instaladas, con el
mismo resultado en la parte de config:

```
venv limpio con: postgres-local-client 0.1.0 + redshift-extractor 0.1.0 + mongo-extractor 0.1.0
paramiko 3.5.1 | sshtunnel 0.4.0 | python-dotenv 1.0.1 | pandas 3.0.5 | psycopg 3.3.4

test_las_configs_no_se_pisan_con_la_hermana_instalada[hermana-primero]  PASSED
test_las_configs_no_se_pisan_con_la_hermana_instalada[nuestra-primero]  PASSED
14 passed, 2 skipped
```

Lo que verifica cada corrida:

- nuestra config usa el `SSH_HOST` de **nuestro** archivo;
- la de la hermana usa el suyo, y **sigue igual despues** de que cargue la nuestra;
- `os.environ` queda identico al inicio del proceso.

El orden de import va en subprocesos separados a proposito: una vez importado un modulo
queda en `sys.modules`, asi que invertir el orden dentro del mismo interprete no probaria
nada.

Ademas, sin necesitar la instalacion cruzada:

- La resolucion conjunta por pares y de todas juntas (seccion 1).
- El escenario de contaminacion simulado en proceso: se carga un env ajeno al entorno del
  proceso con `SSH_HOST`, `SSH_PORT`, `SSH_USER`, `LOG_LEVEL` y `OUTPUT_DIR` planos —lo
  que hacian las hermanas— y nuestra config sigue usando la suya.

**Piso de `pandas>=2.0`.** Se declara por API (`read_sql`, `to_sql`, `itertuples`,
`astype`, `to_parquet` no cambiaron desde 2.0) pero se verifico empiricamente solo sobre
la 3.0.5, que es lo que resuelve pip hoy.

Para repetir la auditoria:

```powershell
$env:PGC_RUN_PIP_AUDIT = "1"
pytest tests/test_coexistencia.py -k resolucion_conjunta
```
