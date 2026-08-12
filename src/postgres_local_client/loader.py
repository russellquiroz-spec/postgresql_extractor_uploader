from __future__ import annotations

from datetime import datetime as dt
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from postgres_local_client import config as _config
from postgres_local_client import engine as _engine
from postgres_local_client.errors import (
    DDLNotAllowedError,
    GuardError,
    ReadOnlyError,
    SchemaMismatchError,
    UpsertTargetError,
)
from postgres_local_client.events import OnEvent, emit
from postgres_local_client.schema import (
    resolve_schema,
    table_columns_on,
    table_exists_on,
    unique_indexes_on,
)
from postgres_local_client.types import PostgresConfig, UpsertResult

IfExists = Literal["append", "replace", "fail"]
Method = Literal["multi", "copy"]

DEFAULT_CHUNKSIZE = 10_000


# -----------------------------------------------------------------------------
# Guardas y validaciones
# -----------------------------------------------------------------------------
def _assert_writable(cfg: PostgresConfig) -> None:
    if not cfg.read_only:
        return
    suggestion = (
        f" Usa un alias con READ_ONLY=false (por convencion '{cfg.alias}_rw')."
        if not cfg.alias.endswith("_rw")
        else " Revisa POSTGRES__{0}__READ_ONLY en el env.".format(cfg.alias)
    )
    raise ReadOnlyError(
        f"El alias '{cfg.alias}' tiene READ_ONLY=true, asi que no acepta escrituras.{suggestion}"
    )


def _to_python_rows(chunk: pd.DataFrame) -> pd.DataFrame:
    """
    Convierte a objetos de Python y NaN/NaT/NA a None.

    `astype(object)` saca los escalares de numpy (int64, bool_) que psycopg no
    adapta, y el `where` unifica los tres tipos de faltante de pandas en None, que
    es lo que COPY interpreta como NULL. Un string vacio se preserva como '' y no
    se confunde con NULL.
    """
    return chunk.astype(object).where(pd.notna(chunk), None)


def _validate_columns(
    conn: sa.Connection,
    cfg: PostgresConfig,
    schema: str,
    table: str,
    df: pd.DataFrame,
    *,
    on_event: Optional[OnEvent] = None,
) -> Tuple[List[str], List[str]]:
    """
    Valida las columnas del DataFrame contra la tabla destino ANTES de escribir.

    Devuelve (columnas de la tabla, columnas de la tabla ausentes en el DataFrame).
    """
    df_columns = [str(column) for column in df.columns]
    duplicated = sorted({name for name in df_columns if df_columns.count(name) > 1})
    if duplicated:
        raise SchemaMismatchError(
            f"El DataFrame tiene columnas duplicadas: {duplicated}. No se puede cargar."
        )

    columns = table_columns_on(conn, schema, table)
    if not columns:
        raise SchemaMismatchError(
            f"La tabla {schema}.{table} no existe o no tiene columnas visibles para el "
            f"usuario {cfg.user}."
        )

    table_columns = [str(column["column_name"]) for column in columns]
    extra = [name for name in df_columns if name not in table_columns]
    absent = [name for name in table_columns if name not in df_columns]

    if extra:
        raise SchemaMismatchError(
            f"Estas columnas del DataFrame no existen en {schema}.{table}: {extra}.\n"
            f"Columnas de la tabla: {table_columns}.\n"
            f"Columnas de la tabla ausentes en el DataFrame: {absent}.\n"
            "Los nombres son sensibles a mayusculas: PostgreSQL pasa a minusculas los "
            "identificadores sin comillas, asi que una columna creada como 'Ventas' se "
            "llama 'ventas' salvo que se haya creado entre comillas dobles."
        )

    required = [
        str(column["column_name"])
        for column in columns
        if not column["is_nullable"]
        and column["column_default"] is None
        and str(column["column_name"]) in absent
    ]
    if required:
        emit(
            on_event,
            level="WARNING",
            event="write_start",
            message=(
                f"Las columnas {required} de {schema}.{table} son NOT NULL sin default y no "
                "vienen en el DataFrame: el servidor va a rechazar la carga."
            ),
            table=f"{schema}.{table}",
            db=cfg.alias,
        )

    return table_columns, absent


