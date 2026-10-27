"""Erzeugt eine haproxy.cfg aus der services.yaml-Konfiguration."""
import ipaddress
import logging
import os
import re
from pathlib import Path

from werkzeug.utils import secure_filename

# Gleicher Logger-Name wie in app.py -> Meldungen landen im App-Log (und damit
# auch in der Log-Anzeige des Admin-GUIs) sowie auf stdout.
_log = logging.getLogger("config-app")

# Pfad zum kombinierten PEM IM haproxy-Container (siehe entrypoint.sh)
CERT_PEM = "/usr/local/etc/haproxy/platform.pem"

# Interne CA (opt-in, Compose-Profil "ca"): Root zum Verifizieren von
# Backend-Zertifikaten der plattform-eigenen step-ca (read-only gemountet).
INTERNAL_CA_FILE = "/certs-ca/root_ca.crt"
# #21: Bundle hochgeladener Trust-Certs (Firmen-/AD-Root-CA oder self-signed
# Ziel-Cert) zum Verifizieren von Backends (read-only gemountet).
BACKEND_CA_BUNDLE = "/certs-trust/ca-bundle.pem"


def _backend_ca_file(s) -> str:
    """#21/v1.8: ca-file je nach Dienst-Einstellung backend_ca."""
    choice = (s.get("backend_ca") or "").lower()
    if choice == "internal":
        return INTERNAL_CA_FILE
    if choice == "custom":
        return BACKEND_CA_BUNDLE
    return "/etc/ssl/certs/ca-certificates.crt"   # oeffentliche CAs (Default)

# E4: Verzeichnis mit Secret-Dateien fuer Health-Check-Header (config-app-seitig).
HC_SECRET_DIR = os.environ.get("HC_SECRET_DIR", "/app/config/secrets")
# E6: optionaler externer Syslog-Server (zusaetzlich zu stdout). Leer = nur stdout.
SYSLOG_ADDRESS = os.environ.get("SYSLOG_ADDRESS", "").strip()
_BALANCE = ("roundrobin", "leastconn")
# Verzeichnis zusaetzlicher Zertifikate (SNI) IM haproxy-Container (Bind-Mount von
# data/config/certs). HAProxy waehlt anhand SNI; leer -> nicht referenziert.
EXTRA_CERT_DIR = "/certs-extra"

# Interner Stats-Port (nur im Docker-Netz erreichbar, NICHT auf dem Host)
STATS_PORT = 8404

