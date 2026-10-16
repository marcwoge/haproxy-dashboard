"""Erzeugt eine haproxy.cfg aus der services.yaml-Konfiguration."""
import ipaddress
import os
import re

# Pfad zum kombinierten PEM IM haproxy-Container (siehe entrypoint.sh)
CERT_PEM = "/usr/local/etc/haproxy/platform.pem"
# Verzeichnis zusaetzlicher Zertifikate (SNI) IM haproxy-Container (Bind-Mount von
# data/config/certs). HAProxy waehlt anhand SNI; leer -> nicht referenziert.
EXTRA_CERT_DIR = "/certs-extra"

# Interner Stats-Port (nur im Docker-Netz erreichbar, NICHT auf dem Host)
STATS_PORT = 8404

# N2: HSTS ist OPT-IN (Env), weil es mit dem selbstsignierten Fallback-Zertifikat
# den Zugriff dauerhaft sperren wuerde. Nur mit echtem Zertifikat aktivieren.
HSTS_ENABLED = os.environ.get("ENABLE_HSTS", "false").lower() == "true"
HSTS_MAX_AGE = os.environ.get("HSTS_MAX_AGE", "31536000")

# N2: moderne TLS-Einstellungen (Mozilla "intermediate"), min. TLS 1.2.
TLS_CIPHERS = ("ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
               "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
               "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
               "DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384")
TLS_CIPHERSUITES = ("TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:"
                    "TLS_CHACHA20_POLY1305_SHA256")


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


# Zusaetzliche K2-Muster fuer die Pro-Service-Optionen (#6/#7/#10).
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")          # Host-Header im Health-Check
_RE_HATIME = re.compile(r"^\d{1,8}(ms|s|m|h)$")             # HAProxy-Zeitangabe
_RE_EXPECT = re.compile(r"^[0-9|]{1,40}$")                  # Statusliste, z. B. 200|301|302
_MAX_TIMEOUT_S = 86400                                      # Obergrenze 24h


def _ht_seconds(t: str):
    """HAProxy-Zeitangabe -> Sekunden (float), oder None bei ungueltig."""
    m = _RE_HATIME.match((t or "").strip().lower())
    if not m:
        return None
    unit = m.group(1)
    num = int((t or "").strip().lower()[:-len(unit)])
    return num * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def _valid_timeout(t: str) -> bool:
    s = _ht_seconds(t)
    return s is not None and 0 < s <= _MAX_TIMEOUT_S


def _deny_list(path: str, deny_paths) -> list:
    """Absolute, geprüfte Pfad-Praefixe (Service-Pfad + Unterpfad) fuer deny_paths."""
    out = []
    for d in (deny_paths or []):
        dd = _norm_path(str(d))
        if dd != "/" and _SAFE_PATH.match(dd) and ".." not in dd:
            out.append(f"{path}{dd}")
    return out


def _alias_lines(aliases, domain: str) -> list:
    """#8: 301-Redirects von Alias-Domains auf die Plattform-Domain (Pfad+Query
    bleiben via %[url] erhalten). Wird in fe_http UND fe_https eingesetzt."""
    out = []
    if not (domain and _SAFE_HOST.match(domain)):
        return out
    for i, a in enumerate(aliases or []):
        host = str(a.get("host", "")).strip().lower()
        rt = _norm_path(str(a.get("redirect_to", "/")))
        if not _SAFE_HOST.match(host) or not _SAFE_PATH.match(rt) or ".." in rt:
            continue
        acl = f"host_alias_{i}"
        out.append(f"    acl {acl} hdr(host),field(1,:) -i {host}")
        if rt == "/":
            out.append(f"    http-request redirect code 301 location https://{domain}%[url] if {acl}")
        elif a.get("keep_path"):
            # Pfad enthaelt das Praefix bereits -> unveraendert; sonst voranstellen.
            out.append(f"    http-request redirect code 301 location https://{domain}%[url] if {acl} {{ path_beg {rt} }}")
            out.append(f"    http-request redirect code 301 location https://{domain}{rt}%[url] if {acl}")
        else:
            out.append(f"    http-request redirect code 301 location https://{domain}{rt}%[url] if {acl}")
    return out


def _clean_comment(s: str) -> str:
    return "".join(c for c in (s or "") if ord(c) >= 0x20 and c != "\x7f")[:80]


def _norm_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _server_opts(backend_addr: str, inter: str = "5s", extra: str = "") -> str:
    """init-addr / resolvers passend zur Adresse (Name vs. IP) + Health-Check.
    `inter` = Health-Check-Intervall, `extra` = zusaetzliche Server-Optionen."""
    host = backend_addr.split(":")[0]
    base = f"check inter {inter}"
    if extra:
        base += " " + extra
    if _is_ip(host):
        return base + " init-addr last,libc,none"
    return base + " resolvers docker init-addr last,libc,none"


