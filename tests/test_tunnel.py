from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from postgres_local_client import config as config_mod
from postgres_local_client.errors import (
    TunnelAuthError,
    TunnelBindError,
    TunnelHostKeyError,
    TunnelNetworkError,
)
from postgres_local_client.tunnel import (
    close_all_tunnels,
    open_tunnel,
    port_is_free,
    probe_postgres,
    tunnel,
    tunnel_status,
)
from fakepg import DumbTCPServer, FakePostgres
from sshserver import generate_client_key

pytestmark = pytest.mark.sshserver


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -----------------------------------------------------------------------------
# Criterio 7 y 8: idempotencia y un tunel por destino
# -----------------------------------------------------------------------------
def test_dos_aperturas_reusan_el_mismo_tunel(tunnel_env, events_log):
    """Criterio 7: dos llamadas consecutivas abren un solo tunel; la 2a emite tunnel_reused."""
    collected, collect = events_log
    tunnel_env()

    primero = open_tunnel(on_event=collect)
    segundo = open_tunnel(on_event=collect)

    assert primero is segundo
    assert primero.local_port == segundo.local_port
    assert len(tunnel_status()) == 1

    aperturas = [e for e in collected if e["event"] == "tunnel_open" and "listo" in e["message"]]
    reusos = [e for e in collected if e["event"] == "tunnel_reused"]
    assert len(aperturas) == 1
    assert len(reusos) == 1
    assert reusos[0]["local_port"] == primero.local_port
    assert reusos[0]["owned"] is True


def test_dos_aliases_mismo_destino_comparten_tunel(tunnel_env, events_log):
    """Criterio 8: local y local_rw sobre la misma base comparten un unico tunel."""
    collected, collect = events_log
    tunnel_env()

    uno = open_tunnel("uno", on_event=collect)
    dos = open_tunnel("dos_rw", on_event=collect)

    assert uno is dos
    assert len(tunnel_status()) == 1
    assert ssh_channels(collected) <= 1


def ssh_channels(events) -> int:
    return len([e for e in events if e["event"] == "tunnel_open" and "listo" in e["message"]])


def test_force_new_abre_otro_tunel_en_otro_puerto(tunnel_env):
    """Criterio 1 de la seccion 7: force_new=True fuerza uno nuevo en otro puerto."""
    tunnel_env()
    primero = open_tunnel()
    segundo = open_tunnel(force_new=True)
    assert primero is not segundo
    assert primero.local_port != segundo.local_port
    assert len(tunnel_status()) == 2


# -----------------------------------------------------------------------------
# Criterio 9 y 10: cierre
# -----------------------------------------------------------------------------
def test_al_salir_del_context_manager_el_puerto_queda_libre(tunnel_env):
    """Criterio 9: verificable con un bind."""
    tunnel_env()
    with tunnel() as info:
        puerto = info.local_port
        assert probe_postgres(puerto)
        assert not port_is_free(puerto)

    time.sleep(0.3)
    assert port_is_free(puerto), f"el puerto {puerto} sigue ocupado despues del with"
    assert not probe_postgres(puerto)


def test_context_manager_no_cierra_un_tunel_preexistente(tunnel_env):
    """El with cierra solo lo que abrio; si reusa, lo deja vivo."""
    tunnel_env()
    previo = open_tunnel()
    with tunnel() as info:
        assert info is previo
    assert probe_postgres(previo.local_port), "el with cerro un tunel que no abrio"


