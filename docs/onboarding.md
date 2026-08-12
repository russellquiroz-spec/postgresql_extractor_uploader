# Onboarding: de cero a `ping` en verde

Guia para que un usuario nuevo del equipo quede operando. Es el paso que bloquea a cada
persona nueva, asi que va en orden y con las trampas senaladas.

Tiempo estimado: 15 minutos (mas lo que tarde registrar la llave en la VM, que depende de
quien administra).

> **Los valores entre `<>` son placeholders.** Este repositorio es publico, asi que la IP
> del bastion, los usuarios y los fingerprints de la VM no estan aqui: pidelos por el
> canal interno del equipo. Lo unico que va en tu `.env.postgres_local_client` son esos
> valores y los NOMBRES de las variables de sistema que guardan las credenciales — nunca
> las credenciales mismas.

---

## Paso 0 — Requisitos

- Python 3.10 o superior (`python --version`).
- Acceso de red al puerto 22 de `<ip-del-bastion>`. Si tu IP publica cambio o estas en otra
  red, el Security Group de AWS puede estar bloqueandote: eso se gestiona fuera de esta
  libreria, pidelo a quien administre la cuenta.

Verifica la conectividad antes de seguir:

```powershell
Test-NetConnection <ip-del-bastion> -Port 22
```

Si `TcpTestSucceeded` es `False`, no sigas: no es un problema de la libreria.

---

## Paso 1 — Instalar

```powershell
cd <ruta-del-repo>
python install.py
.\.venv\Scripts\activate
```

El instalador crea el venv, instala el paquete editable y genera
`.env.postgres_local_client` desde `.env.example` si no existe.

---

## Paso 2 — Credenciales

La libreria **nunca** guarda credenciales en el archivo de configuracion: solo el
**nombre** de la variable de sistema que las contiene.

Necesitas dos variables de sistema (o entradas de KeyringManager):

| Variable | Contiene | Para que |
|---|---|---|
| `VM_SSH_CREDENTIALS` | usuario y password del SSH de la VM | abrir el tunel |
| `VM_DB_CREDENTIALS` | usuario y password de PostgreSQL | conectarse a la base |

Formatos aceptados para cualquiera de las dos:

```text
{"user":"usuario","password":"password"}
USER=usuario;PASSWORD=password
usuario:password
```

Para crearlas de forma persistente en Windows:

```powershell
[Environment]::SetEnvironmentVariable("VM_DB_CREDENTIALS", '{"user":"tu_usuario","password":"tu_password"}', "User")
```

Despues de crearlas, **abre una terminal nueva**: un proceso ya arrancado no ve variables
creadas despues. (La libreria tambien las busca en el registro de Windows, asi que
normalmente funciona sin reiniciar, pero la terminal nueva evita sorpresas.)

Si el equipo usa KeyringManager, basta con que exista una entrada cuyo `env_var` coincida
con esos nombres.

---

## Paso 3 — Registrar la host key de la VM

Este es el paso que mas confunde. La libreria **nunca** acepta una host key desconocida
de forma automatica: si el host no esta registrado, falla con `TunnelHostKeyError`. Es a
proposito — aceptar cualquier llave a ciegas es exactamente como se monta un
man-in-the-middle.

Obtene las llaves del servidor:

```powershell
ssh-keyscan <ip-del-bastion> >> $env:USERPROFILE\.ssh\known_hosts
```

Y **verifica el fingerprint con quien administra la VM antes de confiar en el**:

```powershell
ssh-keygen -l -F <ip-del-bastion>
```

Los fingerprints al 2026-08-12 son:

```
SHA256:<fingerprint-ed25519>  (ED25519)  <- la que usa la libreria
SHA256:<fingerprint-ecdsa>  (ECDSA)
SHA256:<fingerprint-rsa>  (RSA)
```

Si lo que ves no coincide con eso, **para y pregunta**. Puede ser que la VM se haya
recreado (legitimo) o que alguien este interceptando la conexion (no legitimo).

Alternativa: un `known_hosts` por proyecto, que no toca el trust store de tu maquina.

```env
SSH_KNOWN_HOSTS_PATH=C:\ruta\al\repo\.ssh_known_hosts
```

---

## Paso 4 — Llenar el `.env.postgres_local_client`

Abrilo y confirma que diga esto (los valores de ejemplo ya vienen correctos):

```env
SSH_HOST=<ip-del-bastion>
SSH_PORT=22
SSH_CREDENTIALS_ENV=VM_SSH_CREDENTIALS
SSH_LOCAL_PORT=0
SSH_AUTO_OPEN=true
DEFAULT_DB=local

POSTGRES__local__HOST=localhost
POSTGRES__local__PORT=9553
POSTGRES__local__DBNAME=<nombre-base>
POSTGRES__local__CREDENTIALS_ENV=VM_DB_CREDENTIALS
POSTGRES__local__READ_ONLY=true

POSTGRES__local_rw__HOST=localhost
POSTGRES__local_rw__PORT=9553
POSTGRES__local_rw__DBNAME=<nombre-base>
POSTGRES__local_rw__CREDENTIALS_ENV=VM_DB_CREDENTIALS
POSTGRES__local_rw__READ_ONLY=false
```

Dos trampas:

1. **`HOST` y `PORT` son los del destino visto desde DENTRO de la VM** (`localhost:9553`),
   no el puerto local del tunel. Es el error mas comun.