# -----------------------------------------------------------------------------
# Escritura
# -----------------------------------------------------------------------------
def _create_table_from_df(
    conn: sa.Connection,
    df: pd.DataFrame,
    table: str,
    schema: str,
    cfg: PostgresConfig,
    on_event: Optional[OnEvent],
) -> None:
    df.head(0).to_sql(table, conn, schema=schema, index=False, if_exists="fail")
    emit(
        on_event,
        level="WARNING",
        event="write_start",
        message=(
            f"La tabla {schema}.{table} no existia y se creo a partir de los dtypes del "
            "DataFrame. Revisa los tipos: los que infiere pandas rara vez son los que "
            "querrias en produccion."
        ),
        table=f"{schema}.{table}",
        db=cfg.alias,
    )


def _copy_into(
    conn: sa.Connection,
    target: str,
    columns: Sequence[str],
    df: pd.DataFrame,
    chunksize: int,
    cfg: PostgresConfig,
    table_label: str,
    on_event: Optional[OnEvent],
) -> int:
    """Carga con `COPY ... FROM STDIN`, que es el camino rapido de PostgreSQL."""
    column_list = ", ".join(_engine.quote(conn, str(column)) for column in columns)
    statement = f"copy {target} ({column_list}) from stdin"
    raw_connection = conn.connection.dbapi_connection
    total = 0

    with raw_connection.cursor() as cursor:  # type: ignore[union-attr]
        with cursor.copy(statement) as copy:
            for start in range(0, len(df), chunksize):
                chunk = df.iloc[start : start + chunksize]
                for row in _to_python_rows(chunk).itertuples(index=False, name=None):
                    copy.write_row(row)
                total += len(chunk)
                emit(
                    on_event,
                    level="DEBUG",
                    event="write_progress",
                    message=f"COPY {total}/{len(df)} filas.",
                    table=table_label,
                    db=cfg.alias,
                    rows=total,
                )
    return total


def load_dataframe_on(
    conn: sa.Connection,
    cfg: PostgresConfig,
    df: pd.DataFrame,
    table: str,
    *,
    schema: Optional[str] = None,
    if_exists: IfExists = "append",
    chunksize: int = DEFAULT_CHUNKSIZE,
    method: Method = "copy",
    confirm: bool = False,
    on_event: Optional[OnEvent] = None,
) -> int:
    """Implementacion sobre una conexion existente. La usa `transaction()`."""
    _assert_writable(cfg)

    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"df debe ser un pandas.DataFrame, no {type(df).__name__}.")
    if if_exists not in ("append", "replace", "fail"):
        raise ValueError("if_exists debe ser 'append', 'replace' o 'fail'.")
    if method not in ("multi", "copy"):
        raise ValueError("method debe ser 'copy' o 'multi'.")
    if chunksize <= 0:
        raise ValueError("chunksize debe ser mayor a 0.")

    target_schema = resolve_schema(cfg, schema)
    table_label = f"{target_schema}.{table}"
    started = dt.now()

    exists = table_exists_on(conn, target_schema, table)
    if exists and if_exists == "fail":
        raise SchemaMismatchError(
            f"La tabla {table_label} ya existe y if_exists='fail'. Usa 'append' para agregar "
            "o 'replace' (con confirm=True) para reemplazar su contenido."
        )

    if not exists:
        if not cfg.allow_ddl:
            raise DDLNotAllowedError(
                f"La tabla {table_label} no existe y crearla es DDL, pero el alias "
                f"'{cfg.alias}' no tiene ALLOW_DDL=true. Creala a mano con los tipos "
                f"correctos, o define POSTGRES__{cfg.alias}__ALLOW_DDL=true."
            )
        _create_table_from_df(conn, df, table, target_schema, cfg, on_event)

    # La validacion va antes de cualquier accion destructiva: si las columnas no
    # cuadran, la tabla no se toca.
    table_columns, _absent = _validate_columns(
        conn, cfg, target_schema, table, df, on_event=on_event
    )

    emit(
        on_event,
        level="INFO",
        event="write_start",
        message=f"Cargando {len(df)} filas en {table_label} (method={method}, {if_exists}).",
        table=table_label,
        db=cfg.alias,
        rows=int(len(df)),
        cols=int(len(df.columns)),
    )

    if exists and if_exists == "replace":
        if not confirm:
            raise GuardError(
                f"if_exists='replace' borra todo el contenido actual de {table_label}. "
                "Es destructivo, asi que requiere confirm=True."
            )
        target = _engine.qualified(conn, target_schema, table)
        # TRUNCATE es el camino rapido pero es DDL: solo se usa si el alias lo
        # permite. Si no, DELETE consigue lo mismo con permisos de DML.
        conn.execute(sa.text(f"truncate table {target}" if cfg.allow_ddl else f"delete from {target}"))
        emit(
            on_event,
            level="WARNING",
            event="write_start",
            message=(
                f"{table_label} vaciada con "
                f"{'TRUNCATE' if cfg.allow_ddl else 'DELETE'} antes de cargar."
            ),
            table=table_label,
            db=cfg.alias,
        )

    if df.empty:
        emit(
            on_event,
            level="WARNING",
            event="write_done",
            message=f"El DataFrame esta vacio: no se escribio ninguna fila en {table_label}.",
            table=table_label,
            db=cfg.alias,
            rows=0,
            affected=0,
        )
        return 0

    if method == "copy":
        target = _engine.qualified(conn, target_schema, table)
        written = _copy_into(
            conn,
            target,
            [str(column) for column in df.columns],
            df,
            chunksize,
            cfg,
            table_label,
            on_event,
        )
    else:
        df.to_sql(
            table,
            conn,
            schema=target_schema,
            index=False,
            if_exists="append",
            method="multi",
            chunksize=chunksize,
        )
        written = int(len(df))

    emit(
        on_event,
        level="INFO",
        event="write_done",
        message=f"{written} filas cargadas en {table_label}.",
        table=table_label,
        db=cfg.alias,
        rows=written,
        affected=written,
        elapsed_s=round((dt.now() - started).total_seconds(), 3),
    )
    return written


