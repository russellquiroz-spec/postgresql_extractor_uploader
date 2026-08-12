from __future__ import annotations

import pytest

from postgres_local_client import config as config_mod
from postgres_local_client.errors import ConfigError


def test_aliases_normalizados_a_lowercase(write_env, minimal_env):
    """Criterio 1: los aliases se listan normalizados a lowercase."""
    write_env(
        minimal_env
        + "\nPOSTGRES__MAYUSCULAS__HOST=localhost"
        + "\nPOSTGRES__MAYUSCULAS__PORT=5432"
        + "\nPOSTGRES__MAYUSCULAS__DBNAME=x"
        + "\nPOSTGRES__MAYUSCULAS__USER=u"
        + "\nPOSTGRES__MAYUSCULAS__PASSWORD=p\n"
    )
    _app, _ssh, pg_map = config_mod.load_config()
    assert sorted(pg_map) == ["dos_rw", "mayusculas", "uno"]


def test_bom_produce_error_que_menciona_bom(write_env, minimal_env):
    """Criterio 5: un env con BOM falla con un mensaje que dice BOM explicitamente."""
    write_env(minimal_env, bom=True)
    with pytest.raises(ConfigError) as excinfo:
        config_mod.load_config()
    message = str(excinfo.value)
    assert "BOM" in message
    assert "EF BB BF" in message
    assert "UTF-8" in message


def test_env_no_encontrado_menciona_las_dos_rutas(monkeypatch):
    """La seccion 6 pide indicar ambas rutas de resolucion intentadas."""
    monkeypatch.setattr(config_mod, "_SEARCH_DEPTH", 1)
    with pytest.raises(ConfigError) as excinfo:
        config_mod.find_env_file()
    message = str(excinfo.value)
    assert config_mod.ENV_FILE_OVERRIDE_VAR in message
    assert "Busqueda hacia arriba" in message


def test_override_a_archivo_inexistente_falla(monkeypatch, tmp_path):
    monkeypatch.setenv(config_mod.ENV_FILE_OVERRIDE_VAR, str(tmp_path / "no-existe"))
    with pytest.raises(ConfigError, match="inexistente"):
        config_mod.find_env_file()


def test_ssh_host_del_sistema_se_ignora(write_env, monkeypatch):
    """
    Criterio 27: un SSH_HOST suelto en el entorno del sistema no se consume.

    Es el caso real de convivencia: redshift_extractor llama load_dotenv() y deja
    SSH_HOST plano en os.environ apuntando a su propio bastion.
    """
    write_env()
    monkeypatch.setenv("SSH_HOST", "bastion-de-otra-libreria.example")
    monkeypatch.setenv("SSH_USER", "ec2-user")
    monkeypatch.setenv("SSH_PORT", "2222")

    _app, ssh, _pg = config_mod.load_config()
    assert ssh.host == "ssh.example.test"
    assert ssh.user == "tester"
    assert ssh.port == 22


def test_prefijo_pgc_si_sobreescribe(write_env, monkeypatch):
    """Criterio 27: solo PGC_SSH_HOST pisa el valor del archivo."""
    write_env()
    monkeypatch.setenv("PGC_SSH_HOST", "otro.host.test")
    monkeypatch.setenv("PGC_SSH_PORT", "2200")
    monkeypatch.setenv("PGC_DEFAULT_DB", "dos_rw")

    app, ssh, _pg = config_mod.load_config()
    assert ssh.host == "otro.host.test"
    assert ssh.port == 2200
    assert app.default_db == "dos_rw"


def test_prefijo_pgc_sobre_campos_de_alias(write_env, monkeypatch):
    write_env()
    monkeypatch.setenv("PGC_POSTGRES__uno__DBNAME", "otra-base")
    _app, _ssh, pg_map = config_mod.load_config()
    assert pg_map["uno"].dbname == "otra-base"


def test_compresion_ssh_activada_por_default(write_env, minimal_env):
    """La compresion duplica el throughput de las cargas por el tunel."""
    write_env(minimal_env)
    _app, ssh, _pg = config_mod.load_config()
    assert ssh.compression is True

    write_env(minimal_env + "\nSSH_COMPRESSION=false\n")
    _app, ssh, _pg = config_mod.load_config()
    assert ssh.compression is False


def test_defaults_seguros_read_only_y_allow_ddl(write_env):
    """Un alias sin READ_ONLY explicito es de solo lectura."""
    write_env(
        """
SSH_HOST=h
SSH_USER=u
SSH_PKEY_PATH=k
POSTGRES__min__HOST=localhost
POSTGRES__min__PORT=5432
POSTGRES__min__DBNAME=d
POSTGRES__min__USER=u
POSTGRES__min__PASSWORD=p
""".lstrip()
    )
    _app, _ssh, pg_map = config_mod.load_config()
    assert pg_map["min"].read_only is True
    assert pg_map["min"].allow_ddl is False
    assert pg_map["min"].schema == "public"


def test_credentials_env_tiene_prioridad_sobre_user_password(write_env, minimal_env, monkeypatch):
    write_env(
        minimal_env
        + "\nPOSTGRES__uno__CREDENTIALS_ENV=TEST_PGC_CREDS\n"
    )
    monkeypatch.setenv("TEST_PGC_CREDS", '{"user":"desde_env","password":"secreto_env"}')
    _app, _ssh, pg_map = config_mod.load_config()
    assert pg_map["uno"].user == "desde_env"
    assert pg_map["uno"].password == "secreto_env"


