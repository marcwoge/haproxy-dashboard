"""Platform Connector - self-registers this project's service with the platform.

Drop this container into another compose project. On start it registers the
service (dashboard tile + HAProxy route) with the config-app API, keeps it fresh
with a periodic heartbeat, and deregisters again on shutdown - so the tile appears
when the project comes up and disappears when it goes down.

Two modes:
  * MODE=register - your app joins the shared platform network; the route points
    directly at SERVICE_TARGET.
  * MODE=proxy    - the connector listens on the shared network and forwards TCP
    to SERVICE_TARGET, so your app can stay on its own network. The registered
    backend is the connector itself (CONNECTOR_HOST:LISTEN_PORT).

Pure Python standard library - no dependencies.
"""
import asyncio
import json
import logging
import os
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("connector")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _parse_hostport(t: str):
    """Split 'host:port' (IPv6 in [brackets]) into (host, int port)."""
    if t.startswith("["):
        host, _, port = t[1:].partition("]")
        port = port.lstrip(":")
    else:
        host, _, port = t.rpartition(":")
    return host, int(port)


# ---- configuration (from env) ---------------------------------------------
API = _env("PLATFORM_API").rstrip("/")            # e.g. https://platform.example.local
TOKEN = _env("PLATFORM_TOKEN")                    # must match config-app CONNECTOR_TOKEN
MODE = _env("MODE", "register").lower()           # register | proxy
INSECURE = _env_bool("INSECURE", False)           # skip TLS verify (self-signed platform)
CA_BUNDLE = _env("CA_BUNDLE")                      # optional path to a CA bundle
HEARTBEAT = max(0, int(_env("HEARTBEAT", "60") or "60"))   # re-register interval (s); 0 = off
RETRY = max(1, int(_env("RETRY_INTERVAL", "5") or "5"))    # backoff while platform is down
CONNECTOR_ID = _env("CONNECTOR_ID") or _env("SERVICE_NAME") or "connector"

TARGET = _env("SERVICE_TARGET")                   # proxy: forward here / register: backend
LISTEN_PORT = int(_env("LISTEN_PORT", "8080") or "8080")   # proxy: listen port (shared net)
# Host HAProxy uses to reach the connector on the shared network (its own name).
CONNECTOR_HOST = _env("CONNECTOR_HOST") or socket.gethostname()

# In proxy mode HAProxy connects to the connector; in register mode to the app.
_BACKEND = f"{CONNECTOR_HOST}:{LISTEN_PORT}" if MODE == "proxy" else TARGET

SERVICE = {
    "name": _env("SERVICE_NAME"),
    "path": _env("SERVICE_PATH"),
    "backend": _BACKEND,
    "scheme": _env("SERVICE_SCHEME", "http"),
    "ssl_verify": _env_bool("SERVICE_SSL_VERIFY", False),
    "strip_path": _env_bool("SERVICE_STRIP_PATH", False),
    "icon": _env("SERVICE_ICON"),
    "color": _env("SERVICE_COLOR"),
    "description": _env("SERVICE_DESCRIPTION"),
    "enabled": _env_bool("SERVICE_ENABLED", True),
    "connector": CONNECTOR_ID,                    # used as the audit actor
}

_running = True


# ---- platform API ----------------------------------------------------------
def _ssl_context():
    if not API.startswith("https"):
        return None
    if INSECURE:
        log.warning("INSECURE=true - TLS certificate verification is DISABLED")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=CA_BUNDLE or None)


_CTX = _ssl_context()


def _post(path: str, payload: dict):
    """POST JSON to the platform API. Returns (status, body). Raises on network error."""
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def register() -> bool:
    status, body = _post("/api/v1/register", SERVICE)
    if status == 200:
        log.info("registered %s -> %s", SERVICE["path"], SERVICE["backend"])
        return True
    log.error("register failed (HTTP %s): %s", status, body)
    return False


def deregister() -> None:
    try:
        status, body = _post("/api/v1/deregister",
                             {"path": SERVICE["path"], "connector": CONNECTOR_ID})
        if status == 200:
            log.info("deregistered %s", SERVICE["path"])
        else:
            log.warning("deregister returned HTTP %s: %s", status, body)
    except Exception as e:  # noqa: BLE001 - shutdown is best-effort
        log.warning("deregister failed: %s", e)


# ---- TCP proxy (MODE=proxy) ------------------------------------------------
async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001 - connection reset etc.
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(client_reader, client_writer) -> None:
    host, port = _parse_hostport(TARGET)
    try:
        up_reader, up_writer = await asyncio.open_connection(host, port)
    except Exception as e:  # noqa: BLE001 - app not reachable
        log.warning("proxy: upstream %s connect failed: %s", TARGET, e)
        client_writer.close()
        return
    await asyncio.gather(_pipe(client_reader, up_writer),
                         _pipe(up_reader, client_writer))


def _start_proxy() -> None:
    """Bind synchronously (so bind errors are fatal at startup), then serve the
    TCP proxy in a background asyncio loop."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", LISTEN_PORT))
    except OSError as e:
        log.error("proxy: cannot bind :%d - %s", LISTEN_PORT, e)
        sys.exit(2)
    sock.listen(128)
    sock.setblocking(False)

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(asyncio.start_server(_handle, sock=sock))
        log.info("proxy listening on :%d -> %s", LISTEN_PORT, TARGET)
        loop.run_forever()

    threading.Thread(target=_run, name="proxy", daemon=True).start()


# ---- lifecycle -------------------------------------------------------------
def _shutdown(signum, _frame):
    global _running
    log.info("signal %s received - shutting down", signum)
    _running = False


def _validate_config() -> None:
    missing = [k for k, v in {
        "PLATFORM_API": API, "PLATFORM_TOKEN": TOKEN,
        "SERVICE_NAME": SERVICE["name"], "SERVICE_PATH": SERVICE["path"],
        "SERVICE_TARGET": TARGET,
    }.items() if not v]
    if missing:
        log.error("missing required env: %s", ", ".join(missing))
        sys.exit(2)
    if MODE not in ("register", "proxy"):
        log.error("MODE=%s is invalid - use 'register' or 'proxy'", MODE)
        sys.exit(2)
    if MODE == "proxy":
        if not 1 <= LISTEN_PORT <= 65535:
            log.error("LISTEN_PORT=%s is out of range", LISTEN_PORT)
            sys.exit(2)
        try:
            _parse_hostport(TARGET)
        except (ValueError, TypeError):
            log.error("SERVICE_TARGET=%s must be host:port in proxy mode", TARGET)
            sys.exit(2)


def _sleep_interruptible(seconds: int) -> None:
    for _ in range(seconds):
        if not _running:
            return
        time.sleep(1)


def main() -> None:
    _validate_config()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    log.info("platform connector starting (mode=%s, api=%s, path=%s, backend=%s)",
             MODE, API, SERVICE["path"], SERVICE["backend"])

    if MODE == "proxy":
        _start_proxy()

    # Initial registration with retry (the platform may still be starting up).
    while _running and not register():
        _sleep_interruptible(RETRY)

    # Heartbeat: re-register periodically so the entry survives a config-app
    # restart and stays marked live. HEARTBEAT=0 disables it (register once).
    while _running and HEARTBEAT:
        _sleep_interruptible(HEARTBEAT)
        if _running:
            try:
                register()
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat register failed: %s", e)

    # Idle wait if heartbeat is disabled but we are still running.
    while _running and not HEARTBEAT:
        _sleep_interruptible(3600)

    deregister()
    log.info("connector stopped")


if __name__ == "__main__":
    main()
