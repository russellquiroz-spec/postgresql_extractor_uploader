# Pendientes de revisar

Lo que quedo fuera del alcance de la primera entrega, con el porque. No es una lista de
deseos: cada punto tiene una razon concreta por la que no se hizo y una senal de cuando
convendria hacerlo.

Ultima revision: 2026-08-12.

---

## 1. Verificacion cruzada con las hermanas (criterios 31 y 32)

**Estado: CERRADO el 2026-08-12.** Verificados en el venv de un proyecto host real, con
cuatro librerias del ecosistema instaladas a la vez. Config aislada en los dos ordenes de
import, dos tuneles simultaneos a bastiones distintos, y cada libreria cerrando unicamente
el suyo. Evidencia en `docs/compatibilidad.md`.

Los tests siguen en la suite y se saltan con mensaje cuando no hay hermanas instaladas, que
es el caso del venv de desarrollo de esta libreria. Para repetirlo, el procedimiento de
abajo.

| Test | Que cubre | Como se activa |
|---|---|---|
| `test_las_configs_no_se_pisan_con_la_hermana_instalada` | criterio 31 sin red, en los dos ordenes de import | instalar la hermana en el venv |
| `test_tuneles_simultaneos_con_la_hermana` | criterios 31 y 32, dos tuneles vivos | `PGC_RUN_CROSS_TUNNEL=1` |

### Procedimiento

Un venv aparte, para no contaminar el de desarrollo de ninguna de las dos:

```powershell
$F = "C:\Users\<usuario>\OneDrive - <org>\Documents\Funciones"

# Ruta corta a proposito: si el venv queda muy profundo, cryptography no puede cargar
# su DLL en Windows y el import de paramiko truena.
python -m venv C:\Temp\convivencia
C:\Temp\convivencia\Scripts\python.exe -m pip install -q --upgrade pip

C:\Temp\convivencia\Scripts\python.exe -m pip install "$F\postgresql_extractor_uploader[dev]"
C:\Temp\convivencia\Scripts\python.exe -m pip install "$F\redshift_extractor"
C:\Temp\convivencia\Scripts\python.exe -m pip install "$F\mongo_extractor"

# Confirmar que las tres quedaron y con que versiones se resolvio lo compartido
C:\Temp\convivencia\Scripts\python.exe -m pip list | Select-String "postgres-local-client|redshift|mongo|paramiko|sshtunnel|psycopg|pandas|dotenv"
```

Los tests necesitan encontrar el env de cada libreria. Instaladas en site-packages, la
busqueda hacia arriba no los encuentra, asi que hay que apuntarlos:

```powershell
$env:POSTGRES_LOCAL_CLIENT_ENV_FILE = "$F\postgresql_extractor_uploader\.env.postgres_local_client"
$env:REDSHIFT_EXTRACTOR_ENV_FILE    = "$F\redshift_extractor\.env.redshift_extractor"
```

Primero la parte sin red:

```powershell
cd "$F\postgresql_extractor_uploader"
C:\Temp\convivencia\Scripts\python.exe -m pytest tests/test_coexistencia.py -v -m coexistence
```

Y despues, si quieres el ejercicio completo de tuneles simultaneos (abre un tunel al
bastion de Redshift con credenciales de produccion; la consulta es un `select 1`):

```powershell
$env:PGC_RUN_CROSS_TUNNEL = "1"
C:\Temp\convivencia\Scripts\python.exe -m pytest tests/test_coexistencia.py -v -k tuneles_simultaneos
Remove-Item Env:PGC_RUN_CROSS_TUNNEL
```

Criterio cumplido si los dos pasan. Si alguno falla, el resultado va a
`docs/compatibilidad.md` y el fix esperado es aflojar un rango en **esta** libreria, no en
las existentes.

---

## 2. Rendimiento de lectura y escritura

**Estado:** funciona, con techos conocidos y medidos.

El limite lo pone `sshtunnel`, que reenvia en trozos de 1 KB (ver `docs/compatibilidad.md`,
seccion 6). Con `SSH_COMPRESSION=true` hay margen de sobra para los volumenes actuales,
pero si algun dia estorba, en orden de costo/beneficio:

- **`COPY TO` para exportaciones grandes**, sin pasar por pandas. Hoy una lectura grande
  materializa el DataFrame completo en RAM.
- **Streaming real en lectura.** `chunksize` hoy lotea el *fetch* pero concatena todo antes
  de devolver, asi que no baja el pico de memoria. Un `extract_sql_iter()` que devuelva un
  generador seria el cambio, y es aditivo: no rompe la firma actual.
- **`COPY ... FROM STDIN (FORMAT BINARY)`**, que manda menos bytes para datos numericos.
- **Dejar de usar `sshtunnel` para el forwarding** y manejar el canal `direct-tcpip` de
  paramiko con un buffer grande. Es el que mas ganaria y el que mas riesgo trae: habria
  que reimplementar el forwarder local.

Senal para hacerlo: que una carga rutinaria pase de ~1 minuto, o que aparezca un
`MemoryError` en una lectura.

---

## 3. Checks de calidad de datos y metricas post-carga

**Estado:** no empezado.

La libreria reporta cuantas filas escribio y cuantas actualizo, pero no valida nada del
contenido. Lo que tendria sentido, sin convertirla en un framework de calidad:

- Conteo de nulos por columna despues de una carga, comparado contra un umbral.
- Verificar que la PK no traiga duplicados **antes** de mandar el `COPY`, que hoy falla del
  lado del servidor con un error menos claro.
- Registrar en una tabla de auditoria que proceso cargo que, cuando y cuantas filas.

Senal: la primera vez que una carga meta datos malos sin que nadie se entere hasta dias
despues.

---

## 4. Pool de tuneles entre procesos

**Estado:** descartado por ahora.

Abrir el tunel cuesta ~0.5 s una vez por proceso, y se reusa el resto de la vida del
proceso. Un daemon local que compartiera tuneles entre procesos ahorraria eso, a cambio de
un componente con estado, su propio ciclo de vida y sus propios modos de falla.

Senal: que alguien corra muchos procesos cortos en serie y los 0.5 s se noten de verdad.

---

## 5. Extraer `secret_loader` a un paquete compartido

**Estado:** decidido no hacerlo, revisable.

Hoy son cuatro copias del mismo modulo. La decision (seccion 6.4 de la solicitud) fue
copiarlo: un paquete comun agrega una dependencia interna a cada proyecto host, obliga a
versionarlo y publicarlo, y crea justo el acoplamiento que la separacion en repos evita.

Senal para reconsiderar: que el modulo empiece a cambiar seguido. Mientras siga estable,
mantener cuatro copias identicas cuesta menos que coordinar releases. Hay un test que
compara la copia contra la de `redshift_extractor` y falla si divergen mas de la linea del
nombre, asi que la divergencia silenciosa esta cubierta.

---

## 6. Un usuario SSH por persona

**Estado:** fuera del alcance de la libreria, resuelto por infra.

Si todo el equipo entra con la misma cuenta, no hay trazabilidad de quien hizo que. La
libreria ya soporta lo necesario (`SSH_USER` por alias, llave o password); es una decision
de administracion de la VM.
