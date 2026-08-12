from __future__ import annotations

from datetime import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import sqlalchemy as sa

from postgres_local_client import config as _config
from postgres_local_client import engine as _engine
from postgres_local_client import guards as _guards
from postgres_local_client.errors import GuardError
from postgres_local_client.events import OnEvent, emit
from postgres_local_client.io import save_outputs
from postgres_local_client.types import PostgresConfig


def read_query_file(query_file: str) -> str:
    path = Path(query_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo: {path}")
    if not path.is_file():
        raise ValueError(f"No es un archivo: {path}")
    return path.read_text(encoding="utf-8")


def resolve_query(query: Optional[str], query_file: Optional[str]) -> str:
    """`query` tiene prioridad sobre `query_file`, igual que en redshift_extractor."""
    if query is not None and query.strip():
        return query
    if query_file:
        return read_query_file(query_file)
    raise ValueError("Debes proporcionar 'query' o 'query_file'.")


def list_databases(*, on_event: Optional[OnEvent] = None) -> List[str]:
    """Aliases configurados, normalizados a lowercase. No abre el tunel."""
    _app, _ssh, pg_map = _config.load_config(on_event=on_event)
    return sorted(pg_map)


def fetch_dataframe(
    conn: sa.Connection,
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    chunksize: Optional[int] = None,
) -> pd.DataFrame:
    """
    Ejecuta el SQL en una conexion existente y devuelve un DataFrame.

    Los parametros se enlazan con bindparams (`:nombre`), nunca por interpolacion
    de texto.
    """
    result = conn.execute(sa.text(sql), params or {})
    if not result.returns_rows:
        return pd.DataFrame()

    columns = list(result.keys())
    if not chunksize:
        return pd.DataFrame.from_records(result.fetchall(), columns=columns)

    if chunksize <= 0:
        raise ValueError("chunksize debe ser mayor a 0.")

    frames: List[pd.DataFrame] = []
    while True:
        rows = result.fetchmany(chunksize)
        if not rows:
            break
        frames.append(pd.DataFrame.from_records(rows, columns=columns))

    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


def default_base_name(cfg: PostgresConfig) -> str:
    return f"{cfg.alias}_{cfg.dbname}_{dt.now().strftime('%Y%m%d_%H%M%S')}"


def extract_sql(
    query: Optional[str] = None,
    *,
    db: Optional[str] = None,
    query_file: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    save_dir: Optional[str] = None,
    base_name: Optional[str] = None,
    save_csv: bool = False,
    save_parquet: bool = False,
    chunksize: Optional[int] = None,
    on_event: Optional[OnEvent] = None,
) -> pd.DataFrame:
    """
    Ejecuta un SELECT y devuelve un `pandas.DataFrame`, abriendo el tunel si hace falta.

    `db` es keyword-only con default `DEFAULT_DB`, para que el caso comun
    `extract_sql("select 1")` funcione sin ceremonia y para que los scripts de
    postgres_local_extractor migren sin editar cada llamada.

    Persistencia opcional: si `save_dir` es None o vacio solo devuelve el DataFrame.
    """
    started = dt.now()
    sql = resolve_query(query, query_file)
    app, cfg = _config.resolve(db, on_event=on_event)

    statements = _guards.assert_allowed(
        sql, read_only=cfg.read_only, allow_ddl=cfg.allow_ddl, alias=cfg.alias
    )
    non_read = [statement.keyword for statement in statements if statement.kind != _guards.READ]
    if non_read:
        raise GuardError(
            f"extract_sql solo ejecuta lectura y este SQL incluye {non_read}. "
            "Usa execute_sql() para DML/DDL, load_dataframe()/upsert_dataframe() para cargar, "
            "o delete_where() para borrar."
        )

    emit(
        on_event,
        level="INFO",
        event="query_start",
        message=f"Ejecutando query en {cfg.target}.",
        db=cfg.alias,
        chunksize=chunksize,
    )

    def work(conn: sa.Connection) -> pd.DataFrame:
        return fetch_dataframe(conn, sql, params, chunksize=chunksize)

    df = _engine.run(cfg, work, on_event=on_event)

    emit(
        on_event,
        level="INFO",
        event="query_done",
        message="Query ejecutado correctamente.",
        db=cfg.alias,
        rows=int(len(df)),
        cols=int(len(df.columns)),
        elapsed_s=round((dt.now() - started).total_seconds(), 3),
    )

    save_outputs(
        df,
        save_dir=save_dir if save_dir is not None else None,
        base_name=base_name or default_base_name(cfg),
        save_csv=save_csv,
        save_parquet=save_parquet,
        on_event=on_event,
    )
    return df


__all__ = ["extract_sql", "fetch_dataframe", "list_databases", "read_query_file"]
