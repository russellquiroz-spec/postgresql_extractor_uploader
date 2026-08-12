from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import sqlglot
from sqlglot import exp

from postgres_local_client.errors import (
    DDLNotAllowedError,
    FullTableOperationError,
    ReadOnlyError,
    SqlParseError,
)
from postgres_local_client.logging import quiet_logger

DIALECT = "postgres"

READ = "read"
WRITE = "write"
DDL = "ddl"
UNKNOWN = "unknown"


def _classes(*names: str) -> Tuple[type, ...]:
    """
    Resuelve clases de sqlglot por nombre.

    Los nombres cambiaron entre versiones (`AlterTable` paso a ser `Alter`), y el
    rango declarado es `sqlglot>=20`, asi que se resuelven en runtime y se ignoran
    las que no existan en la version instalada.
    """
    found = []
    for name in names:
        candidate = getattr(exp, name, None)
        if isinstance(candidate, type):
            found.append(candidate)
    return tuple(found)


_READ_CLASSES = _classes("Select", "Union", "Except", "Intersect", "Values", "Subquery")
_WRITE_CLASSES = _classes("Insert", "Update", "Delete", "Merge", "Copy")
_DDL_CLASSES = _classes(
    "Create",
    "Drop",
    "Alter",
    "AlterTable",
    "TruncateTable",
    "Grant",
    "Revoke",
    "Comment",
    "Set",
    "Analyze",
    "Cluster",
    "Reindex",
)

#: Sentencias que sqlglot no modela y deja como `Command`.
_DDL_COMMANDS = frozenset(
    {
        "TRUNCATE",
        "VACUUM",
        "ANALYZE",
        "GRANT",
        "REVOKE",
        "CLUSTER",
        "REINDEX",
        "CREATE",
        "DROP",
        "ALTER",
        "COMMENT",
        "SET",
        "RESET",
        "DISCARD",
        "LOCK",
        "REFRESH",
        "CALL",
        "DO",
        "COPY",
        "IMPORT",
        "SECURITY",
    }
)
_READ_COMMANDS = frozenset({"SHOW"})

#: Opciones que pueden ir entre EXPLAIN y la sentencia real.
_EXPLAIN_OPTION_RE = re.compile(
    r"^\s*(?:\([^)]*\)|analyze|verbose|costs|buffers|timing|summary|settings|wal|"
    r"generic_plan|memory|serialize|format\s+\w+|on|off|true|false|,)\s*",
    re.IGNORECASE,
)

_NEEDS_WHERE_CLASSES = _classes("Update", "Delete")


@dataclass(frozen=True)
class Statement:
    kind: str
    keyword: str
    needs_where: bool
    sql: str


def _command_keyword(expr: exp.Expression) -> str:
    this = expr.this
    return str(this).strip().upper() if this is not None else ""


def _command_remainder(expr: exp.Expression) -> str:
    remainder = expr.args.get("expression")
    if remainder is None:
        return ""
    name = getattr(remainder, "name", None)
    return str(name if name is not None else remainder).strip()


def _classify(expr: exp.Expression, *, depth: int = 0) -> Tuple[str, str, bool]:
    """Devuelve (kind, keyword, needs_where)."""
    keyword = type(expr).__name__.upper()

    if isinstance(expr, _NEEDS_WHERE_CLASSES):
        # `expr.args["where"]` cubre tanto `UPDATE ... WHERE` como
        # `DELETE ... USING ... WHERE` y `WITH ... DELETE ... WHERE`.
        has_where = expr.args.get("where") is not None
        return WRITE, keyword, not has_where

    if isinstance(expr, _WRITE_CLASSES):
        return WRITE, keyword, False

    if isinstance(expr, exp.Command):
        command = _command_keyword(expr)
        if command == "EXPLAIN" and depth == 0:
            # EXPLAIN ANALYZE DELETE ... SI ejecuta el DELETE, asi que no se puede
            # dar por buena la sentencia solo porque empiece con EXPLAIN: se
            # clasifica lo que venga despues de las opciones.
            remainder = _EXPLAIN_OPTION_RE.sub("", _command_remainder(expr))
            inner = _parse(remainder) if remainder else []
            if len(inner) == 1:
                kind, inner_keyword, needs_where = _classify(inner[0], depth=depth + 1)
                return kind, f"EXPLAIN {inner_keyword}", needs_where
            return UNKNOWN, "EXPLAIN", False
        if command in _READ_COMMANDS:
            return READ, command, False
        if command in _DDL_COMMANDS:
            return DDL, command, False
        return UNKNOWN, command or "COMMAND", False

    if isinstance(expr, _DDL_CLASSES):
        return DDL, keyword, False

    if isinstance(expr, _READ_CLASSES):
        return READ, keyword, False

    return UNKNOWN, keyword, False