2. **Guardalo en UTF-8 sin BOM.** Si lo editas con PowerShell (`Set-Content`, `>`,
   `Out-File`) le agrega BOM y la primera variable se leeria vacia. La libreria detecta
   el BOM y te lo dice, pero mejor evitarlo: usa VS Code o Notepad++ con "UTF-8 sin BOM".

`SSH_LOCAL_PORT=0` es el recomendado: usa un puerto libre automatico. Si fijas `9553` y
tu maquina ya tiene un PostgreSQL propio ahi, la conexion **funcionaria** pero apuntaria a
la base equivocada — el peor error posible.

---

## Paso 5 — Verificar

```powershell
postgres-local-client ls
```

Debe imprimir:

```
local
local_rw
```

Y ahora la prueba de verdad:

```powershell
postgres-local-client ping --db local
```

Salida esperada:

```
ok: True
db: local
server_version: PostgreSQL 17.4 on x86_64-windows, ...
database: <nombre-base>
user: <usuario-bd>
schema: public
remote_port: 9553
tunnel_port: 64900          <- puerto efimero, cambia en cada corrida
tunnel_owned: True
latency_ms: 1397.56
```

Fijate en `database` y `user`: los reporta **el servidor**, no la config. Si dicen algo
distinto de lo que esperabas, te conectaste a otra base (tipicamente por un puerto local
colisionado).

Ya esta. Desde Python:

```python
from postgres_local_client import extract_sql

df = extract_sql("select 1 as test;")
```

---

## Si algo falla

| Error | Que significa | Que hacer |
|---|---|---|
| `TunnelHostKeyError: ... no esta en known_hosts` | falta el paso 3 | corre `ssh-keyscan` y verifica el fingerprint |
| `TunnelHostKeyError: ... NO coincide` | la host key cambio | **pregunta antes de reemplazarla**; el mensaje trae los dos fingerprints |
| `TunnelNetworkError: Timeout` | no hay ruta al puerto 22 | Security Group de AWS o tu IP publica cambio |
| `TunnelNetworkError: Conexion rechazada` | el host responde pero no hay sshd | el servicio `sshd` de la VM esta caido |
| `TunnelAuthError: Autenticacion SSH rechazada` | usuario o password/llave malos | revisa `VM_SSH_CREDENTIALS`; si usas llave, mira la nota de abajo |
| `TunnelBindError` | el puerto local que fijaste esta ocupado | usa `SSH_LOCAL_PORT=0` |
| `ConfigError: ... BOM` | guardaste el `.env` con BOM | el mensaje trae el comando exacto para arreglarlo |
| `ConfigError: La variable de sistema '...' no existe` | falta el paso 2, o la terminal es vieja | crea la variable y abre una terminal nueva |
| `ReadOnlyError` | intentaste escribir con el alias `local` | usa `db="local_rw"` |
| `AttributeError: module 'paramiko' has no attribute 'DSSKey'` | tienes `paramiko>=4`, incompatible con `sshtunnel` | reinstala respetando el `pyproject.toml`; ver `docs/compatibilidad.md` |

---

## Cuando el equipo pase a llaves SSH

Hoy la VM solo tiene autenticacion por password, y por eso el paso 2 usa
`SSH_CREDENTIALS_ENV`. Lo recomendado es llave por usuario. Cuando se haga:

### 1. Generar la llave (cada usuario la suya, nunca compartirla)

```powershell
ssh-keygen -t ed25519 -C "tu.correo@empresa.com"
```

Ponle passphrase. Guardala en una variable de sistema y referenciala:

```env
SSH_USER=<usuario-ssh>
SSH_PKEY_PATH=C:\Users\<usuario>\.ssh\id_ed25519
SSH_PKEY_PASSPHRASE_ENV=RABBIT_VM_SSH_PASSPHRASE
```

(y comenta `SSH_CREDENTIALS_ENV`, que tiene prioridad sobre estas).

La llave **privada** nunca se comparte ni se commitea. Solo se entrega el `.pub`.

### 2. Registrarla en la VM — la trampa de Windows Server

Para el la cuenta de administrador, OpenSSH en Windows **no** lee
`~\.ssh\authorized_keys`. Lee:

```
C:\ProgramData\ssh\administrators_authorized_keys
```

Y ademas ese archivo debe tener la ACL restringida a `Administrators` y `SYSTEM`. Si
tiene permisos mas amplios, `sshd` lo ignora **en silencio**: la llave es correcta, el
archivo esta bien, y no entra. Es la causa numero uno de este problema.

En la VM, como administrador:

```powershell
# Agregar la llave publica del usuario
Add-Content C:\ProgramData\ssh\administrators_authorized_keys "ssh-ed25519 AAAA... tu.correo@empresa.com"

# Corregir la ACL (obligatorio)
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r
icacls C:\ProgramData\ssh\administrators_authorized_keys /grant "Administrators:F"
icacls C:\ProgramData\ssh\administrators_authorized_keys /grant "SYSTEM:F"

# Reiniciar el servicio
Restart-Service sshd
```

Verifica con `postgres-local-client ping --db local`. Si falla, en la VM:

```powershell
Get-WinEvent -LogName "OpenSSH/Operational" -MaxEvents 20
```

Ahi aparece si `sshd` rechazo el archivo por permisos.

### 3. Idealmente, un usuario SSH por persona

Que todo el equipo comparta una sola cuenta significa que no hay trazabilidad de quien hizo que.
Fuera del alcance de esta libreria, pero vale la pena plantearlo.
