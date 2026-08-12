"""
postgres_local_client: libreria interna para explotar un PostgreSQL alojado en
una VM desde maquinas locales, a traves de un tunel SSH.

Extrae, carga y modifica. El tunel es transparente: la primera operacion que
necesite la base lo abre y se reusa por el resto del proceso.

    from postgres_local_client import extract_sql

    df = extract_sql("select 1 as test;")

Principios que importan al usarla:
  - `db` es keyword-only y por default toma `DEFAULT_DB` del env propio.
  - Solo lee `.env.postgres_local_client`; nunca el `.env` del proyecto host, y
    nunca escribe en `os.environ`.
  - Escrituras seguras por default: un alias sin `READ_ONLY` explicito es de solo
    lectura, y toda operacion destructiva pide opt-in.
"""

from postgres_local_client.errors import (
    ConfigError,
    DDLNotAllowedError,
    FullTableOperationError,
    GuardError,
    PostgresLocalClientError,
    ReadOnlyError,
    SchemaMismatchError,
    SqlParseError,
    TunnelAuthError,
    TunnelBindError,
    TunnelError,
    TunnelHostKeyError,
    TunnelNetworkError,
    UpsertTargetError,
)
from postgres_local_client.extractor import extract_sql, list_databases
from postgres_local_client.loader import load_dataframe, upsert_dataframe
from postgres_local_client.mutator import delete_where, execute_sql
from postgres_local_client.schema import (
    describe_database,
    describe_table,
    list_schemas,
    list_tables,
    ping,
    table_exists,
)
from postgres_local_client.tunnel import (
    close_all_tunnels,
    close_tunnel,
    open_tunnel,
    tunnel,
    tunnel_status,
)
from postgres_local_client.tx import transaction
from postgres_local_client.types import (
    AppConfig,
    PostgresConfig,
    SSHConfig,
    TunnelInfo,
    UpsertResult,
)

__all__ = [
    # lectura
    "extract_sql",
    "list_databases",
    # carga
    "load_dataframe",
    "upsert_dataframe",
    # modificacion
    "delete_where",
    "execute_sql",
    "transaction",
    # descubrimiento y esquema
    "describe_database",
    "describe_table",
    "list_schemas",
    "list_tables",
    "ping",
    "table_exists",
    # tunel
    "close_all_tunnels",
    "close_tunnel",
    "open_tunnel",
    "tunnel",
    "tunnel_status",
    # contratos
    "AppConfig",
    "PostgresConfig",
    "SSHConfig",
    "TunnelInfo",
    "UpsertResult",
    # errores
    "ConfigError",
    "DDLNotAllowedError",
    "FullTableOperationError",
    "GuardError",
    "PostgresLocalClientError",
    "ReadOnlyError",
    "SchemaMismatchError",
    "SqlParseError",
    "TunnelAuthError",
    "TunnelBindError",
    "TunnelError",
    "TunnelHostKeyError",
    "TunnelNetworkError",
    "UpsertTargetError",
]
__version__ = "0.1.0"