def _parse(sql: str) -> List[exp.Expression]:
    # sqlglot loggea un warning por cada sentencia que no modela y cae a `Command`.
    # Se silencia solo durante el parseo y se restaura: es un logger ajeno.
    with quiet_logger("sqlglot"):
        parsed = sqlglot.parse(sql, read=DIALECT)
    return [expr for expr in parsed if expr is not None]


def analyze(sql: str) -> List[Statement]:
    """
    Parsea el SQL y clasifica cada sentencia.

    Se parsea de verdad en vez de buscar substrings: un `WHERE` dentro de un
    comentario o de un string literal no cuenta como WHERE valido, y al reves, un
    `DELETE` comentado no debe contar como borrado.
    """
    if not sql or not sql.strip():
        raise SqlParseError("El SQL esta vacio.")

    try:
        parsed = _parse(sql)
    except Exception as exc:  # sqlglot.errors.ParseError y derivados
        raise SqlParseError(
            f"No se pudo parsear el SQL, asi que no se puede validar si es destructivo: {exc}\n"
            "Si el SQL es valido en PostgreSQL, reportalo: las guardas fallan cerradas a "
            "proposito."
        ) from exc

    if not parsed:
        raise SqlParseError("El SQL no contiene ninguna sentencia ejecutable.")

    statements: List[Statement] = []
    for expr in parsed:
        kind, keyword, needs_where = _classify(expr)
        statements.append(
            Statement(kind=kind, keyword=keyword, needs_where=needs_where, sql=expr.sql(DIALECT))
        )
    return statements


def is_read(sql: str) -> bool:
    """True si todas las sentencias son de lectura."""
    return all(statement.kind == READ for statement in analyze(sql))


def assert_allowed(
    sql: str,
    *,
    read_only: bool,
    allow_ddl: bool,
    allow_full_table: bool = False,
    alias: Optional[str] = None,
) -> Sequence[Statement]:
    """
    Aplica las guardas de escritura sobre el SQL parseado.

    Reglas:
      - alias con READ_ONLY=true: solo SELECT / WITH ... SELECT (y SHOW/EXPLAIN de lectura);
      - UPDATE o DELETE sin WHERE: requiere allow_full_table=True;
      - DDL (DROP, TRUNCATE, ALTER, CREATE, ...): requiere ALLOW_DDL=true en el alias;
      - sentencia que no se puede clasificar: se trata como DDL (falla cerrado).
    """
    statements = analyze(sql)
    where = f"el alias '{alias}'" if alias else "este alias"
    suggestion = (
        f"'{alias}_rw'" if alias and not alias.endswith("_rw") else "el alias de escritura"
    )

    for statement in statements:
        if read_only and statement.kind != READ:
            raise ReadOnlyError(
                f"{where} tiene READ_ONLY=true y la sentencia es {statement.keyword}, "
                f"que no es de lectura. Usa un alias con READ_ONLY=false (por convencion "
                f"{suggestion}) o corrige la sentencia."
            )

        if statement.kind in (DDL, UNKNOWN) and not allow_ddl:
            detail = (
                f"la sentencia {statement.keyword} es DDL"
                if statement.kind == DDL
                else f"no se pudo clasificar la sentencia {statement.keyword}"
            )
            raise DDLNotAllowedError(
                f"Rechazado: {detail} y {where} no tiene ALLOW_DDL=true. "
                f"Si de verdad lo necesitas, define POSTGRES__{alias or '<alias>'}__ALLOW_DDL=true."
            )

        if statement.needs_where and not allow_full_table:
            raise FullTableOperationError(
                f"Rechazado: {statement.keyword} sin WHERE afecta la tabla completa. "
                "Agrega un WHERE o pasa allow_full_table=True si es intencional."
            )

    return statements


__all__ = [
    "DDL",
    "READ",
    "UNKNOWN",
    "WRITE",
    "Statement",
    "analyze",
    "assert_allowed",
    "is_read",
]
