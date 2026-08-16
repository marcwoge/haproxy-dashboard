"""Acme Platform - Dashboard + Admin-GUI.

Liefert das Kachel-Dashboard und ein Web-GUI zur Verwaltung der Services
und des Erscheinungsbilds. Schreibt services.yaml und generiert daraus die
haproxy.cfg (der haproxy-Container laedt sie automatisch neu).
"""
import base64
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import urllib.request
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, send_file, session, url_for)
from waitress import serve

from haproxy_render import render as render_haproxy, _norm_path, _safe_id
import updater
import notifier
import i18n

DEFAULT_LANG = os.environ.get("PLATFORM_LANG", "de")

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))
CONFIG_FILE = CONFIG_DIR / "services.yaml"
DEFAULT_FILE = Path(__file__).parent / "default_services.yaml"
HAPROXY_OUT = Path(os.environ.get("HAPROXY_OUT", "/app/haproxy_out"))
HAPROXY_CFG = HAPROXY_OUT / "haproxy.cfg"
STATUS_FILE = HAPROXY_OUT / "status.txt"
HAPROXY_LOG = HAPROXY_OUT / "haproxy.log"
APP_LOG = HAPROXY_OUT / "config-app.log"
STATS_URL = os.environ.get("STATS_URL", "http://haproxy:8404/stats;csv")


def _secret(name: str, default: str = "") -> str:
    """M4: Wert aus Docker-Secret-Datei (<NAME>_FILE) lesen, sonst aus Env.
    Ermoeglicht Login-Daten via Docker/Compose *secrets* statt Klartext-Env."""
    path = os.environ.get(name + "_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return os.environ.get(name, default)


ADMIN_USER = _secret("ADMIN_USER", "admin")
ADMIN_PASSWORD = _secret("ADMIN_PASSWORD", "")  # leer = Admin-GUI gesperrt (H1)

# Getrennte, hoehere Rolle fuer system.update (RBAC). Wenn gesetzt, benoetigen
# Update-Aktionen DIESE Credentials; sonst faellt es auf den Admin zurueck.
PLATFORM_ADMIN_USER = _secret("PLATFORM_ADMIN_USER", "")
PLATFORM_ADMIN_PASSWORD = _secret("PLATFORM_ADMIN_PASSWORD", "")

# H1: bekannte triviale Passwoerter, die (falls gesetzt) laut angemahnt werden.
WEAK_PASSWORDS = {
    "changeme", "change-me", "password", "passwort", "admin", "administrator",
    "123456", "12345678", "secret", "changethis", "test", "root", "default",
}

# H2: Brute-Force-Schutz am Admin-Login (pro Client-IP).
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW = int(os.environ.get("LOGIN_WINDOW", "300"))     # Zeitfenster (s)
LOGIN_LOCKOUT = int(os.environ.get("LOGIN_LOCKOUT", "300"))   # Sperrdauer (s)


class LoginRateLimiter:
    """Einfacher In-Memory-Ratelimiter: sperrt eine IP nach zu vielen
    Fehlversuchen im Zeitfenster. Zaehlt nur falsche Credentials."""

    def __init__(self, max_attempts: int, window: int, lockout: int):
        self.max = max_attempts
        self.window = window
        self.lockout = lockout
        self._fails: dict[str, list] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for ip in list(self._locked):
            if self._locked[ip] <= now:
                del self._locked[ip]
        for ip in list(self._fails):
            self._fails[ip] = [t for t in self._fails[ip] if now - t < self.window]
            if not self._fails[ip]:
                del self._fails[ip]

    def locked_for(self, ip: str) -> int:
        """Verbleibende Sperrdauer in Sekunden (0 = nicht gesperrt)."""
        now = time.time()
        with self._lock:
            until = self._locked.get(ip, 0)
            return int(until - now) + 1 if until > now else 0

    def record_failure(self, ip: str) -> bool:
        """True, wenn diese IP dadurch neu gesperrt wurde."""
        now = time.time()
        with self._lock:
            self._prune(now)
            fails = self._fails.setdefault(ip, [])
            fails.append(now)
            if len(fails) >= self.max:
                self._locked[ip] = now + self.lockout
                self._fails.pop(ip, None)
                return True
            return False

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
            self._locked.pop(ip, None)


_limiter = LoginRateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW, LOGIN_LOCKOUT)

DEFAULT_THEME = {
    "title": "Acme Platform",
    "subtitle": "Service Dashboard",
    "background": "#0f172a",
    "background2": "#1e293b",
    "tile_color": "#1e293b",
    "tile_text": "#f1f5f9",
    "accent": "#38bdf8",
    "columns": 4,
    "logo": "",          # Dateiname im CONFIG_DIR (leer = kein Logo)
    "logo_height": 64,   # Anzeigehoehe in Pixeln
    "show_status": True, # Backend-Status auf dem Dashboard anzeigen
}

