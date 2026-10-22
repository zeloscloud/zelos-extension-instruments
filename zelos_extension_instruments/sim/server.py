"""Serves a fake instrument, or replays a recorded transcript, over line-framed TCP."""

import contextlib
import socket
import socketserver
import sys
import threading
from collections import deque
from collections.abc import Sequence
from typing import Any, Self

from .fake import FakeInstrument
from .faults import Faults

_GARBAGE = b"\xff\xfe??"


class FakeServer:
    """Serves one FakeInstrument over TCP on a thread, or replays a transcript strictly.

    Commands end with the client's write terminator and replies with the read terminator it
    expects, from the instrument's profile unless given. In replay mode the server answers only
    the next expected command; anything else is recorded in `mismatch` and closes the connection.
    """

    def __init__(
        self,
        instrument: FakeInstrument | None = None,
        *,
        replay: Sequence[tuple[str, str | None]] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        faults: Faults | None = None,
        write_terminator: str | None = None,
        read_terminator: str | None = None,
    ) -> None:
        if instrument is None and replay is None:
            raise ValueError("a FakeServer needs an instrument or a transcript to replay")
        link = instrument.profile.link if instrument is not None else None
        if write_terminator is None:
            write_terminator = link.write_terminator if link is not None else "\n"
        if read_terminator is None:
            read_terminator = link.read_terminator if link is not None else "\n"
        if not write_terminator or not read_terminator:
            raise ValueError("a FakeServer needs non-empty terminators to frame lines")
        self.instrument = instrument
        self.faults = faults if faults is not None else Faults()
        self.host = host
        self.mismatch: str | None = None
        self._port = port
        self._command_end = write_terminator.encode()
        self._reply_end = read_terminator.encode()
        self._replay = deque(replay) if replay is not None else None
        self._lock = threading.Lock()
        self._clients: set[socket.socket] = set()
        self._stopping = threading.Event()
        self._tcp: _TcpServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._tcp is None:
            raise RuntimeError("the server is not started")
        return self._tcp.socket.getsockname()[1]

    @property
    def remaining(self) -> list[tuple[str, str | None]]:
        """The transcript pairs not yet replayed."""
        with self._lock:
            return list(self._replay or ())

    def start(self) -> Self:
        if self._tcp is not None:
            return self
        self._stopping.clear()
        self._tcp = _TcpServer(self, (self.host, self._port))
        self.faults._restart_hooks.append(self._drop_clients)
        self._thread = threading.Thread(
            target=self._tcp.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        tcp, thread = self._tcp, self._thread
        if tcp is None:
            return
        self._stopping.set()
        tcp.shutdown()
        self._drop_clients()
        tcp.server_close()
        if thread is not None:
            thread.join()
        self.faults._restart_hooks.remove(self._drop_clients)
        self._tcp = self._thread = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _admit(self, sock: socket.socket) -> bool:
        with self._lock:
            if self.faults.refuse_second_client and self._clients:
                return False
            self._clients.add(sock)
            return True

    def _drop_clients(self) -> None:
        with self._lock:
            clients = list(self._clients)
        for sock in clients:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            # Windows wakes a blocked recv only when the socket closes, not on shutdown.
            sock.close()

    def _serve(self, sock: socket.socket) -> None:
        buffer = b""
        try:
            while True:
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while self._command_end in buffer:
                    raw, buffer = buffer.split(self._command_end, 1)
                    if not self._respond(sock, raw.decode("utf-8", "replace").strip()):
                        return
        finally:
            with self._lock:
                self._clients.discard(sock)

    def _respond(self, sock: socket.socket, command: str) -> bool:
        """Answer one command; False when the connection should close."""
        if not command:
            return True
        with self._lock:
            if self._replay is None:
                assert self.instrument is not None
                reply = self.instrument.handle(command)
            elif self.mismatch is not None:
                return False
            elif not self._replay:
                self.mismatch = f"got {command!r} after the transcript ended"
                return False
            elif command != self._replay[0][0]:
                self.mismatch = f"expected {self._replay[0][0]!r}, got {command!r}"
                return False
            else:
                reply = self._replay.popleft()[1]
        if reply is None:
            return True
        return self._send(sock, reply)

    def _send(self, sock: socket.socket, reply: str | bytes) -> bool:
        fault = self.faults.next_reply()
        if fault.stall > 0 and self._stopping.wait(fault.stall):
            return False
        if fault.close_before:
            return False
        data = bytes(reply) if isinstance(reply, bytes) else reply.encode()
        try:
            if fault.half:
                sock.sendall(data[: len(data) // 2])
                return False
            sock.sendall((_GARBAGE if fault.garbage else data) + self._reply_end)
        except OSError:
            return False
        return not fault.close_after


class _TcpServer(socketserver.ThreadingTCPServer):
    # Windows lets a second socket bind a port SO_REUSEADDR has marked.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, owner: FakeServer, address: tuple[str, int]) -> None:
        self.owner = owner
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    def verify_request(self, request: Any, client_address: Any) -> bool:
        return self.owner._admit(request)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server, sock = self.server, self.request
        if isinstance(server, _TcpServer) and isinstance(sock, socket.socket):
            server.owner._serve(sock)