# E3: TCP-Streams. Diese Ports sind durch HAProxy-Frontends belegt und tabu.
TCP_RESERVED_PORTS = {80, 443, STATS_PORT}
# TCP-Verbindungen sind oft langlebig (DB, SSH, ...) -> grosszuegiger Default,
# falls pro Stream kein Timeout gesetzt ist.
TCP_DEFAULT_TIMEOUT = "1h"

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
    bleiben via %[pathq] erhalten). Wird in fe_http UND fe_https eingesetzt.
    %[pathq] statt %[url]: bei HTTP/2 (und absoluter Request-URI) liefert %[url]
    die komplette URL inkl. Schema/Host -> doppelter/falscher Location-Header;
    %[pathq] ist immer nur Pfad + Query."""
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
        # E5: bare "/" auf der Alias-Domain getrennt vom Pfad-Erhalt umleiten.
        root_to = str(a.get("root_to") or "").strip()
        if root_to:
            rr = _norm_path(root_to)
            if _SAFE_PATH.match(rr) and ".." not in rr:
                out.append(f"    http-request redirect code 301 location https://{domain}{rr} if {acl} {{ path / }}")
        if rt == "/":
            out.append(f"    http-request redirect code 301 location https://{domain}%[pathq] if {acl}")
        elif a.get("keep_path"):
            # Pfad enthaelt das Praefix bereits -> unveraendert; sonst voranstellen.
            out.append(f"    http-request redirect code 301 location https://{domain}%[pathq] if {acl} {{ path_beg {rt} }}")
            out.append(f"    http-request redirect code 301 location https://{domain}{rt}%[pathq] if {acl}")
        else:
            out.append(f"    http-request redirect code 301 location https://{domain}{rt}%[pathq] if {acl}")
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


def _service_servers(s) -> list:
    """E1: gueltige Backend-Adressen. `servers:` (mehrere) hat Vorrang, sonst das
    einzelne `backend`-Feld. Ungueltige/leere Adressen werden verworfen."""
    raw = s.get("servers")
    if isinstance(raw, list) and raw:
        addrs = [str(x).strip() for x in raw if str(x).strip()]
    else:
        addrs = [str(s.get("backend", "")).strip()]
    return [a for a in addrs if a and _SAFE_BACKEND.match(a)]


def _valid_port(p) -> int:
    """E3: gueltiger, nicht reservierter TCP-Port (1-65535); 0 = ungueltig."""
    try:
        n = int(str(p).strip())
    except (TypeError, ValueError):
        return 0
    return n if (1 <= n <= 65535 and n not in TCP_RESERVED_PORTS) else 0


def _tcp_streams(config) -> list:
    """E3: validierte TCP-Streams als (sid, stream, port, servers). Streams ohne
    gueltige Server sowie doppelte/reservierte Ports werden verworfen."""
    out, used = [], set()
    for idx, st in enumerate(config.get("tcp_streams", []) or []):
        if not st.get("enabled", True):
            continue
        port = _valid_port(st.get("port"))
        servers = _service_servers(st)   # gleiche Logik wie bei HTTP-Diensten
        if not port or port in used or not servers:
            continue
        used.add(port)
        out.append((f"tcp_{_safe_id(st.get('name', 'stream'), idx)}", st, port, servers))
    return out


# #19: externe Link-Kachel. Strikt: nur http/https, Host, optional Port/Pfad;
# keine Leerzeichen/Quotes/spitzen Klammern (kein javascript:, keine Injection).
_RE_LINK = re.compile(r"^https?://[A-Za-z0-9.-]{1,253}(?::\d{1,5})?(?:/[^\s\"'<>\\]*)?$")


def _link_parse(url):
    """#19: (scheme, host, port, path) einer externen Link-URL, sonst None."""
    url = str(url or "").strip()
    m = re.match(r"^(https?)://([A-Za-z0-9.-]{1,253})(?::(\d{1,5}))?(/[^\s\"'<>\\]*)?$", url)
    if not m:
        return None
    scheme, host, port, path = m.group(1), m.group(2), m.group(3), m.group(4) or "/"
    if not _SAFE_HOST.match(host):
        return None
    p = int(port) if port else (443 if scheme == "https" else 80)
    if not (1 <= p <= 65535):
        return None
    return scheme, host, p, path


_RE_HDRNAME = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def _hc_header(hc) -> str:
    """E4: optionaler Health-Check-Header, dessen WERT aus einer Secret-Datei kommt
    (nicht aus services.yaml). Leer, wenn nicht konfiguriert/lesbar/ungueltig."""
    name = str(hc.get("header_name") or "").strip()
    # secure_filename bereinigt den Dateinamen (von CodeQL als Sanitizer gegen
    # Path-Injection anerkannt; schneidet Pfadtrenner/"..", keine Traversal).
    fname = secure_filename(str(hc.get("header_file") or "").strip())
    if not name and not fname:
        return ""   # kein Header konfiguriert -> normal, keine Meldung
    if not _RE_HDRNAME.match(name) or not fname:
        _log.warning("Health-Check-Header unvollstaendig/ungueltig "
                     "(header_name=%r, header_file=%r) -> Header wird weggelassen.",
                     name, fname)
        return ""
    try:
        val = (Path(HC_SECRET_DIR) / fname).read_text(encoding="utf-8").strip()
    except OSError as e:
        # Haeufige Ursache: die Datei gehoert nicht root. Der Container laeuft als
        # root, aber mit cap_drop ALL (kein CAP_DAC_OVERRIDE) -> er kann eine
        # 0600-Datei nur lesen, wenn sie uid 0 gehoert. Sonst faellt der Header
        # still weg und der Health-Check scheitert (z. B. 401 -> 503).
        _log.warning("Health-Check-Header %r: Secret-Datei %r im Secret-Verzeichnis "
                     "nicht lesbar (%s) -> Header wird weggelassen. Datei im Container "
                     "lesbar machen (root:root, 0600).", name, fname, e)
        return ""
    # Steuerzeichen sowie " und \ wuerden das Quoting sprengen -> abweisen.
    if not val or any(ord(c) < 0x20 or ord(c) == 0x7f for c in val) \
            or '"' in val or "\\" in val:
        _log.warning("Health-Check-Header %r: Secret-Datei %r ist leer oder enthaelt "
                     "ungueltige Zeichen -> Header wird weggelassen.", name, fname)
        return ""   # keine Header-Injection
    # '%' ist in HAProxy-Log-Format-Strings ein Sonderzeichen -> verdoppeln.
    val = val.replace("%", "%%")
    # In doppelte Anfuehrungszeichen: so bleibt ein Wert mit Leerzeichen
    # (z. B. "Bearer <token>") EIN Argument an 'http-check send ... hdr'.
    return f' hdr {name} "{val}"'


