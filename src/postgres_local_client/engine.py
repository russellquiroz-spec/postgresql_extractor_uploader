from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

import sqlalchemy as sa
from sqlalchemy.engine import Engine, URL

from postgres_local_client.events import OnEvent, emit
from postgres_local_client.tunnel import ensure_tunnel, invalidate_tunnel, probe_postgres
from postgres_local_client.types import PostgresConfig

T = TypeVar("T")

#: Timeout de conexion al puerto local del tunel. No es el timeout del tunel.
CONNECT_TIMEOUT_S = 15

#: (alias, user, local_port). El puerto entra en la llave para que un tunel nuevo
#: en otro puerto no reuse un engine que apunta al viejo.
EngineKey = Tuple[str, str, int]

_engines: Dict[EngineKey, Engine] = {}
_lock = threading.RLock()


def build_url(cfg: PostgresConfig, local_port: int) -> URL:
    """
    Arma la URL con `URL.create`, nunca por concatenacion.

    Es lo que hace que un password con caracteres especiales como `( ) + | $` se
    escape correctamente, y que un nombre de base con guion no necesite tratamiento
    aparte.
    """
    return URL.create(
        "postgresql+psycopg",
        username=cfg.user,
        password=cfg.password,
        host="127.0.0.1",
        port=local_port,
        database=cfg.dbname,
    )


def server_options(cfg: PostgresConfig) -> str:
    """
    Opciones que se mandan al servidor al conectar.

    Van por `options` de la conexion y no por `SET` en el evento "connect" a
    proposito: el pool hace rollback al devolver la conexion, y un `SET` es
    transaccional en PostgreSQL, asi que se perderia. Por `options` quedan como
    default de la sesion.
    """
    options: List[str] = []
    if cfg.statement_timeout_s:
        options.append(f"-c statement_timeout={int(cfg.statement_timeout_s) * 1000}")
    if cfg.read_only:
        # Defensa en profundidad: las guardas de guards.py son la primera linea,
        # esto lo hace cumplir el servidor incluso si algo se nos escapa.
        options.append("-c default_transaction_read_only=on")
    search_path = cfg.schema if cfg.schema == "public" else f"{cfg.schema},public"
    options.append(f"-c search_path={search_path}")
    return " ".join(options)


def get_engine(cfg: PostgresConfig, local_port: int) -> Engine:
    key: EngineKey = (cfg.alias, cfg.user, local_port)
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = sa.create_engine(
                build_url(cfg, local_port),
                pool_pre_ping=True,
                connect_args={
                    "connect_timeout": CONNECT_TIMEOUT_S,
                    "options": server_options(cfg),
                },
            )
            _engines[key] = engine
        return engine


def dispose_for_port(local_port: int) -> None:
    """Dispone los engines asociados a un puerto local. Se llama antes de cerrar el tunel."""
    with _lock:
        keys = [key for key in _engines if key[2] == local_port]
        engines = [_engines.pop(key) for key in keys]
    for engine in engines:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001 - el dispose no debe tumbar el cierre
            pass


def dispose_all() -> None:
    with _lock:
        engines = list(_engines.values())
        _engines.clear()
    for engine in engines:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001
            pass


def _is_lost_connection(exc: BaseException, local_port: int) -> bool:
    """
    Decide si el fallo fue del tunel y no de la sentencia.

    La señal fuerte es que el puerto local ya no responda como PostgreSQL: eso se
    verifica de verdad en vez de comparar substrings del mensaje de error.
    """
    if getattr(exc, "connection_invalidated", False):
        return True
    return not probe_postgres(local_port)


def run(
    cfg: PostgresConfig,
    fn: Callable[[sa.Connection], T],
    *,
    write: bool = False,
    retry: bool = True,
    on_event: Optional[OnEvent] = None,
) -> T:
    """
    Ejecuta `fn(conn)` sobre una conexion del alias, garantizando el tunel.

    Con `write=True` la operacion completa va en una transaccion: commit al salir
    bien, rollback ante cualquier excepcion.

    Si el tunel muere a media sesion se reabre una vez y se reintenta la operacion,
    emitiendo `tunnel_retry`. El reintento es seguro incluso para escrituras porque
    la caida del tunel aborta la transaccion del lado del servidor, asi que no queda
    nada aplicado a medias. Nunca reintenta mas de una vez.
    """
    attempts = 2 if retry else 1
    for attempt in range(1, attempts + 1):
        info = ensure_tunnel(cfg, on_event=on_event)
        engine = get_engine(cfg, info.local_port)
        try:
            emit(
                on_event,
                level="DEBUG",
                event="connect",
                message=f"Conectando a {cfg.target} via localhost:{info.local_port}.",
                db=cfg.alias,
                local_port=info.local_port,
            )
            context = engine.begin() if write else engine.connect()
            with context as conn:
                return fn(conn)
        except sa.exc.DBAPIError as exc:
            if attempt >= attempts or not _is_lost_connection(exc, info.local_port):
                raise
            emit(
                on_event,
                level="WARNING",
                event="tunnel_retry",
                message=(
                    "La conexion se perdio y el tunel ya no responde. Se reabre y se "
                    "reintenta la operacion una vez."
                ),
                db=cfg.alias,
                local_port=info.local_port,
                ssh_host=info.ssh_host,
                owned=info.owned,
            )
            invalidate_tunnel(info, on_event=on_event)

    raise AssertionError("run() agoto los intentos sin devolver ni lanzar")  # pragma: no cover


def quote(conn: sa.Connection, name: str) -> str:
    """Cita un identificador con las reglas del dialecto (maneja guiones y reservadas)."""
    return conn.dialect.identifier_preparer.quote(name)


def qualified(conn: sa.Connection, schema: str, table: str) -> str:
    return f"{quote(conn, schema)}.{quote(conn, table)}"


__all__ = [
    "CONNECT_TIMEOUT_S",
    "build_url",
    "dispose_all",
    "dispose_for_port",
    "get_engine",
    "qualified",
    "quote",
    "run",
    "server_options",
]
