from __future__ import annotations

import pytest

from postgres_local_client import guards
from postgres_local_client.errors import (
    DDLNotAllowedError,
    FullTableOperationError,
    ReadOnlyError,
    SqlParseError,
)

# (sql, kind esperado, needs_where esperado)
CLASIFICACION = [
    ("select 1", guards.READ, False),
    ("select * from t where x = 1", guards.READ, False),
    ("with c as (select 1) select * from c", guards.READ, False),
    ("values (1),(2)", guards.READ, False),
    ("show search_path", guards.READ, False),
    ("explain select 1", guards.READ, False),
    ("explain analyze select 1", guards.READ, False),
    ("insert into t (a) values (1)", guards.WRITE, False),
    ("update t set a = 1 where id = 2", guards.WRITE, False),
    ("update t set a = 1", guards.WRITE, True),
    ("update t set a = 1 from y where t.id = y.id", guards.WRITE, False),
    ("delete from t where id = 1", guards.WRITE, False),
    ("delete from public.t", guards.WRITE, True),
    ("with c as (select 1) delete from t using c where t.id = c.id", guards.WRITE, False),
    ("drop table t", guards.DDL, False),
    ("truncate table t", guards.DDL, False),
    ("alter table t add column c int", guards.DDL, False),
    ("create table t (a int)", guards.DDL, False),
    ("create index ix on t (a)", guards.DDL, False),
    ("grant select on t to u", guards.DDL, False),
    ("vacuum analyze t", guards.DDL, False),
    ("set statement_timeout = 100", guards.DDL, False),
    ("refresh materialized view mv", guards.DDL, False),
    # EXPLAIN ANALYZE de un DELETE si ejecuta el DELETE: no puede pasar como lectura.
    ("explain analyze delete from t", guards.WRITE, True),
    ("explain analyze delete from t where id = 1", guards.WRITE, False),
]


@pytest.mark.parametrize("sql,kind,needs_where", CLASIFICACION)
def test_clasificacion(sql, kind, needs_where):
    statements = guards.analyze(sql)
    assert len(statements) == 1
    assert statements[0].kind == kind
    assert statements[0].needs_where is needs_where


# Los casos por los que existe el parseo real en vez de str.contains.
TRAMPAS = [
    # WHERE dentro de un comentario: NO cuenta como WHERE.
    ("-- where id = 1\ndelete from t", True),
    ("/* where 1=1 */ delete from t", True),
    ("delete from t -- where id = 1", True),
    # WHERE dentro de un string literal: NO cuenta como WHERE.
    ("update t set nota = 'where id = 1'", True),
    ("delete from t where nota = 'where'", False),
    ("update t set nota = 'no where aqui' where id = 1", False),
]


@pytest.mark.parametrize("sql,sin_where", TRAMPAS)
def test_where_en_comentario_o_literal(sql, sin_where):
    statements = guards.analyze(sql)
    assert statements[0].needs_where is sin_where


def test_multiples_sentencias():
    statements = guards.analyze("delete from t where id=1; delete from y")
    assert [s.needs_where for s in statements] == [False, True]


def test_sql_vacio():
    with pytest.raises(SqlParseError, match="vacio"):
        guards.analyze("   ")


def test_sql_no_parseable_falla_cerrado():
    with pytest.raises(SqlParseError):
        guards.analyze("select from where )))(((")


def test_read_only_rechaza_escritura():
    """Criterio 21: contra un alias READ_ONLY toda escritura falla."""
    with pytest.raises(ReadOnlyError, match="READ_ONLY"):
        guards.assert_allowed(
            "insert into t (a) values (1)", read_only=True, allow_ddl=False, alias="local"
        )


def test_read_only_permite_lectura():
    """Criterio 21: ninguna lectura se ve afectada."""
    for sql in ("select 1", "with c as (select 1) select * from c", "show search_path"):
        assert guards.assert_allowed(sql, read_only=True, allow_ddl=False, alias="local")


def test_read_only_menciona_el_alias_de_escritura():
    with pytest.raises(ReadOnlyError) as excinfo:
        guards.assert_allowed("delete from t where a=1", read_only=True, allow_ddl=False, alias="local")
    assert "local_rw" in str(excinfo.value)


def test_delete_sin_where_falla():
    """Criterio 20: execute_sql('delete from public.t') falla."""
    with pytest.raises(FullTableOperationError, match="sin WHERE"):
        guards.assert_allowed("delete from public.t", read_only=False, allow_ddl=False)


def test_delete_sin_where_con_allow_full_table_procede():
    """Criterio 20: con allow_full_table=True procede."""
    statements = guards.assert_allowed(
        "delete from public.t", read_only=False, allow_ddl=False, allow_full_table=True
    )
    assert statements[0].kind == guards.WRITE


def test_update_sin_where_falla():
    with pytest.raises(FullTableOperationError):
        guards.assert_allowed("update t set a = 1", read_only=False, allow_ddl=False)


@pytest.mark.parametrize(
    "sql", ["drop table t", "truncate table t", "alter table t add column c int", "create table t (a int)"]
)
def test_ddl_requiere_allow_ddl(sql):
    with pytest.raises(DDLNotAllowedError, match="ALLOW_DDL"):
        guards.assert_allowed(sql, read_only=False, allow_ddl=False, alias="local_rw")
    assert guards.assert_allowed(sql, read_only=False, allow_ddl=True, alias="local_rw")


def test_sentencia_no_clasificable_se_trata_como_ddl():
    with pytest.raises(DDLNotAllowedError, match="clasificar|DDL"):
        guards.assert_allowed("call mi_proc()", read_only=False, allow_ddl=False)


def test_is_read():
    assert guards.is_read("select 1")
    assert guards.is_read("with c as (select 1) select * from c")
    assert not guards.is_read("delete from t where a = 1")
    assert not guards.is_read("select 1; delete from t where a = 1")


def test_una_sentencia_mala_en_lote_falla_todo():
    """Si cualquiera de las sentencias no pasa, se rechaza el lote completo."""
    with pytest.raises(FullTableOperationError):
        guards.assert_allowed(
            "delete from t where id = 1; delete from y", read_only=False, allow_ddl=False
        )