def load_dataframe(
    df: pd.DataFrame,
    table: str,
    *,
    db: Optional[str] = None,
    schema: Optional[str] = None,
    if_exists: IfExists = "append",
    chunksize: int = DEFAULT_CHUNKSIZE,
    method: Method = "copy",
    confirm: bool = False,
    on_event: Optional[OnEvent] = None,
) -> int:
    """
    Carga un DataFrame en una tabla y devuelve el numero de filas escritas.

    `method="copy"` usa `COPY ... FROM STDIN`. `if_exists="replace"` es destructivo y
    requiere `confirm=True`. Las columnas se validan contra la tabla destino antes de
    escribir la primera fila.
    """
    _app, cfg = _config.resolve(db, on_event=on_event)

    def work(conn: sa.Connection) -> int:
        return load_dataframe_on(
            conn,
            cfg,
            df,
            table,
            schema=schema,
            if_exists=if_exists,
            chunksize=chunksize,
            method=method,
            confirm=confirm,
            on_event=on_event,
        )

    return _engine.run(cfg, work, write=True, on_event=on_event)


# -----------------------------------------------------------------------------
# Upsert
# -----------------------------------------------------------------------------
def _assert_conflict_target(
    conn: sa.Connection, schema: str, table: str, conflict_cols: Sequence[str]
) -> None:
    """
    Verifica que exista PK o indice unico sobre exactamente esas columnas.

    Sin eso, `ON CONFLICT (cols)` falla con un error de PostgreSQL poco claro
    ("no existe una restriccion... que coincida"), asi que se comprueba antes.
    """
    indexes = unique_indexes_on(conn, schema, table)
    wanted = {name.lower() for name in conflict_cols}
    for index in indexes:
        if {str(column).lower() for column in index["columnas"]} == wanted:
            return

    available = (
        "; ".join(
            f"{index['indice']} ({', '.join(index['columnas'])})"
            + (" [PK]" if index["es_pk"] else "")
            for index in indexes
        )
        or "ninguno"
    )
    raise UpsertTargetError(
        f"No hay PK ni indice unico sobre {list(conflict_cols)} en {schema}.{table}, asi que "
        f"ON CONFLICT no puede usarlas como llave.\nIndices unicos existentes: {available}.\n"
        "Crea un indice unico sobre esas columnas o usa las de uno existente."
    )


