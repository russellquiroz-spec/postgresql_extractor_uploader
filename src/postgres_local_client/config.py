from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from dotenv import dotenv_values

import postgres_local_client.secret_loader as _secret_loader
from postgres_local_client.errors import ConfigError
from postgres_local_client.events import OnEvent, emit, register_secret
from postgres_local_client.types import AppConfig, PostgresConfig, SSHConfig

ENV_FILE_NAME = ".env.postgres_local_client"

# Excepcion deliberada a la regla del prefijo PGC_: el nombre ya esta acotado por
# el nombre de la libreria, asi que no puede colisionar con el de una hermana
# (cada una tiene su propio <NOMBRE>_ENV_FILE).
ENV_FILE_OVERRIDE_VAR = "POSTGRES_LOCAL_CLIENT_ENV_FILE"

#: Prefijo obligatorio para leer configuracion desde el entorno del proceso.
#: Un SSH_HOST suelto en el sistema —puesto por otra libreria o por el usuario—
#: nunca debe ser consumido por esta.
ENV_PREFIX = "PGC_"

_SEARCH_DEPTH = 8
_BOM = b"\xef\xbb\xbf"

_ALIAS_KEY_RE = re.compile(r"^POSTGRES__(?P<alias>[A-Za-z0-9_-]+)__(?P<field>[A-Z0-9_]+)$")
_REQUIRED_ALIAS_FIELDS = ("HOST", "PORT", "DBNAME")
_SSH_OVERRIDE_FIELDS = ("SSH_HOST", "SSH_PORT", "SSH_USER", "SSH_PKEY_PATH")
_ALIAS_FIELDS = frozenset(
    {
        "HOST",
        "PORT",
        "DBNAME",
        "USER",
        "PASSWORD",
        "CREDENTIALS_ENV",
        "READ_ONLY",
        "ALLOW_DDL",
        "SCHEMA",
        "STATEMENT_TIMEOUT_S",
        *_SSH_OVERRIDE_FIELDS,
    }
)

_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}

# El schema viaja en las `options` de la conexion (-c search_path=...), asi que se
# exige que sea un identificador simple. Un valor con espacios o comillas ahi seria
# inyeccion en la cadena de opciones.
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


# -----------------------------------------------------------------------------
# Localizacion y lectura del env propio
# -----------------------------------------------------------------------------
def find_env_file() -> Path:
    """
    Localiza `.env.postgres_local_client` sin depender del cwd del notebook.

    Orden:
      1) POSTGRES_LOCAL_CLIENT_ENV_FILE (ruta absoluta)
      2) busqueda hacia arriba desde el paquete instalado
    """
    override = os.environ.get(ENV_FILE_OVERRIDE_VAR)
    if override and override.strip():
        path = Path(override.strip()).expanduser().resolve()
        if not path.exists():
            raise ConfigError(
                f"{ENV_FILE_OVERRIDE_VAR} apunta a un archivo inexistente: {path}"
            )
        return path

    searched: List[Path] = []
    current = Path(__file__).resolve().parent
    for _ in range(_SEARCH_DEPTH):
        candidate = current / ENV_FILE_NAME
        searched.append(candidate)
        if candidate.exists():
            return candidate
        if current.parent == current:
            break
        current = current.parent

    rendered = "\n".join(f"  - {path}" for path in searched)
    raise ConfigError(
        f"No se encontro {ENV_FILE_NAME}. Se intentaron las dos rutas de resolucion:\n"
        f"1) La variable {ENV_FILE_OVERRIDE_VAR} (no esta definida o esta vacia).\n"
        f"2) Busqueda hacia arriba desde el paquete instalado:\n{rendered}\n"
        f"Copia .env.example a {ENV_FILE_NAME} en la raiz del repo, o define "
        f"{ENV_FILE_OVERRIDE_VAR} con una ruta absoluta."
    )