def test_cierre_al_terminar_el_proceso_por_excepcion(tunnel_env, tmp_path):
    """
    Criterio 10: al terminar el proceso por excepcion no capturada no queda socket vivo.
    """
    env_path = tunnel_env()
    script = tmp_path / "muere.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sys
            from postgres_local_client import open_tunnel
            info = open_tunnel()
            print(info.local_port, flush=True)
            raise RuntimeError("muerte no capturada")
            """
        ).lstrip(),
        encoding="utf-8",
    )
    env = dict(os_environ_copy(), POSTGRES_LOCAL_CLIENT_ENV_FILE=str(env_path))
    proceso = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=120
    )
    assert proceso.returncode != 0, "el script debia morir por la excepcion"
    assert "RuntimeError" in proceso.stderr
    puerto = int(proceso.stdout.strip().splitlines()[0])

    time.sleep(0.5)
    assert port_is_free(puerto), f"quedo el puerto {puerto} ocupado tras morir el proceso"


def os_environ_copy() -> dict:
    import os

    return dict(os.environ)


# -----------------------------------------------------------------------------
# Criterio 11: puerto efimero y concurrencia
# -----------------------------------------------------------------------------
def test_dos_procesos_concurrentes_con_puerto_efimero(tunnel_env, tmp_path):
    """Criterio 11: con SSH_LOCAL_PORT=0 dos procesos funcionan en paralelo."""
    env_path = tunnel_env(local_port=0)
    script = tmp_path / "abre.py"
    script.write_text(
        textwrap.dedent(
            """
            import time
            from postgres_local_client import open_tunnel
            from postgres_local_client.tunnel import probe_postgres
            info = open_tunnel()
            assert probe_postgres(info.local_port)
            print(info.local_port, flush=True)
            time.sleep(2)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    env = dict(os_environ_copy(), POSTGRES_LOCAL_CLIENT_ENV_FILE=str(env_path))
    uno = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    dos = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    salida_uno = uno.communicate(timeout=120)
    salida_dos = dos.communicate(timeout=120)

    assert uno.returncode == 0, salida_uno[1]
    assert dos.returncode == 0, salida_dos[1]
    puerto_uno = int(salida_uno[0].strip())
    puerto_dos = int(salida_dos[0].strip())
    assert puerto_uno != puerto_dos, "los dos procesos usaron el mismo puerto local"


# -----------------------------------------------------------------------------
# Criterio 12: tunel zombie
# -----------------------------------------------------------------------------
def test_tunel_zombie_se_detecta_y_se_reabre(tunnel_env, ssh_server, events_log):
    """
    Criterio 12: proceso vivo pero sesion SSH muerta -> se detecta caido y se reabre.
    """
    collected, collect = events_log
    tunnel_env()

    primero = open_tunnel(on_event=collect)
    assert probe_postgres(primero.local_port)

    # Mata la sesion SSH del otro lado. El listener local de sshtunnel sigue arriba.
    ssh_server.kill_sessions()
    time.sleep(0.5)
    assert not probe_postgres(primero.local_port), "el zombie no se detecto como caido"

    segundo = open_tunnel(on_event=collect)
    assert probe_postgres(segundo.local_port)
    assert segundo is not primero

    reintentos = [e for e in collected if e["event"] == "tunnel_retry"]
    assert reintentos, "no se emitio tunnel_retry"
    assert reintentos[0]["level"] == "WARNING"


# -----------------------------------------------------------------------------
# Criterio 13: tres errores distinguibles
# -----------------------------------------------------------------------------
def test_host_inalcanzable_da_tunnel_network_error(tmp_path, monkeypatch, fake_pg):
    puerto_cerrado = _free_port()
    content = textwrap.dedent(
        f"""
        SSH_HOST=127.0.0.1
        SSH_PORT={puerto_cerrado}
        SSH_USER=tester
        SSH_PKEY_PATH={tmp_path / 'no-importa'}
        SSH_PASSWORD_ENV=PGC_TEST_PWD
        SSH_CONNECT_TIMEOUT_S=3
        DEFAULT_DB=uno
        POSTGRES__uno__HOST=127.0.0.1
        POSTGRES__uno__PORT={fake_pg.port}
        POSTGRES__uno__DBNAME=d
        POSTGRES__uno__USER=u
        POSTGRES__uno__PASSWORD=p
        """
    ).lstrip()
    path = tmp_path / ".env.postgres_local_client"
    path.write_bytes(content.encode("utf-8"))
    monkeypatch.setenv("POSTGRES_LOCAL_CLIENT_ENV_FILE", str(path))
    monkeypatch.setenv("PGC_TEST_PWD", "loquesea")

    with pytest.raises(TunnelNetworkError) as excinfo:
        open_tunnel()
    mensaje = str(excinfo.value)
    assert "127.0.0.1" in mensaje
    assert "sshd" in mensaje or "Security Group" in mensaje


def test_llave_invalida_da_tunnel_auth_error(tunnel_env, tmp_path):
    otra_llave, otra_ruta = generate_client_key(tmp_path / "otras", name="intrusa")
    tunnel_env(pkey=otra_ruta)
    with pytest.raises(TunnelAuthError) as excinfo:
        open_tunnel()
    mensaje = str(excinfo.value)
    assert "Autenticacion SSH rechazada" in mensaje
    assert "administrators_authorized_keys" in mensaje


