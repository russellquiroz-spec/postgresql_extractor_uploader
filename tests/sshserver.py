"""
Servidor SSH en proceso con forwarding direct-tcpip, para probar el tunel de verdad.

Permite cubrir apertura, reuso, caida, cierre y los tres modos de fallo sin depender
de la VM. Si se le apunta a un PostgreSQL real, los tests de datos corren a traves de
un tunel de verdad y no de un mock.
"""

from __future__ import annotations

import select
import socket
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import paramiko

Address = Tuple[str, int]


class _ServerInterface(paramiko.ServerInterface):
    def __init__(
        self,
        allowed_pubkey: Optional[paramiko.PKey],
        password: Optional[str],
        destinations: Dict[int, Address],
    ) -> None:
        self.allowed_pubkey = allowed_pubkey
        self.password = password
        self.destinations = destinations

    def get_allowed_auths(self, username: str) -> str:
        methods = []
        if self.allowed_pubkey is not None:
            methods.append("publickey")
        if self.password is not None:
            methods.append("password")
        return ",".join(methods) or "none"

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        if self.allowed_pubkey is not None and key == self.allowed_pubkey:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username: str, password: str) -> int:
        if self.password is not None and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "direct-tcpip":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(
        self, chanid: int, origin: Address, destination: Address
    ) -> int:
        self.destinations[chanid] = destination
        return paramiko.OPEN_SUCCEEDED


class ForwardingSSHServer:
    """
    Servidor SSH de prueba que reenvia lo que le pidan por direct-tcpip.

    Uso:
        server = ForwardingSSHServer(allowed_pubkey=key)
        server.start()
        ...
        server.stop()
    """

    def __init__(
        self,
        *,
        allowed_pubkey: Optional[paramiko.PKey] = None,
        password: Optional[str] = None,
        host: str = "127.0.0.1",
    ) -> None:
        self.host = host
        self.port = 0
        self.host_key = paramiko.RSAKey.generate(2048)
        self.allowed_pubkey = allowed_pubkey
        self.password = password

        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._transports: List[paramiko.Transport] = []
        self._lock = threading.Lock()
        self.channels_opened = 0
        self.auth_failures = 0

    # -- ciclo de vida ------------------------------------------------------
    def start(self) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, 0))
        listener.listen(16)
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        self.kill_sessions()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5)
            self._accept_thread = None

    def kill_sessions(self) -> None:
        """
        Mata las sesiones SSH dejando el listener local de sshtunnel vivo.

        Es la forma de reproducir un tunel zombie: el proceso local sigue
        escuchando pero del otro lado ya no hay nada.
        """
        with self._lock:
            transports = list(self._transports)
            self._transports.clear()
        for transport in transports:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    def write_known_hosts(self, path: Path) -> Path:
        host_keys = paramiko.HostKeys()
        # Puerto distinto de 22, asi que la entrada va en formato [host]:puerto.
        host_keys.add(f"[{self.host}]:{self.port}", self.host_key.get_name(), self.host_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        host_keys.save(str(path))
        return path

    def write_wrong_known_hosts(self, path: Path) -> Path:
        """known_hosts con una host key que NO es la de este servidor."""
        other = paramiko.RSAKey.generate(2048)
        host_keys = paramiko.HostKeys()
        host_keys.add(f"[{self.host}]:{self.port}", other.get_name(), other)
        path.parent.mkdir(parents=True, exist_ok=True)
        host_keys.save(str(path))
        return path

    # -- internos -----------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client: socket.socket) -> None:
        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_key)
        destinations: Dict[int, Address] = {}
        interface = _ServerInterface(self.allowed_pubkey, self.password, destinations)
        try:
            transport.start_server(server=interface)
        except Exception:  # noqa: BLE001 - handshake fallido, no es un error del test
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass
            return

        with self._lock:
            self._transports.append(transport)

        while not self._stop.is_set():
            try:
                channel = transport.accept(timeout=0.5)
            except Exception:  # noqa: BLE001
                break
            if channel is None:
                if not transport.is_active():
                    break
                continue
            destination = destinations.get(channel.get_id())
            if destination is None:
                channel.close()
                continue
            with self._lock:
                self.channels_opened += 1
            threading.Thread(
                target=self._pipe, args=(channel, destination), daemon=True
            ).start()

        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass

    def _pipe(self, channel: paramiko.Channel, destination: Address) -> None:
        try:
            remote = socket.create_connection(destination, timeout=5)
        except OSError:
            channel.close()
            return

        try:
            while True:
                readable, _, _ = select.select([channel, remote], [], [], 0.5)
                if channel in readable:
                    data = channel.recv(32768)
                    if not data:
                        break
                    remote.sendall(data)
                if remote in readable:
                    data = remote.recv(32768)
                    if not data:
                        break
                    channel.sendall(data)
        except (OSError, EOFError):
            pass
        finally:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                remote.close()
            except OSError:
                pass


def generate_client_key(directory: Path, name: str = "id_rsa") -> Tuple[paramiko.PKey, Path]:
    """Genera una llave de cliente y la escribe a disco. Devuelve (llave, ruta)."""
    key = paramiko.RSAKey.generate(2048)
    path = directory / name
    directory.mkdir(parents=True, exist_ok=True)
    key.write_private_key_file(str(path))
    return key, path


__all__ = ["ForwardingSSHServer", "generate_client_key"]
