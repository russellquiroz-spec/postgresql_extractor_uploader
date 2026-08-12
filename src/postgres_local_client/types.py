from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional, TypedDict


@dataclass(frozen=True)
class SSHConfig:
    """
    Parametros del tunel SSH. `password` y `pkey_passphrase` llevan repr=False para
    que un repr accidental de la config no los exponga.
    """

    host: str
    user: str
    port: int = 22
    pkey_path: Optional[str] = None
    pkey_passphrase: Optional[str] = field(default=None, repr=False)
    password: Optional[str] = field(default=None, repr=False)
    local_port: int = 0
    auto_open: bool = True
    keepalive_s: float = 30.0
    connect_timeout_s: float = 15.0
    known_hosts_path: Optional[str] = None
    #: Compresion del transporte SSH. Activada por default porque sshtunnel reenvia
    #: en trozos de 1 KB y eso limita el throughput: medido sobre un COPY de 100k
    #: filas contra la VM, baja de ~4.1 s a ~2.1 s. Ver docs/compatibilidad.md.
    compression: bool = True

    def with_overrides(self, **overrides: Any) -> "SSHConfig":
        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean) if clean else self


@dataclass(frozen=True)
class PostgresConfig:
    """Config de un alias. `password` lleva repr=False (ver SSHConfig)."""

    alias: str
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    ssh: Optional[SSHConfig] = None
    schema: str = "public"
    read_only: bool = True
    allow_ddl: bool = False
    statement_timeout_s: Optional[int] = None

    @property
    def target(self) -> str:
        """Referencia segura para logs y errores: nunca la URL completa."""
        return f"{self.host}:{self.port}/{self.dbname}"


@dataclass(frozen=True)
class AppConfig:
    log_level: str = "INFO"
    output_dir: str = "./output"
    default_db: Optional[str] = None


@dataclass
class TunnelInfo:
    """
    Estado de un tunel. `owned=True` significa que lo abrio esta libreria y por
    lo tanto es la unica que puede cerrarlo.
    """

    local_port: int
    remote_host: str
    remote_port: int
    ssh_host: str
    ssh_user: str
    opened_at: datetime
    owned: bool
    ssh_port: int = 22
    forwarder: Any = field(default=None, repr=False, compare=False)

    @property
    def is_alive(self) -> bool:
        """
        Verificacion real: handshake TCP contra el puerto local mas respuesta del
        servidor PostgreSQL del otro lado. Que el proceso SSH exista no basta.
        """
        from postgres_local_client.tunnel import probe_postgres

        return probe_postgres(self.local_port)

    def as_dict(self) -> dict[str, Any]:
        return {
            "local_port": self.local_port,
            "remote_host": self.remote_host,
            "remote_port": self.remote_port,
            "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "ssh_user": self.ssh_user,
            "opened_at": self.opened_at.isoformat(timespec="seconds"),
            "owned": self.owned,
            "is_alive": self.is_alive,
        }


class LoadResult(TypedDict):
    table: str
    rows: int
    chunks: int


class UpsertResult(TypedDict):
    inserted: int
    updated: int


__all__ = [
    "AppConfig",
    "LoadResult",
    "PostgresConfig",
    "SSHConfig",
    "TunnelInfo",
    "UpsertResult",
]
