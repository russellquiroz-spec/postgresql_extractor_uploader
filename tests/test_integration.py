"""
Integracion contra la base real de la VM, a traves del tunel SSH.

Los valores concretos (base, usuario) se leen de la config con `describe_database()` en
vez de estar escritos como literales: asi el test no depende de un entorno en particular.

Se salta completo si la VM no responde. Todo lo que escriben estos tests vive en un
esquema desechable (`pytest_tmp`) que un fixture crea y destruye con
`DROP SCHEMA ... CASCADE`: `public` no se toca ni por accidente.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from postgres_local_client import (
    delete_where,
    describe_database,
    describe_table,
    execute_sql,
    extract_sql,
    list_databases,
    list_schemas,
    list_tables,
    load_dataframe,
    ping,
    table_exists,
    transaction,
    upsert_dataframe,
)
from postgres_local_client.errors import (
    DDLNotAllowedError,
    FullTableOperationError,
    GuardError,
    ReadOnlyError,
    SchemaMismatchError,
    UpsertTargetError,
)

pytestmark = pytest.mark.integration

#: Las tablas de prueba viven en el esquema desechable, no en public.
ESQUEMA = "pytest_tmp"


# -----------------------------------------------------------------------------
# Conexion
# -----------------------------------------------------------------------------
def test_ping_sin_tunel_manual(real_env):
    """Criterio 2: ok=True desde una maquina local, sin tunel abierto a mano."""
    info = describe_database("local")
    resultado = ping("local")
    assert resultado["ok"] is True
    # Contra la config, no contra literales: `ping` reporta lo que ve el servidor, asi
    # que esto verifica que el tunel llego a la base que la config dice.
    assert resultado["database"] == info["dbname"]
    assert resultado["user"] == info["user"]
    assert resultado["tunnel_port"] > 0
    assert "PostgreSQL 17" in resultado["server_version"]
    assert resultado["latency_ms"] > 0


def test_extract_sql_devuelve_dataframe_1x1(real_env):
    """Criterio 3: extract_sql('select 1 as test;') con DEFAULT_DB."""
    df = extract_sql("select 1 as test;")
    assert df.shape == (1, 1)
    assert df.columns.tolist() == ["test"]
    assert df.iloc[0, 0] == 1


def test_list_databases_y_schemas(real_env):
    """Criterio 1 contra la config real."""
    assert list_databases() == ["local", "local_rw"]
    assert "public" in list_schemas("local")


def test_credenciales_con_caracteres_especiales_conectan(real_env):
    """
    Criterio 6: el password de la VM trae ( ) + | $ y la conexion funciona.

    Si la URL se armara por concatenacion en vez de con URL.create, esto fallaria con
    un error que no apunta a la causa.
    """
    assert ping("local")["ok"] is True


# -----------------------------------------------------------------------------
# Lectura
# -----------------------------------------------------------------------------
def test_query_tiene_prioridad_sobre_query_file(real_env, tmp_path):
    """Criterio 16."""
    archivo = tmp_path / "otro.sql"
    archivo.write_text("select 999 as desde_archivo", encoding="utf-8")

    df = extract_sql("select 1 as desde_query", query_file=str(archivo))
    assert df.columns.tolist() == ["desde_query"]

    df_archivo = extract_sql(query_file=str(archivo))
    assert df_archivo.columns.tolist() == ["desde_archivo"]
    assert df_archivo.iloc[0, 0] == 999


def test_query_file_inexistente(real_env):
    with pytest.raises(FileNotFoundError):
        extract_sql(query_file="no-existe-este-archivo.sql")


def test_params_se_enlazan_por_bindparams(real_env):
    """
    Los parametros van por bindparams, no interpolados en el texto.

    Nota: no usar `:n::int` — el `::` de cast confunde al parser de bindparams de
    SQLAlchemy y el parametro se queda literal. Se usa `cast(:n as int)`.
    """
    df = extract_sql(
        "select cast(:n as int) as numero, :texto as texto",
        params={"n": 42, "texto": "hola'; drop table x --"},
    )
    assert df.iloc[0]["numero"] == 42
    assert df.iloc[0]["texto"] == "hola'; drop table x --"


def test_guarda_csv_y_parquet_y_devuelve_dataframe(real_env, tmp_path):
    """Criterio 15."""
    df = extract_sql(
        "select generate_series(1,5) as n",
        save_dir=str(tmp_path),
        base_name="prueba",
        save_csv=True,
        save_parquet=True,
    )
    assert len(df) == 5
    assert (tmp_path / "prueba.csv").exists()
    assert (tmp_path / "prueba.parquet").exists()
    assert pd.read_parquet(tmp_path / "prueba.parquet").equals(df)


def test_chunksize_devuelve_el_mismo_dataframe(real_env):
    completo = extract_sql("select generate_series(1,1000) as n")
    por_lotes = extract_sql("select generate_series(1,1000) as n", chunksize=100)
    assert completo.equals(por_lotes)
    assert len(por_lotes) == 1000


def test_base_name_por_defecto(real_env, tmp_path):
    extract_sql("select 1 as x", save_dir=str(tmp_path), save_csv=True)
    generados = list(tmp_path.glob("*.csv"))
    assert len(generados) == 1
    assert generados[0].name.startswith(f"local_{describe_database('local')['dbname']}_")


# -----------------------------------------------------------------------------
# Guardas contra la base real
# -----------------------------------------------------------------------------
def test_alias_read_only_rechaza_escrituras_y_no_afecta_lecturas(real_env):
    """Criterio 21."""
    with pytest.raises(ReadOnlyError):
        execute_sql("create table public.pytest_no_deberia (a int)", db="local")
    with pytest.raises(ReadOnlyError):
        delete_where("cualquiera", "1=1", db="local")
    with pytest.raises(ReadOnlyError):
        load_dataframe(pd.DataFrame({"a": [1]}), "cualquiera", db="local")

    # Las lecturas siguen intactas.
    assert extract_sql("select 1 as ok", db="local").iloc[0, 0] == 1


def test_read_only_tambien_lo_hace_cumplir_el_servidor(real_env, tmp_table):
    """
    Defensa en profundidad: ademas de las guardas, la sesion va con
    default_transaction_read_only=on.
    """
    import sqlalchemy as sa

    from postgres_local_client import config as config_mod
    from postgres_local_client import engine as engine_mod
    from postgres_local_client.tunnel import ensure_tunnel

    tabla = tmp_table("a int")
    _app, cfg = config_mod.resolve("local")
    info = ensure_tunnel(cfg)
    engine = engine_mod.get_engine(cfg, info.local_port)
    with engine.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError) as excinfo:
            conn.execute(sa.text(f"insert into {ESQUEMA}.{tabla} values (1)"))
    assert "read-only" in str(excinfo.value).lower() or "solo lectura" in str(excinfo.value).lower()


def test_delete_sin_where_falla_y_con_allow_full_table_procede(real_env, tmp_table):
    """Criterio 20."""
    tabla = tmp_table("a int")
    execute_sql(f"insert into {ESQUEMA}.{tabla} values (1),(2),(3)", db="pytest_rw")

    with pytest.raises(FullTableOperationError):
        execute_sql(f"delete from {ESQUEMA}.{tabla}", db="pytest_rw")
    assert extract_sql(f"select count(*) as n from {ESQUEMA}.{tabla}", db="pytest_rw").iloc[0, 0] == 3

    afectadas = execute_sql(
        f"delete from {ESQUEMA}.{tabla}", db="pytest_rw", allow_full_table=True
    )
    assert afectadas == 3
    assert extract_sql(f"select count(*) as n from {ESQUEMA}.{tabla}", db="pytest_rw").iloc[0, 0] == 0


def test_delete_where_obligatorio(real_env, tmp_table):
    tabla = tmp_table("a int")
    with pytest.raises(ValueError, match="obligatorio"):
        delete_where(tabla, "", db="pytest_rw")
    with pytest.raises(ValueError, match="obligatorio"):
        delete_where(tabla, "   ", db="pytest_rw")


def test_delete_where_borra_solo_lo_filtrado(real_env, tmp_table):
    tabla = tmp_table("a int, etiqueta text")
    execute_sql(
        f"insert into {ESQUEMA}.{tabla} values (1,'si'),(2,'no'),(3,'si')", db="pytest_rw"
    )
    borradas = delete_where(
        tabla, "etiqueta = :etiqueta", {"etiqueta": "si"}, db="pytest_rw"
    )
    assert borradas == 2
    quedan = extract_sql(f"select a from {ESQUEMA}.{tabla}", db="pytest_rw")
    assert quedan["a"].tolist() == [2]


def test_delete_where_con_punto_y_coma_se_rechaza(real_env, tmp_table):
    tabla = tmp_table("a int")
    with pytest.raises(GuardError, match="mas de una sentencia|DELETE"):
        delete_where(tabla, "1=1; drop table public.otra", db="pytest_rw")


def test_ddl_requiere_allow_ddl(real_env):
    """El alias local_rw escribe pero no hace DDL."""
    with pytest.raises(DDLNotAllowedError):
        execute_sql("create table public.pytest_no (a int)", db="local_rw")


# -----------------------------------------------------------------------------
# Carga
# -----------------------------------------------------------------------------
def test_load_dataframe_100k_filas_por_copy(real_env, tmp_table):
    """Criterio 17: 100k filas via COPY en menos de 10s, con el conteo exacto."""
    tabla = tmp_table("a bigint, b double precision, c text")
    rng = np.random.default_rng(0)
    df = pd.DataFrame(
        {
            "a": np.arange(100_000, dtype="int64"),
            "b": rng.random(100_000),
            "c": [f"fila {i}" for i in range(100_000)],
        }
    )

    inicio = time.perf_counter()
    escritas = load_dataframe(df, tabla, db="pytest_rw", method="copy")
    transcurrido = time.perf_counter() - inicio

    assert escritas == 100_000
    # El limite es el enlace, no el codigo: la conversion pandas -> Python son ~10 ms
    # y sshtunnel reenvia en trozos de 1 KB. Con SSH_COMPRESSION=true el margen sobre
    # los 10 s del criterio es amplio; si esto falla, revisa la compresion y la latencia.
    filas_por_segundo = escritas / transcurrido
    assert transcurrido < 10, (
        f"COPY de 100k filas tardo {transcurrido:.2f}s "
        f"({filas_por_segundo:,.0f} filas/s). Revisa SSH_COMPRESSION y la latencia al bastion."
    )
    assert extract_sql(
        f"select count(*) as n from {ESQUEMA}.{tabla}", db="pytest_rw"
    ).iloc[0, 0] == 100_000


def test_load_dataframe_valida_columnas_antes_de_escribir(real_env, tmp_table):
    """Criterio 18: falla antes de escribir una sola fila."""
    tabla = tmp_table("a int, b text")
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"], "columna_fantasma": [9, 9]})

    with pytest.raises(SchemaMismatchError) as excinfo:
        load_dataframe(df, tabla, db="pytest_rw")

    mensaje = str(excinfo.value)
    assert "columna_fantasma" in mensaje
    assert "'a', 'b'" in mensaje or "['a', 'b']" in mensaje
    # Ni una fila escrita.
    assert extract_sql(f"select count(*) as n from {ESQUEMA}.{tabla}", db="pytest_rw").iloc[0, 0] == 0


def test_load_dataframe_preserva_nulos_y_strings_vacios(real_env, tmp_table):
    tabla = tmp_table("i bigint, t text, f double precision, d timestamp")
    df = pd.DataFrame(
        {
            "i": pd.Series([1, None, 3], dtype="Int64"),
            "t": ["a", "", None],
            "f": [1.5, np.nan, 3.0],
            "d": pd.to_datetime(["2026-01-01", None, "2026-03-05"]),
        }
    )
    assert load_dataframe(df, tabla, db="pytest_rw") == 3

    leido = extract_sql(
        f"select count(*) filter (where t = '') as vacios, "
        f"count(*) filter (where t is null) as nulos, "
        f"count(*) filter (where i is null) as i_nulos, "
        f"count(*) filter (where d is null) as d_nulos "
        f"from {ESQUEMA}.{tabla}",
        db="pytest_rw",
    )
    assert leido.iloc[0]["vacios"] == 1, "un string vacio no debe convertirse en NULL"
    assert leido.iloc[0]["nulos"] == 1
    assert leido.iloc[0]["i_nulos"] == 1
    assert leido.iloc[0]["d_nulos"] == 1


def test_load_dataframe_method_multi(real_env, tmp_table):
    tabla = tmp_table("a int, b text")
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    assert load_dataframe(df, tabla, db="pytest_rw", method="multi") == 3


def test_load_dataframe_replace_requiere_confirm(real_env, tmp_table):
    tabla = tmp_table("a int")
    df = pd.DataFrame({"a": [1, 2]})
    load_dataframe(df, tabla, db="pytest_rw")

    with pytest.raises(GuardError, match="confirm=True"):
        load_dataframe(df, tabla, db="pytest_rw", if_exists="replace")
    assert extract_sql(f"select count(*) as n from {ESQUEMA}.{tabla}", db="pytest_rw").iloc[0, 0] == 2

    load_dataframe(df, tabla, db="pytest_rw", if_exists="replace", confirm=True)
    assert extract_sql(f"select count(*) as n from {ESQUEMA}.{tabla}", db="pytest_rw").iloc[0, 0] == 2


def test_load_dataframe_fail_si_existe(real_env, tmp_table):
    tabla = tmp_table("a int")
    with pytest.raises(SchemaMismatchError, match="ya existe"):
        load_dataframe(pd.DataFrame({"a": [1]}), tabla, db="pytest_rw", if_exists="fail")


def test_load_dataframe_tabla_inexistente_sin_allow_ddl(real_env):
    with pytest.raises(DDLNotAllowedError, match="ALLOW_DDL"):
        load_dataframe(pd.DataFrame({"a": [1]}), "pytest_tmp_no_existe_jamas", db="local_rw")


def test_load_dataframe_vacio_no_escribe(real_env, tmp_table):
    tabla = tmp_table("a int")
    assert load_dataframe(pd.DataFrame({"a": []}), tabla, db="pytest_rw") == 0


# -----------------------------------------------------------------------------
# Upsert
# -----------------------------------------------------------------------------
def test_upsert_reporta_insertadas_y_actualizadas(real_env, tmp_table):
    """Criterio 19."""
    tabla = tmp_table("id int primary key, valor text")
    load_dataframe(pd.DataFrame({"id": [1, 2], "valor": ["viejo", "viejo"]}), tabla, db="pytest_rw")

    resultado = upsert_dataframe(
        pd.DataFrame({"id": [1, 3, 4], "valor": ["nuevo", "nuevo", "nuevo"]}),
        tabla,
        ["id"],
        db="pytest_rw",
    )
    assert resultado == {"inserted": 2, "updated": 1}

    final = extract_sql(f"select id, valor from {ESQUEMA}.{tabla} order by id", db="pytest_rw")
    assert final["id"].tolist() == [1, 2, 3, 4]
    assert final["valor"].tolist() == ["nuevo", "viejo", "nuevo", "nuevo"]


def test_upsert_sin_indice_unico_falla_con_mensaje_claro(real_env, tmp_table):
    tabla = tmp_table("id int, valor text")
    with pytest.raises(UpsertTargetError) as excinfo:
        upsert_dataframe(
            pd.DataFrame({"id": [1], "valor": ["x"]}), tabla, ["id"], db="pytest_rw"
        )
    assert "indice unico" in str(excinfo.value)


def test_upsert_con_update_cols_explicito(real_env, tmp_table):
    tabla = tmp_table("id int primary key, a text, b text")
    load_dataframe(pd.DataFrame({"id": [1], "a": ["vieja"], "b": ["vieja"]}), tabla, db="pytest_rw")
    upsert_dataframe(
        pd.DataFrame({"id": [1], "a": ["nueva"], "b": ["nueva"]}),
        tabla,
        ["id"],
        update_cols=["a"],
        db="pytest_rw",
    )
    fila = extract_sql(f"select a, b from {ESQUEMA}.{tabla}", db="pytest_rw").iloc[0]
    assert fila["a"] == "nueva"
    assert fila["b"] == "vieja", "b no estaba en update_cols y no debio cambiar"


def test_upsert_columna_de_conflicto_ausente_en_el_dataframe(real_env, tmp_table):
    tabla = tmp_table("id int primary key, valor text")
    with pytest.raises(SchemaMismatchError, match="conflicto"):
        upsert_dataframe(pd.DataFrame({"valor": ["x"]}), tabla, ["id"], db="pytest_rw")


def test_upsert_indice_unico_compuesto(real_env, tmp_table):
    tabla = tmp_table("a int, b int, v text, unique (a, b)")
    load_dataframe(pd.DataFrame({"a": [1], "b": [1], "v": ["viejo"]}), tabla, db="pytest_rw")
    resultado = upsert_dataframe(
        pd.DataFrame({"a": [1, 2], "b": [1, 2], "v": ["nuevo", "nuevo"]}),
        tabla,
        ["a", "b"],
        db="pytest_rw",
    )
    assert resultado == {"inserted": 1, "updated": 1}


# -----------------------------------------------------------------------------
# Transacciones
# -----------------------------------------------------------------------------
def test_transaccion_hace_rollback_completo(real_env, tmp_table):
    """Criterio 22: una excepcion a media transaccion deja la base sin cambios."""
    tabla = tmp_table("a int")
    execute_sql(f"insert into {ESQUEMA}.{tabla} values (1)", db="pytest_rw")

    with pytest.raises(RuntimeError, match="falla a proposito"):
        with transaction(db="pytest_rw") as tx:
            tx.execute_sql(f"insert into {ESQUEMA}.{tabla} values (2)")
            tx.load_dataframe(pd.DataFrame({"a": [3, 4]}), tabla)
            assert tx.extract_sql(f"select count(*) as n from {ESQUEMA}.{tabla}").iloc[0, 0] == 4
            raise RuntimeError("falla a proposito")

    quedan = extract_sql(f"select a from {ESQUEMA}.{tabla} order by a", db="pytest_rw")
    assert quedan["a"].tolist() == [1], "el rollback no revirtio todo"


def test_transaccion_commitea_al_salir_bien(real_env, tmp_table):
    tabla = tmp_table("a int")
    with transaction(db="pytest_rw") as tx:
        tx.execute_sql(f"insert into {ESQUEMA}.{tabla} values (1)")
        tx.load_dataframe(pd.DataFrame({"a": [2, 3]}), tabla)
    assert extract_sql(f"select count(*) as n from {ESQUEMA}.{tabla}", db="pytest_rw").iloc[0, 0] == 3


def test_transaccion_emite_rollback_con_la_operacion_fallida(real_env, tmp_table, events_log):
    collected, collect = events_log
    tabla = tmp_table("a int")
    with pytest.raises(RuntimeError):
        with transaction(db="pytest_rw", on_event=collect) as tx:
            tx.load_dataframe(pd.DataFrame({"a": [1]}), tabla)
            raise RuntimeError("boom")

    rollbacks = [event for event in collected if event["event"] == "tx_rollback"]
    assert rollbacks
    assert rollbacks[0]["level"] == "ERROR"
    assert "load_dataframe" in str(rollbacks[0]["operation"])


def test_transaccion_respeta_las_guardas(real_env, tmp_table):
    tabla = tmp_table("a int")
    with pytest.raises(FullTableOperationError):
        with transaction(db="pytest_rw") as tx:
            tx.execute_sql(f"delete from {ESQUEMA}.{tabla}")


# -----------------------------------------------------------------------------
# Utilidades de esquema
# -----------------------------------------------------------------------------
def test_list_tables(real_env, tmp_table):
    tabla = tmp_table("a int")
    frame = list_tables("pytest_rw")
    assert "nombre" in frame.columns
    assert "tipo" in frame.columns
    assert "tamano" in frame.columns
    assert tabla in frame["nombre"].tolist()


def test_describe_table(real_env, tmp_table):
    tabla = tmp_table("id int primary key, nombre text not null, monto numeric(10,2)")
    frame = describe_table(tabla, "pytest_rw")
    assert frame["column_name"].tolist() == ["id", "nombre", "monto"]
    assert frame.set_index("column_name").loc["id", "es_pk"]
    assert not frame.set_index("column_name").loc["nombre", "es_pk"]
    assert not frame.set_index("column_name").loc["nombre", "is_nullable"]


def test_table_exists(real_env, tmp_table):
    tabla = tmp_table("a int")
    assert table_exists(tabla, "pytest_rw") is True
    assert table_exists("pytest_tmp_no_existe_jamas", "pytest_rw") is False


def test_describe_table_inexistente(real_env):
    with pytest.raises(ValueError, match="no existe"):
        describe_table("pytest_tmp_no_existe_jamas", "local")


# -----------------------------------------------------------------------------
# Criterio 23: nada de credenciales en los eventos
# -----------------------------------------------------------------------------
def test_ningun_evento_contiene_credenciales(real_env, tmp_table, events_log):
    """
    Criterio 23: cero coincidencias del password de BD y del password SSH en la
    salida de eventos.
    """
    from postgres_local_client import config as config_mod

    collected, collect = events_log
    _app, ssh, pg_map = config_mod.load_config()
    secretos = [pg_map["local"].password, ssh.password]
    secretos = [secreto for secreto in secretos if secreto]
    assert secretos, "no hay credenciales que verificar"

    tabla = tmp_table("a int")
    ping("local", on_event=collect)
    extract_sql("select 1 as x", db="local", on_event=collect)
    load_dataframe(pd.DataFrame({"a": [1, 2]}), tabla, db="pytest_rw", on_event=collect)
    upsert_result = None
    try:
        upsert_result = upsert_dataframe(
            pd.DataFrame({"a": [1]}), tabla, ["a"], db="pytest_rw", on_event=collect
        )
    except UpsertTargetError:
        pass
    assert upsert_result is None or upsert_result

    texto = "\n".join(repr(event) for event in collected)
    for secreto in secretos:
        assert secreto not in texto, "una credencial aparecio en un evento"
    assert "postgresql+psycopg://" not in texto, "la URL completa no debe aparecer"
