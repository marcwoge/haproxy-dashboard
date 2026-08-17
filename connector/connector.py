"""Platform Connector - self-registers this project's service with the platform.

Drop this container into another compose project. On start it registers the
service (dashboard tile + HAProxy route) with the config-app API, keeps the entry
fresh with a periodic heartbeat, and deregisters again on shutdown - so the tile
appears when the project comes up and disappears when it goes down.

Phase 2: MODE=register (the app must join the shared platform network; the route
points directly at SERVICE_TARGET). MODE=proxy (the connector proxies traffic so
the app can stay isolated) arrives in a later build.

Pure Python standard library - no dependencies.
"""
import json
import logging
import os
import signal
import ssl
import sys
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


# ---- configuration (from env) ---------------------------------------------
API = _env("PLATFORM_API").rstrip("/")            # e.g. https://platform.example.local
TOKEN = _env("PLATFORM_TOKEN")                    # must match config-app CONNECTOR_TOKEN
MODE = _env("MODE", "register").lower()           # register | proxy (proxy: later build)
INSECURE = _env_bool("INSECURE", False)           # skip TLS verify (self-signed platform)
CA_BUNDLE = _env("CA_BUNDLE")                      # optional path to a CA bundle
HEARTBEAT = max(0, int(_env("HEARTBEAT", "60") or "60"))   # re-register interval (s); 0 = off
RETRY = max(1, int(_env("RETRY_INTERVAL", "5") or "5"))    # backoff while platform is down
CONNECTOR_ID = _env("CONNECTOR_ID") or _env("SERVICE_NAME") or "connector"

SERVICE = {
    "name": _env("SERVICE_NAME"),
    "path": _env("SERVICE_PATH"),
    "backend": _env("SERVICE_TARGET"),            # <app>:<port> reachable by HAProxy
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


def _shutdown(signum, _frame):
    global _running
    log.info("signal %s received - shutting down", signum)
    _running = False


def _validate_config() -> None:
    missing = [k for k, v in {
        "PLATFORM_API": API, "PLATFORM_TOKEN": TOKEN,
        "SERVICE_NAME": SERVICE["name"], "SERVICE_PATH": SERVICE["path"],
        "SERVICE_TARGET": SERVICE["backend"],
    }.items() if not v]
    if missing:
        log.error("missing required env: %s", ", ".join(missing))
        sys.exit(2)
    if MODE != "register":
        log.error("MODE=%s is not supported in this build - use MODE=register", MODE)
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
    log.info("platform connector starting (mode=register, api=%s, path=%s)",
             API, SERVICE["path"])

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
