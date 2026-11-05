"""Raw-text caption delivery over TCP (Telnet-style) to an external encoder.

Whisper phrases are sent as plain text lines instead of being embedded in SDI
VANC. This needs no DeckLink hardware or patched FFmpeg, so it suits a Linux
VM (for example on Proxmox) that receives program audio over UDP/SRT/file.

Two connection modes:
  tcp://encoder-host:port            client: connect to the caption encoder
  tcp://0.0.0.0:port?listen=1        listener: encoders/telnet clients connect

Received bytes (prompts, Telnet negotiation) are read and discarded; nothing
from the network is executed or interpreted.
"""
from __future__ import annotations

from collections import deque
import queue
import select
import socket
import threading
import time
import unicodedata
from urllib.parse import parse_qsl, urlsplit

LINE_ENDINGS = {"crlf": b"\r\n", "cr": b"\r", "lf": b"\n"}
MAX_LISTEN_CLIENTS = 8
QUEUE_LIMIT = 64
_TRANSLITERATE = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-", "…": "..."})


class TcpAddressError(ValueError):
    pass


def parse_address(address: str) -> tuple[str, int, bool]:
    """Return (host, port, listen) for tcp://host:port[?listen=1]."""
    parts = urlsplit(address)
    try:
        port = parts.port
    except ValueError as exc:
        raise TcpAddressError("Invalid TCP port") from exc
    if parts.scheme != "tcp" or not parts.hostname or port is None or not 1 <= port <= 65535:
        raise TcpAddressError("Caption TCP output needs tcp://host:port")
    if parts.path not in ("", "/") or parts.fragment or parts.username or parts.password:
        raise TcpAddressError("Caption TCP address accepts only tcp://host:port[?listen=1]")
    options = dict(parse_qsl(parts.query, keep_blank_values=True))
    if set(options) - {"listen"} or options.get("listen", "0") not in ("0", "1"):
        raise TcpAddressError("Caption TCP address supports only ?listen=1")
    return parts.hostname, port, options.get("listen") == "1"


def format_line(text: str, *, uppercase: bool = False, ending: str = "crlf") -> bytes:
    """Encoder-safe printable ASCII; one Whisper phrase per line."""
    text = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATE))
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Collapse whitespace (including embedded newlines) before replacing any
    # remaining control/non-ASCII characters, so one phrase stays one line.
    text = " ".join("".join(c if 32 <= ord(c) < 127 else "?" for c in " ".join(text.split())).split())
    if not text:
        return b""
    if uppercase:
        text = text.upper()
    return text.encode("ascii") + LINE_ENDINGS[ending]


