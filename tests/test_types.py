from __future__ import annotations

from datetime import datetime

import pytest

from postgres_local_client.types import (
    AppConfig,
    PostgresConfig,
    SSHConfig,
    TunnelInfo,
)


def test_ssh_config_defaults():
    ssh = SSHConfig(host="h", user="u")
    assert ssh.port == 22
    assert ssh.local_port == 0, "el default debe ser puerto efimero"
    assert ssh.auto_open is True
    assert ssh.keepalive_s == 30.0
    assert ssh.connect_timeout_s == 15.0
    assert ssh.pkey_path is None


def test_ssh_config_with_overrides():
    base = SSHConfig(host="h", user="u", pkey_path="k", port=22)
    con_override = base.with_overrides(host="otro", port=2222)
    assert con_override.host == "otro"
    assert con_override.port == 2222
    assert con_override.user == "u"
    assert con_override.pkey_path == "k"
    # Los None no sobreescriben.
    assert base.with_overrides(host=None, user=None) is base


def test_ssh_config_no_expone_secretos_en_repr():
    ssh = SSHConfig(host="h", user="u", password="clave-secreta", pkey_passphrase="frase-secreta")
    texto = repr(ssh)
    assert "clave-secreta" not in texto
    assert "frase-secreta" not in texto


def test_postgres_config_target_no_incluye_credenciales():
    cfg = PostgresConfig(
        alias="local",
        host="localhost",
        port=9553,
        dbname="base-con-guion",
        user="usuario_bd",
        password="secreto",
    )
    assert cfg.target == "localhost:9553/base-con-guion"
    assert "secreto" not in repr(cfg)
    assert "secreto" not in cfg.target


def test_postgres_config_defaults_seguros():
    cfg = PostgresConfig(
        alias="x", host="h", port=1, dbname="d", user="u", password="p"
    )
    assert cfg.read_only is True
    assert cfg.allow_ddl is False
    assert cfg.schema == "public"
    assert cfg.statement_timeout_s is None


def test_app_config_defaults():
    app = AppConfig()
    assert app.log_level == "INFO"
    assert app.output_dir == "./output"
    assert app.default_db is None


def test_tunnel_info_as_dict():
    info = TunnelInfo(
        local_port=54321,
        remote_host="localhost",
        remote_port=9553,
        ssh_host="1.2.3.4",
        ssh_user="usuario_ssh",
        opened_at=datetime(2026, 8, 12, 10, 30, 0),
        owned=True,
    )
    data = info.as_dict()
    assert data["local_port"] == 54321
    assert data["owned"] is True
    assert data["opened_at"] == "2026-08-12T10:30:00"
    # is_alive hace la verificacion real: sin nada escuchando, es False.
    assert data["is_alive"] is False


def test_tunnel_info_is_alive_sin_puerto():
    info = TunnelInfo(
        local_port=0,
        remote_host="h",
        remote_port=1,
        ssh_host="s",
        ssh_user="u",
        opened_at=datetime.now(),
        owned=False,
    )
    assert info.is_alive is False


def test_upsert_result_es_un_dict_con_las_dos_llaves():
    from postgres_local_client.types import UpsertResult

    resultado: UpsertResult = {"inserted": 3, "updated": 2}
    assert set(resultado) == {"inserted", "updated"}


@pytest.mark.parametrize("campo", ["password", "pkey_passphrase"])
def test_campos_sensibles_marcados_repr_false(campo):
    """El repr=False es lo que evita que un print(cfg) filtre credenciales."""
    campos = {field.name: field for field in SSHConfig.__dataclass_fields__.values()}
    assert campos[campo].repr is False