def render(config: dict, stats_auth: str = "", extra_certs=None, trust_cas=None) -> str:
    all_services = config.get("services", [])
    trust_cas = sorted(trust_cas or [])   # #21: nur fuer den cfg-Hash (Reload-Trigger)
    domain = str(config.get("domain", "")).strip()
    alias_lines = _alias_lines(config.get("domain_aliases"), domain)   # #8
    extra_certs = sorted(extra_certs or [])                            # #9 (SNI)

    # #6/#15: timeout client im Frontend an das laengste timeout_server anheben,
    # sonst greift der 60s-Client-Timeout vor einer langen Server-Antwort. NICHT
    # aus timeout_tunnel ableiten: 'timeout tunnel' ersetzt nach dem Upgrade
    # (WebSocket/CONNECT) ohnehin client+server; ein globaler Client-Timeout von
    # z. B. 1h wuerde sonst alle HTTP-Verbindungen fuer Slowloris anfaellig machen.
    max_to = 60
    for s in all_services:
        if not s.get("enabled", True):
            continue
        if _valid_timeout(s.get("timeout_server")):
            max_to = max(max_to, int(_ht_seconds(s.get("timeout_server"))))

    lines = []
    A = lines.append

    A("# Automatisch generiert von config-app - nicht von Hand editieren.")
    A("# Aenderungen ueber das Admin-GUI (/admin) oder services.yaml.")
    if trust_cas:
        # #21: aendert den cfg-Hash bei Upload/Loeschen -> Watcher reload.
        A("# Vertrauenswuerdige Backend-CAs: " + ", ".join(trust_cas))
    A("")
    A("global")
    A("    log stdout format raw local0")
    if SYSLOG_ADDRESS and re.match(r"^[A-Za-z0-9@._:/-]{1,120}$", SYSLOG_ADDRESS):
        A(f"    log {SYSLOG_ADDRESS} local0")   # E6: zusaetzlich an externen Syslog
    A("    maxconn 4096")
    A("    tune.ssl.default-dh-param 2048")
    A("    # N2: moderne TLS-Defaults, min. TLS 1.2")
    A(f"    ssl-default-bind-ciphers {TLS_CIPHERS}")
    A(f"    ssl-default-bind-ciphersuites {TLS_CIPHERSUITES}")
    A("    ssl-default-bind-options ssl-min-ver TLSv1.2 no-tls-tickets")
    A("")
    # E3/#13: eigene Defaults fuer TCP-Streams OHNE HTTP-Optionen -> keine
    # 'option forwardfor ignored'-Warnungen. MUSS vor den unbenannten (HTTP-)
    # Defaults stehen: HAProxy nimmt die LETZTE Defaults-Sektion als implizite
    # Vorgabe; die benannte 'tcp' wird ausschliesslich ueber 'from tcp' genutzt.
    A("defaults tcp")
    A("    log global")
    A("    mode tcp")
    A("    option tcplog")
    A("    timeout connect 5s")
    A("    timeout client 1h")
    A("    timeout server 1h")
    A("    default-server init-addr last,libc,none")
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
    # #15: langsam gesendete Request-Header nicht vom grossen Client-Timeout
    # profitieren lassen (Slowloris-Schutz). Gilt nur in der HTTP-Phase.
    A("    timeout http-request 10s")
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
    A("    http-request set-header X-Forwarded-Port %[dst_port]")   # E5
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
        if _link_parse(s.get("link_url")):
            continue   # #19: externe Link-Kachel -> keine Route (ACL/use_backend)
        servers = _service_servers(s)      # E1: ein oder mehrere Backends
        if not servers:
            continue
        sid = _safe_id(s.get("name", "svc"), idx)
        path = _norm_path(s.get("path", "/"))
        if path == "/":
            continue  # "/" ist dem Dashboard vorbehalten
        if not _SAFE_PATH.match(path) or ".." in path:
            A(f"    # uebersprungen (unsicherer Pfad): {_clean_comment(s.get('name', sid))}")
            continue
        A(f"    # {_clean_comment(s.get('name', sid))}")
        # E2: Match-Art exact (Default) oder prefix (path_beg ohne abschliessendes /).
        if (s.get("match") or "exact").lower() == "prefix":
            A(f"    acl is_{sid} path_beg {path}")
        else:
            A(f"    acl is_{sid} path {path}")
            A(f"    acl is_{sid} path_beg {path}/")
        # E2: zusaetzliche Pfade auf dasselbe Backend (ohne eigene Kachel).
        for ep in (s.get("extra_paths") or []):
            epn = _norm_path(str(ep))
            if epn != "/" and _SAFE_PATH.match(epn) and ".." not in epn:
                A(f"    acl is_{sid} path {epn}")
                A(f"    acl is_{sid} path_beg {epn}/")
        # #7: gesperrte Unterpfade -> 403. http-request wird von HAProxy ohnehin
        # vor use_backend verarbeitet; alle use_backend kommen daher gesammelt
        # ans Ende (sonst warnt HAProxy "http-request after use_backend").
        denies = _deny_list(path, s.get("deny_paths"))
        if denies:
            A(f"    http-request deny deny_status 403 if is_{sid} {{ path_beg {' '.join(denies)} }}")
        backends.append((sid, s, path, servers))

    for sid, _s, _p, _sv in backends:
        A(f"    use_backend bk_{sid} if is_{sid}")
    A("    default_backend bk_dashboard")
    A("")
    A("# Dashboard + Admin-GUI")
    A("backend bk_dashboard")
    A("    server dashboard config-app:5000 check inter 5s resolvers docker init-addr last,libc,none")
    A("")

    for sid, s, path, servers in backends:
        scheme = (s.get("scheme") or "http").lower()
        A(f"backend bk_{sid}")
        if s.get("strip_path"):
            # Prefix /xyz vor Weiterleitung entfernen -> Backend sieht / ...
            A(f"    http-request set-path %[path,regsub(^{path}/?,/)]")

        # E1: Lastverteilung nur bei mehreren Servern (Default roundrobin).
        if len(servers) > 1:
            balance = (s.get("balance") or "").lower()
            if balance not in _BALANCE:
                balance = "roundrobin"
            A(f"    balance {balance}")

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
            send += _hc_header(hc)   # E4: Header-Wert aus Secret-Datei
            A(send)
            expect = str(hc.get("expect_status") or "").strip()
            if _RE_EXPECT.match(expect):
                A(f"    http-check expect rstatus ^({expect})$")
            else:
                A("    http-check expect rstatus ^([23][0-9][0-9])$")   # Default: 2xx/3xx
            extra = "fall 3 rise 2"

        for n, addr in enumerate(servers):   # E1: ein oder mehrere Server
            srv = f"    server s_{sid}_{n} {addr}"
            if scheme == "https":
                srv += " ssl"
                if s.get("ssl_verify"):
                    srv += f" verify required ca-file {_backend_ca_file(s)}"
                    # internal/custom = namensbasierte interne Ziele -> SNI mitsenden
                    # (check-sni = Backend-Hostname fuer den Health-Check).
                    if (s.get("backend_ca") or "").lower() in ("internal", "custom"):
                        bhost = addr.split(":")[0].strip("[]")
                        srv += f" sni str({bhost}) check-sni {bhost}"
                else:
                    srv += " verify none"
            srv += " " + _server_opts(addr, inter=inter, extra=extra)
            A(srv)
        A("")

    # #19: externe Link-Kacheln mit Health-Check -> reines Pruef-Backend (von
    # keiner Route genutzt). So liefern Status-Punkt, Admin-Status und die
    # "Backend DOWN"-Benachrichtigung ueber den vorhandenen Stats-Weg Ergebnisse.
    for idx, s in enumerate(all_services):
        if not s.get("enabled", True):
            continue
        link = _link_parse(s.get("link_url"))
        hc = s.get("healthcheck") or {}
        if not link or not hc:
            continue
        scheme, host, port, lpath = link
        sid = _safe_id(s.get("name", "svc"), idx)
        A(f"# Externer Link (nur Health-Check): {_clean_comment(s.get('name', sid))}")
        A(f"backend bk_{sid}")
        uri = _norm_path(str(hc.get("uri") or lpath))
        if not _SAFE_PATH.match(uri) or ".." in uri:
            uri = "/"
        interval = str(hc.get("interval") or "").strip().lower()
        inter = interval if _valid_timeout(interval) else "30s"
        A("    option httpchk")
        send = f"    http-check send meth GET uri {uri} ver HTTP/1.1 hdr Host {host}"
        send += _hc_header(hc)   # E4: optionaler Header-Wert aus Secret-Datei
        A(send)
        expect = str(hc.get("expect_status") or "").strip()
        if _RE_EXPECT.match(expect):
            A(f"    http-check expect rstatus ^({expect})$")
        else:
            A("    http-check expect rstatus ^([23][0-9][0-9])$")
        srv = f"    server s_{sid}_0 {host}:{port}"
        if scheme == "https":
            srv += " ssl"
            if s.get("ssl_verify"):
                srv += (f" verify required ca-file {_backend_ca_file(s)}"
                        f" sni str({host}) check-sni {host}")   # #21: public/internal/custom
            else:
                srv += f" verify none sni str({host}) check-sni {host}"
        # Externer Host -> per libc aufloesen (nicht ueber den Docker-Resolver).
        A(srv + f" check inter {inter} init-addr last,libc,none")
        A("")

    # E3: TCP-Streams - eigene Ports, mode tcp, optional TLS-Terminierung mit dem
    # Plattform-Zertifikat. Die Host-Ports muessen in docker-compose.yml published
    # werden (siehe Admin-GUI-Hinweis), HAProxy bindet sie nur containerintern.
    for sid, st, port, servers in _tcp_streams(config):
        # 'from tcp' -> erbt die TCP-Defaults (mode tcp, option tcplog, ...) statt
        # der HTTP-Defaults (#13: keine 'option forwardfor ignored'-Warnungen).
        A(f"# TCP-Stream: {_clean_comment(st.get('name', sid))}")
        A(f"frontend fe_{sid} from tcp")
        bind = f"    bind :{port}"
        if st.get("tls"):
            bind += f" ssl crt {CERT_PEM}"     # TLS-Terminierung (Plattform-Zertifikat)
        A(bind)
        if st.get("clitcpka"):
            A("    option clitcpka")            # Client-TCP-Keepalive
        tc = str(st.get("timeout_client") or "").strip().lower()
        A(f"    timeout client {tc if _valid_timeout(tc) else TCP_DEFAULT_TIMEOUT}")
        A(f"    default_backend bk_{sid}")
        A("")
        A(f"backend bk_{sid} from tcp")
        if len(servers) > 1:   # Lastverteilung nur ab 2 Servern (Default leastconn).
            balance = (st.get("balance") or "").lower()
            if balance not in _BALANCE:
                balance = "leastconn"
            A(f"    balance {balance}")
        ts = str(st.get("timeout_server") or "").strip().lower()
        A(f"    timeout server {ts if _valid_timeout(ts) else TCP_DEFAULT_TIMEOUT}")
        for n, addr in enumerate(servers):
            A(f"    server s_{sid}_{n} {addr} " + _server_opts(addr))
        A("")

    return "\n".join(lines) + "\n"