class CaptionSocket:
    """One TCP caption destination with its own thread; never blocks callers."""

    def __init__(self, name, address, *, uppercase=False, ending="crlf", log=print,
                 connect_timeout=5.0, retry_max=10.0):
        self.name, self.address = name, address
        self.host, self.port, self.listen = parse_address(address)
        self.uppercase, self.ending, self.log = uppercase, ending, log
        self.connect_timeout, self.retry_max = connect_timeout, retry_max
        self.queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.sent = self.dropped = 0
        self.clients = 0
        self.status = "starting"
        self.last_error = ""
        self.recent = deque(maxlen=10)
        self.server = None
        self.thread = threading.Thread(target=self._run, name="caption-tcp-" + name, daemon=True)

    # Public API -----------------------------------------------------------
    def start(self):
        if self.listen:
            # Bind synchronously so a busy port fails the route start visibly.
            family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
            server = socket.socket(family, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind((self.host, self.port))
                server.listen(MAX_LISTEN_CLIENTS)
            except OSError:
                server.close()
                raise
            self.server = server
            self.port = server.getsockname()[1]
        self.thread.start()
        return self

    def send(self, text):
        line = format_line(text, uppercase=self.uppercase, ending=self.ending)
        if not line:
            return False
        try:
            self.queue.put_nowait(line)
            return True
        except queue.Full:
            # Keep captions live: discard the oldest stale phrase.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            with self.lock:
                self.dropped += 1
            self.queue.put_nowait(line)
            return True

    def close(self):
        if not self.stopping.is_set():
            self._set("closed")
        self.stopping.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def state(self):
        with self.lock:
            return {"name": self.name, "mode": "listen" if self.listen else "client",
                    "endpoint": f"{self.host}:{self.port}", "status": self.status,
                    "clients": self.clients, "sent": self.sent, "dropped": self.dropped,
                    "error": self.last_error, "recent": list(self.recent)}

    # Internals ------------------------------------------------------------
    def _set(self, status, error=None):
        with self.lock:
            changed = status != self.status
            self.status = status
            if error is not None:
                self.last_error = error
        if changed:
            self.log(f"Caption TCP {self.name}: {status}" + (f" ({error})" if error else ""))

    def _sent(self, line):
        with self.lock:
            self.sent += 1
            self.recent.append(line.rstrip(b"\r\n").decode("ascii"))

    def _run(self):
        try:
            (self._serve if self.listen else self._connect_loop)()
        except Exception as exc:  # never take down the engine thread
            self._set("failed", str(exc))
        finally:
            if not self.stopping.is_set():
                self._set("stopped")

    @staticmethod
    def _drain(sock):
        """Discard any bytes the peer sent (prompts, Telnet IAC negotiation)."""
        while True:
            readable, _, _ = select.select([sock], [], [], 0)
            if not readable:
                return True
            if not sock.recv(4096):
                return False  # orderly close by peer

    def _next_line(self, timeout=0.5):
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return b""

    def _connect_loop(self):
        delay = 1.0
        while not self.stopping.is_set():
            self._set("connecting")
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
            except OSError as exc:
                self._set("disconnected", str(exc))
                self.stopping.wait(delay)
                delay = min(delay * 2, self.retry_max)
                continue
            delay = 1.0
            sock.settimeout(self.connect_timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            with self.lock:
                self.clients = 1
            self._set("connected", "")
            pending = b""
            try:
                while not self.stopping.is_set():
                    if not self._drain(sock):
                        raise ConnectionError("encoder closed the connection")
                    line = pending or self._next_line()
                    if line is None:
                        return
                    if line:
                        pending = line
                        sock.sendall(line)
                        pending = b""
                        self._sent(line)
            except OSError as exc:
                self._set("disconnected", str(exc))
                if pending:
                    # Resend the phrase that failed once reconnected.
                    try:
                        self.queue.put_nowait(pending)
                    except queue.Full:
                        pass
            finally:
                with self.lock:
                    self.clients = 0
                sock.close()
            self.stopping.wait(delay)

    def _serve(self):
        server, peers = self.server, []
        self._set("listening", "")
        try:
            while not self.stopping.is_set():
                readable, _, _ = select.select([server, *peers], [], [], 0.05)
                for sock in readable:
                    if sock is server:
                        try:
                            peer, remote = server.accept()
                        except OSError:
                            continue
                        if len(peers) >= MAX_LISTEN_CLIENTS:
                            peer.close()
                            continue
                        peer.settimeout(self.connect_timeout)
                        peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        peers.append(peer)
                        self.log(f"Caption TCP {self.name}: client {remote[0]} connected")
                    else:
                        try:
                            alive = bool(sock.recv(4096))
                        except OSError:
                            alive = False
                        if not alive:
                            peers.remove(sock)
                            sock.close()
                with self.lock:
                    self.clients = len(peers)
                line = self._next_line(timeout=0.05)
                if line is None:
                    return
                if not line:
                    continue
                for peer in list(peers):
                    try:
                        peer.sendall(line)
                    except OSError:
                        peers.remove(peer)
                        peer.close()
                if peers:
                    self._sent(line)
                else:
                    with self.lock:
                        self.dropped += 1
        finally:
            for peer in peers:
                peer.close()


class CaptionFanout:
    """All caption TCP outputs of the running route."""

    def __init__(self, sinks):
        self.sinks = sinks

    @classmethod
    def start(cls, cfg, log):
        sinks = []
        try:
            for out in cfg["outputs"]:
                if out["enabled"] and out["kind"] == "tcp":
                    sinks.append(CaptionSocket(out["name"], out["address"],
                        uppercase=out.get("tcp_uppercase", False),
                        ending=out.get("tcp_line_ending", "crlf"), log=log).start())
        except OSError:
            for sink in sinks:
                sink.close()
            raise
        return cls(sinks) if sinks else None

    def send(self, text):
        for sink in self.sinks:
            sink.send(text)

    def close(self):
        for sink in self.sinks:
            sink.close()

    def state(self):
        return [sink.state() for sink in self.sinks]


def wait_for(predicate, timeout=5.0):
    """Small test/diagnostic helper."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False
