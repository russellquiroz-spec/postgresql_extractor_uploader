from __future__ import annotations

import pytest
from typer.testing import CliRunner

from postgres_local_client.cli import EXIT_BUSINESS, EXIT_CONFIG, EXIT_OK, EXIT_TUNNEL, app, apply_limit

runner = CliRunner()

# typer 0.27 expone stdout y stderr por separado; output trae los dos, que es lo
# que interesa porque los errores de la CLI se escriben en stderr.


# -----------------------------------------------------------------------------
# apply_limit
# -----------------------------------------------------------------------------
def test_apply_limit_envuelve_selects():
    resultado = apply_limit("select * from ventas", 10)
    assert "limit 10" in resultado
    assert "select * from ventas" in resultado


def test_apply_limit_quita_punto_y_coma():
    assert apply_limit("select 1;;;", 5).count(";") == 0


def test_apply_limit_full_no_envuelve():
    assert apply_limit("select 1", None) == "select 1"


def test_apply_limit_acepta_with():
    resultado = apply_limit("with c as (select 1) select * from c", 3)
    assert "limit 3" in resultado


def test_apply_limit_rechaza_no_selects():
    """El tipo de sentencia se decide con el SQL parseado, no por la primera palabra."""
    with pytest.raises(ValueError, match="SELECT/WITH"):
        apply_limit("delete from t where a=1", 10)
    with pytest.raises(ValueError, match="SELECT/WITH"):
        apply_limit("-- select 1\ndelete from t where a=1", 10)


def test_apply_limit_vacio():
    with pytest.raises(ValueError, match="vacio"):
        apply_limit("   ", 10)


def test_apply_limit_negativo():
    with pytest.raises(ValueError, match="mayor a 0"):
        apply_limit("select 1", 0)


# -----------------------------------------------------------------------------
# Codigos de salida
# -----------------------------------------------------------------------------
def test_ls_con_env_valido(write_env, minimal_env):
    write_env(minimal_env)
    resultado = runner.invoke(app, ["ls"])
    assert resultado.exit_code == EXIT_OK
    assert "uno" in resultado.output
    assert "dos_rw" in resultado.output


def test_error_de_configuracion_da_exit_2(write_env):
    """Codigo 2: error de configuracion."""
    write_env("SSH_HOST=h\nSSH_USER=u\nSSH_PKEY_PATH=k\n")  # sin aliases
    resultado = runner.invoke(app, ["ls"])
    assert resultado.exit_code == EXIT_CONFIG
    assert "ERROR DE CONFIGURACION" in resultado.output


def test_env_con_bom_da_exit_2(write_env, minimal_env):
    write_env(minimal_env, bom=True)
    resultado = runner.invoke(app, ["ls"])
    assert resultado.exit_code == EXIT_CONFIG
    assert "BOM" in resultado.output


def test_alias_inexistente_da_exit_2(write_env, minimal_env):
    write_env(minimal_env)
    resultado = runner.invoke(app, ["describe", "--db", "fantasma"])
    assert resultado.exit_code == EXIT_CONFIG


def test_error_de_tunel_da_exit_3(write_env, minimal_env):
    """Codigo 3: error de tunel. El host del env minimo no resuelve."""
    write_env(minimal_env)
    resultado = runner.invoke(app, ["ping", "--db", "uno"])
    assert resultado.exit_code == EXIT_TUNNEL
    assert "ERROR DE TUNEL" in resultado.output


def test_error_de_negocio_da_exit_1(write_env, minimal_env):
    """Codigo 1: error de negocio (formato de salida invalido)."""
    write_env(minimal_env)
    resultado = runner.invoke(app, ["run", "--query", "select 1", "--fmt", "xml"])
    assert resultado.exit_code == EXIT_BUSINESS


def test_describe_no_muestra_credenciales(write_env, minimal_env):
    write_env(minimal_env)
    resultado = runner.invoke(app, ["describe", "--db", "dos_rw"])
    assert resultado.exit_code == EXIT_OK
    assert "pa(ss)+wo|rd$" not in resultado.output
    assert "read_only: False" in resultado.output
    assert "localhost:5432/base-con-guion" in resultado.output


def test_tunnel_status_sin_tuneles(write_env, minimal_env):
    write_env(minimal_env)
    resultado = runner.invoke(app, ["tunnel", "status"])
    assert resultado.exit_code == EXIT_OK
    assert "No hay tuneles" in resultado.output


def test_load_rechaza_alias_read_only(write_env, minimal_env, tmp_path):
    """La CLI debe rechazar cargar contra un alias read-only e indicar cual usar."""
    archivo = tmp_path / "datos.csv"
    archivo.write_text("a\n1\n", encoding="utf-8")
    write_env(minimal_env)

    resultado = runner.invoke(
        app, ["load", "--file", str(archivo), "--table", "ventas", "--db", "uno"]
    )
    assert resultado.exit_code == EXIT_BUSINESS
    assert "READ_ONLY" in resultado.output
    assert "uno_rw" in resultado.output


def test_load_extension_no_soportada(write_env, minimal_env, tmp_path):
    archivo = tmp_path / "datos.txt"
    archivo.write_text("a\n1\n", encoding="utf-8")
    write_env(minimal_env)
    resultado = runner.invoke(
        app, ["load", "--file", str(archivo), "--table", "t", "--db", "dos_rw"]
    )
    assert resultado.exit_code == EXIT_BUSINESS
    assert "Extension no soportada" in resultado.output


def test_run_file_dry_run_no_conecta(write_env, minimal_env, tmp_path):
    consulta = tmp_path / "consulta.sql"
    consulta.write_text("select 1 as uno", encoding="utf-8")
    write_env(minimal_env)

    resultado = runner.invoke(
        app, ["run-file", str(consulta), "--db", "uno", "--print-sql", "--dry-run"]
    )
    assert resultado.exit_code == EXIT_OK
    assert "DRY RUN" in resultado.output
    assert "limit 10" in resultado.output


def test_run_file_full_imprime_el_sql_completo(write_env, minimal_env, tmp_path):
    consulta = tmp_path / "consulta.sql"
    consulta.write_text("select 1 as uno", encoding="utf-8")
    write_env(minimal_env)

    resultado = runner.invoke(
        app, ["run-file", str(consulta), "--db", "uno", "--full", "--print-sql", "--dry-run"]
    )
    assert resultado.exit_code == EXIT_OK
    assert "Modo: FULL" in resultado.output
    assert "limit" not in resultado.output.lower().split("modo: full")[1]


def test_run_file_archivo_inexistente(write_env, minimal_env):
    write_env(minimal_env)
    resultado = runner.invoke(app, ["run-file", "no-existe.sql", "--db", "uno"])
    assert resultado.exit_code == EXIT_BUSINESS


def test_ayuda_lista_los_comandos():
    resultado = runner.invoke(app, ["--help"])
    assert resultado.exit_code == EXIT_OK
    for comando in ("ls", "ping", "run", "run-file", "tables", "load", "tunnel"):
        assert comando in resultado.output


def test_ayuda_de_tunnel():
    resultado = runner.invoke(app, ["tunnel", "--help"])
    assert resultado.exit_code == EXIT_OK
    for subcomando in ("status", "open", "close"):
        assert subcomando in resultado.output
