"""Acme Platform - Dashboard + Admin-GUI.

Liefert das Kachel-Dashboard und ein Web-GUI zur Verwaltung der Services
und des Erscheinungsbilds. Schreibt services.yaml und generiert daraus die
haproxy.cfg (der haproxy-Container laedt sie automatisch neu).
"""
import base64
import csv
import hashlib
import hmac
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
}

ALLOWED_LOGO_EXT = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".ico"}

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
    return cfg


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


def fetch_backend_status() -> dict:
    """Holt die HAProxy-Stats (CSV, Basic-Auth) und liefert {backend_name: {status, check}}."""
    try:
        req = urllib.request.Request(STATS_URL)
        cred = base64.b64encode(f"{STATS_USER}:{STATS_SECRET}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
        with urllib.request.urlopen(req, timeout=2) as r:
            data = r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - jede Netz-/Verbindungsstoerung
        return {"_error": str(exc)}
    lines = data.splitlines()
    if not lines:
        return {}
    header = lines[0].lstrip("# ").split(",")
    result = {}
    for row in csv.DictReader(lines[1:], fieldnames=header):
        px, sv = row.get("pxname"), row.get("svname")
        if not px or sv in (None, "FRONTEND", "BACKEND"):
            continue
        if px.startswith("bk_"):
            result[px] = {"status": row.get("status", ""),
                          "check": row.get("check_status", "")}
    return result


def _tail(path: Path, n: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(noch keine Logeintraege)"


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
    return Response(
        "Admin-GUI ist deaktiviert: bitte ADMIN_PASSWORD in .env setzen und neu starten.",
        403,
    )


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
    """Gibt eine Liste von Fehlermeldungen zurueck (leer = gueltig)."""
    errors = []
    name = svc.get("name", "")
    if not name or _has_ctrl(name):
        errors.append("Name fehlt oder enthält unzulässige Steuerzeichen.")
    path = svc.get("path", "")
    if not _RE_PATH.match(path) or ".." in path:
        errors.append("Pfad ungültig: erlaubt sind /, Buchstaben, Ziffern, . _ - (keine Leer-/Sonderzeichen).")
    backend = svc.get("backend", "")
    if backend and not _RE_BACKEND.match(backend):
        errors.append("Backend ungültig: nur host/container[:port] bzw. ip[:port] erlaubt.")
    if svc.get("scheme") not in _RE_SCHEME:
        errors.append("Schema muss http oder https sein.")
    color = svc.get("color", "")
    if color and not _RE_COLOR.match(color):
        errors.append("Farbe ungültig: Hex (z. B. #22c55e) oder einfacher Farbname.")
    if _has_ctrl(svc.get("icon", "")) or _has_ctrl(svc.get("description", "")):
        errors.append("Icon/Beschreibung enthält unzulässige Steuerzeichen.")
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
@app.route("/")
def dashboard():
    cfg = load_config()
    services = [s for s in cfg["services"] if s.get("enabled", True)]
    return render_template("dashboard.html", theme=cfg["theme"],
                           services=services, logo_url=_logo_url(cfg["theme"]))


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
        update=updater.check(force=False),
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
    res = updater.check(force=True)
    if res.get("error"):
        flash(f"Update-Prüfung: {res['error']}", "error")
    elif res.get("update_available"):
        flash(f"Update verfügbar: {res.get('tag')}", "ok")
    else:
        flash("Du verwendest bereits die aktuelle Version.", "ok")
    return redirect(url_for("admin") + "#updates")


@app.route("/admin/update/apply", methods=["POST"])
@requires_platform_admin
def admin_update_apply():
    if updater.MODE != "host-agent":
        flash("Automatisches Update ist nicht aktiv (UPDATE_MODE=manual). Bitte den angezeigten Befehl ausführen.", "error")
        return redirect(url_for("admin") + "#updates")

    target = request.form.get("target", "latest").strip()
    confirm = request.form.get("confirm", "").strip()
    actor = _current_actor()

    # H3: Ziel-Tag strikt validieren (verhindert Injection in request.json/.env).
    if not _RE_TARGET.match(target):
        updater.audit(actor, "UPDATE_TARGET_INVALID", f"target={target!r}")
        flash("Ungültige Zielversion (erlaubt: Buchstaben, Ziffern, . _ -).", "error")
        return redirect(url_for("admin") + "#updates")

    # Zwei-Schritt-Bestaetigung: Zielversion muss exakt eingetippt werden.
    if confirm != target:
        updater.audit(actor, "UPDATE_CONFIRM_MISMATCH", f"target={target} eingegeben={confirm}")
        flash(f"Bestätigung fehlgeschlagen: Bitte exakt „{target}“ zur Bestätigung eintippen.", "error")
        return redirect(url_for("admin") + "#updates")

    updater.request_update(target, actor)
    _log.info("Update angefordert von %s (Ziel: %s)", actor, target)
    flash(f"Update auf {target} angefordert – der host-seitige Updater prüft Signatur, "
          f"lädt die Images und startet mit Healthcheck/Rollback neu …", "ok")
    return redirect(url_for("admin") + "#updates")


@app.route("/admin/service", methods=["POST"])
@requires_auth
def admin_service():
    cfg = load_config()
    svc = _service_from_form(request.form)
    if not svc["name"] or not svc["path"]:
        flash("Name und Pfad sind Pflichtfelder.", "error")
        return redirect(url_for("admin"))

    # K2: strikte Validierung gegen HAProxy-Config-Injection
    errors = validate_service(svc)
    if errors:
        for e in errors:
            flash(e, "error")
        _log.warning("Service abgelehnt (Validierung): %r", {k: svc.get(k) for k in ("name", "path", "backend")})
        return redirect(url_for("admin"))

    # M6: Hinweis, wenn https-Backend OHNE Zertifikatspruefung gespeichert wird.
    if svc["scheme"] == "https" and not svc["ssl_verify"]:
        flash(f"Achtung: „{svc['name']}“ nutzt https OHNE Zertifikatsprüfung (MITM möglich).", "error")
        _log.warning("Service %s: https mit verify none", svc["name"])

    idx = request.form.get("index", "")
    if idx.isdigit() and int(idx) < len(cfg["services"]):
        cfg["services"][int(idx)] = svc
        flash(f"Service „{svc['name']}“ aktualisiert.", "ok")
        _log.info("Service aktualisiert: %s (%s -> %s)", svc["name"], svc["path"], svc["backend"])
    else:
        cfg["services"].append(svc)
        flash(f"Service „{svc['name']}“ hinzugefuegt.", "ok")
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
        flash(f"Service „{removed.get('name')}“ geloescht.", "ok")
        _log.info("Service geloescht: %s", removed.get("name"))
    return redirect(url_for("admin"))


@app.route("/admin/theme", methods=["POST"])
@requires_auth
def admin_theme():
    cfg = load_config()
    cfg["domain"] = request.form.get("domain", cfg["domain"]).strip()
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
            flash("Logo-Format nicht unterstuetzt (erlaubt: png, jpg, svg, gif, webp, ico).", "error")
        elif ext == ".svg" and not _svg_is_safe(data):
            _log.warning("SVG-Upload abgelehnt (aktive Inhalte)")
            flash("SVG abgelehnt: enthält Skripte/Event-Handler (mögliches XSS).", "error")
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
    flash("Erscheinungsbild gespeichert.", "ok")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    # Beim Start einmal alles erzeugen, damit haproxy sofort eine Config hat.
    regenerate(load_config())
    check_admin_password()
    _log.info("config-app gestartet, bereit auf :5000")
    serve(app, host="0.0.0.0", port=5000)