def read_env_file(path: Path) -> Dict[str, str]:
    """
    Lee el archivo con `dotenv_values`, que devuelve un dict y NO toca os.environ.

    Nunca se carga el env al entorno del proceso. Si dos librerias del ecosistema
    lo hicieran, la primera en cargar ganaria (python-dotenv usa override=False por
    defecto) y la segunda se quedaria en silencio con los valores de la otra.
    """
    raw = path.read_bytes()
    if raw.startswith(_BOM):
        raise ConfigError(
            f"El archivo {path} empieza con BOM (bytes EF BB BF).\n"
            "python-dotenv no lo maneja: la PRIMERA variable del archivo se leeria vacia.\n"
            "Guardalo en UTF-8 SIN BOM. PowerShell 5.1 (Set-Content, >, Out-File) agrega BOM; "
            "usa un editor que permita 'UTF-8 sin BOM' o recrealo con Python:\n"
            '  python -c "import io,sys; p=r\'' + str(path) + "'; "
            "t=io.open(p,encoding='utf-8-sig').read(); "
            'io.open(p,\'w\',encoding=\'utf-8\',newline=\'\').write(t)"'
        )

    values = dotenv_values(path, encoding="utf-8")
    return {key: value for key, value in values.items() if value is not None}


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    return text


def _lookup(file_values: Mapping[str, str], key: str) -> Optional[str]:
    """
    Precedencia: variables de proceso con prefijo PGC_ > valores del archivo propio.

    Los argumentos explicitos de cada funcion publica tienen prioridad sobre ambos y
    se resuelven en el llamador.
    """
    from_env = os.environ.get(ENV_PREFIX + key)
    if from_env is not None and from_env.strip():
        return _unquote(from_env)
    from_file = file_values.get(key)
    if from_file is not None and from_file.strip():
        return _unquote(from_file)
    return None


def _as_bool(value: Optional[str], *, default: bool, what: str) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise ConfigError(
        f"{what} no es un booleano valido: '{value}'. Usa true/false (o 1/0, yes/no, on/off)."
    )


def _as_int(value: Optional[str], *, default: Optional[int], what: str) -> Optional[int]:
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{what} no es un entero valido: '{value}'.") from exc


