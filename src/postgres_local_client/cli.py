from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pandas as pd
import typer

from postgres_local_client import guards as _guards
from postgres_local_client.errors import ConfigError, PostgresLocalClientError, TunnelError
from postgres_local_client.extractor import extract_sql, list_databases, read_query_file
from postgres_local_client.io import save_dataframe
from postgres_local_client.loader import load_dataframe
from postgres_local_client.logging import configure_logging
from postgres_local_client.schema import describe_database, list_tables, ping, table_exists
from postgres_local_client.tunnel import (
    close_all_tunnels,
    open_tunnel,
    tunnel_status,
)

app = typer.Typer(add_completion=False, help="Cliente de PostgreSQL en la VM via tunel SSH.")
tunnel_app = typer.Typer(add_completion=False, help="Gestion del tunel SSH.")
app.add_typer(tunnel_app, name="tunnel")

DEFAULT_LIMIT = 10

#: Codigos de salida del contrato de la CLI.
EXIT_OK = 0
EXIT_BUSINESS = 1
EXIT_CONFIG = 2
EXIT_TUNNEL = 3


def _console_level() -> str:
    """
    Nivel del logger de consola de la CLI.

    WARNING a proposito: los eventos INFO ya le llegan al usuario por `on_event`
    (que es como ve el progreso del tunel), asi que mandarlos tambien al log solo
    duplica ruido en comandos simples como `ls`. Con `--debug`, el printer de eventos
    es el que baja a DEBUG.
    """
    return "WARNING"


def _printer(debug: bool = False) -> Callable[[Dict[str, Any]], None]:
    def printer(event: Dict[str, Any]) -> None:
        if event["level"] == "DEBUG" and not debug:
            return
        extras = {
            key: value
            for key, value in event.items()
            if key not in ("ts", "level", "event", "message")
        }
        typer.echo(
            f'{event["ts"]} [{event["level"]}] {event["event"]}: {event["message"]} | {extras}',
            err=True,
        )

    return printer


