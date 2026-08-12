from __future__ import annotations


class PostgresLocalClientError(Exception):
    """Base de todos los errores de la libreria."""


class ConfigError(PostgresLocalClientError):
    """Configuracion ausente, incompleta o invalida. CLI: exit code 2."""


class TunnelError(PostgresLocalClientError):
    """Base de los errores de tunel. CLI: exit code 3."""


class TunnelNetworkError(TunnelError):
    """No hay ruta al puerto SSH: Security Group, IP local cambiada o VM apagada."""


class TunnelAuthError(TunnelError):
    """Llave o password SSH invalidos, o llave inaccesible."""


class TunnelHostKeyError(TunnelError):
    """La host key del servidor SSH no esta en known_hosts o no coincide."""


class TunnelBindError(TunnelError):
    """El puerto local pedido esta ocupado y no es un tunel reusable."""


class GuardError(PostgresLocalClientError):
    """Base de las guardas de escritura."""


class ReadOnlyError(GuardError):
    """El alias esta marcado READ_ONLY=true y la sentencia no es de lectura."""


class DDLNotAllowedError(GuardError):
    """La sentencia es DDL y el alias no tiene ALLOW_DDL=true."""


class FullTableOperationError(GuardError):
    """UPDATE/DELETE sin WHERE sin allow_full_table=True."""


class SqlParseError(GuardError):
    """El SQL no se pudo parsear, asi que no se puede validar."""


class SchemaMismatchError(PostgresLocalClientError):
    """Las columnas del DataFrame no coinciden con la tabla destino."""


class UpsertTargetError(PostgresLocalClientError):
    """No existe PK ni indice unico sobre las columnas de conflicto."""


__all__ = [
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
