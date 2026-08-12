from __future__ import annotations

import atexit
import base64
import hashlib
import logging
import os
import signal
import socket
import struct
import threading
import warnings
from contextlib import contextmanager
from datetime import datetime as dt
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import paramiko
from sshtunnel import SSHTunnelForwarder

from postgres_local_client import config as _config
from postgres_local_client.errors import (
    TunnelAuthError,
    TunnelBindError,
    TunnelError,
    TunnelHostKeyError,
    TunnelNetworkError,
)
from postgres_local_client.events import OnEvent, emit
from postgres_local_client.logging import get_logger, get_ssh_logger
from postgres_local_client.types import PostgresConfig, SSHConfig, TunnelInfo

#: (ssh_host, ssh_port, ssh_user, remote_host, remote_port)
#: Un tunel por destino SSH, no por alias: dos aliases sobre la misma base
#: comparten un solo tunel.
DestKey = Tuple[str, int, str, str, int]

_LOCALHOST = "127.0.0.1"
_PROBE_TIMEOUT_S = 3.0

# Mensaje SSLRequest del protocolo de PostgreSQL: longitud 8 + codigo 80877103.
# Sirve para verificar que del otro lado responde un servidor PostgreSQL sin
# necesidad de credenciales.
_PG_SSL_REQUEST = struct.pack("!ii", 8, 80877103)
_PG_SSL_REPLIES = (b"S", b"N", b"E")

_log = get_logger("tunnel")
_lock = threading.RLock()
_current: Dict[DestKey, TunnelInfo] = {}
_opened: List[TunnelInfo] = []
_cleanup_registered = False


# -----------------------------------------------------------------------------
# Verificacion de estado
# -----------------------------------------------------------------------------
def probe_postgres(
    local_port: int, *, host: str = _LOCALHOST, timeout_s: float = _PROBE_TIMEOUT_S
) -> bool:
    """
    Verificacion real de que el tunel esta vivo.

    Hace handshake TCP contra el puerto local y ademas exige respuesta del
    servidor PostgreSQL del otro lado. Que el proceso SSH exista no basta: el caso
    comun de falla es un tunel zombie cuya sesion SSH ya murio del otro lado, con
    el socket local todavia escuchando.
    """
    if not local_port:
        return False
    try:
        with socket.create_connection((host, local_port), timeout=timeout_s) as sock:
            sock.settimeout(timeout_s)
            sock.sendall(_PG_SSL_REQUEST)
            reply = sock.recv(1)
    except OSError:
        return False
    return reply in _PG_SSL_REPLIES


def port_is_free(port: int, *, host: str = _LOCALHOST) -> bool:
    if not port:
        return True
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


# -----------------------------------------------------------------------------
# Aislamiento de los efectos secundarios de sshtunnel
# -----------------------------------------------------------------------------
@contextmanager
def _no_logging_side_effects() -> Iterator[None]:
    """
    Envuelve toda llamada a sshtunnel para que no deje estado global modificado.

    `sshtunnel.create_logger()` hace tres cosas que afectan al proceso entero:
      1. `logging.captureWarnings(True)`, que redirige el modulo warnings a logging;
      2. agrega handlers al logger `py.warnings`;
      3. asigna handlers al logger global `paramiko.transport`.

    Ninguna es aceptable en una libreria que puede convivir con otra que tambien
    use paramiko, asi que se toma snapshot y se restaura. El restore nunca lanza.
    """
    paramiko_logger = logging.getLogger("paramiko.transport")
    pywarnings_logger = logging.getLogger("py.warnings")
    saved_paramiko_handlers = list(paramiko_logger.handlers)
    saved_paramiko_level = paramiko_logger.level
    saved_paramiko_propagate = paramiko_logger.propagate
    saved_pywarnings_handlers = list(pywarnings_logger.handlers)
    saved_showwarning = warnings.showwarning
    saved_logging_showwarning = getattr(logging, "_warnings_showwarning", None)
    try:
        yield
    finally:
        try:
            paramiko_logger.handlers = saved_paramiko_handlers
            paramiko_logger.level = saved_paramiko_level
            paramiko_logger.propagate = saved_paramiko_propagate
            pywarnings_logger.handlers = saved_pywarnings_handlers
            warnings.showwarning = saved_showwarning
            logging._warnings_showwarning = saved_logging_showwarning  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - restaurar logging jamas debe fallar
            _log.debug("No se pudo restaurar el estado de logging: %s", exc)