def test_puerto_local_ocupado_da_tunnel_bind_error(tunnel_env):
    ocupado = DumbTCPServer()
    puerto = ocupado.start()
    try:
        tunnel_env(local_port=puerto)
        with pytest.raises(TunnelBindError) as excinfo:
            open_tunnel()
        assert str(puerto) in str(excinfo.value)
        assert "SSH_LOCAL_PORT=0" in str(excinfo.value)
    finally:
        ocupado.stop()


def test_host_desconocido_da_tunnel_host_key_error(tunnel_env, tmp_path):
    vacio = tmp_path / "known_hosts_vacio"
    vacio.write_text("", encoding="utf-8")
    tunnel_env(extra=f"\nSSH_KNOWN_HOSTS_PATH={vacio}\n")
    with pytest.raises(TunnelHostKeyError) as excinfo:
        open_tunnel()
    assert "ssh-keyscan" in str(excinfo.value)


def test_verificacion_por_fingerprint(tunnel_env, ssh_server, tmp_path):
    """
    Con SSH_HOST_FINGERPRINT se verifica contra el hash y no se usa known_hosts.

    Es mas fuerte que el camino de known_hosts, donde el usuario agrega la entrada con
    ssh-keyscan (trust on first use, sin autenticar nada).
    """
    from postgres_local_client.tunnel import fingerprint

    esperado = fingerprint(ssh_server.host_key)
    # known_hosts apuntando a un archivo que no existe: el fingerprint debe bastar.
    inexistente = tmp_path / "no-hay-known-hosts"
    tunnel_env(
        extra=f"\nSSH_KNOWN_HOSTS_PATH={inexistente}\nSSH_HOST_FINGERPRINT={esperado}\n"
    )

    info = open_tunnel()
    assert info.local_port > 0
    assert probe_postgres(info.local_port)


def test_fingerprint_equivocado_da_tunnel_host_key_error(tunnel_env):
    otro = "SHA256:" + "A" * 43
    tunnel_env(extra=f"\nSSH_HOST_FINGERPRINT={otro}\n")

    with pytest.raises(TunnelHostKeyError) as excinfo:
        open_tunnel()

    mensaje = str(excinfo.value)
    assert "no coincide con ningun fingerprint" in mensaje
    # El mensaje trae el recibido y el esperado, para poder comparar a ojo.
    assert "recibido" in mensaje
    assert otro in mensaje
    assert "postgres-local-client fingerprint" in mensaje


def test_fingerprint_tiene_prioridad_sobre_known_hosts(tunnel_env, ssh_server, tmp_path):
    """Si hay fingerprint, un known_hosts con la llave equivocada no estorba."""
    from postgres_local_client.tunnel import fingerprint

    equivocada = ssh_server.write_wrong_known_hosts(tmp_path / "known_hosts_malo")
    tunnel_env(
        extra=(
            f"\nSSH_KNOWN_HOSTS_PATH={equivocada}"
            f"\nSSH_HOST_FINGERPRINT={fingerprint(ssh_server.host_key)}\n"
        )
    )
    info = open_tunnel()
    assert probe_postgres(info.local_port)


def test_varios_fingerprints_acepta_el_que_coincida(tunnel_env, ssh_server):
    from postgres_local_client.tunnel import fingerprint

    correcto = fingerprint(ssh_server.host_key)
    tunnel_env(extra=f"\nSSH_HOST_FINGERPRINT=SHA256:{'B' * 43},{correcto}\n")
    info = open_tunnel()
    assert probe_postgres(info.local_port)


def test_fetch_remote_host_key_devuelve_la_llave_del_servidor(tunnel_env, ssh_server):
    from postgres_local_client import config as cfg_mod
    from postgres_local_client.tunnel import fetch_remote_host_key, fingerprint

    tunnel_env()
    _app, cfg = cfg_mod.resolve("uno")
    key = fetch_remote_host_key(cfg.ssh)
    assert fingerprint(key) == fingerprint(ssh_server.host_key)
    assert key.get_name() == ssh_server.host_key.get_name()


