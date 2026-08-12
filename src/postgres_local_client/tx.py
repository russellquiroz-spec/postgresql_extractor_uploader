from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Sequence

import pandas as pd
import sqlalchemy as sa

from postgres_local_client import config as _config
from postgres_local_client import engine as _engine
from postgres_local_client.events import OnEvent, emit
from postgres_local_client.extractor import fetch_dataframe
from postgres_local_client.loader import (
    DEFAULT_CHUNKSIZE,
    IfExists,
    Method,
    load_dataframe_on,
    upsert_dataframe_on,
)
from postgres_local_client.mutator import delete_where_on, execute_sql_on
from postgres_local_client.tunnel import ensure_tunnel
from postgres_local_client.types import PostgresConfig, UpsertResult


class Transaction:
    """
    Las mismas operaciones, sobre una unica conexion y transaccion.

    Cada metodo registra que operacion esta corriendo para que el evento de rollback
    pueda decir cual fue la que fallo.
    """

    def __init__(
        self,
        conn: sa.Connection,
        cfg: PostgresConfig,
        on_event: Optional[OnEvent] = None,
    ) -> None:
        self._conn = conn
        self._cfg = cfg
        self._on_event = on_event
        self.last_operation: Optional[str] = None

    @property
    def connection(self) -> sa.Connection:
        return self._conn

    @property
    def db(self) -> str:
        return self._cfg.alias

    def extract_sql(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        chunksize: Optional[int] = None,
    ) -> pd.DataFrame:
        self.last_operation = f"extract_sql({query[:60]!r})"
        return fetch_dataframe(self._conn, query, params, chunksize=chunksize)

    def execute_sql(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        allow_full_table: bool = False,
    ) -> int:
        self.last_operation = f"execute_sql({sql[:60]!r})"
        return execute_sql_on(
            self._conn,
            self._cfg,
            sql,
            params,
            allow_full_table=allow_full_table,
            on_event=self._on_event,
        )

    def load_dataframe(
        self,
        df: pd.DataFrame,
        table: str,
        *,
        schema: Optional[str] = None,
        if_exists: IfExists = "append",
        chunksize: int = DEFAULT_CHUNKSIZE,
        method: Method = "copy",
        confirm: bool = False,
    ) -> int:
        self.last_operation = f"load_dataframe(table={table!r})"
        return load_dataframe_on(
            self._conn,
            self._cfg,
            df,
            table,
            schema=schema,
            if_exists=if_exists,
            chunksize=chunksize,
            method=method,
            confirm=confirm,
            on_event=self._on_event,
        )

    def upsert_dataframe(
        self,
        df: pd.DataFrame,
        table: str,
        conflict_cols: Sequence[str],
        *,
        update_cols: Optional[Sequence[str]] = None,
        schema: Optional[str] = None,
        chunksize: int = DEFAULT_CHUNKSIZE,
    ) -> UpsertResult:
        self.last_operation = f"upsert_dataframe(table={table!r})"
        return upsert_dataframe_on(
            self._conn,
            self._cfg,
            df,
            table,
            conflict_cols,
            update_cols=update_cols,
            schema=schema,
            chunksize=chunksize,
            on_event=self._on_event,
        )

    def delete_where(
        self,
        table: str,
        where: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        schema: Optional[str] = None,
    ) -> int:
        self.last_operation = f"delete_where(table={table!r})"
        return delete_where_on(
            self._conn,
            self._cfg,
            table,
            where,
            params,
            schema=schema,
            on_event=self._on_event,
        )


@contextmanager
def transaction(
    db: Optional[str] = None, *, on_event: Optional[OnEvent] = None
) -> Iterator[Transaction]:
    """
    Varias operaciones en una sola transaccion: commit al salir sin excepcion,
    rollback garantizado ante cualquier excepcion.

    No reintenta ante caida del tunel: si la conexion muere a media transaccion, el
    servidor ya aborto todo y reintentar a ciegas podria repetir operaciones. El
    error se propaga y la base queda sin cambios.
    """
    _app, cfg = _config.resolve(db, on_event=on_event)
    info = ensure_tunnel(cfg, on_event=on_event)
    engine = _engine.get_engine(cfg, info.local_port)

    with engine.connect() as conn:
        trans = conn.begin()
        emit(
            on_event,
            level="INFO",
            event="tx_begin",
            message=f"Transaccion abierta en {cfg.target}.",
            db=cfg.alias,
            local_port=info.local_port,
        )
        tx = Transaction(conn, cfg, on_event)
        try:
            yield tx
        except Exception as exc:
            trans.rollback()
            emit(
                on_event,
                level="ERROR",
                event="tx_rollback",
                message=(
                    f"Rollback: {type(exc).__name__}: {exc}. "
                    f"Operacion fallida: {tx.last_operation or 'desconocida'}."
                ),
                db=cfg.alias,
                operation=tx.last_operation,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        trans.commit()
        emit(
            on_event,
            level="INFO",
            event="tx_commit",
            message="Transaccion confirmada.",
            db=cfg.alias,
        )


__all__ = ["Transaction", "transaction"]