# -----------------------------------------------------------------------------
# Autenticacion y host key
# -----------------------------------------------------------------------------
def _known_hosts_path(ssh: SSHConfig) -> Path:
    if ssh.known_hosts_path:
        return Path(ssh.known_hosts_path).expanduser()
    return Path.home() / ".ssh" / "known_hosts"


def _host_key_names(ssh: SSHConfig) -> List[str]:
    if ssh.port == 22:
        return [ssh.host]
    return [f"[{ssh.host}]:{ssh.port}", ssh.host]


def _load_known_host_key(ssh: SSHConfig) -> paramiko.PKey:
    """
    Carga la host key esperada desde known_hosts.

    La verificacion no se deshabilita nunca. Si el host es desconocido se falla con
    instrucciones para agregarlo, en vez de aceptarlo automaticamente.
    """
    path = _known_hosts_path(ssh)
    hint = (
        f"Agrega la host key a {path} y verifica el fingerprint con quien administra "
        f"la VM antes de confiar en ella:\n"
        f"  ssh-keyscan -p {ssh.port} {ssh.host} >> {path}\n"
        f"o conecta una vez a mano y acepta el fingerprint:\n"
        f"  ssh -p {ssh.port} {ssh.user}@{ssh.host}"
    )

    if not path.exists():
        raise TunnelHostKeyError(
            f"No existe el archivo known_hosts: {path}. No se puede verificar la identidad "
            f"del servidor SSH {ssh.host}:{ssh.port}.\n{hint}"
        )

    host_keys = paramiko.HostKeys()
    try:
        host_keys.load(str(path))
    except OSError as exc:
        raise TunnelHostKeyError(f"No se pudo leer {path}: {exc}") from exc

    # paramiko negocia exactamente el tipo de la llave que se le pasa, asi que se
    # prefiere la mas fuerte que este registrada para ese host.
    preference = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "rsa-sha2-512")
    for name in _host_key_names(ssh):
        entry = host_keys.lookup(name)
        if not entry:
            continue
        available = list(entry.keys())
        for keytype in preference:
            if keytype in available:
                return entry[keytype]
        return entry[available[0]]

    raise TunnelHostKeyError(
        f"El host SSH {ssh.host}:{ssh.port} no esta en {path}, asi que no se puede "
        f"verificar su identidad.\n{hint}"
    )


