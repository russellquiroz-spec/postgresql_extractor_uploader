from __future__ import annotations

from postgres_local_client import engine as engine_mod
from postgres_local_client.types import PostgresConfig, SSHConfig


def _cfg(**overrides) -> PostgresConfig:
    base = dict(
        alias="local_rw",
        host="localhost",
        port=9553,
        dbname="base-con-guion",
        user="usuario_bd",
        password="pa(ss)+wo|rd$",
        ssh=SSHConfig(host="h", user="u", pkey_path="k"),
        schema="public",
        read_only=False,
        allow_ddl=False,
        statement_timeout_s=None,
    )
    base.update(overrides)
    return PostgresConfig(**base)


def test_url_create_escapa_password_con_caracteres_especiales():
    """
    Criterio 6: el password de la VM trae ( ) + | $.

    Con URL.create el escaping es automatico; por concatenacion la URL se parsearia
    mal y el error no apuntaria a la causa.
    """
    url = engine_mod.build_url(_cfg(), 54321)
    assert url.password == "pa(ss)+wo|rd$"
    assert url.database == "base-con-guion"
    assert url.host == "127.0.0.1"
    assert url.port == 54321
    assert url.drivername == "postgresql+psycopg"
    # En la forma renderizada el password va escapado, y al volver a parsearla se
    # recupera identico: es lo que garantiza que psycopg reciba el password correcto.
    import sqlalchemy as sa

    rendered = url.render_as_string(hide_password=False)
    assert "pa(ss)+wo|rd$" not in rendered, "el password debe ir escapado en la URL"
    assert "%28" in rendered and "%29" in rendered and "%7C" in rendered and "%24" in rendered
    assert sa.engine.make_url(rendered).password == "pa(ss)+wo|rd$"


def test_str_de_la_url_oculta_el_password():
    """Criterio 23: la URL nunca se expone completa."""
    url = engine_mod.build_url(_cfg(), 54321)
    assert "pa(ss)+wo|rd$" not in str(url)
    assert "***" in str(url)


def test_nombre_de_base_con_guion_no_necesita_escape_en_la_url():
    """Seccion 6.5.2: un nombre de base con guion va tal cual en la URL."""
    url = engine_mod.build_url(_cfg(dbname="base-con-guion"), 1234)
    assert url.database == "base-con-guion"


def test_server_options_read_only_y_timeout():
    options = engine_mod.server_options(_cfg(read_only=True, statement_timeout_s=300))
    assert "-c statement_timeout=300000" in options
    assert "-c default_transaction_read_only=on" in options
    assert "-c search_path=public" in options


def test_server_options_sin_read_only():
    options = engine_mod.server_options(_cfg(read_only=False, statement_timeout_s=None))
    assert "default_transaction_read_only" not in options
    assert "statement_timeout" not in options


def test_server_options_schema_no_public_agrega_public():
    options = engine_mod.server_options(_cfg(schema="staging"))
    assert "-c search_path=staging,public" in options


def test_engine_se_cachea_por_alias_y_puerto():
    cfg = _cfg()
    primero = engine_mod.get_engine(cfg, 1111)
    segundo = engine_mod.get_engine(cfg, 1111)
    tercero = engine_mod.get_engine(cfg, 2222)
    assert primero is segundo
    assert primero is not tercero
    engine_mod.dispose_all()


def test_dispose_for_port_solo_afecta_ese_puerto():
    cfg = _cfg()
    uno = engine_mod.get_engine(cfg, 3333)
    engine_mod.get_engine(cfg, 4444)
    engine_mod.dispose_for_port(3333)
    assert engine_mod.get_engine(cfg, 3333) is not uno
    engine_mod.dispose_all()


def test_quoting_de_identificadores():
    """Seccion 6.5.2: un nombre con guion debe ir entre comillas dobles en el SQL."""
    from sqlalchemy.dialects import postgresql

    preparer = postgresql.dialect().identifier_preparer
    assert preparer.quote("base-con-guion") == '"base-con-guion"'
    assert preparer.quote("ventas") == "ventas"
    assert preparer.quote("select") == '"select"'