def _as_float(value: Optional[str], *, default: float, what: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{what} no es un numero valido: '{value}'.") from exc


#: Un fingerprint SHA256 con su prefijo, tal como lo imprime OpenSSH.
_SHA256_RE = re.compile(r"SHA256:([A-Za-z0-9+/]{43}=*)")
_MENCION_SHA256_RE = re.compile(r"SHA256:", re.IGNORECASE)


def _as_fingerprints(value: Optional[str]) -> Tuple[str, ...]:
    """
    Normaliza los fingerprints de host key a `SHA256:<base64 sin padding>`.

    Un fingerprint no es un secreto —es el hash de una llave publica— asi que puede
    vivir en el archivo de config. Se aceptan tres formas:

      1. La linea completa de `ssh-keygen -l`, tal como sale, con el tamano de la llave
         y el nombre del host alrededor. Tambien varias lineas de una vez:
             256 SHA256:AAA... 1.2.3.4 (ED25519)
             256 SHA256:BBB... 1.2.3.4 (ECDSA)
      2. Solo los fingerprints, separados por coma, punto y coma o espacios.
      3. El base64 pelado, sin el prefijo `SHA256:`.

    El espacio no puede ser a la vez separador entre fingerprints y parte de la linea de
    `ssh-keygen`, asi que se decide por el prefijo: si el valor menciona `SHA256:`, se
    extraen esos tokens y se ignora lo que los rodea; si no, se parte por separadores y
    cada pieza tiene que ser un base64 pelado.
    """
    if not value or not value.strip():
        return ()

    texto = value.strip()

    if "MD5:" in texto.upper():
        raise ConfigError(
            "SSH_HOST_FINGERPRINT trae un fingerprint MD5. MD5 esta obsoleto para esto; "
            "usa el SHA256 que imprime 'ssh-keygen -l -f <known_hosts>' o "
            "'postgres-local-client fingerprint'."
        )

    menciones = len(_MENCION_SHA256_RE.findall(texto))
    if menciones:
        encontrados = _SHA256_RE.findall(texto)
        # Si alguna mencion de SHA256: no produjo un fingerprint valido, no se ignora en
        # silencio: descartar callado un fingerprint que el usuario quiso poner deja la
        # verificacion mas debil de lo que el cree.
        if len(encontrados) != menciones:
            raise ConfigError(
                f"SSH_HOST_FINGERPRINT tiene {menciones} entradas 'SHA256:' pero solo "
                f"{len(encontrados)} son validas. Cada una debe ser 'SHA256:' seguido de "
                f"43 caracteres base64. Valor recibido: '{texto[:120]}'."
            )
        return tuple(
            dict.fromkeys(f"SHA256:{base.rstrip('=')}" for base in encontrados)
        )

    fingerprints = []
    for pieza in re.split(r"[,;\s]+", texto):
        if not pieza:
            continue
        limpia = pieza.strip().strip("'\"").rstrip("=")
        if not re.fullmatch(r"[A-Za-z0-9+/]{43}", limpia):
            raise ConfigError(
                f"SSH_HOST_FINGERPRINT no parece un fingerprint SHA256 valido: '{pieza}'. "
                "Se espera 'SHA256:' seguido de 43 caracteres base64, o el base64 solo. "
                "Tambien se acepta pegar tal cual la linea de 'ssh-keygen -l'."
            )
        fingerprints.append(f"SHA256:{limpia}")
    return tuple(dict.fromkeys(fingerprints))


def _secret_from_env_name(env_name: Optional[str], what: str) -> Optional[str]:
    """
    Resuelve un secreto a partir del NOMBRE de una variable de sistema.

    El env propio guarda el nombre, nunca el valor.
    """
    if not env_name:
        return None
    value = _secret_loader.read_system_env_value(env_name.strip())
    if not value:
        raise ConfigError(
            f"{what} apunta a la variable de sistema '{env_name.strip()}', "
            "que no existe o esta vacia. Abre una terminal nueva si la acabas de crear."
        )
    secret = _secret_loader.normalize_plain_secret(value)
    register_secret(secret)
    return secret


# -----------------------------------------------------------------------------
# Bloques de configuracion
# -----------------------------------------------------------------------------
def _build_app_config(file_values: Mapping[str, str]) -> AppConfig:
    return AppConfig(
        log_level=(_lookup(file_values, "LOG_LEVEL") or "INFO").upper(),
        output_dir=_lookup(file_values, "OUTPUT_DIR") or "./output",
        default_db=(_lookup(file_values, "DEFAULT_DB") or None),
    )


def _build_ssh_config(file_values: Mapping[str, str]) -> SSHConfig:
    host = _lookup(file_values, "SSH_HOST")
    user = _lookup(file_values, "SSH_USER")
    password: Optional[str] = None

    # SSH_CREDENTIALS_ENV resuelve usuario Y password de una sola variable de sistema,
    # con el mismo contrato de parseo que los aliases (secret_loader). Igual que ahi,
    # tiene prioridad sobre SSH_USER/SSH_PASSWORD_ENV.
    credentials_env = _lookup(file_values, "SSH_CREDENTIALS_ENV")
    if credentials_env:
        try:
            user, password = _secret_loader.resolve_secret_reference(credentials_env.strip())
        except (ValueError, RuntimeError) as exc:
            raise ConfigError(f"SSH_CREDENTIALS_ENV: {exc}") from exc
        register_secret(password)
    else:
        password = _secret_from_env_name(
            _lookup(file_values, "SSH_PASSWORD_ENV"), "SSH_PASSWORD_ENV"
        )

    missing = [name for name, value in (("SSH_HOST", host), ("SSH_USER", user)) if not value]
    if missing:
        raise ConfigError(
            f"Faltan variables SSH en {ENV_FILE_NAME}: {missing}. El unico camino a la base es "
            "el tunel SSH; no hay conexion directa. El usuario puede venir de SSH_USER o de "
            "SSH_CREDENTIALS_ENV."
        )

    pkey_path = _lookup(file_values, "SSH_PKEY_PATH")
    passphrase = _secret_from_env_name(
        _lookup(file_values, "SSH_PKEY_PASSPHRASE_ENV"), "SSH_PKEY_PASSPHRASE_ENV"
    )

    if not pkey_path and not password:
        raise ConfigError(
            f"No hay metodo de autenticacion SSH en {ENV_FILE_NAME}. Define SSH_PKEY_PATH "
            "(recomendado), o SSH_CREDENTIALS_ENV / SSH_PASSWORD_ENV con el NOMBRE de la "
            "variable de sistema que tiene la credencial. El password nunca va inline."
        )

    return SSHConfig(
        host=str(host),
        user=str(user),
        port=int(_as_int(_lookup(file_values, "SSH_PORT"), default=22, what="SSH_PORT") or 22),
        pkey_path=pkey_path,
        pkey_passphrase=passphrase,
        password=password,
        local_port=int(
            _as_int(_lookup(file_values, "SSH_LOCAL_PORT"), default=0, what="SSH_LOCAL_PORT") or 0
        ),
        auto_open=_as_bool(
            _lookup(file_values, "SSH_AUTO_OPEN"), default=True, what="SSH_AUTO_OPEN"
        ),
        keepalive_s=_as_float(
            _lookup(file_values, "SSH_KEEPALIVE_S"), default=30.0, what="SSH_KEEPALIVE_S"
        ),
        connect_timeout_s=_as_float(
            _lookup(file_values, "SSH_CONNECT_TIMEOUT_S"),
            default=15.0,
            what="SSH_CONNECT_TIMEOUT_S",
        ),
        known_hosts_path=_lookup(file_values, "SSH_KNOWN_HOSTS_PATH"),
        host_fingerprints=_as_fingerprints(_lookup(file_values, "SSH_HOST_FINGERPRINT")),
        compression=_as_bool(
            _lookup(file_values, "SSH_COMPRESSION"), default=True, what="SSH_COMPRESSION"
        ),
    )


def _alias_buckets(file_values: Mapping[str, str]) -> Dict[str, Dict[str, str]]:
    buckets: Dict[str, Dict[str, str]] = {}

    def absorb(key: str, value: str) -> None:
        match = _ALIAS_KEY_RE.match(key)
        if not match or value is None or not value.strip():
            return
        alias = match.group("alias").lower()
        buckets.setdefault(alias, {})[match.group("field")] = _unquote(value)

    for key, value in file_values.items():
        absorb(key, value)

    # Las variables de proceso con prefijo pisan el archivo. En Windows os.environ
    # normaliza las claves a mayusculas; por eso el alias se pasa a lowercase.
    for key, value in os.environ.items():
        if key.startswith(ENV_PREFIX):
            absorb(key[len(ENV_PREFIX) :], value)

    return buckets


def _resolve_db_credentials(alias: str, fields: Mapping[str, str]) -> Tuple[str, str]:
    credentials_env = fields.get("CREDENTIALS_ENV")
    user: Optional[str] = fields.get("USER")
    password: Optional[str] = fields.get("PASSWORD")

    if credentials_env:
        # CREDENTIALS_ENV apunta a una variable de sistema SIN prefijo por diseno:
        # es el contrato de secret_loader compartido con las librerias hermanas.
        try:
            user, password = _secret_loader.resolve_secret_reference(credentials_env.strip())
        except (ValueError, RuntimeError) as exc:
            raise ConfigError(f"Alias '{alias}': {exc}") from exc

    missing = [name for name, value in (("USER", user), ("PASSWORD", password)) if not value]
    if missing:
        raise ConfigError(
            f"Config PostgreSQL incompleta para alias '{alias}'. Faltan: {missing}. "
            f"Define POSTGRES__{alias}__USER/PASSWORD o POSTGRES__{alias}__CREDENTIALS_ENV."
        )

    register_secret(password)
    return str(user), str(password)


def _build_postgres_config(
    alias: str, fields: Mapping[str, str], ssh: SSHConfig
) -> PostgresConfig:
    unknown = sorted(set(fields) - _ALIAS_FIELDS)
    if unknown:
        raise ConfigError(
            f"Alias '{alias}': campos no reconocidos {unknown}. "
            f"Campos validos: {sorted(_ALIAS_FIELDS)}."
        )

    missing = [field for field in _REQUIRED_ALIAS_FIELDS if field not in fields]
    if missing:
        raise ConfigError(
            f"Config PostgreSQL incompleta para alias '{alias}'. Faltan: {missing}. "
            "HOST/PORT son los del destino visto desde el otro extremo del tunel "
            "(dentro de la VM), no el puerto local."
        )

    user, password = _resolve_db_credentials(alias, fields)
    port = _as_int(fields.get("PORT"), default=None, what=f"POSTGRES__{alias}__PORT")

    schema = fields.get("SCHEMA") or "public"
    if not _SCHEMA_RE.match(schema):
        raise ConfigError(
            f"POSTGRES__{alias}__SCHEMA='{schema}' no es un identificador simple. "
            "Usa letras, digitos y '_' empezando por letra o '_'."
        )

    alias_ssh = ssh.with_overrides(
        host=fields.get("SSH_HOST"),
        user=fields.get("SSH_USER"),
        port=_as_int(fields.get("SSH_PORT"), default=None, what=f"POSTGRES__{alias}__SSH_PORT"),
        pkey_path=fields.get("SSH_PKEY_PATH"),
    )

    return PostgresConfig(
        alias=alias,
        host=str(fields["HOST"]),
        port=int(port),  # type: ignore[arg-type]
        dbname=str(fields["DBNAME"]),
        user=user,
        password=password,
        ssh=alias_ssh,
        schema=schema,
        # Defaults seguros: un alias sin READ_ONLY explicito es de solo lectura.
        read_only=_as_bool(
            fields.get("READ_ONLY"), default=True, what=f"POSTGRES__{alias}__READ_ONLY"
        ),
        allow_ddl=_as_bool(
            fields.get("ALLOW_DDL"), default=False, what=f"POSTGRES__{alias}__ALLOW_DDL"
        ),
        statement_timeout_s=_as_int(
            fields.get("STATEMENT_TIMEOUT_S"),
            default=None,
            what=f"POSTGRES__{alias}__STATEMENT_TIMEOUT_S",
        ),
    )


# -----------------------------------------------------------------------------
# API del modulo
# -----------------------------------------------------------------------------
def load_config(
    *, on_event: Optional[OnEvent] = None
) -> Tuple[AppConfig, SSHConfig, Dict[str, PostgresConfig]]:
    """
    Carga configuracion unicamente desde el env propio y las variables `PGC_*`.

    No lee el `.env` del proyecto host y no escribe en os.environ.
    """
    env_path = find_env_file()
    try:
        file_values = read_env_file(env_path)
    except ConfigError as exc:
        emit(on_event, level="ERROR", event="error", message=str(exc), path=str(env_path))
        raise

    app = _build_app_config(file_values)
    ssh = _build_ssh_config(file_values)

    buckets = _alias_buckets(file_values)
    if not buckets:
        raise ConfigError(
            f"No se encontraron variables POSTGRES__<alias>__* en {env_path}. "
            "Define al menos un alias (ver .env.example)."
        )

    pg_map = {
        alias: _build_postgres_config(alias, fields, ssh) for alias, fields in buckets.items()
    }

    emit(
        on_event,
        level="INFO",
        event="config_loaded",
        message="Config cargada.",
        path=str(env_path),
        aliases=sorted(pg_map),
        ssh_host=ssh.host,
        ssh_port=ssh.port,
        default_db=app.default_db,
    )
    return app, ssh, pg_map


def select_alias(
    db: Optional[str], app: AppConfig, pg_map: Mapping[str, PostgresConfig]
) -> PostgresConfig:
    requested = (db or app.default_db or "").strip().lower()
    available = sorted(pg_map)

    if not requested:
        raise ConfigError(
            "No se indico alias y DEFAULT_DB no esta definido en "
            f"{ENV_FILE_NAME}. Pasa db='<alias>' o define DEFAULT_DB. "
            f"Aliases disponibles: {', '.join(available)}."
        )
    if requested not in pg_map:
        raise ConfigError(
            f"El alias '{requested}' no existe. Disponibles: {', '.join(available)}."
        )
    return pg_map[requested]


def resolve(
    db: Optional[str] = None, *, on_event: Optional[OnEvent] = None
) -> Tuple[AppConfig, PostgresConfig]:
    """Atajo: carga config y resuelve el alias pedido (o DEFAULT_DB)."""
    app, _ssh, pg_map = load_config(on_event=on_event)
    return app, select_alias(db, app, pg_map)


__all__ = [
    "ENV_FILE_NAME",
    "ENV_FILE_OVERRIDE_VAR",
    "ENV_PREFIX",
    "find_env_file",
    "load_config",
    "read_env_file",
    "resolve",
    "select_alias",
]