def test_fetch_remote_host_key_host_inalcanzable(tmp_path, monkeypatch, fake_pg):
    from postgres_local_client import config as cfg_mod
    from postgres_local_client.tunnel import fetch_remote_host_key

    puerto_cerrado = _free_port()
    contenido = textwrap.dedent(
        f"""
        SSH_HOST=127.0.0.1
        SSH_PORT={puerto_cerrado}
        SSH_USER=tester
        SSH_PASSWORD_ENV=PGC_TEST_PWD
        SSH_CONNECT_TIMEOUT_S=3
        DEFAULT_DB=uno
        POSTGRES__uno__HOST=127.0.0.1
        POSTGRES__uno__PORT={fake_pg.port}
        POSTGRES__uno__DBNAME=d
        POSTGRES__uno__USER=u
        POSTGRES__uno__PASSWORD=p
        """
    ).lstrip()
    path = tmp_path / ".env.postgres_local_client"
    path.write_bytes(contenido.encode("utf-8"))
    monkeypatch.setenv("POSTGRES_LOCAL_CLIENT_ENV_FILE", str(path))
    monkeypatch.setenv("PGC_TEST_PWD", "loquesea")

    _app, cfg = cfg_mod.resolve("uno")
    with pytest.raises(TunnelNetworkError):
        fetch_remote_host_key(cfg.ssh)


def test_host_key_que_no_coincide_da_tunnel_host_key_error(tunnel_env, ssh_server, tmp_path):
    equivocada = ssh_server.write_wrong_known_hosts(tmp_path / "known_hosts_malo")
    tunnel_env(extra=f"\nSSH_KNOWN_HOSTS_PATH={equivocada}\n")
    with pytest.raises(TunnelHostKeyError) as excinfo:
        open_tunnel()
    assert "NO coincide" in str(excinfo.value)


def test_los_tres_errores_son_clases_distintas():
    """Criterio 13: colapsarlos en un generico es motivo de rechazo."""
    assert TunnelNetworkError is not TunnelAuthError
    assert TunnelAuthError is not TunnelBindError
    assert not issubclass(TunnelNetworkError, (TunnelAuthError, TunnelBindError))
    assert not issubclass(TunnelAuthError, (TunnelNetworkError, TunnelBindError))
    assert not issubclass(TunnelBindError, (TunnelNetworkError, TunnelAuthError))


# -----------------------------------------------------------------------------
# Criterio 14: tunel externo preexistente
# -----------------------------------------------------------------------------
def test_tunel_externo_se_reusa_y_sigue_vivo(tunnel_env, events_log):
    """
    Criterio 14: un tunel que la libreria no abrio se reusa, se marca owned=False y
    sigue vivo despues.
    """
    collected, collect = events_log
    externo = FakePostgres()
    puerto_externo = externo.start()
    try:
        tunnel_env(local_port=puerto_externo)
        info = open_tunnel(on_event=collect)

        assert info.owned is False
        assert info.local_port == puerto_externo

        avisos = [e for e in collected if e["event"] == "tunnel_reused"]
        assert avisos and avisos[0]["level"] == "WARNING"
        assert "NO se cerrara" in avisos[0]["message"]

        close_all_tunnels()
        assert probe_postgres(puerto_externo), "se cerro un tunel ajeno"
    finally:
        externo.stop()


# -----------------------------------------------------------------------------
# Fuga de recursos
# -----------------------------------------------------------------------------
def test_sin_fuga_de_sockets_ni_hilos_en_50_ciclos(tunnel_env):
    tunnel_env()
    hilos_antes = threading.active_count()
    puertos = []

    for _ in range(50):
        with tunnel() as info:
            puertos.append(info.local_port)
        close_all_tunnels()

    time.sleep(1.0)
    hilos_despues = threading.active_count()
    assert hilos_despues - hilos_antes <= 5, (
        f"posible fuga de hilos: {hilos_antes} -> {hilos_despues}"
    )
    ocupados = [puerto for puerto in set(puertos) if not port_is_free(puerto)]
    assert not ocupados, f"puertos sin liberar: {ocupados}"
    assert tunnel_status() == []


# -----------------------------------------------------------------------------
# Health check
# -----------------------------------------------------------------------------
def test_probe_postgres_distingue_servidor_real_de_puerto_mudo(fake_pg):
    assert probe_postgres(fake_pg.port)

    mudo = DumbTCPServer()
    puerto = mudo.start()
    try:
        # Acepta TCP pero no contesta el SSLRequest: no cuenta como vivo.
        assert not probe_postgres(puerto, timeout_s=1.0)
    finally:
        mudo.stop()

    assert not probe_postgres(_free_port(), timeout_s=1.0)
    assert not probe_postgres(0)


def test_ssh_auto_open_false_no_abre_y_explica(tunnel_env):
    from postgres_local_client.errors import TunnelError
    from postgres_local_client.tunnel import ensure_tunnel

    tunnel_env(extra="\nSSH_AUTO_OPEN=false\n")
    _app, cfg = config_mod.resolve("uno")
    with pytest.raises(TunnelError, match="SSH_AUTO_OPEN"):
        ensure_tunnel(cfg)