def render(config: dict, stats_auth: str = "", extra_certs=None) -> str:
    all_services = config.get("services", [])
    domain = str(config.get("domain", "")).strip()
    alias_lines = _alias_lines(config.get("domain_aliases"), domain)   # #8
    extra_certs = sorted(extra_certs or [])                            # #9 (SNI)

    # #6: timeout client im Frontend auf das Maximum der Service-Timeouts anheben,
    # sonst greift der 60s-Client-Timeout vor einem hoeheren Server-/Tunnel-Timeout.
    max_to = 60
    for s in all_services:
        if not s.get("enabled", True):
            continue
        for key in ("timeout_server", "timeout_tunnel"):
            if _valid_timeout(s.get(key)):
                max_to = max(max_to, int(_ht_seconds(s.get(key))))

    lines = []
    A = lines.append

    A("# Automatisch generiert von config-app - nicht von Hand editieren.")
    A("# Aenderungen ueber das Admin-GUI (/admin) oder services.yaml.")
    A("")
    A("global")
    A("    log stdout format raw local0")
    A("    maxconn 4096")
    A("    tune.ssl.default-dh-param 2048")
    A("    # N2: moderne TLS-Defaults, min. TLS 1.2")
    A(f"    ssl-default-bind-ciphers {TLS_CIPHERS}")
    A(f"    ssl-default-bind-ciphersuites {TLS_CIPHERSUITES}")
    A("    ssl-default-bind-options ssl-min-ver TLSv1.2 no-tls-tickets")
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
    if stats_auth:
        # N3: Stats erfordern Basic-Auth (kein unauth. Info-Leak im Docker-Netz).
        A("    stats realm HAProxy\\ Statistics")
        A(f"    stats auth {stats_auth}")
    A("")
    A("# HTTP -> HTTPS Redirect (+ Alias-Domain-Redirects auf die Plattform)")
    A("frontend fe_http")
    A("    bind :80")
    for ln in alias_lines:   # #8: Alias-Hosts direkt auf https://<platform> leiten
        A(ln)
    A("    http-request redirect scheme https code 301 unless { ssl_fc }")
    A("")
    A("frontend fe_https")
    bind = f"    bind :443 ssl crt {CERT_PEM}"
    if extra_certs:
        # #9: zusaetzliche Zertifikate per SNI. Der Kommentar listet sie, damit sich
        # der cfg-Hash bei Cert-Aenderungen aendert -> Watcher validiert + reload.
        A("    # SNI-Zertifikate: " + ", ".join(extra_certs))
        bind += f" crt {EXTRA_CERT_DIR}"
    A(bind + " alpn h2,http/1.1")
    A("    http-request set-header X-Forwarded-Proto https")
    A("    http-request set-header X-Forwarded-Host %[req.hdr(host)]")
    # Echte Client-IP fuer das Rate-Limiting; set-header ueberschreibt Spoofing.
    A("    http-request set-header X-Client-IP %[src]")
    if max_to > 60:
        A(f"    timeout client {max_to}s")   # #6: an laengstes Service-Timeout anpassen
    if HSTS_ENABLED:
        # N2: HSTS nur bei echtem Zertifikat (opt-in via ENABLE_HSTS=true).
        A(f'    http-response set-header Strict-Transport-Security "max-age={HSTS_MAX_AGE}; includeSubDomains"')
    for ln in alias_lines:   # #8: Alias-Hosts auch auf 443 auf die Plattform leiten
        A(ln)
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
        # #7: gesperrte Unterpfade -> 403. http-request wird von HAProxy ohnehin
        # vor use_backend verarbeitet; alle use_backend kommen daher gesammelt
        # ans Ende (sonst warnt HAProxy "http-request after use_backend").
        denies = _deny_list(path, s.get("deny_paths"))
        if denies:
            A(f"    http-request deny deny_status 403 if is_{sid} {{ path_beg {' '.join(denies)} }}")
        backends.append((sid, s, path))

    for sid, _s, _p in backends:
        A(f"    use_backend bk_{sid} if is_{sid}")
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

        # #6: optionale Timeouts pro Service (Default = globaler Wert).
        if _valid_timeout(s.get("timeout_server")):
            A(f"    timeout server {str(s['timeout_server']).strip().lower()}")
        if _valid_timeout(s.get("timeout_tunnel")):
            A(f"    timeout tunnel {str(s['timeout_tunnel']).strip().lower()}")

        # #10: optionaler HTTP-Health-Check (sonst einfacher TCP-Check wie bisher).
        hc = s.get("healthcheck") or {}
        inter, extra = "5s", ""
        if hc:
            uri = _norm_path(str(hc.get("uri") or path))
            if not _SAFE_PATH.match(uri) or ".." in uri:
                uri = path
            interval = str(hc.get("interval") or "").strip().lower()
            if _valid_timeout(interval):
                inter = interval
            A("    option httpchk")
            send = f"    http-check send meth GET uri {uri} ver HTTP/1.1"
            if domain and _SAFE_HOST.match(domain):
                send += f" hdr Host {domain}"
            send += " hdr X-Forwarded-Proto https"
            A(send)
            expect = str(hc.get("expect_status") or "").strip()
            if _RE_EXPECT.match(expect):
                A(f"    http-check expect rstatus ^({expect})$")
            else:
                A("    http-check expect rstatus ^([23][0-9][0-9])$")   # Default: 2xx/3xx
            extra = "fall 3 rise 2"

        srv = f"    server s_{sid} {backend_addr}"
        if scheme == "https":
            srv += " ssl"
            if s.get("ssl_verify"):
                srv += " verify required ca-file /etc/ssl/certs/ca-certificates.crt"
            else:
                srv += " verify none"
        srv += " " + _server_opts(backend_addr, inter=inter, extra=extra)
        A(srv)
        A("")

    return "\n".join(lines) + "\n"