def upsert_dataframe_on(
    conn: sa.Connection,
    cfg: PostgresConfig,
    df: pd.DataFrame,
    table: str,
    conflict_cols: Sequence[str],
    *,
    update_cols: Optional[Sequence[str]] = None,
    schema: Optional[str] = None,
    chunksize: int = DEFAULT_CHUNKSIZE,
    on_event: Optional[OnEvent] = None,
) -> UpsertResult:
    """Implementacion sobre una conexion existente. La usa `transaction()`."""
    _assert_writable(cfg)

    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"df debe ser un pandas.DataFrame, no {type(df).__name__}.")
    if not conflict_cols:
        raise ValueError("conflict_cols no puede estar vacio.")
    if chunksize <= 0:
        raise ValueError("chunksize debe ser mayor a 0.")

    target_schema = resolve_schema(cfg, schema)
    table_label = f"{target_schema}.{table}"
    started = dt.now()

    df_columns = [str(column) for column in df.columns]
    missing_conflict = [name for name in conflict_cols if name not in df_columns]
    if missing_conflict:
        raise SchemaMismatchError(
            f"Las columnas de conflicto {missing_conflict} no estan en el DataFrame. "
            f"Columnas disponibles: {df_columns}."
        )

    if not table_exists_on(conn, target_schema, table):
        raise SchemaMismatchError(
            f"La tabla {table_label} no existe. upsert_dataframe no la crea porque necesita "
            "una PK o indice unico sobre las columnas de conflicto."
        )

    _validate_columns(conn, cfg, target_schema, table, df, on_event=on_event)
    _assert_conflict_target(conn, target_schema, table, conflict_cols)

    if update_cols is None:
        effective_update = [name for name in df_columns if name not in set(conflict_cols)]
    else:
        unknown = [name for name in update_cols if name not in df_columns]
        if unknown:
            raise SchemaMismatchError(
                f"update_cols contiene columnas que no estan en el DataFrame: {unknown}."
            )
        effective_update = list(update_cols)

    if df.empty:
        emit(
            on_event,
            level="WARNING",
            event="write_done",
            message=f"El DataFrame esta vacio: no se hizo upsert en {table_label}.",
            table=table_label,
            db=cfg.alias,
            rows=0,
        )
        return {"inserted": 0, "updated": 0}

    emit(
        on_event,
        level="INFO",
        event="write_start",
        message=(
            f"Upsert de {len(df)} filas en {table_label} por {list(conflict_cols)} "
            f"(actualiza {effective_update or 'nada'})."
        ),
        table=table_label,
        db=cfg.alias,
        rows=int(len(df)),
    )

    target_table = sa.Table(table, sa.MetaData(), autoload_with=conn, schema=target_schema)
    inserted = 0
    updated = 0

    for start in range(0, len(df), chunksize):
        chunk = df.iloc[start : start + chunksize]
        records: List[Dict[str, Any]] = _to_python_rows(chunk).to_dict("records")

        statement = pg_insert(target_table).values(records)
        if effective_update:
            statement = statement.on_conflict_do_update(
                index_elements=list(conflict_cols),
                set_={name: statement.excluded[name] for name in effective_update},
            )
        else:
            # Sin columnas que actualizar el upsert degenera en insert idempotente.
            statement = statement.on_conflict_do_nothing(index_elements=list(conflict_cols))

        # (xmax = 0) es true en las filas insertadas y false en las actualizadas.
        statement = statement.returning(sa.literal_column("(xmax = 0)").label("pgc_inserted"))

        for row in conn.execute(statement):
            if row[0]:
                inserted += 1
            else:
                updated += 1

        emit(
            on_event,
            level="DEBUG",
            event="write_progress",
            message=f"Upsert {min(start + chunksize, len(df))}/{len(df)} filas.",
            table=table_label,
            db=cfg.alias,
            rows=inserted + updated,
        )

    result: UpsertResult = {"inserted": inserted, "updated": updated}
    emit(
        on_event,
        level="INFO",
        event="write_done",
        message=f"Upsert en {table_label}: {inserted} insertadas, {updated} actualizadas.",
        table=table_label,
        db=cfg.alias,
        rows=inserted + updated,
        affected=inserted + updated,
        elapsed_s=round((dt.now() - started).total_seconds(), 3),
    )
    return result


def upsert_dataframe(
    df: pd.DataFrame,
    table: str,
    conflict_cols: Sequence[str],
    *,
    update_cols: Optional[Sequence[str]] = None,
    db: Optional[str] = None,
    schema: Optional[str] = None,
    chunksize: int = DEFAULT_CHUNKSIZE,
    on_event: Optional[OnEvent] = None,
) -> UpsertResult:
    """
    `INSERT ... ON CONFLICT (conflict_cols) DO UPDATE`, reportando ambos conteos.

    `update_cols=None` actualiza todas las columnas del DataFrame menos las de
    conflicto. Exige PK o indice unico sobre `conflict_cols`.
    """
    _app, cfg = _config.resolve(db, on_event=on_event)

    def work(conn: sa.Connection) -> UpsertResult:
        return upsert_dataframe_on(
            conn,
            cfg,
            df,
            table,
            conflict_cols,
            update_cols=update_cols,
            schema=schema,
            chunksize=chunksize,
            on_event=on_event,
        )

    return _engine.run(cfg, work, write=True, on_event=on_event)


__all__ = [
    "DEFAULT_CHUNKSIZE",
    "load_dataframe",
    "load_dataframe_on",
    "upsert_dataframe",
    "upsert_dataframe_on",
]
