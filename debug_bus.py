"""Debug event bus with TCP JSON Lines server.

Other modules call ``emit(event_type, data)`` to publish events.  When no
debug client is connected the call is essentially a no-op (fast path).

A lightweight TCP server on ``127.0.0.1:8766`` streams events as newline-
delimited JSON to any connected client.
"""

import json
import logging
import socket
import threading
import time

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8766

# ── Singleton state ──────────────────────────────────────────────────────────

_lock = threading.Lock()
_clients: list[socket.socket] = []
_server_thread: threading.Thread | None = None
_shutdown_event: threading.Event | None = None


def emit(event_type: str, data: dict | None = None):
    """Publish a debug event.  No-op when no clients are connected."""
    with _lock:
        if not _clients:
            return
        targets = list(_clients)

    event = {
        "ts": time.time(),
        "type": event_type,
        "data": data or {},
    }
    line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
    raw = line.encode("utf-8")

    dead: list[socket.socket] = []
    for sock in targets:
        try:
            sock.sendall(raw)
        except OSError:
            dead.append(sock)

    if dead:
        with _lock:
            for s in dead:
                try:
                    _clients.remove(s)
                except ValueError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass


def _accept_loop(server_sock: socket.socket, shutdown: threading.Event):
    """Accept new debug clients until shutdown."""
    server_sock.settimeout(1.0)
    while not shutdown.is_set():
        try:
            conn, addr = server_sock.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with _lock:
                _clients.append(conn)
            logger.info("Debug client connected: %s", addr)
        except socket.timeout:
            continue
        except OSError:
            break
    # Cleanup
    with _lock:
        for s in _clients:
            try:
                s.close()
            except OSError:
                pass
        _clients.clear()
    server_sock.close()


def start(port: int = _DEFAULT_PORT, shutdown_event: threading.Event | None = None):
    """Start the debug TCP server (idempotent)."""
    global _server_thread, _shutdown_event
    if _server_thread is not None:
        return

    _shutdown_event = shutdown_event or threading.Event()
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_sock.bind(("127.0.0.1", port))
    except OSError as e:
        logger.warning("Debug server bind failed on port %d: %s", port, e)
        return
    server_sock.listen(4)
    _server_thread = threading.Thread(
        target=_accept_loop, args=(server_sock, _shutdown_event),
        name="debug_server", daemon=True,
    )
    _server_thread.start()
    logger.info("Debug server listening on 127.0.0.1:%d", port)


def stop():
    """Stop the debug server."""
    global _server_thread
    if _shutdown_event:
        _shutdown_event.set()
    if _server_thread:
        _server_thread.join(timeout=3)
        _server_thread = None