ALLOWED_LOGO_EXT = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".ico"}

DEFAULT_NOTIFICATIONS = {
    "enabled": False,
    "email_mode": "smtp",         # "smtp" (Relay) | "direct" (Selbstversand via MX)
    "from_addr": "",
    "to_addrs": "",               # kommagetrennt
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",          # sensibel - im GUI nie zurueckgegeben
    "smtp_security": "starttls",  # starttls | ssl | none
    "webhook_url": "",
    "events": {"update_available": True, "update_result": True, "backend_down": True},
}
NOTIFY_INTERVAL = int(os.environ.get("NOTIFY_INTERVAL", "60"))

# N1: SVGs koennen aktives XSS enthalten. Uploads mit diesen Mustern ablehnen.
_SVG_DANGER = re.compile(
    rb"<script|javascript:|<!entity|<foreignobject|\son[a-z]+\s*=", re.IGNORECASE)


def _svg_is_safe(data: bytes) -> bool:
    return _SVG_DANGER.search(data or b"") is None

def _read_or_create_secret(path: Path) -> str:
    """Persistentes Zufallsgeheimnis: lesen oder einmalig erzeugen (0600)."""
    try:
        if path.exists():
            val = path.read_text(encoding="utf-8").strip()
            if val:
                return val
        path.parent.mkdir(parents=True, exist_ok=True)
        val = secrets.token_hex(32)
        path.write_text(val, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return val
    except OSError:
        return secrets.token_hex(32)   # fluechtig (ueberlebt Neustart nicht)


def _load_secret_key() -> str:
    """SECRET_KEY aus Secret/Env, sonst persistent generieren (kein hartkodierter Default)."""
    return _secret("SECRET_KEY") or _read_or_create_secret(CONFIG_DIR / ".secret_key")


# N3: interne HAProxy-Stats-Schnittstelle mit Basic-Auth absichern (Shared Secret).
STATS_USER = os.environ.get("STATS_USER", "statsuser")
STATS_SECRET = _read_or_create_secret(CONFIG_DIR / ".stats_secret")
STATS_AUTH = f"{STATS_USER}:{STATS_SECRET}"

app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    # nur ueber HTTPS (haproxy terminiert TLS). Nur fuer lokale Tests abschaltbar.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
)
_lock = threading.Lock()

