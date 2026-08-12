from __future__ import annotations

from datetime import datetime as dt
from typing import Any, Dict, Optional

import sqlalchemy as sa

from postgres_local_client import config as _config
from postgres_local_client import engine as _engine
from postgres_local_client import guards as _guards
from postgres_local_client.errors import GuardError
from postgres_local_client.events import OnEvent, emit
from postgres_local_client.schema import resolve_schema
from postgres_local_client.types import PostgresConfig


def execute_sql_on(
    conn: sa.Connection,
    cfg: PostgresConfig,
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    allow_full_table: bool = False,
    on_event: Optional[OnEvent] = None,
) -> int:
    """Implementacion sobre una conexion existente. La usa `transaction()`."""
    statements = _guards.assert_allowed(
        sql,
        read_only=cfg.read_only,
        allow_ddl=cfg.allow_ddl,
        allow_full_table=allow_full_table,
        alias=cfg.alias,
    )
    started = dt.now()
    emit(
        on_event,
        level="INFO",
        event="query_start",
        message=f"Ejecutando {[statement.keyword for statement in statements]} en {cfg.target}.",
        db=cfg.alias,
    )

    result = conn.execute(sa.text(sql), params or {})
    affected = int(result.rowcount) if result.rowcount is not None else -1

    emit(
        on_event,
        level="INFO",
        event="query_done",
        message=f"Sentencia ejecutada. Filas afectadas: {affected}.",
        db=cfg.alias,
        affected=affected,
        elapsed_s=round((dt.now() - started).total_seconds(), 3),
    )
    return affected


def execute_sql(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    db: Optional[str] = None,
    allow_full_table: bool = False,
    on_event: Optional[OnEvent] = None,
) -> int:
    """
    Ejecuta DML/DDL y devuelve las filas afectadas (-1 si el driver no lo reporta).

    Las guardas se evaluan sobre el SQL parseado: un UPDATE o DELETE sin WHERE falla
    salvo `allow_full_table=True`, el DDL requiere ALLOW_DDL en el alias, y un alias
    READ_ONLY rechaza cualquier cosa que no sea lectura.
    """
    _app, cfg = _config.resolve(db, on_event=on_event)

    def work(conn: sa.Connection) -> int:
        return execute_sql_on(
            conn, cfg, sql, params, allow_full_table=allow_full_table, on_event=on_event
        )

    return _engine.run(cfg, work, write=True, on_event=on_event)


def delete_where_on(
    conn: sa.Connection,
    cfg: PostgresConfig,
    table: str,
    where: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    schema: Optional[str] = None,
    on_event: Optional[OnEvent] = None,
) -> int:
    """Implementacion sobre una conexion existente. La usa `transaction()`."""
    if not where or not where.strip():
        raise ValueError(
            "El parametro 'where' es obligatorio y no puede estar vacio. Para borrar la "
            "tabla completa usa execute_sql(..., allow_full_table=True) de forma explicita."
        )

    target_schema = resolve_schema(cfg, schema)
    target = _engine.qualified(conn, target_schema, table)
    sql = f"delete from {target} where {where}"

    statements = _guards.assert_allowed(
        sql,
        read_only=cfg.read_only,
        allow_ddl=cfg.allow_ddl,
        allow_full_table=False,
        alias=cfg.alias,
    )
    if len(statements) != 1 or statements[0].keyword != "DELETE":
        raise GuardError(
            "El parametro 'where' produjo mas de una sentencia o una que no es DELETE: "
            f"{[statement.keyword for statement in statements]}. Revisa que no traiga ';'."
        )

    started = dt.now()
    emit(
        on_event,
        level="INFO",
        event="query_start",
        message=f"Borrando de {target_schema}.{table} con filtro.",
        db=cfg.alias,
        table=f"{target_schema}.{table}",
    )

    affected = int(conn.execute(sa.text(sql), params or {}).rowcount)

    emit(
        on_event,
        level="INFO",
        event="query_done",
        message=f"{affected} filas borradas de {target_schema}.{table}.",
        db=cfg.alias,
        table=f"{target_schema}.{table}",
        affected=affected,
        elapsed_s=round((dt.now() - started).total_seconds(), 3),
    )
    return affected


def delete_where(
    table: str,
    where: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    db: Optional[str] = None,
    schema: Optional[str] = None,
    on_event: Optional[OnEvent] = None,
) -> int:
    """Borra filas de una tabla con un filtro obligatorio y devuelve cuantas borro."""
    _app, cfg = _config.resolve(db, on_event=on_event)

    def work(conn: sa.Connection) -> int:
        return delete_where_on(
            conn, cfg, table, where, params, schema=schema, on_event=on_event
        )

    return _engine.run(cfg, work, write=True, on_event=on_event)


__all__ = ["delete_where", "delete_where_on", "execute_sql", "execute_sql_on"]
