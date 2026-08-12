from __future__ import annotations

import os
import socket
from importlib import import_module
from pathlib import Path
from typing import Callable, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_ENV_FILE = REPO_ROOT / ".env.postgres_local_client"

#: Env minimo valido para los tests unitarios. Sin credenciales reales.
MINIMAL_ENV = """
SSH_HOST=ssh.example.test
SSH_PORT=22
SSH_USER=tester
SSH_PKEY_PATH=C:\\keys\\id_rsa
SSH_LOCAL_PORT=0
DEFAULT_DB=uno

POSTGRES__uno__HOST=localhost
POSTGRES__uno__PORT=5432
POSTGRES__uno__DBNAME=base-con-guion
POSTGRES__uno__USER=lector
POSTGRES__uno__PASSWORD=pass-lectura
POSTGRES__uno__READ_ONLY=true

POSTGRES__dos_rw__HOST=localhost
POSTGRES__dos_rw__PORT=5432
POSTGRES__dos_rw__DBNAME=base-con-guion
POSTGRES__dos_rw__USER=escritor
POSTGRES__dos_rw__PASSWORD=pa(ss)+wo|rd$
POSTGRES__dos_rw__READ_ONLY=false
POSTGRES__dos_rw__ALLOW_DDL=false
""".lstrip()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch):
    """
    Deja el proceso sin estado entre tests.

    Importa: si no se borra POSTGRES_LOCAL_CLIENT_ENV_FILE, un test unitario podria
    acabar leyendo el env real del repo por la busqueda hacia arriba.
    """
    from postgres_local_client import engine as engine_mod
    from postgres_local_client import events as events_mod

    # importlib y no `from postgres_local_client import tunnel`: el paquete re-exporta
    # el context manager `tunnel`, que sombrea el submodulo del mismo nombre.
    tunnel_mod = import_module("postgres_local_client.tunnel")

    for key in [name for name in os.environ if name.startswith("PGC_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("POSTGRES_LOCAL_CLIENT_ENV_FILE", raising=False)

    yield

    try:
        tunnel_mod.close_all_tunnels()
    finally:
        engine_mod.dispose_all()
        tunnel_mod._reset_for_tests()
        events_mod.clear_secrets()


@pytest.fixture
def minimal_env() -> str:
    return MINIMAL_ENV


@pytest.fixture
def write_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Escribe un env propio en tmp y apunta la variable de override hacia el."""

    def _write(content: str = MINIMAL_ENV, *, bom: bool = False, point_to_it: bool = True) -> Path:
        path = tmp_path / ".env.postgres_local_client"
        data = content.encode("utf-8")
        if bom:
            data = b"\xef\xbb\xbf" + data
        path.write_bytes(data)
        if point_to_it:
            monkeypatch.setenv("POSTGRES_LOCAL_CLIENT_ENV_FILE", str(path))
        return path

    return _write


# -----------------------------------------------------------------------------
# Tunel de prueba en proceso (servidor SSH + PostgreSQL falso)
# -----------------------------------------------------------------------------
@pytest.fixture
def fake_pg():
    from fakepg import FakePostgres

    server = FakePostgres()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def client_key(tmp_path):
    from sshserver import generate_client_key

    return generate_client_key(tmp_path / "keys")


@pytest.fixture
def ssh_server(client_key):
    from sshserver import ForwardingSSHServer

    key, _path = client_key
    server = ForwardingSSHServer(allowed_pubkey=key)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def tunnel_env(tmp_path, monkeypatch, ssh_server, client_key, fake_pg):
    """
    Env que apunta al servidor SSH de prueba y, del otro lado, al PostgreSQL falso.

    Permite ejercitar el tunel de verdad —handshake SSH incluido— sin la VM.
    """
    import textwrap

    _key, key_path = client_key
    known_hosts = ssh_server.write_known_hosts(tmp_path / "known_hosts")

    def _write(*, local_port: int = 0, extra: str = "", pkey: Path = key_path) -> Path:
        content = textwrap.dedent(
            f"""
            SSH_HOST=127.0.0.1
            SSH_PORT={ssh_server.port}
            SSH_USER=tester
            SSH_PKEY_PATH={pkey}
            SSH_KNOWN_HOSTS_PATH={known_hosts}
            SSH_LOCAL_PORT={local_port}
            SSH_CONNECT_TIMEOUT_S=5
            DEFAULT_DB=uno

            POSTGRES__uno__HOST=127.0.0.1
            POSTGRES__uno__PORT={fake_pg.port}
            POSTGRES__uno__DBNAME=base-con-guion
            POSTGRES__uno__USER=u
            POSTGRES__uno__PASSWORD=p
            POSTGRES__uno__READ_ONLY=true

            POSTGRES__dos_rw__HOST=127.0.0.1
            POSTGRES__dos_rw__PORT={fake_pg.port}
            POSTGRES__dos_rw__DBNAME=base-con-guion
            POSTGRES__dos_rw__USER=u
            POSTGRES__dos_rw__PASSWORD=p
            POSTGRES__dos_rw__READ_ONLY=false
            """
        ).lstrip() + extra
        path = tmp_path / ".env.postgres_local_client"
        path.write_bytes(content.encode("utf-8"))
        monkeypatch.setenv("POSTGRES_LOCAL_CLIENT_ENV_FILE", str(path))
        return path

    return _write


@pytest.fixture
def events_log() -> tuple[list[dict], Callable[[dict], None]]:
    """Colector de eventos: devuelve (lista, callback)."""
    collected: list[dict] = []

    def collect(event: dict) -> None:
        collected.append(event)

    return collected, collect


# -----------------------------------------------------------------------------
# Integracion contra la VM real
# -----------------------------------------------------------------------------
def _tcp_open(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _real_ssh_host() -> Optional[tuple[str, int]]:
    if not REAL_ENV_FILE.exists():
        return None
    from postgres_local_client.config import read_env_file

    try:
        values = read_env_file(REAL_ENV_FILE)
    except Exception:  # noqa: BLE001
        return None
    host = values.get("SSH_HOST")
    if not host:
        return None
    return host, int(values.get("SSH_PORT", "22"))


@pytest.fixture(scope="session")
def vm_available() -> bool:
    target = _real_ssh_host()
    return bool(target and _tcp_open(*target))


@pytest.fixture
def real_env(
    clean_state: None, monkeypatch: pytest.MonkeyPatch, vm_available: bool
) -> Path:
    """
    Apunta la config al env real del repo. Salta el test si no hay VM alcanzable.

    Depende de `clean_state` a proposito, para que el borrado de la variable de
    override ocurra ANTES de que este fixture la ponga.
    """
    if not REAL_ENV_FILE.exists():
        pytest.skip(f"No existe {REAL_ENV_FILE.name}: no hay contra que integrar.")
    if not vm_available:
        pytest.skip("La VM no responde en el puerto SSH: integracion saltada.")
    monkeypatch.setenv("POSTGRES_LOCAL_CLIENT_ENV_FILE", str(REAL_ENV_FILE))
    return REAL_ENV_FILE


#: Esquema desechable para los tests de integracion. Nunca se toca `public`.
TEST_SCHEMA = "pytest_tmp"


@pytest.fixture
def rw_env(real_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Env derivado del real con un alias de pruebas que escribe y permite DDL.

    Apunta al esquema desechable, no a `public`.
    """
    from postgres_local_client.config import read_env_file

    values = read_env_file(real_env)
    lines = [f"{key}={value}" for key, value in values.items()]
    lines += [
        f"POSTGRES__pytest_rw__HOST={values['POSTGRES__local__HOST']}",
        f"POSTGRES__pytest_rw__PORT={values['POSTGRES__local__PORT']}",
        f"POSTGRES__pytest_rw__DBNAME={values['POSTGRES__local__DBNAME']}",
        f"POSTGRES__pytest_rw__CREDENTIALS_ENV={values['POSTGRES__local__CREDENTIALS_ENV']}",
        "POSTGRES__pytest_rw__READ_ONLY=false",
        "POSTGRES__pytest_rw__ALLOW_DDL=true",
        f"POSTGRES__pytest_rw__SCHEMA={TEST_SCHEMA}",
    ]
    path = tmp_path / ".env.postgres_local_client"
    path.write_bytes("\n".join(lines).encode("utf-8"))
    monkeypatch.setenv("POSTGRES_LOCAL_CLIENT_ENV_FILE", str(path))
    return path


@pytest.fixture
def tmp_schema(rw_env: Path) -> str:
    """
    Esquema desechable, creado y destruido por fixture.

    `DROP SCHEMA ... CASCADE` al salir garantiza que no quede nada, y al vivir todo
    fuera de `public` una prueba no puede pisar datos reales ni por accidente.
    """
    from postgres_local_client import execute_sql

    execute_sql(f"create schema if not exists {TEST_SCHEMA}", db="pytest_rw")
    try:
        yield TEST_SCHEMA
    finally:
        try:
            execute_sql(f"drop schema if exists {TEST_SCHEMA} cascade", db="pytest_rw")
        except Exception:  # noqa: BLE001 - la limpieza no debe tumbar la suite
            pass


@pytest.fixture
def tmp_table(tmp_schema: str) -> Callable[[str], str]:
    """
    Fabrica de tablas desechables dentro del esquema de pruebas.

    Devuelve una funcion que recibe el DDL de columnas y devuelve el nombre creado
    (sin cualificar: el esquema lo aporta el alias `pytest_rw`).
    """
    from postgres_local_client import execute_sql

    counter = {"n": 0}

    def _create(columns_ddl: str) -> str:
        counter["n"] += 1
        name = f"t{os.getpid()}_{counter['n']}"
        execute_sql(f"create table {tmp_schema}.{name} ({columns_ddl})", db="pytest_rw")
        return name

    return _create
