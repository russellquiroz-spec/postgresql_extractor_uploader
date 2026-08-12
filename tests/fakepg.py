"""
Servidor TCP que se hace pasar por PostgreSQL ante el health check del tunel.

`probe_postgres` manda un SSLRequest del protocolo de PostgreSQL y espera 'S', 'N' o
'E'. Con esto los tests de tunel no necesitan una base real.
"""

from __future__ import annotations

import socket
import threading
from typing import Optional


class FakePostgres:
    def __init__(self, host: str = "127.0.0.1", reply: bytes = b"N") -> None:
        self.host = host
        self.reply = reply
        self.port = 0
        self.connections = 0
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self, port: int = 0) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, port))
        listener.listen(32)
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
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
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(2.0)
            client.recv(8)
            client.sendall(self.reply)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass


class DumbTCPServer:
    """Escucha y no contesta nada: sirve para simular un puerto ocupado que NO es PostgreSQL."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = 0
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self, port: int = 0) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, port))
        listener.listen(8)
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        clients = []
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
            clients.append(client)  # se queda callado a proposito
        for client in clients:
            try:
                client.close()
            except OSError:
                pass


__all__ = ["DumbTCPServer", "FakePostgres"]