def test_ssh_credentials_env_resuelve_usuario_y_password(write_env, monkeypatch):
    write_env(
        """
SSH_HOST=h
SSH_CREDENTIALS_ENV=TEST_PGC_SSH
POSTGRES__uno__HOST=localhost
POSTGRES__uno__PORT=5432
POSTGRES__uno__DBNAME=d
POSTGRES__uno__USER=u
POSTGRES__uno__PASSWORD=p
""".lstrip()
    )
    monkeypatch.setenv("TEST_PGC_SSH", "usuario_ssh:clave$rara")
    _app, ssh, _pg = config_mod.load_config()
    assert ssh.user == "usuario_ssh"
    assert ssh.password == "clave$rara"


def test_ssh_sin_metodo_de_autenticacion_falla(write_env):
    write_env(
        """
SSH_HOST=h
SSH_USER=u
POSTGRES__uno__HOST=localhost
POSTGRES__uno__PORT=5432
POSTGRES__uno__DBNAME=d
POSTGRES__uno__USER=u
POSTGRES__uno__PASSWORD=p
""".lstrip()
    )
    with pytest.raises(ConfigError, match="autenticacion SSH"):
        config_mod.load_config()


def test_override_de_ssh_por_alias(write_env, minimal_env):
    write_env(
        minimal_env
        + "\nPOSTGRES__dos_rw__SSH_HOST=otro-bastion.test"
        + "\nPOSTGRES__dos_rw__SSH_USER=otro_user\n"
    )
    _app, ssh, pg_map = config_mod.load_config()
    assert ssh.host == "ssh.example.test"
    assert pg_map["uno"].ssh.host == "ssh.example.test"
    assert pg_map["dos_rw"].ssh.host == "otro-bastion.test"
    assert pg_map["dos_rw"].ssh.user == "otro_user"
    # Lo que no se sobreescribe se hereda del bloque global.
    assert pg_map["dos_rw"].ssh.pkey_path == "C:\\keys\\id_rsa"


def test_campo_de_alias_desconocido_falla(write_env, minimal_env):
    """Un typo como READONLY no debe pasar silenciosamente a modo lectura."""
    write_env(minimal_env + "\nPOSTGRES__uno__READONLY=false\n")
    with pytest.raises(ConfigError, match="no reconocidos"):
        config_mod.load_config()


def test_campos_requeridos_faltantes(write_env):
    write_env(
        """
SSH_HOST=h
SSH_USER=u
SSH_PKEY_PATH=k
POSTGRES__uno__HOST=localhost
POSTGRES__uno__USER=u
POSTGRES__uno__PASSWORD=p
""".lstrip()
    )
    with pytest.raises(ConfigError) as excinfo:
        config_mod.load_config()
    assert "PORT" in str(excinfo.value)
    assert "DBNAME" in str(excinfo.value)


def test_sin_aliases_falla(write_env):
    write_env("SSH_HOST=h\nSSH_USER=u\nSSH_PKEY_PATH=k\n")
    with pytest.raises(ConfigError, match="POSTGRES__"):
        config_mod.load_config()


def test_booleano_invalido_falla(write_env, minimal_env):
    write_env(minimal_env.replace("POSTGRES__uno__READ_ONLY=true", "POSTGRES__uno__READ_ONLY=si"))
    with pytest.raises(ConfigError, match="booleano"):
        config_mod.load_config()


@pytest.mark.parametrize("valor", ["true", "TRUE", "1", "yes", "on", "t"])
def test_booleanos_aceptados(write_env, minimal_env, valor):
    write_env(minimal_env.replace("POSTGRES__uno__READ_ONLY=true", f"POSTGRES__uno__READ_ONLY={valor}"))
    _app, _ssh, pg_map = config_mod.load_config()
    assert pg_map["uno"].read_only is True


def test_schema_no_identificador_falla(write_env, minimal_env):
    write_env(minimal_env + '\nPOSTGRES__uno__SCHEMA=mal esquema"\n')
    with pytest.raises(ConfigError, match="identificador simple"):
        config_mod.load_config()


def test_select_alias(write_env):
    write_env()
    app, _ssh, pg_map = config_mod.load_config()
    assert config_mod.select_alias(None, app, pg_map).alias == "uno"
    assert config_mod.select_alias("DOS_RW", app, pg_map).alias == "dos_rw"
    with pytest.raises(ConfigError, match="no existe"):
        config_mod.select_alias("fantasma", app, pg_map)


def test_select_alias_sin_default_db(write_env, minimal_env):
    write_env(minimal_env.replace("DEFAULT_DB=uno", ""))
    app, _ssh, pg_map = config_mod.load_config()
    with pytest.raises(ConfigError, match="DEFAULT_DB"):
        config_mod.select_alias(None, app, pg_map)


def test_password_no_aparece_en_repr(write_env):
    """Criterio 23: un repr accidental de la config no debe exponer credenciales."""
    write_env()
    _app, ssh, pg_map = config_mod.load_config()
    assert "pa(ss)+wo|rd$" not in repr(pg_map["dos_rw"])
    assert "pass-lectura" not in repr(pg_map["uno"])
    assert repr(ssh).count("password") == 0


def test_config_loaded_emite_evento(write_env, events_log):
    collected, collect = events_log
    write_env()
    config_mod.load_config(on_event=collect)
    nombres = [event["event"] for event in collected]
    assert "config_loaded" in nombres
    evento = next(event for event in collected if event["event"] == "config_loaded")
    assert evento["aliases"] == ["dos_rw", "uno"]
    assert evento["ssh_host"] == "ssh.example.test"