# Eigenes Logfile (zusaetzlich zu stdout), damit das Admin-GUI es anzeigen kann.
HAPROXY_OUT.mkdir(parents=True, exist_ok=True)
_log = logging.getLogger("config-app")
_log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = RotatingFileHandler(APP_LOG, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_log.addHandler(_fh)
_log.addHandler(_sh)


# ----------------------------------------------------------------------------
# CSRF-Schutz (K1): Token in der (signierten) Session, Pflicht bei jedem POST.
# ----------------------------------------------------------------------------
def _csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_hex(32)
        session["_csrf"] = tok
    return tok


@app.context_processor
def _inject_csrf():
    return {"csrf_token": _csrf_token}


@app.context_processor
def _inject_i18n():
    lang = load_config().get("language", DEFAULT_LANG)
    return {
        "t": i18n.translator(lang),
        "lang": lang,
        "languages": [(c, i18n.name(c)) for c in i18n.available()],
    }


@app.before_request
def _csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("_csrf", "")
        if not expected or not hmac.compare_digest(str(expected), str(sent)):
            _log.warning("CSRF-Token ungueltig fuer POST %s", request.path)
            abort(400, "CSRF-Token ungültig oder fehlt.")


# ----------------------------------------------------------------------------
# Konfiguration laden / speichern
# ----------------------------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(DEFAULT_FILE, CONFIG_FILE)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("domain", os.environ.get("PLATFORM_DOMAIN", "platform.example.local"))
    theme = dict(DEFAULT_THEME)
    theme.update(cfg.get("theme") or {})
    cfg["theme"] = theme
    cfg.setdefault("services", [])
    notif = dict(DEFAULT_NOTIFICATIONS)
    stored = cfg.get("notifications") or {}
    events = dict(DEFAULT_NOTIFICATIONS["events"])
    events.update(stored.get("events") or {})
    notif.update(stored)
    notif["events"] = events
    cfg["notifications"] = notif
    lang = str(cfg.get("language") or DEFAULT_LANG)
    cfg["language"] = lang if lang in i18n.available() else DEFAULT_LANG
    return cfg


def _t():
    """Uebersetzer-Funktion t(key, **kwargs) fuer die konfigurierte Sprache."""
    return i18n.translator(load_config().get("language", DEFAULT_LANG))


def save_config(cfg: dict) -> None:
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        tmp.replace(CONFIG_FILE)
        regenerate(cfg)


def regenerate(cfg: dict) -> None:
    HAPROXY_OUT.mkdir(parents=True, exist_ok=True)
    text = render_haproxy(cfg, stats_auth=STATS_AUTH)
    tmp = HAPROXY_CFG.with_suffix(".cfg.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    tmp.replace(HAPROXY_CFG)
    _log.info("haproxy.cfg neu generiert (%d Service(s))", len(cfg.get("services", [])))


# ----------------------------------------------------------------------------
# Status, Backend-Health, Logs
# ----------------------------------------------------------------------------
def _file_md5(path: Path) -> str:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def read_validation_status() -> dict:
    """Liest die vom HAProxy-Watcher geschriebene status.txt."""
    res = {"ok": None, "action": "pending", "ts": None, "cfg_md5": "",
           "message": "", "is_current": False}
    try:
        raw = STATUS_FILE.read_text(encoding="utf-8")
    except OSError:
        return res
    head, _, msg = raw.partition("---MESSAGE---")
    parsed = {}
    for line in head.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k.strip()] = v.strip()
    res["ok"] = parsed.get("ok") == "1"
    res["action"] = parsed.get("action", "pending")
    res["cfg_md5"] = parsed.get("cfg_md5", "")
    res["message"] = msg.strip()
    try:
        res["ts"] = int(parsed.get("ts"))
    except (TypeError, ValueError):
        res["ts"] = None
    # Spiegelt der Status die aktuell generierte Config wider?
    res["is_current"] = bool(res["cfg_md5"]) and res["cfg_md5"] == _file_md5(HAPROXY_CFG)
    return res


# Kurz-Cache, damit haeufige Dashboard-/Admin-Abfragen HAProxy nicht ueberlasten.
_STATS_CACHE = {"ts": 0.0, "data": None}
_STATS_TTL = float(os.environ.get("STATS_CACHE_TTL", "2"))


def fetch_backend_status() -> dict:
    """Holt die HAProxy-Stats (CSV, Basic-Auth) und liefert {backend_name: {status, check}}."""
    now = time.time()
    cached = _STATS_CACHE["data"]
    if cached is not None and now - _STATS_CACHE["ts"] < _STATS_TTL:
        return cached
    try:
        req = urllib.request.Request(STATS_URL)
        cred = base64.b64encode(f"{STATS_USER}:{STATS_SECRET}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
        with urllib.request.urlopen(req, timeout=2) as r:
            data = r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - jede Netz-/Verbindungsstoerung
        result = {"_error": str(exc)}
        _STATS_CACHE["ts"], _STATS_CACHE["data"] = now, result
        return result
    result = {}
    lines = data.splitlines()
    if lines:
        header = lines[0].lstrip("# ").split(",")
        for row in csv.DictReader(lines[1:], fieldnames=header):
            px, sv = row.get("pxname"), row.get("svname")
            if not px or sv in (None, "FRONTEND", "BACKEND"):
                continue
            if px.startswith("bk_"):
                result[px] = {"status": row.get("status", ""),
                              "check": row.get("check_status", "")}
    _STATS_CACHE["ts"], _STATS_CACHE["data"] = now, result
    return result


def _tail(path: Path, n: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""   # leer -> die Anzeige uebersetzt (common.empty)


def _dot_class(status: str) -> str:
    """Normalisiert einen HAProxy-Status auf eine kurze CSS-taugliche Klasse."""
    s = (status or "").strip().lower()
    if s.startswith("up"):
        return "up"
    if s.startswith("down"):
        return "down"
    if s.startswith("maint") or "resolution" in s:
        return "maint"
    if "no check" in s:
        return "no_check"
    if s in ("disabled", "none"):
        return s
    return "unknown"


def _service_states(cfg: dict):
    """Pro Service den Backend-Status fuers GUI aufbereiten."""
    bk = fetch_backend_status()
    # N4: interne Fehlerdetails nur ins Log, dem GUI nur eine generische Meldung.
    stats_error = None
    if "_error" in bk:
        _log.warning("Backend-Stats nicht abrufbar: %s", bk["_error"])
        stats_error = "zurzeit nicht abrufbar"
    states = []
    for idx, s in enumerate(cfg["services"]):
        name = f"bk_{_safe_id(s.get('name', 'svc'), idx)}"
        st = bk.get(name)
        if not s.get("enabled", True):
            status = "disabled"
        elif not s.get("backend") or _norm_path(s.get("path", "/")) == "/":
            status = "none"
        elif stats_error is not None or st is None:
            status = "unknown"
        else:
            status = st.get("status", "")
        states.append({
            "index": idx,
            "status": status,
            "dot": _dot_class(status),
            "check": (st or {}).get("check", ""),
        })
    return states, stats_error


# ----------------------------------------------------------------------------
# Basic-Auth. H1: leeres Passwort SPERRT das Admin-GUI (statt es offen zu lassen).
# ----------------------------------------------------------------------------
def _basic_ok(auth, user: str, password: str) -> bool:
    """Konstantzeit-Vergleich von Basic-Auth-Credentials (deckt M2 mit ab)."""
    if not auth:
        return False
    return (hmac.compare_digest((auth.username or ""), user)
            & hmac.compare_digest((auth.password or ""), password))


def _admin_locked_response() -> Response:
    return Response(_t()("flash.admin_locked"), 403)


def _client_ip() -> str:
    """Echte Client-IP. HAProxy setzt X-Client-IP per `set-header` (ueberschreibt
    jeden Client-Wert -> spoof-sicher). Fallback: remote_addr."""
    return request.headers.get("X-Client-IP") or request.remote_addr or "unknown"


def _auth_challenge(realm: str) -> Response:
    return Response("Authentifizierung erforderlich.", 401,
                    {"WWW-Authenticate": f'Basic realm="{realm}"'})


def _too_many_response(retry: int) -> Response:
    return Response(
        f"Zu viele fehlgeschlagene Anmeldeversuche. Bitte in {retry} s erneut versuchen.",
        429, {"Retry-After": str(retry)})


def _auth_or_response(user: str, password: str, realm: str):
    """None = authentifiziert; sonst eine Response (403/401/429). H2-Ratelimit."""
    if not password:
        return _admin_locked_response()
    ip = _client_ip()
    remaining = _limiter.locked_for(ip)
    if remaining:
        return _too_many_response(remaining)
    auth = request.authorization
    if not auth:
        # Noch keine Credentials -> nur Prompt, NICHT als Fehlversuch werten.
        return _auth_challenge(realm)
    if not _basic_ok(auth, user, password):
        newly = _limiter.record_failure(ip)
        _log.warning("Fehlgeschlagener Admin-Login von %s%s", ip,
                     " -> IP gesperrt" if newly else "")
        return _too_many_response(_limiter.lockout) if newly else _auth_challenge(realm)
    _limiter.record_success(ip)
    return None


def check_admin_password() -> None:
    """Warnt beim Start vor leerem/schwachem Passwort (kein stiller offener Admin)."""
    if not ADMIN_PASSWORD:
        _log.warning("ADMIN_PASSWORD ist LEER -> Admin-GUI ist GESPERRT. "
                     "Bitte ein starkes Passwort in .env setzen und neu starten.")
    elif ADMIN_PASSWORD.lower() in WEAK_PASSWORDS:
        _log.warning("ADMIN_PASSWORD ist ein bekanntes SCHWACHES Passwort -> bitte umgehend aendern!")
    if PLATFORM_ADMIN_PASSWORD and PLATFORM_ADMIN_PASSWORD.lower() in WEAK_PASSWORDS:
        _log.warning("PLATFORM_ADMIN_PASSWORD ist schwach -> bitte aendern!")


def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        resp = _auth_or_response(ADMIN_USER, ADMIN_PASSWORD, "Acme Platform Admin")
        return f(*args, **kwargs) if resp is None else resp
    return wrapper


def _current_actor() -> str:
    auth = request.authorization
    return auth.username if auth and auth.username else "unbekannt"


def requires_platform_admin(f):
    """RBAC-Gate fuer system.update. Verlangt die Plattform-Admin-Rolle, falls
    konfiguriert; sonst mindestens den normalen Admin (mit Warnhinweis)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Wenn eine dedizierte Plattform-Admin-Rolle gesetzt ist, diese verlangen.
        if PLATFORM_ADMIN_PASSWORD:
            resp = _auth_or_response(PLATFORM_ADMIN_USER, PLATFORM_ADMIN_PASSWORD,
                                     "Acme Platform-Admin (system.update)")
            return f(*args, **kwargs) if resp is None else resp
        # Fallback: mindestens normaler Admin (gesperrt, wenn ADMIN_PASSWORD leer).
        return requires_auth(f)(*args, **kwargs)
    return wrapper


def _service_from_form(form) -> dict:
    return {
        "name": form.get("name", "").strip(),
        "path": _norm_path(form.get("path", "")),
        "backend": form.get("backend", "").strip(),
        "scheme": form.get("scheme", "http").strip().lower(),
        "ssl_verify": form.get("ssl_verify") == "on",
        "strip_path": form.get("strip_path") == "on",
        "icon": form.get("icon", "").strip(),
        "color": form.get("color", "").strip(),
        "description": form.get("description", "").strip(),
        "enabled": form.get("enabled") == "on",
    }


# --- K2: strikte Validierung gegen HAProxy-Config-Injection ------------------
# Pfad: fuehrender Slash, nur unbedenkliche Zeichen (keine Whitespace/Newline).
_RE_PATH = re.compile(r"^/[A-Za-z0-9_./-]*$")
# Backend: Hostname/Container/IP(v4/v6 in Klammern) optional :Port - keine Sonderzeichen.
_RE_BACKEND = re.compile(r"^\[?[A-Za-z0-9_.:-]+\]?(?::[0-9]{1,5})?$")
# Kachelfarbe: Hex oder einfacher CSS-Farbname (verhindert CSS-Injection im Dashboard).
_RE_COLOR = re.compile(r"^(#[0-9A-Fa-f]{3,8}|[A-Za-z]{1,20})$")
_RE_SCHEME = ("http", "https")
# H3: Update-Ziel-Tag (Docker-Tag-Zeichensatz), z. B. "latest" oder "v1.2.3".
_RE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _has_ctrl(s: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7f for c in (s or ""))


def validate_service(svc: dict) -> list:
    """Gibt eine Liste von Fehler-Keys zurueck (leer = gueltig). Uebersetzung
    erfolgt im Aufrufer ueber die konfigurierte Sprache."""
    errors = []
    name = svc.get("name", "")
    if not name or _has_ctrl(name):
        errors.append("flash.field_invalid_name")
    path = svc.get("path", "")
    if not _RE_PATH.match(path) or ".." in path:
        errors.append("flash.field_invalid_path")
    backend = svc.get("backend", "")
    if backend and not _RE_BACKEND.match(backend):
        errors.append("flash.field_invalid_backend")
    if svc.get("scheme") not in _RE_SCHEME:
        errors.append("flash.field_invalid_scheme")
    color = svc.get("color", "")
    if color and not _RE_COLOR.match(color):
        errors.append("flash.field_invalid_color")
    if _has_ctrl(svc.get("icon", "")) or _has_ctrl(svc.get("description", "")):
        errors.append("flash.field_invalid_icon")
    return errors


def _logo_url(theme) -> str | None:
    """URL des hochgeladenen Logos inkl. Cache-Buster (mtime), sonst None."""
    fname = theme.get("logo")
    if not fname:
        return None
    p = CONFIG_DIR / fname
    if not p.exists():
        return None
    return f"/logo?v={int(p.stat().st_mtime)}"


# ----------------------------------------------------------------------------
# Routen
# ----------------------------------------------------------------------------
def _dashboard_services(cfg: dict):
    """Aktivierte Services fuer das Dashboard, optional mit Backend-Status."""
    show_status = cfg["theme"].get("show_status", True)
    status_by_index = {}
    if show_status:
        states, _ = _service_states(cfg)
        status_by_index = {st["index"]: st for st in states}
    services = []
    for idx, s in enumerate(cfg["services"]):
        if not s.get("enabled", True):
            continue
        item = dict(s)
        st = status_by_index.get(idx)
        item["dot"] = st["dot"] if st else None
        services.append(item)
    return services, show_status


@app.route("/")
def dashboard():
    cfg = load_config()
    services, show_status = _dashboard_services(cfg)
    return render_template("dashboard.html", theme=cfg["theme"], services=services,
                           show_status=show_status, logo_url=_logo_url(cfg["theme"]))


@app.route("/status")
def public_status():
    """Oeffentlicher, minimaler Status je Kachel (nur Pfad + Status-Klasse) fuers
    Live-Update des Dashboards - keine internen Details."""
    cfg = load_config()
    if not cfg["theme"].get("show_status", True):
        abort(404)
    services, _ = _dashboard_services(cfg)
    return {"services": [{"path": s.get("path"), "dot": s.get("dot") or "unknown"}
                         for s in services]}


@app.route("/logo")
def logo():
    cfg = load_config()
    fname = cfg["theme"].get("logo")
    if not fname:
        abort(404)
    path = CONFIG_DIR / fname
    if not path.exists():
        abort(404)
    resp = send_file(path)
    # N1: hochgeladene Logos (v. a. SVG) abgesichert ausliefern - verhindert
    # Stored-XSS, falls jemand /logo direkt aufruft. sandbox neutralisiert Skripte.
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return resp


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.route("/admin")
@requires_auth
def admin():
    cfg = load_config()
    edit_idx = request.args.get("edit")
    edit_service = None
    if edit_idx is not None and edit_idx.isdigit() and int(edit_idx) < len(cfg["services"]):
        edit_service = cfg["services"][int(edit_idx)]
    states, stats_error = _service_states(cfg)
    status_by_index = {st["index"]: st for st in states}
    update_info = updater.check(force=False)
    update_info["previous"] = updater.previous_version()   # live, nicht gecacht
    if update_info.get("error"):   # Updater liefert Keys -> hier uebersetzen
        t = i18n.translator(cfg.get("language", DEFAULT_LANG))
        update_info["error"] = t(update_info["error"], arg=update_info.get("error_arg", ""))
    return render_template(
        "admin.html",
        cfg=cfg,
        theme=cfg["theme"],
        services=cfg["services"],
        edit_idx=edit_idx if edit_service else None,
        edit_service=edit_service,
        logo_url=_logo_url(cfg["theme"]),
        validation=read_validation_status(),
        status_by_index=status_by_index,
        stats_error=stats_error,
        update=update_info,
        update_audit=updater.read_audit(40),
    )


@app.route("/admin/state")
@requires_auth
def admin_state():
    """JSON fuers Live-Polling: Validierungsstatus + Backend-Health."""
    cfg = load_config()
    states, stats_error = _service_states(cfg)
    return {
        "validation": read_validation_status(),
        "services": states,
        "stats_error": stats_error,
    }


@app.route("/admin/logs")
@requires_auth
def admin_logs():
    which = request.args.get("which", "haproxy")
    try:
        lines = int(request.args.get("lines", "200"))
    except ValueError:
        lines = 200
    lines = max(10, min(1000, lines))
    path = APP_LOG if which == "app" else HAPROXY_LOG
    return {"which": which, "lines": lines, "text": _tail(path, lines)}


# ----------------------------------------------------------------------------
# Update-Kanal
# ----------------------------------------------------------------------------
@app.route("/admin/update/state")
@requires_auth
def admin_update_state():
    data = updater.check(force=False)
    data.update(updater.update_state())
    return data


@app.route("/admin/update/check", methods=["POST"])
@requires_auth
def admin_update_check():
    t = _t()
    res = updater.check(force=True)
    if res.get("error"):
        detail = t(res["error"], arg=res.get("error_arg", ""))
        flash(t("flash.update_check_error", error=detail), "error")
    elif res.get("update_available"):
        flash(t("flash.update_available", tag=res.get("tag")), "ok")
    else:
        flash(t("flash.update_up_to_date"), "ok")
    return redirect(url_for("admin") + "#updates")


@app.route("/admin/update/apply", methods=["POST"])
@requires_platform_admin
def admin_update_apply():
    t = _t()
    if updater.MODE != "host-agent":
        flash(t("flash.update_manual"), "error")
        return redirect(url_for("admin") + "#updates")

    target = request.form.get("target", "latest").strip()
    confirm = request.form.get("confirm", "").strip()
    actor = _current_actor()

    # H3: Ziel-Tag strikt validieren (verhindert Injection in request.json/.env).
    if not _RE_TARGET.match(target):
        updater.audit(actor, "UPDATE_TARGET_INVALID", f"target={target!r}")
        flash(t("flash.target_invalid"), "error")
        return redirect(url_for("admin") + "#updates")

    # Zwei-Schritt-Bestaetigung: Zielversion muss exakt eingetippt werden.
    if confirm != target:
        updater.audit(actor, "UPDATE_CONFIRM_MISMATCH", f"target={target} eingegeben={confirm}")
        flash(t("flash.confirm_mismatch", target=target), "error")
        return redirect(url_for("admin") + "#updates")

    updater.request_update(target, actor)
    _log.info("Update angefordert von %s (Ziel: %s)", actor, target)
    flash(t("flash.update_requested", target=target), "ok")
    return redirect(url_for("admin") + "#updates")


@app.route("/admin/update/rollback", methods=["POST"])
@requires_platform_admin
def admin_update_rollback():
    t = _t()
    if updater.MODE != "host-agent":
        flash(t("flash.rollback_disabled"), "error")
        return redirect(url_for("admin") + "#updates")
    actor = _current_actor()
    target = updater.previous_version()
    if not target:
        flash(t("flash.rollback_none"), "error")
        return redirect(url_for("admin") + "#updates")
    if not _RE_TARGET.match(target):
        updater.audit(actor, "ROLLBACK_TARGET_INVALID", f"target={target!r}")
        flash(t("flash.rollback_invalid"), "error")
        return redirect(url_for("admin") + "#updates")
    updater.request_rollback(target, actor)
    _log.info("Rollback angefordert von %s (Ziel: %s)", actor, target)
    flash(t("flash.rollback_requested", target=target), "ok")
    return redirect(url_for("admin") + "#updates")


# ----------------------------------------------------------------------------
# Benachrichtigungen (E-Mail / Webhook)
# ----------------------------------------------------------------------------
@app.route("/admin/notify", methods=["POST"])
@requires_auth
def admin_notify():
    cfg = load_config()
    n = cfg["notifications"]
    f = request.form
    n["enabled"] = f.get("enabled") == "on"
    n["email_mode"] = f.get("email_mode", "smtp")
    n["from_addr"] = f.get("from_addr", "").strip()
    n["to_addrs"] = f.get("to_addrs", "").strip()
    n["smtp_host"] = f.get("smtp_host", "").strip()
    try:
        n["smtp_port"] = max(1, min(65535, int(f.get("smtp_port") or 587)))
    except ValueError:
        n["smtp_port"] = 587
    n["smtp_user"] = f.get("smtp_user", "").strip()
    pw = f.get("smtp_password", "")
    if pw:                       # nur ueberschreiben, wenn neu eingegeben
        n["smtp_password"] = pw
    n["smtp_security"] = f.get("smtp_security", "starttls")
    n["webhook_url"] = f.get("webhook_url", "").strip()
    n["events"] = {
        "update_available": f.get("ev_update_available") == "on",
        "update_result": f.get("ev_update_result") == "on",
        "backend_down": f.get("ev_backend_down") == "on",
    }
    save_config(cfg)
    flash(_t()("flash.notify_saved"), "ok")
    return redirect(url_for("admin") + "#notify")


@app.route("/admin/notify/test", methods=["POST"])
@requires_auth
def admin_notify_test():
    t = _t()
    n = load_config()["notifications"]
    results = notifier.dispatch(n, "test", "Testbenachrichtigung",
                                "Dies ist eine Testnachricht des HAProxy-Dashboards.",
                                {"test": True})
    if not results:
        flash(t("flash.notify_no_channel"), "error")
    for channel, ok, msg in results:
        flash(t("flash.notify_test", channel=channel,
                result=("OK" if ok else "– " + msg)), "ok" if ok else "error")
    return redirect(url_for("admin") + "#notify")


@app.route("/admin/service", methods=["POST"])
@requires_auth
def admin_service():
    cfg = load_config()
    t = i18n.translator(cfg.get("language", DEFAULT_LANG))
    svc = _service_from_form(request.form)
    if not svc["name"] or not svc["path"]:
        flash(t("flash.name_path_required"), "error")
        return redirect(url_for("admin"))

    # K2: strikte Validierung gegen HAProxy-Config-Injection
    errors = validate_service(svc)
    if errors:
        for e in errors:
            flash(t(e), "error")
        _log.warning("Service abgelehnt (Validierung): %r", {k: svc.get(k) for k in ("name", "path", "backend")})
        return redirect(url_for("admin"))

    # M6: Hinweis, wenn https-Backend OHNE Zertifikatspruefung gespeichert wird.
    if svc["scheme"] == "https" and not svc["ssl_verify"]:
        flash(t("flash.ssl_none_warn", name=svc["name"]), "error")
        _log.warning("Service %s: https mit verify none", svc["name"])

    idx = request.form.get("index", "")
    if idx.isdigit() and int(idx) < len(cfg["services"]):
        cfg["services"][int(idx)] = svc
        flash(t("flash.svc_updated", name=svc["name"]), "ok")
        _log.info("Service aktualisiert: %s (%s -> %s)", svc["name"], svc["path"], svc["backend"])
    else:
        cfg["services"].append(svc)
        flash(t("flash.svc_added", name=svc["name"]), "ok")
        _log.info("Service hinzugefuegt: %s (%s -> %s)", svc["name"], svc["path"], svc["backend"])
    save_config(cfg)
    return redirect(url_for("admin"))


@app.route("/admin/service/delete", methods=["POST"])
@requires_auth
def admin_service_delete():
    cfg = load_config()
    idx = request.form.get("index", "")
    if idx.isdigit() and int(idx) < len(cfg["services"]):
        removed = cfg["services"].pop(int(idx))
        save_config(cfg)
        flash(_t()("flash.svc_deleted", name=removed.get("name")), "ok")
        _log.info("Service geloescht: %s", removed.get("name"))
    return redirect(url_for("admin"))


@app.route("/admin/theme", methods=["POST"])
@requires_auth
def admin_theme():
    cfg = load_config()
    cfg["domain"] = request.form.get("domain", cfg["domain"]).strip()
    lang = request.form.get("language", cfg.get("language", DEFAULT_LANG)).strip()
    if lang in i18n.available():
        cfg["language"] = lang
    t = i18n.translator(cfg.get("language", DEFAULT_LANG))
    theme = cfg["theme"]
    for key in ("title", "subtitle", "background", "background2",
                "tile_color", "tile_text", "accent"):
        theme[key] = request.form.get(key, theme.get(key)).strip()
    try:
        theme["columns"] = max(1, min(8, int(request.form.get("columns", theme["columns"]))))
    except (ValueError, TypeError):
        pass
    try:
        theme["logo_height"] = max(16, min(400, int(request.form.get("logo_height", theme.get("logo_height", 64)))))
    except (ValueError, TypeError):
        pass
    theme["show_status"] = request.form.get("show_status") == "on"

    # Logo: entfernen, ersetzen oder unveraendert lassen
    upload = request.files.get("logo_file")
    if request.form.get("remove_logo") == "on":
        old = theme.get("logo")
        if old and (CONFIG_DIR / old).exists():
            (CONFIG_DIR / old).unlink()
        theme["logo"] = ""
    elif upload and upload.filename:
        ext = os.path.splitext(upload.filename)[1].lower()
        data = upload.read()
        if ext not in ALLOWED_LOGO_EXT:
            flash(t("flash.logo_bad_format"), "error")
        elif ext == ".svg" and not _svg_is_safe(data):
            _log.warning("SVG-Upload abgelehnt (aktive Inhalte)")
            flash(t("flash.svg_rejected"), "error")
        else:
            old = theme.get("logo")
            if old and old != f"logo{ext}" and (CONFIG_DIR / old).exists():
                (CONFIG_DIR / old).unlink()
            fname = f"logo{ext}"
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (CONFIG_DIR / fname).write_bytes(data)
            theme["logo"] = fname

    cfg["theme"] = theme
    save_config(cfg)
    flash(t("flash.theme_saved"), "ok")
    return redirect(url_for("admin"))


# ----------------------------------------------------------------------------
# Benachrichtigungs-Monitor (Hintergrund-Thread): erkennt Ereignisse und meldet.
# ----------------------------------------------------------------------------
NOTIFY_STATE = CONFIG_DIR / ".notify_state.json"


def _notify_state_load() -> dict:
    try:
        return json.loads(NOTIFY_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _notify_state_save(state: dict) -> None:
    try:
        NOTIFY_STATE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _fire(n: dict, event: str, subject: str, body: str, payload: dict) -> None:
    for channel, ok, msg in notifier.dispatch(n, event, subject, body, payload):
        if ok:
            _log.info("Benachrichtigung (%s) gesendet: %s", channel, subject)
        else:
            _log.warning("Benachrichtigung (%s) fehlgeschlagen: %s", channel, msg)


def _notify_monitor() -> None:
    state = _notify_state_load()
    down_baselined = False
    down = set()
    while True:
        time.sleep(NOTIFY_INTERVAL)
        try:
            cfg = load_config()
            n = cfg["notifications"]
            if not n.get("enabled"):
                down_baselined, down = False, set()
                continue

            # 1) Update verfuegbar (einmal je Version)
            chk = updater.check(force=False)
            tag = chk.get("tag")
            if chk.get("update_available") and tag and state.get("notified_version") != tag:
                _fire(n, "update_available", f"Update verfügbar: {tag}",
                      f"Version {tag} steht bereit (laufend: {chk.get('current')}).",
                      {"version": tag, "current": chk.get("current")})
                state["notified_version"] = tag
                _notify_state_save(state)

            # 2) Update-Ergebnis (einmal je Ergebnis)
            res = updater.update_state().get("result")
            if res and res.get("ts") and state.get("result_ts") != res.get("ts"):
                st = res.get("state")
                _fire(n, "update_result", f"Update-Ergebnis: {st}",
                      f"Der host-seitige Updater meldet „{st}“ (Detail: {res.get('detail', '')}).",
                      {"result": res})
                state["result_ts"] = res.get("ts")
                _notify_state_save(state)

            # 3) Backend DOWN (bei Uebergang; erste Runde ist Baseline)
            states, _ = _service_states(cfg)
            names = {i: s.get("name") for i, s in enumerate(cfg["services"])}
            now_down = {st["index"] for st in states if st["dot"] == "down"}
            if not down_baselined:
                down, down_baselined = now_down, True
            else:
                for idx in now_down - down:
                    _fire(n, "backend_down", f"Backend DOWN: {names.get(idx)}",
                          f"Der Service „{names.get(idx)}“ ist nicht erreichbar (DOWN).",
                          {"service": names.get(idx)})
                down = now_down
        except Exception as e:  # noqa: BLE001
            _log.warning("Notify-Monitor: %s", e)


if __name__ == "__main__":
    # Beim Start einmal alles erzeugen, damit haproxy sofort eine Config hat.
    regenerate(load_config())
    check_admin_password()
    threading.Thread(target=_notify_monitor, name="notify-monitor", daemon=True).start()
    _log.info("config-app gestartet, bereit auf :5000")
    serve(app, host="0.0.0.0", port=5000)
