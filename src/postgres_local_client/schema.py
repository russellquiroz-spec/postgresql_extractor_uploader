from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import sqlalchemy as sa

from postgres_local_client import config as _config
from postgres_local_client import engine as _engine
from postgres_local_client.events import OnEvent, emit
from postgres_local_client.tunnel import ensure_tunnel
from postgres_local_client.types import PostgresConfig

_COLUMNS_SQL = sa.text(
    """
    select column_name,
           data_type,
           is_nullable = 'YES' as is_nullable,
           column_default,
           character_maximum_length,
           numeric_precision,
           numeric_scale,
           ordinal_position
    from information_schema.columns
    where table_schema = :schema and table_name = :table
    order by ordinal_position
    """
)

_TABLE_EXISTS_SQL = sa.text(
    """
    select exists (
        select 1 from information_schema.tables
        where table_schema = :schema and table_name = :table
    )
    """
)

_TABLES_SQL = sa.text(
    """
    select c.relname as nombre,
           case c.relkind
               when 'r' then 'tabla'
               when 'p' then 'tabla particionada'
               when 'v' then 'vista'
               when 'm' then 'vista materializada'
               when 'f' then 'tabla foranea'
               else c.relkind::text
           end as tipo,
           case when c.reltuples < 0 then null else c.reltuples::bigint end as filas_aprox,
           pg_total_relation_size(c.oid) as bytes,
           pg_size_pretty(pg_total_relation_size(c.oid)) as tamano
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = :schema
      and c.relkind in ('r', 'p', 'v', 'm', 'f')
    order by c.relname
    """
)

_UNIQUE_INDEXES_SQL = sa.text(
    """
    select i.indexrelid::regclass::text as indice,
           i.indisprimary as es_pk,
           array_agg(a.attname order by k.ord) as columnas
    from pg_index i
    join pg_class c on c.oid = i.indrelid
    join pg_namespace n on n.oid = c.relnamespace
    join unnest(i.indkey) with ordinality k(attnum, ord) on true
    join pg_attribute a on a.attrelid = c.oid and a.attnum = k.attnum
    where i.indisunique
      and i.indpred is null
      and n.nspname = :schema
      and c.relname = :table
    group by i.indexrelid, i.indisprimary
    """
)

_PK_COLUMNS_SQL = sa.text(
    """
    select a.attname as columna
    from pg_index i
    join pg_class c on c.oid = i.indrelid
    join pg_namespace n on n.oid = c.relnamespace
    join unnest(i.indkey) with ordinality k(attnum, ord) on true
    join pg_attribute a on a.attrelid = c.oid and a.attnum = k.attnum
    where i.indisprimary and n.nspname = :schema and c.relname = :table
    order by k.ord
    """
)


# -----------------------------------------------------------------------------
# Helpers de introspeccion (reutilizados por loader.py)
# -----------------------------------------------------------------------------
def table_exists_on(conn: sa.Connection, schema: str, table: str) -> bool:
    return bool(conn.execute(_TABLE_EXISTS_SQL, {"schema": schema, "table": table}).scalar())


def table_columns_on(conn: sa.Connection, schema: str, table: str) -> List[Dict[str, Any]]:
    rows = conn.execute(_COLUMNS_SQL, {"schema": schema, "table": table}).mappings().all()
    return [dict(row) for row in rows]


def unique_indexes_on(conn: sa.Connection, schema: str, table: str) -> List[Dict[str, Any]]:
    rows = conn.execute(_UNIQUE_INDEXES_SQL, {"schema": schema, "table": table}).mappings().all()
    return [dict(row) for row in rows]


def resolve_schema(cfg: PostgresConfig, schema: Optional[str]) -> str:
    return (schema or cfg.schema or "public").strip()


# -----------------------------------------------------------------------------
# API publica
# -----------------------------------------------------------------------------
def describe_database(db: Optional[str] = None, *, on_event: Optional[OnEvent] = None) -> Dict[str, Any]:
    """Config efectiva del alias, sin credenciales. No abre el tunel."""
    _app, cfg = _config.resolve(db, on_event=on_event)
    ssh = cfg.ssh
    return {
        "alias": cfg.alias,
        "host": cfg.host,
        "port": cfg.port,
        "dbname": cfg.dbname,
        "user": cfg.user,
        "schema": cfg.schema,
        "read_only": cfg.read_only,
        "allow_ddl": cfg.allow_ddl,
        "statement_timeout_s": cfg.statement_timeout_s,
        "target": cfg.target,
        "ssh_host": ssh.host if ssh else None,
        "ssh_port": ssh.port if ssh else None,
        "ssh_user": ssh.user if ssh else None,
        "ssh_local_port": ssh.local_port if ssh else None,
        "ssh_auth": ("llave" if ssh and ssh.pkey_path else "password") if ssh else None,
    }