def _load_private_key(ssh: SSHConfig, on_event: Optional[OnEvent]) -> Optional[paramiko.PKey]:
    if not ssh.pkey_path:
        return None

    path = Path(ssh.pkey_path).expanduser()
    if not path.exists():
        if ssh.password:
            emit(
                on_event,
                level="WARNING",
                event="tunnel_open",
                message=(
                    f"La llave privada {path} no existe; se usara el password de "
                    "SSH_PASSWORD_ENV."
                ),
                ssh_host=ssh.host,
            )
            return None
        raise TunnelAuthError(
            f"La llave privada SSH no existe: {path}. Corrige SSH_PKEY_PATH o define "
            "SSH_PASSWORD_ENV con el nombre de la variable de sistema que tiene el password."
        )

    passphrase = ssh.pkey_passphrase

    def passphrase_error(detail: str) -> TunnelAuthError:
        if passphrase:
            return TunnelAuthError(
                f"La llave {path} esta cifrada y la passphrase de SSH_PKEY_PASSPHRASE_ENV no "
                f"la abre. Verifica el valor de esa variable de sistema. Detalle: {detail}"
            )
        return TunnelAuthError(
            f"La llave {path} esta protegida con passphrase y no se proporciono ninguna. "
            "Define SSH_PKEY_PASSPHRASE_ENV con el NOMBRE de la variable de sistema que la "
            f"contiene. Detalle: {detail}"
        )

    from_path = getattr(paramiko.PKey, "from_path", None)
    if from_path is not None:
        try:
            return from_path(path, passphrase=passphrase)
        except paramiko.PasswordRequiredException as exc:
            raise passphrase_error(str(exc)) from exc
        except TypeError as exc:
            # cryptography lanza TypeError ("Password was not given but private key is
            # encrypted") cuando la llave esta cifrada y no se paso passphrase.
            raise passphrase_error(str(exc)) from exc
        except (paramiko.SSHException, ValueError, OSError) as exc:
            if passphrase:
                raise passphrase_error(str(exc)) from exc
            raise TunnelAuthError(
                f"No se pudo leer la llave privada {path}: {exc}. Verifica que sea la llave "
                "PRIVADA (no el .pub) y que el formato este soportado."
            ) from exc

    # paramiko < 3.2 no tiene PKey.from_path: se prueba cada tipo de llave.
    errors: List[str] = []
    for attr in ("Ed25519Key", "RSAKey", "ECDSAKey", "DSSKey"):
        key_class = getattr(paramiko, attr, None)
        if key_class is None:
            continue
        try:
            return key_class.from_private_key_file(str(path), password=passphrase)
        except paramiko.PasswordRequiredException as exc:
            raise passphrase_error(str(exc)) from exc
        except TypeError as exc:
            raise passphrase_error(str(exc)) from exc
        except (paramiko.SSHException, ValueError, OSError) as exc:
            errors.append(f"{attr}: {exc}")

    raise TunnelAuthError(
        f"No se pudo leer la llave privada {path} con ningun tipo de llave conocido. "
        f"Si esta cifrada, define SSH_PKEY_PASSPHRASE_ENV. Detalle: {'; '.join(errors)}"
    )


def _preflight(ssh: SSHConfig) -> None:
    """
    Comprueba que el puerto SSH sea alcanzable antes de involucrar a sshtunnel.

    Es necesario porque sshtunnel captura `socket.error` y `AuthenticationException`
    internamente y termina lanzando un unico error generico, con lo que se perderia
    la distincion entre "no hay ruta" y "credenciales invalidas".
    """
    try:
        with socket.create_connection((ssh.host, ssh.port), timeout=ssh.connect_timeout_s):
            return
    except socket.gaierror as exc:
        raise TunnelNetworkError(
            f"No se pudo resolver el host SSH '{ssh.host}': {exc}. Revisa SSH_HOST."
        ) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise TunnelNetworkError(
            f"Timeout al conectar a {ssh.host}:{ssh.port} despues de "
            f"{ssh.connect_timeout_s:g}s. Causas tipicas, en orden de probabilidad:\n"
            "  1. El Security Group de AWS no permite el puerto 22 desde tu IP publica "
            "actual (cambia al reconectar la red o el VPN).\n"
            "  2. La VM esta apagada.\n"
            "El puerto 22 se gestiona fuera de esta libreria."
        ) from exc
    except ConnectionRefusedError as exc:
        raise TunnelNetworkError(
            f"Conexion rechazada en {ssh.host}:{ssh.port}. El host responde pero no hay un "
            "servidor SSH escuchando ahi: revisa que el servicio 'sshd' de la VM este "
            "arriba y que SSH_PORT sea el correcto."
        ) from exc
    except OSError as exc:
        raise TunnelNetworkError(
            f"No se pudo alcanzar {ssh.host}:{ssh.port}: {exc}"
        ) from exc


