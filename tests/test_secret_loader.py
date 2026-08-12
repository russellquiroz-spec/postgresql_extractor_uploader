from __future__ import annotations

import json

import pytest

from postgres_local_client import secret_loader as sl

# Criterio 4: los tres formatos documentados, mas JSON anidado y escapado.
FORMATOS = [
    ('{"user":"db_user","password":"db_password"}', ("db_user", "db_password")),
    ("USER=db_user;PASSWORD=db_password", ("db_user", "db_password")),
    ("db_user:db_password", ("db_user", "db_password")),
    # JSON con campos extra
    (
        '{"engine":"postgres","user":"db_user","password":"db_password","port":5432}',
        ("db_user", "db_password"),
    ),
    # JSON anidado
    (
        '{"credentials":{"nested":{"username":"db_user","pwd":"db_password"}}}',
        ("db_user", "db_password"),
    ),
    # JSON escapado
    ('"{\\"user\\":\\"db_user\\",\\"password\\":\\"db_password\\"}"', ("db_user", "db_password")),
    # JSON envuelto en comillas simples
    ("'{\"user\":\"db_user\",\"password\":\"db_password\"}'", ("db_user", "db_password")),
    # pares con salto de linea y comillas
    ('user="db_user"\npassword="db_password"', ("db_user", "db_password")),
    # separador por pipe
    ("db_user|db_password", ("db_user", "db_password")),
]


@pytest.mark.parametrize("raw,esperado", FORMATOS)
def test_parse_credentials_secret(raw, esperado):
    assert sl.parse_credentials_secret(raw) == esperado


def test_password_con_caracteres_especiales():
    """Criterio 6: el password real de la VM trae ( ) + | $."""
    raw = json.dumps({"user": "usuario_bd", "password": "pa(ss)+wo|rd$"})
    assert sl.parse_credentials_secret(raw) == ("usuario_bd", "pa(ss)+wo|rd$")


def test_password_con_dos_puntos_via_json():
    """El formato user:password es ambiguo si el password trae ':'; JSON no."""
    raw = json.dumps({"user": "u", "password": "a:b:c"})
    assert sl.parse_credentials_secret(raw) == ("u", "a:b:c")


def test_formato_invalido_devuelve_none():
    assert sl.parse_credentials_secret("solo-un-texto-sin-separador") is None


def test_resolve_secret_reference_variable_ausente(monkeypatch):
    monkeypatch.delenv("PGC_TEST_NO_EXISTE", raising=False)
    with pytest.raises(ValueError, match="no existe o esta vacia"):
        sl.resolve_secret_reference("PGC_TEST_NO_EXISTE")


def test_resolve_secret_reference_formato_invalido(monkeypatch):
    monkeypatch.setenv("PGC_TEST_MALO", "no-parseable")
    with pytest.raises(ValueError, match="formato valido"):
        sl.resolve_secret_reference("PGC_TEST_MALO")


def test_resolve_secret_reference_desde_variable(monkeypatch):
    monkeypatch.setenv("PGC_TEST_OK", "USER=u1;PASSWORD=p1")
    assert sl.resolve_secret_reference("PGC_TEST_OK") == ("u1", "p1")


def test_normalize_plain_secret_quita_comillas_y_escapes():
    assert sl.normalize_plain_secret('  "valor"  ') == "valor"
    assert sl.normalize_plain_secret("'valor'") == "valor"
    assert sl.normalize_plain_secret(r'va\"lor') == 'va"lor'


def test_keyring_manager_sin_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    assert sl.resolve_secret_reference_from_keyring_manager("CUALQUIERA") is None


def test_keyring_manager_archivo_con_formato_invalido(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData"
    (appdata / "KeyringManager").mkdir(parents=True)
    (appdata / "KeyringManager" / "credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    with pytest.raises(ValueError, match="formato esperado"):
        sl.resolve_secret_reference_from_keyring_manager("X")


def test_keyring_manager_entrada_no_encontrada(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData"
    (appdata / "KeyringManager").mkdir(parents=True)
    (appdata / "KeyringManager" / "credentials.json").write_text(
        json.dumps([{"env_var": "OTRA", "usuario": "u", "service": "s"}]), encoding="utf-8"
    )
    monkeypatch.setenv("APPDATA", str(appdata))
    assert sl.resolve_secret_reference_from_keyring_manager("LA_QUE_BUSCO") is None


def test_contrato_identico_al_de_redshift_extractor():
    """
    La decision fue copiar el modulo sin tocar el contrato de parseo.

    Si existe la libreria hermana en la maquina, se compara el archivo: la unica
    diferencia permitida es el nombre de la libreria en el mensaje de la dependencia
    faltante de keyring.
    """
    from pathlib import Path

    hermana = (
        Path(__file__).resolve().parents[2]
        / "redshift_extractor"
        / "src"
        / "redshift_extractor"
        / "secret_loader.py"
    )
    if not hermana.exists():
        pytest.skip("redshift_extractor no esta disponible en esta maquina.")

    propio = Path(sl.__file__).read_text(encoding="utf-8").splitlines()
    ajeno = hermana.read_text(encoding="utf-8").splitlines()
    diferencias = [
        (a, b) for a, b in zip(ajeno, propio, strict=False) if a != b
    ]
    assert len(ajeno) == len(propio), "el modulo copiado cambio de tamano"
    assert len(diferencias) == 1, f"se esperaba 1 linea distinta, hay {len(diferencias)}"
    assert "redshift_extractor" in diferencias[0][0]
    assert "postgres_local_client" in diferencias[0][1]
