"""Local Python control service. No PHP or cloud dependency.

Loopback-only by default. A headless Linux VM (e.g. on Proxmox) can either be
reached through an SSH tunnel, or bind a LAN address with --host, which then
requires a SPANKVANC_PASSWORD (HTTP Basic) and an explicit Host allowlist.
"""
from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket

from .engine import ConfigError, Engine, plan, scrub


WEB = Path(__file__).parent / "web"


LOOPBACK_NAMES = ("127.0.0.1", "localhost")


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_port(value):
    """Split a Host header into (lowercase host, port or None)."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else None
    else:
        host, _, port = value.partition(":") if value.count(":") == 1 else (value, "", "")
        port = port or None
    return host, port


def make_handler(engine, token, port, allowed_hosts=LOOPBACK_NAMES, password=None):
    allowed = {h.lower().strip("[]") for h in allowed_hosts}
    origins = {f"http://{h}:{port}" if ":" not in h else f"http://[{h}]:{port}" for h in allowed}

    class Handler(BaseHTTPRequestHandler):
        server_version = "SpankVANC/0.14.0"

        def authorized_host(self):
            # Exact Host allowlist blocks DNS rebinding from other web pages.
            host, request_port = host_port(self.headers.get("Host"))
            return host in allowed and request_port == str(port)

        def authenticated(self):
            if password is None:
                return True
            scheme, _, value = (self.headers.get("Authorization") or "").partition(" ")
            try:
                supplied = base64.b64decode(value, validate=True).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                return False
            return scheme.lower() == "basic" and secrets.compare_digest(supplied.encode(), password.encode())

        def challenge(self):
            body = b"Authentication required"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="SpankVANC", charset="UTF-8"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def reply(self, data, status=200, mime="application/json"):
            if mime == "application/json":
                data = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if password is not None and not self.authenticated():
                return self.challenge()
            if not self.authorized_host():
                return self.reply({"error": "Invalid host"}, 403)
            if self.path in ("/api/state", "/api/monitor"):
                if not secrets.compare_digest(self.headers.get("X-SpankVANC-Token", ""), token):
                    return self.reply({"error": "Invalid control token"}, 403)
                return self.reply(engine.monitor_state() if self.path == "/api/monitor" else engine.state())
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path not in files:
                return self.reply({"error": "Not found"}, 404)
            filename, mime = files[self.path]
            data = (WEB / filename).read_bytes()
            if filename == "index.html":
                data = data.replace(b"__CONTROL_TOKEN__", token.encode())
            self.reply(data, mime=mime)

        def do_POST(self):
            if password is not None and not self.authenticated():
                return self.challenge()
            if not self.authorized_host() or not secrets.compare_digest(self.headers.get("X-SpankVANC-Token", ""), token):
                return self.reply({"error": "Invalid control token"}, 403)
            if self.headers.get("Origin") not in (None, *origins):
                return self.reply({"error": "Invalid origin"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ConfigError("Request body must contain 1–65536 bytes")
                data = json.loads(self.rfile.read(length))
                if self.path == "/api/monitor/start":
                    result = engine.monitor_start(data)
                elif self.path == "/api/monitor/stop":
                    result = engine.monitor_stop()
                elif self.path == "/api/translation":
                    result = engine.translation.submit(data.get("text"))
                elif self.path == "/api/devices":
                    result = engine.refresh_devices()
                elif self.path == "/api/plan":
                    result = plan(data, engine.capabilities)
                elif self.path == "/api/save":
                    engine.save(data)
                    result = engine.state()
                elif self.path == "/api/start":
                    result = engine.start(data)
                elif self.path == "/api/scte/inject":
                    result = engine.inject_scte(data)
                elif self.path == "/api/stop":
                    result = engine.stop()
                else:
                    return self.reply({"error": "Not found"}, 404)
                self.reply(result)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                self.reply({"error": scrub(str(exc))}, 400)

        def log_message(self, fmt, *args):
            # Media URLs and saved configuration never enter the HTTP access log.
            pass

    return Handler


class DualStackServer(ThreadingHTTPServer):
    daemon_threads = True


def serve(port, state_dir, host="127.0.0.1", allow_hosts=()):
    if not 1 <= port <= 65535:
        raise ConfigError("Port must be between 1 and 65535")
    allowed = list(LOOPBACK_NAMES)
    password = None
    if not is_loopback(host):
        password = os.environ.get("SPANKVANC_PASSWORD") or None
        if not password or len(password) < 12:
            raise ConfigError("Binding beyond loopback needs SPANKVANC_PASSWORD (12+ characters); "
                              "or keep the default and use an SSH tunnel")
        names = [h.strip().lower().strip("[]") for h in allow_hosts if h.strip()]
        if host not in ("0.0.0.0", "::"):
            names.append(host.lower())
        if not names:
            raise ConfigError("Name the address you will browse to with --allow-host (VM IP or DNS name)")
        allowed += names
    engine = Engine(state_dir)
    DualStackServer.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server = DualStackServer((host, port), make_handler(engine, secrets.token_urlsafe(32), port, allowed, password))
    for name in allowed if password else allowed[:1]:
        shown = f"[{name}]" if ":" in name else name
        print(f"SpankVANC control panel: http://{shown}:{port}", flush=True)
    if password:
        print("LAN access enabled: HTTP Basic password required. Traffic is not encrypted; use only on a trusted network or VPN.", flush=True)
    print("Development preview. SCTE-104 program-splice conversion is available with the private build. SDI playout and live Whisper captions require the matching local engine.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        engine.monitor_stop()
        server.server_close()