def ping(db: Optional[str] = None, *, on_event: Optional[OnEvent] = None) -> Dict[str, Any]:
    """
    Verifica la conexion de punta a punta y reporta a donde quedo conectada de verdad.

    `database`, `user` y `tunnel_port` salen del servidor, no de la config: es la
    forma de detectar un puerto local colisionado con otro PostgreSQL.
    """
    _app, cfg = _config.resolve(db, on_event=on_event)
    info = ensure_tunnel(cfg, on_event=on_event)
    started = time.perf_counter()

    def work(conn: sa.Connection) -> Dict[str, Any]:
        row = conn.execute(
            sa.text(
                "select version() as server_version, current_database() as database, "
                "current_user as usuario, current_schema() as esquema, "
                "inet_server_port() as server_port"
            )
        ).mappings().one()
        return dict(row)

    row = _engine.run(cfg, work, on_event=on_event)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    result = {
        "ok": True,
        "db": cfg.alias,
        "server_version": row["server_version"],
        "database": row["database"],
        "user": row["usuario"],
        "schema": row["esquema"],
        "remote_port": row["server_port"],
        "tunnel_port": info.local_port,
        "tunnel_owned": info.owned,
        "latency_ms": latency_ms,
    }
    emit(
        on_event,
        level="INFO",
        event="query_done",
        message=f"ping ok a {row['database']} como {row['usuario']}.",
        db=cfg.alias,
        local_port=info.local_port,
        elapsed_s=round(latency_ms / 1000, 3),
    )
    return result


def list_schemas(db: Optional[str] = None, *, on_event: Optional[OnEvent] = None) -> List[str]:
    _app, cfg = _config.resolve(db, on_event=on_event)

    def work(conn: sa.Connection) -> List[str]:
        rows = conn.execute(
            sa.text(
                "select schema_name from information_schema.schemata "
                "where schema_name not like 'pg\\_%' and schema_name <> 'information_schema' "
                "order by schema_name"
            )
        ).scalars().all()
        return [str(row) for row in rows]

    return _engine.run(cfg, work, on_event=on_event)


def list_tables(
    db: Optional[str] = None, schema: Optional[str] = None, *, on_event: Optional[OnEvent] = None
) -> pd.DataFrame:
    """Tablas y vistas del esquema: nombre, tipo, filas aproximadas y tamano."""
    _app, cfg = _config.resolve(db, on_event=on_event)
    target_schema = resolve_schema(cfg, schema)

    def work(conn: sa.Connection) -> pd.DataFrame:
        result = conn.execute(_TABLES_SQL, {"schema": target_schema})
        return pd.DataFrame.from_records(result.fetchall(), columns=list(result.keys()))

    return _engine.run(cfg, work, on_event=on_event)


def describe_table(
    table: str,
    db: Optional[str] = None,
    schema: Optional[str] = None,
    *,
    on_event: Optional[OnEvent] = None,
) -> pd.DataFrame:
    """Columnas de la tabla, con tipo, nulabilidad, default y si es parte de la PK."""
    _app, cfg = _config.resolve(db, on_event=on_event)
    target_schema = resolve_schema(cfg, schema)

    def work(conn: sa.Connection) -> pd.DataFrame:
        columns = table_columns_on(conn, target_schema, table)
        if not columns:
            raise ValueError(
                f"La tabla {target_schema}.{table} no existe o no tiene columnas visibles "
                f"para el usuario {cfg.user}."
            )
        pk: Sequence[str] = (
            conn.execute(_PK_COLUMNS_SQL, {"schema": target_schema, "table": table})
            .scalars()
            .all()
        )
        frame = pd.DataFrame(columns)
        frame["es_pk"] = frame["column_name"].isin(list(pk))
        return frame

    return _engine.run(cfg, work, on_event=on_event)


def table_exists(
    table: str,
    db: Optional[str] = None,
    schema: Optional[str] = None,
    *,
    on_event: Optional[OnEvent] = None,
) -> bool:
    _app, cfg = _config.resolve(db, on_event=on_event)
    target_schema = resolve_schema(cfg, schema)
    return _engine.run(
        cfg, lambda conn: table_exists_on(conn, target_schema, table), on_event=on_event
    )


__all__ = [
    "describe_database",
    "describe_table",
    "list_schemas",
    "list_tables",
    "ping",
    "table_exists",
]