def _guarded(action: Callable[[], None]) -> None:
    """
    Traduce excepciones a codigos de salida: 1 negocio, 2 configuracion, 3 tunel.
    """
    try:
        action()
    except ConfigError as exc:
        typer.echo(f"ERROR DE CONFIGURACION - {exc}", err=True)
        raise typer.Exit(code=EXIT_CONFIG)
    except TunnelError as exc:
        typer.echo(f"ERROR DE TUNEL - {exc}", err=True)
        raise typer.Exit(code=EXIT_TUNNEL)
    except (PostgresLocalClientError, Exception) as exc:
        if isinstance(exc, typer.Exit):
            raise
        typer.echo(f"ERROR - {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=EXIT_BUSINESS)


def _strip_trailing_semicolons(sql: str) -> str:
    cleaned = sql.strip()
    while cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def apply_limit(sql: str, limit: Optional[int]) -> str:
    """
    Envuelve el query con LIMIT para la prueba rapida. Solo aplica a SELECT/WITH.

    El tipo de sentencia se decide con el SQL parseado, no por la primera palabra.
    """
    cleaned = _strip_trailing_semicolons(sql)
    if not cleaned:
        raise ValueError("El archivo SQL esta vacio.")
    if limit is None:
        return cleaned
    if limit <= 0:
        raise ValueError("--limit debe ser mayor a 0. Usa --full si no quieres limite.")
    if not _guards.is_read(cleaned):
        raise ValueError(
            "El modo LIMIT solo funciona con SELECT/WITH. Usa --full para ejecutar este SQL."
        )
    return f"select *\nfrom (\n{cleaned}\n) as query_limitada\nlimit {limit}"


# -----------------------------------------------------------------------------
# Comandos
# -----------------------------------------------------------------------------
@app.command()
def ls(debug: bool = typer.Option(False, "--debug", help="Muestra eventos DEBUG.")) -> None:
    """Lista los aliases configurados."""

    def action() -> None:
        configure_logging(_console_level())
        for alias in list_databases(on_event=_printer(debug) if debug else None):
            typer.echo(alias)

    _guarded(action)


@app.command("describe")
def describe(
    db: Optional[str] = typer.Option(None, "--db", help="Alias (default: DEFAULT_DB)."),
) -> None:
    """Muestra la config efectiva de un alias, sin credenciales."""

    def action() -> None:
        configure_logging(_console_level())
        for key, value in describe_database(db).items():
            typer.echo(f"{key}: {value}")

    _guarded(action)


@app.command("ping")
def ping_command(
    db: Optional[str] = typer.Option(None, "--db", help="Alias (default: DEFAULT_DB)."),
    debug: bool = typer.Option(False, "--debug", help="Muestra eventos DEBUG."),
) -> None:
    """Verifica la conexion de punta a punta abriendo el tunel si hace falta."""

    def action() -> None:
        configure_logging(_console_level())
        result = ping(db, on_event=_printer(debug))
        for key, value in result.items():
            typer.echo(f"{key}: {value}")

    _guarded(action)


@app.command()
def run(
    db: Optional[str] = typer.Option(None, "--db", help="Alias (default: DEFAULT_DB)."),
    query: str = typer.Option(..., "--query", help="SQL a ejecutar (entre comillas)."),
    out: Optional[str] = typer.Option(None, "--out", help="Ruta de salida."),
    fmt: str = typer.Option("parquet", "--fmt", help="csv|parquet"),
    debug: bool = typer.Option(False, "--debug", help="Muestra eventos DEBUG."),
) -> None:
    """Ejecuta un query y opcionalmente guarda el resultado."""

    def action() -> None:
        configure_logging(_console_level())
        if fmt not in ("csv", "parquet"):
            raise ValueError("--fmt debe ser 'csv' o 'parquet'.")
        df = extract_sql(query, db=db, on_event=_printer(debug))
        typer.echo(f"Filas: {len(df):,} | Columnas: {len(df.columns):,}")
        typer.echo(df.head(DEFAULT_LIMIT).to_string(index=False))
        if out:
            path = save_dataframe(df, out, fmt=fmt)  # type: ignore[arg-type]
            typer.echo(f"OK -> {path}")

    _guarded(action)


@app.command("run-file")
def run_file(
    sql_file: Path = typer.Argument(..., help="Ruta del archivo .sql a ejecutar."),
    db: Optional[str] = typer.Option(None, "--db", help="Alias (default: DEFAULT_DB)."),
    limit: int = typer.Option(
        DEFAULT_LIMIT, "--limit", help=f"Filas para la prueba rapida. Default: {DEFAULT_LIMIT}"
    ),
    full: bool = typer.Option(False, "--full", help="Ejecuta el query completo, sin LIMIT."),
    retries: int = typer.Option(3, "--retries", help="Intentos si falla la conexion."),
    retry_wait: float = typer.Option(5.0, "--retry-wait", help="Segundos entre reintentos."),
    output: Optional[Path] = typer.Option(None, "--output", help="Guarda el resultado en CSV."),
    print_sql: bool = typer.Option(False, "--print-sql", help="Imprime el SQL final."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo arma el SQL; no lo ejecuta."),
    debug: bool = typer.Option(False, "--debug", help="Muestra eventos DEBUG."),
) -> None:
    """Ejecuta un archivo .sql. Por defecto aplica LIMIT 10 (usa --full para el completo)."""

    def action() -> None:
        configure_logging(_console_level())
        if retries <= 0:
            raise ValueError("--retries debe ser mayor a 0.")

        raw_sql = read_query_file(str(sql_file))
        final_sql = apply_limit(raw_sql, None if full else limit)

        typer.echo(f"Conexion: {db or 'DEFAULT_DB'}")
        typer.echo(f"Archivo: {sql_file}")
        typer.echo(f"Modo: {'FULL' if full else f'LIMIT {limit}'}")
        if print_sql:
            typer.echo("")
            typer.echo(final_sql)
            typer.echo("")
        if dry_run:
            typer.echo("DRY RUN - No se ejecuto el query.")
            return

        started = time.perf_counter()
        last_error: Optional[Exception] = None
        df: Optional[pd.DataFrame] = None
        for attempt in range(1, retries + 1):
            try:
                if attempt > 1:
                    typer.echo(f"Reintento {attempt}/{retries}...")
                df = extract_sql(final_sql, db=db, on_event=_printer(debug))
                break
            except TunnelError as error:
                last_error = error
                if attempt == retries:
                    raise
                typer.echo(f"Fallo de conexion. Esperando {retry_wait:.1f}s...", err=True)
                time.sleep(retry_wait)

        if df is None:  # pragma: no cover - el bucle relanza antes de llegar aqui
            raise last_error or RuntimeError("No se pudo ejecutar el query.")

        typer.echo(f"OK - Query ejecutado en {time.perf_counter() - started:.1f}s")
        typer.echo(f"Filas: {len(df):,}")
        typer.echo(f"Columnas: {len(df.columns):,}")
        typer.echo("")
        typer.echo(df.head(DEFAULT_LIMIT).to_string(index=False))

        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output, index=False)
            typer.echo(f"\nCSV guardado en: {output}")

    _guarded(action)


@app.command()
def tables(
    db: Optional[str] = typer.Option(None, "--db", help="Alias (default: DEFAULT_DB)."),
    schema: Optional[str] = typer.Option(None, "--schema", help="Esquema (default: el del alias)."),
    debug: bool = typer.Option(False, "--debug", help="Muestra eventos DEBUG."),
) -> None:
    """Lista tablas y vistas del esquema con filas aproximadas y tamano."""

    def action() -> None:
        configure_logging(_console_level())
        frame = list_tables(db, schema, on_event=_printer(debug))
        if frame.empty:
            typer.echo("No hay tablas visibles en el esquema.")
            return
        typer.echo(frame.to_string(index=False))

    _guarded(action)


@app.command("load")
def load_command(
    file: Path = typer.Option(..., "--file", help="CSV o Parquet a cargar."),
    table: str = typer.Option(..., "--table", help="Tabla destino."),
    db: Optional[str] = typer.Option(None, "--db", help="Alias de escritura."),
    schema: Optional[str] = typer.Option(None, "--schema", help="Esquema destino."),
    if_exists: str = typer.Option("append", "--if-exists", help="append|replace|fail"),
    chunksize: int = typer.Option(10_000, "--chunksize", help="Filas por lote."),
    method: str = typer.Option("copy", "--method", help="copy|multi"),
    yes: bool = typer.Option(False, "--yes", help="No pedir confirmacion en replace."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Valida columnas y reporta el plan sin escribir."
    ),
    debug: bool = typer.Option(False, "--debug", help="Muestra eventos DEBUG."),
) -> None:
    """Carga un archivo a una tabla. Rechaza los aliases read-only."""

    def action() -> None:
        configure_logging(_console_level())
        if if_exists not in ("append", "replace", "fail"):
            raise ValueError("--if-exists debe ser 'append', 'replace' o 'fail'.")
        if method not in ("copy", "multi"):
            raise ValueError("--method debe ser 'copy' o 'multi'.")

        info = describe_database(db)
        if info["read_only"]:
            raise ValueError(
                f"El alias '{info['alias']}' es READ_ONLY y no acepta cargas. Usa el alias de "
                f"escritura (ve los disponibles con 'postgres-local-client ls'; por convencion "
                f"'{info['alias']}_rw' con READ_ONLY=false)."
            )

        suffix = file.suffix.lower()
        if suffix == ".csv":
            frame = pd.read_csv(file)
        elif suffix in (".parquet", ".pq"):
            frame = pd.read_parquet(file)
        else:
            raise ValueError(f"Extension no soportada: {suffix}. Usa .csv o .parquet.")

        target_schema = schema or info["schema"]
        typer.echo(f"Archivo: {file} ({len(frame):,} filas, {len(frame.columns)} columnas)")
        typer.echo(f"Destino: {target_schema}.{table} en '{info['alias']}' ({info['target']})")
        typer.echo(f"Modo: {if_exists} | method={method} | chunksize={chunksize}")

        if dry_run:
            exists = table_exists(table, db, target_schema)
            typer.echo(f"La tabla {'existe' if exists else 'NO existe'}.")
            if exists:
                from postgres_local_client.schema import describe_table

                destino = describe_table(table, db, target_schema)
                table_columns = [str(name) for name in destino["column_name"]]
                extra = [str(c) for c in frame.columns if str(c) not in table_columns]
                absent = [name for name in table_columns if name not in set(map(str, frame.columns))]
                typer.echo(f"Columnas del archivo que no existen en destino: {extra or 'ninguna'}")
                typer.echo(f"Columnas de la tabla ausentes en el archivo: {absent or 'ninguna'}")
                typer.echo("DRY RUN - " + ("HAY PROBLEMAS" if extra else "el plan es valido"))
            else:
                typer.echo(
                    "DRY RUN - la tabla no existe; se crearia a partir de los dtypes "
                    f"(requiere ALLOW_DDL=true, ahora {info['allow_ddl']})."
                )
            return

        if if_exists == "replace" and not yes:
            confirmed = typer.confirm(
                f"if_exists=replace borra TODO el contenido de {target_schema}.{table}. Continuar?"
            )
            if not confirmed:
                typer.echo("Cancelado.")
                return

        written = load_dataframe(
            frame,
            table,
            db=db,
            schema=schema,
            if_exists=if_exists,  # type: ignore[arg-type]
            chunksize=chunksize,
            method=method,  # type: ignore[arg-type]
            confirm=True,
            on_event=_printer(debug),
        )
        typer.echo(f"OK - {written:,} filas cargadas en {target_schema}.{table}")

    _guarded(action)


# -----------------------------------------------------------------------------
# Subcomandos de tunel
# -----------------------------------------------------------------------------
@tunnel_app.command("status")
def tunnel_status_command() -> None:
    """Muestra los tuneles que conoce este proceso."""

    def action() -> None:
        configure_logging(_console_level())
        infos = tunnel_status()
        if not infos:
            typer.echo("No hay tuneles abiertos en este proceso.")
            return
        for info in infos:
            data = info.as_dict()
            typer.echo(
                f"localhost:{data['local_port']} -> {data['remote_host']}:{data['remote_port']} "
                f"via {data['ssh_user']}@{data['ssh_host']}:{data['ssh_port']} | "
                f"vivo={data['is_alive']} propio={data['owned']} desde={data['opened_at']}"
            )

    _guarded(action)


@tunnel_app.command("open")
def tunnel_open_command(
    db: Optional[str] = typer.Option(None, "--db", help="Alias (default: DEFAULT_DB)."),
    keep_alive: bool = typer.Option(
        False, "--keep-alive", help="Deja el tunel arriba en foreground hasta Ctrl+C."
    ),
    debug: bool = typer.Option(False, "--debug", help="Muestra eventos DEBUG."),
) -> None:
    """Abre el tunel. Con --keep-alive queda en foreground para DBeaver o pgAdmin."""

    def action() -> None:
        configure_logging(_console_level())
        info = open_tunnel(db, on_event=_printer(debug))
        typer.echo(f"Tunel arriba en localhost:{info.local_port}")
        typer.echo(f"  destino : {info.remote_host}:{info.remote_port}")
        typer.echo(f"  ssh     : {info.ssh_user}@{info.ssh_host}:{info.ssh_port}")
        typer.echo(f"  propio  : {info.owned}")

        if not keep_alive:
            typer.echo(
                "\nEl tunel se cierra al terminar este proceso. Usa --keep-alive para dejarlo "
                "arriba y conectarte desde un cliente externo."
            )
            return

        typer.echo(
            f"\nConecta tu cliente a localhost:{info.local_port}. Ctrl+C para cerrar."
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            typer.echo("\nCerrando tunel...")

    _guarded(action)


@tunnel_app.command("close")
def tunnel_close_command(
    debug: bool = typer.Option(False, "--debug", help="Muestra eventos DEBUG.")
) -> None:
    """Cierra los tuneles abiertos por este proceso (nunca los ajenos)."""

    def action() -> None:
        configure_logging(_console_level())
        close_all_tunnels(on_event=_printer(debug))
        typer.echo("Tuneles propios cerrados.")

    _guarded(action)


if __name__ == "__main__":  # pragma: no cover
    app()
