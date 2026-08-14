"""Erzeugt eine haproxy.cfg aus der services.yaml-Konfiguration."""
import ipaddress
import re

# Pfad zum kombinierten PEM IM haproxy-Container (siehe entrypoint.sh)
CERT_PEM = "/usr/local/etc/haproxy/platform.pem"

# Interner Stats-Port (nur im Docker-Netz erreichbar, NICHT auf dem Host)
STATS_PORT = 8404


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _safe_id(name: str, idx: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return f"{base or 'svc'}_{idx}"


def backend_name(name: str, idx: int) -> str:
    """Name des HAProxy-Backends fuer einen Service (stabil ueber den Index)."""
    return f"bk_{_safe_id(name, idx)}"


# Defense-in-Depth (K2): Werte mit Steuerzeichen/Whitespace koennen HAProxy-
# Direktiven injizieren. Solche Eintraege werden beim Rendern uebersprungen
# (die primaere Validierung passiert im GUI, dies schuetzt vor Hand-Edits).
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./-]*$")
_SAFE_BACKEND = re.compile(r"^\[?[A-Za-z0-9_.:-]+\]?(?::[0-9]{1,5})?$")


def _is_safe(path: str, backend: str) -> bool:
    if not _SAFE_PATH.match(path or "") or ".." in (path or ""):
        return False
    if not _SAFE_BACKEND.match(backend or ""):
        return False
    return True


def _clean_comment(s: str) -> str:
    return "".join(c for c in (s or "") if ord(c) >= 0x20 and c != "\x7f")[:80]


def _norm_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _server_opts(backend_addr: str) -> str:
    """init-addr / resolvers passend zur Adresse (Name vs. IP) + Health-Check."""
    host = backend_addr.split(":")[0]
    if _is_ip(host):
        return "check inter 5s init-addr last,libc,none"
    return "check inter 5s resolvers docker init-addr last,libc,none"


def render(config: dict) -> str:
    all_services = config.get("services", [])

    lines = []
    A = lines.append

    A("# Automatisch generiert von config-app - nicht von Hand editieren.")
    A("# Aenderungen ueber das Admin-GUI (/admin) oder services.yaml.")
    A("")
    A("global")
    A("    log stdout format raw local0")
    A("    maxconn 4096")
    A("    tune.ssl.default-dh-param 2048")
    A("")
    A("defaults")
    A("    log global")
    A("    mode http")
    A("    option httplog")
    A("    option forwardfor")
    A("    option http-server-close")
    A("    timeout connect 5s")
    A("    timeout client 60s")
    A("    timeout server 60s")
    A("    timeout check 3s")
    A("    default-server init-addr last,libc,none")
    A("")
    A("# Docker-internes DNS fuer Backends per Container-Name")
    A("resolvers docker")
    A("    nameserver dns 127.0.0.11:53")
    A("    resolve_retries 3")
    A("    timeout resolve 1s")
    A("    timeout retry 1s")
    A("    hold valid 10s")
    A("")
    A("# Interne Stats-Schnittstelle (nur im Docker-Netz, fuer das Admin-GUI)")
    A("frontend fe_stats")
    A(f"    bind :{STATS_PORT}")
    A("    stats enable")
    A("    stats uri /stats")
    A("    stats refresh 5s")
    A("")
    A("# HTTP -> HTTPS Redirect")
    A("frontend fe_http")
    A("    bind :80")
    A("    http-request redirect scheme https code 301 unless { ssl_fc }")
    A("")
    A("frontend fe_https")
    A(f"    bind :443 ssl crt {CERT_PEM} alpn h2,http/1.1")
    A("    http-request set-header X-Forwarded-Proto https")
    A("    http-request set-header X-Forwarded-Host %[req.hdr(host)]")
    A("")

    backends = []
    for idx, s in enumerate(all_services):
        if not s.get("enabled", True):
            continue
        if not s.get("backend"):
            continue
        sid = _safe_id(s.get("name", "svc"), idx)
        path = _norm_path(s.get("path", "/"))
        if path == "/":
            continue  # "/" ist dem Dashboard vorbehalten
        if not _is_safe(path, str(s.get("backend", "")).strip()):
            A(f"    # uebersprungen (unsichere Werte): {_clean_comment(s.get('name', sid))}")
            continue
        A(f"    # {_clean_comment(s.get('name', sid))}")
        A(f"    acl is_{sid} path {path}")
        A(f"    acl is_{sid} path_beg {path}/")
        A(f"    use_backend bk_{sid} if is_{sid}")
        backends.append((sid, s, path))

    A("    default_backend bk_dashboard")
    A("")
    A("# Dashboard + Admin-GUI")
    A("backend bk_dashboard")
    A("    server dashboard config-app:5000 check inter 5s resolvers docker init-addr last,libc,none")
    A("")

    for sid, s, path in backends:
        scheme = (s.get("scheme") or "http").lower()
        backend_addr = str(s["backend"]).strip()
        A(f"backend bk_{sid}")
        if s.get("strip_path"):
            # Prefix /xyz vor Weiterleitung entfernen -> Backend sieht / ...
            A(f"    http-request set-path %[path,regsub(^{path}/?,/)]")
        srv = f"    server s_{sid} {backend_addr}"
        if scheme == "https":
            srv += " ssl"
            if s.get("ssl_verify"):
                srv += " verify required ca-file /etc/ssl/certs/ca-certificates.crt"
            else:
                srv += " verify none"
        srv += " " + _server_opts(backend_addr)
        A(srv)
        A("")

    return "\n".join(lines) + "\n"