def fingerprint(key: paramiko.PKey) -> str:
    """Fingerprint en el mismo formato que imprime OpenSSH (SHA256:base64)."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _diagnose_open_failure(
    ssh: SSHConfig, host_key: paramiko.PKey, pkey: Optional[paramiko.PKey], original: Exception
) -> TunnelError:
    """
    Reintenta el handshake con paramiko directo para clasificar el fallo.

    Solo corre en el camino de error, asi que no cuesta nada en el caso normal, y es
    lo que permite distinguir auth de red de host key en vez de colapsar los tres en
    un generico "no se pudo conectar".

    La host key se compara aqui a mano en vez de dejarsela a `Transport.connect`:
    ese metodo lanza un `SSHException` generico con el texto "Bad host key from
    server" (`BadHostKeyException` solo la lanza `SSHClient`), y comparar cadenas de
    error es fragil. Comparandola directamente, ademas, se pueden reportar los dos
    fingerprints.
    """
    transport: Optional[paramiko.Transport] = None
    try:
        transport = paramiko.Transport((ssh.host, ssh.port))
        with _no_logging_side_effects():
            # Fija el algoritmo de host key al de la llave esperada; si no, el servidor
            # podria ofrecer otro tipo y la comparacion daria un falso desajuste.
            try:
                transport._preferred_keys = [host_key.get_name()]
            except Exception:  # noqa: BLE001 - si la API interna cambia, se sigue igual
                pass

            transport.start_client(timeout=ssh.connect_timeout_s)
            remote_key = transport.get_remote_server_key()
            if remote_key.asbytes() != host_key.asbytes():
                return TunnelHostKeyError(
                    f"La host key de {ssh.host}:{ssh.port} NO coincide con la registrada en "
                    f"{_known_hosts_path(ssh)}.\n"
                    f"  esperada : {fingerprint(host_key)} ({host_key.get_name()})\n"
                    f"  recibida : {fingerprint(remote_key)} ({remote_key.get_name()})\n"
                    "Puede ser que la VM se haya recreado o que alguien este interceptando la "
                    "conexion. Verifica el fingerprint con quien administra la VM y solo "
                    "entonces reemplaza la entrada en known_hosts."
                )

            if pkey is not None:
                try:
                    transport.auth_publickey(ssh.user, pkey)
                except paramiko.AuthenticationException:
                    if not ssh.password:
                        raise
                    transport.auth_password(ssh.user, ssh.password)
            else:
                transport.auth_password(ssh.user, str(ssh.password))
    except paramiko.BadHostKeyException:
        return TunnelHostKeyError(
            f"La host key de {ssh.host}:{ssh.port} NO coincide con la registrada en "
            f"{_known_hosts_path(ssh)}. Verifica el fingerprint con quien administra la VM "
            "y solo entonces reemplaza la entrada en known_hosts."
        )
    except paramiko.AuthenticationException:
        method = "llave privada" if pkey is not None else "password"
        detail = (
            f"la llave {ssh.pkey_path}" if pkey is not None else "el password de SSH_PASSWORD_ENV"
        )
        return TunnelAuthError(
            f"Autenticacion SSH rechazada para el usuario '{ssh.user}' en "
            f"{ssh.host}:{ssh.port} usando {method}. Revisa SSH_USER y {detail}.\n"
            "En Windows Server, para una cuenta del grupo Administrators la llave publica "
            "debe estar en "
            r"C:\ProgramData\ssh\administrators_authorized_keys"
            " (no en ~\\.ssh\\authorized_keys), con ACL restringida a Administrators y "
            "SYSTEM. Si no, sshd la ignora en silencio: es la causa numero uno de 'la llave "
            "es correcta pero no entra'."
        )
    except paramiko.SSHException as exc:
        return TunnelError(
            f"Fallo el protocolo SSH contra {ssh.host}:{ssh.port}: {exc}"
        )
    except OSError as exc:
        return TunnelNetworkError(f"No se pudo alcanzar {ssh.host}:{ssh.port}: {exc}")
    else:
        return TunnelError(
            f"No se pudo abrir el tunel a {ssh.host}:{ssh.port} y el diagnostico posterior "
            f"si logro autenticarse, asi que probablemente sea intermitente. "
            f"Error original: {original}"
        )
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass


# -----------------------------------------------------------------------------
# Apertura y cierre
# -----------------------------------------------------------------------------
def _dest_key(ssh: SSHConfig, remote_host: str, remote_port: int) -> DestKey:
    return (ssh.host, ssh.port, ssh.user, remote_host, remote_port)


def _register_cleanup() -> None:
    """
    Registra la limpieza al terminar el proceso.

    `atexit` es aditivo y por lo tanto seguro. El handler de SIGTERM se encadena al
    previo en vez de reemplazarlo. SIGINT no se toca: su comportamiento por default
    es levantar KeyboardInterrupt, que es justo lo que queremos para que atexit
    corra normalmente.
    """
    global _cleanup_registered
    if _cleanup_registered:
        return
    _cleanup_registered = True

    atexit.register(_atexit_cleanup)

    if threading.current_thread() is not threading.main_thread():
        return
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (ValueError, AttributeError):  # pragma: no cover - plataforma sin SIGTERM
        return

    def _handler(signum: int, frame: Any) -> None:
        _atexit_cleanup()
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            try:
                os.kill(os.getpid(), signum)
            except Exception:  # noqa: BLE001 - nunca fallar durante el cierre
                pass

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        pass


def _atexit_cleanup() -> None:
    """Cierra lo que abrimos. Tolerante a fallos: jamas lanza durante el cierre."""
    try:
        close_all_tunnels()
    except Exception as exc:  # noqa: BLE001
        try:
            _log.debug("Fallo la limpieza de tuneles al salir: %s", exc)
        except Exception:  # noqa: BLE001
            pass


def _dispose_engines(local_port: int) -> None:
    """Dispone el pool de SQLAlchemy antes de cerrar el tunel (ese orden importa)."""
    try:
        from postgres_local_client import engine as _engine

        _engine.dispose_for_port(local_port)
    except Exception as exc:  # noqa: BLE001
        _log.debug("Fallo el dispose de engines para el puerto %s: %s", local_port, exc)


def _abort_forwarder(forwarder: SSHTunnelForwarder) -> None:
    """
    Limpia un forwarder cuyo `start()` fallo a medias, sin usar `stop()`.

    En sshtunnel 0.4.0, si `start()` falla por autenticacion, el forward server local
    ya quedo en `_server_list` pero su hilo `serve_forever` nunca arranco. `stop()`
    llama `srv.shutdown()`, que se queda esperando para siempre el evento que solo
    pone ese hilo — y `force=True` no ayuda porque el `shutdown()` es incondicional
    (sshtunnel.py:1463). Asi que se cierran los sockets directamente.
    """
    for server in list(getattr(forwarder, "_server_list", None) or []):
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
    try:
        forwarder._server_list = []
        forwarder.is_alive = False
    except Exception:  # noqa: BLE001
        pass

    transport = getattr(forwarder, "_transport", None)
    if transport is None:
        return
    for method in ("close", "stop_thread"):
        try:
            getattr(transport, method)()
        except Exception:  # noqa: BLE001
            pass


def _shutdown_forwarder(forwarder: Optional[SSHTunnelForwarder]) -> None:
    """Cierra el forwarder por la via normal si arranco bien, o a mano si no."""
    if forwarder is None:
        return
    if getattr(forwarder, "is_alive", False):
        try:
            with _no_logging_side_effects():
                forwarder.stop()
            return
        except Exception as exc:  # noqa: BLE001
            _log.debug("stop() del forwarder fallo, se cierra a mano: %s", exc)
    _abort_forwarder(forwarder)


def _open_forwarder(
    ssh: SSHConfig,
    remote_host: str,
    remote_port: int,
    local_port: int,
    on_event: Optional[OnEvent],
) -> SSHTunnelForwarder:
    _preflight(ssh)
    host_key = _load_known_host_key(ssh)
    pkey = _load_private_key(ssh, on_event)

    if pkey is None and not ssh.password:
        raise TunnelAuthError(
            "No hay credencial SSH utilizable: define SSH_PKEY_PATH (recomendado) o "
            "SSH_PASSWORD_ENV."
        )

    forwarder: Optional[SSHTunnelForwarder] = None
    try:
        with _no_logging_side_effects():
            forwarder = SSHTunnelForwarder(
                (ssh.host, ssh.port),
                ssh_username=ssh.user,
                ssh_pkey=pkey,
                ssh_password=ssh.password,
                ssh_host_key=host_key,
                remote_bind_address=(remote_host, remote_port),
                local_bind_address=(_LOCALHOST, local_port),
                set_keepalive=ssh.keepalive_s,
                # sshtunnel reenvia en trozos de 1024 bytes (sshtunnel.py:309), lo que
                # limita el throughput de un COPY grande. La compresion reduce los
                # bytes que pasan por ese bucle y aproximadamente lo duplica.
                compression=ssh.compression,
                logger=get_ssh_logger(),
                # Deterministico: no leer ~/.ssh/config ni probar llaves al azar del
                # agente o de ~/.ssh. Solo lo que dice el env propio.
                ssh_config_file=None,
                allow_agent=False,
                host_pkey_directories=[],
            )
            forwarder.start()
    except Exception as exc:  # noqa: BLE001 - se reclasifica abajo
        _shutdown_forwarder(forwarder)
        if local_port and not port_is_free(local_port):
            raise TunnelBindError(
                f"El puerto local {local_port} esta ocupado y no responde como un servidor "
                "PostgreSQL valido, asi que no se reusa a ciegas. Libera el puerto o usa "
                "SSH_LOCAL_PORT=0 para que se asigne uno libre automaticamente."
            ) from exc
        raise _diagnose_open_failure(ssh, host_key, pkey, exc) from exc

    return forwarder


def _open_for_config(
    cfg: PostgresConfig, *, force_new: bool, on_event: Optional[OnEvent]
) -> TunnelInfo:
    ssh = cfg.ssh
    if ssh is None:  # pragma: no cover - config siempre lo llena
        raise TunnelError(f"El alias '{cfg.alias}' no tiene configuracion SSH.")

    key = _dest_key(ssh, cfg.host, cfg.port)
    started = dt.now()

    with _lock:
        existing = _current.get(key)
        if existing is not None and not force_new:
            if probe_postgres(existing.local_port):
                emit(
                    on_event,
                    level="INFO",
                    event="tunnel_reused",
                    message=f"Reusando tunel vivo en localhost:{existing.local_port}.",
                    db=cfg.alias,
                    local_port=existing.local_port,
                    ssh_host=ssh.host,
                    owned=existing.owned,
                    elapsed_s=0.0,
                )
                return existing

            emit(
                on_event,
                level="WARNING",
                event="tunnel_retry",
                message=(
                    f"El tunel en localhost:{existing.local_port} ya no responde "
                    "(sesion SSH muerta del otro lado). Se reabre."
                ),
                db=cfg.alias,
                local_port=existing.local_port,
                ssh_host=ssh.host,
                owned=existing.owned,
            )
            _forget(existing, close=True, on_event=on_event)

        # Reuso de un tunel externo: solo posible con puerto local fijo, porque un
        # puerto efimero no se puede adivinar.
        if not force_new and ssh.local_port and probe_postgres(ssh.local_port):
            adopted = TunnelInfo(
                local_port=ssh.local_port,
                remote_host=cfg.host,
                remote_port=cfg.port,
                ssh_host=ssh.host,
                ssh_port=ssh.port,
                ssh_user=ssh.user,
                opened_at=started,
                owned=False,
                forwarder=None,
            )
            _current[key] = adopted
            _opened.append(adopted)
            emit(
                on_event,
                level="WARNING",
                event="tunnel_reused",
                message=(
                    f"El puerto local {ssh.local_port} ya tiene un tunel valido que esta "
                    "libreria no abrio: se reusa y NO se cerrara al terminar. No se puede "
                    "verificar a que base apunta sin conectarse; confirma con ping()."
                ),
                db=cfg.alias,
                local_port=ssh.local_port,
                ssh_host=ssh.host,
                owned=False,
                elapsed_s=0.0,
            )
            return adopted

        local_port = 0 if force_new else ssh.local_port
        if local_port and not port_is_free(local_port):
            raise TunnelBindError(
                f"El puerto local {local_port} esta ocupado por algo que no responde como "
                "servidor PostgreSQL. No se reusa a ciegas: si fuera otro PostgreSQL local, "
                "la conexion funcionaria pero apuntaria a la base equivocada. Libera el "
                "puerto o usa SSH_LOCAL_PORT=0."
            )

        emit(
            on_event,
            level="INFO",
            event="tunnel_open",
            message=f"Abriendo tunel SSH a {ssh.host}:{ssh.port} -> {cfg.host}:{cfg.port}.",
            db=cfg.alias,
            ssh_host=ssh.host,
            ssh_user=ssh.user,
            local_port=local_port or "efimero",
            owned=True,
        )

        forwarder = _open_forwarder(ssh, cfg.host, cfg.port, local_port, on_event)
        info = TunnelInfo(
            local_port=int(forwarder.local_bind_port),
            remote_host=cfg.host,
            remote_port=cfg.port,
            ssh_host=ssh.host,
            ssh_port=ssh.port,
            ssh_user=ssh.user,
            opened_at=dt.now(),
            owned=True,
            forwarder=forwarder,
        )
        _current[key] = info
        _opened.append(info)
        _register_cleanup()

        emit(
            on_event,
            level="INFO",
            event="tunnel_open",
            message=f"Tunel listo en localhost:{info.local_port}.",
            db=cfg.alias,
            ssh_host=ssh.host,
            ssh_user=ssh.user,
            local_port=info.local_port,
            owned=True,
            elapsed_s=round((dt.now() - started).total_seconds(), 3),
        )
        return info


def _forget(info: TunnelInfo, *, close: bool, on_event: Optional[OnEvent] = None) -> None:
    """Saca el tunel del registro y, si es nuestro y `close`, lo cierra."""
    with _lock:
        for key, current in list(_current.items()):
            if current is info:
                del _current[key]
        if info in _opened:
            _opened.remove(info)

    _dispose_engines(info.local_port)

    if not close:
        return
    if not info.owned:
        emit(
            on_event,
            level="DEBUG",
            event="tunnel_close",
            message=(
                f"El tunel en localhost:{info.local_port} no lo abrio esta libreria; "
                "se deja vivo."
            ),
            local_port=info.local_port,
            ssh_host=info.ssh_host,
            owned=False,
        )
        return

    _shutdown_forwarder(info.forwarder)

    emit(
        on_event,
        level="INFO",
        event="tunnel_close",
        message=f"Tunel cerrado (localhost:{info.local_port}).",
        local_port=info.local_port,
        ssh_host=info.ssh_host,
        owned=True,
        elapsed_s=round((dt.now() - info.opened_at).total_seconds(), 3),
    )


# -----------------------------------------------------------------------------
# API publica
# -----------------------------------------------------------------------------
def open_tunnel(
    db: Optional[str] = None, *, force_new: bool = False, on_event: Optional[OnEvent] = None
) -> TunnelInfo:
    """
    Abre el tunel para el destino del alias, o devuelve el que ya este vivo.

    Es idempotente: con un tunel vivo devuelve el existente en vez de abrir otro.
    `force_new=True` fuerza uno nuevo en otro puerto local (siempre efimero, porque
    dos tuneles no pueden compartir puerto local).
    """
    _app, cfg = _config.resolve(db, on_event=on_event)
    return _open_for_config(cfg, force_new=force_new, on_event=on_event)


def ensure_tunnel(
    cfg: PostgresConfig, *, on_event: Optional[OnEvent] = None
) -> TunnelInfo:
    """
    Garantiza un tunel vivo para un alias ya resuelto. Uso interno de engine.py.

    Con `SSH_AUTO_OPEN=false` no abre nada: falla indicando como abrirlo a mano.
    """
    ssh = cfg.ssh
    if ssh is None:  # pragma: no cover
        raise TunnelError(f"El alias '{cfg.alias}' no tiene configuracion SSH.")

    with _lock:
        existing = _current.get(_dest_key(ssh, cfg.host, cfg.port))
        if existing is not None and probe_postgres(existing.local_port):
            emit(
                on_event,
                level="INFO",
                event="tunnel_reused",
                message=f"Reusando tunel vivo en localhost:{existing.local_port}.",
                db=cfg.alias,
                local_port=existing.local_port,
                ssh_host=ssh.host,
                owned=existing.owned,
                elapsed_s=0.0,
            )
            return existing

    if not ssh.auto_open:
        raise TunnelError(
            f"No hay tunel vivo hacia {cfg.host}:{cfg.port} y SSH_AUTO_OPEN=false. "
            "Abrelo con open_tunnel() o con 'postgres-local-client tunnel open --keep-alive', "
            "o pon SSH_AUTO_OPEN=true."
        )
    return _open_for_config(cfg, force_new=False, on_event=on_event)


def close_tunnel(db: Optional[str] = None, *, on_event: Optional[OnEvent] = None) -> None:
    """Cierra el tunel del destino del alias. Solo cierra lo que esta libreria abrio."""
    _app, cfg = _config.resolve(db, on_event=on_event)
    ssh = cfg.ssh
    if ssh is None:  # pragma: no cover
        return
    with _lock:
        info = _current.get(_dest_key(ssh, cfg.host, cfg.port))
    if info is None:
        emit(
            on_event,
            level="DEBUG",
            event="tunnel_close",
            message=f"No hay tunel registrado para {cfg.host}:{cfg.port}.",
            db=cfg.alias,
        )
        return
    _forget(info, close=True, on_event=on_event)


def close_all_tunnels(*, on_event: Optional[OnEvent] = None) -> None:
    """Cierra todos los tuneles propios. Los adoptados solo se dejan de rastrear."""
    with _lock:
        infos = list(_opened)
    for info in infos:
        _forget(info, close=True, on_event=on_event)


def invalidate_tunnel(info: TunnelInfo, *, on_event: Optional[OnEvent] = None) -> None:
    """
    Descarta un tunel que dejo de responder.

    Dispone primero el pool de SQLAlchemy y luego cierra el tunel, en ese orden, y
    solo lo cierra si esta libreria lo abrio.
    """
    _forget(info, close=True, on_event=on_event)


def tunnel_status() -> List[TunnelInfo]:
    with _lock:
        return list(_opened)


@contextmanager
def tunnel(
    db: Optional[str] = None, *, on_event: Optional[OnEvent] = None
) -> Iterator[TunnelInfo]:
    """
    Control explicito del ciclo de vida del tunel.

    Cierra al salir unicamente lo que este bloque abrio: un tunel que ya estaba
    vivo (propio o externo) sigue vivo despues del bloque.
    """
    before = {id(info) for info in tunnel_status()}
    info = open_tunnel(db, on_event=on_event)
    opened_here = id(info) not in before
    try:
        yield info
    finally:
        if opened_here:
            _forget(info, close=True, on_event=on_event)


def _reset_for_tests() -> None:
    """Limpia el registro sin cerrar nada. Uso exclusivo de la suite de tests."""
    with _lock:
        _current.clear()
        _opened.clear()


__all__ = [
    "close_all_tunnels",
    "close_tunnel",
    "ensure_tunnel",
    "invalidate_tunnel",
    "open_tunnel",
    "port_is_free",
    "probe_postgres",
    "tunnel",
    "tunnel_status",
]
