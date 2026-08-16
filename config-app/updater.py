"""Update-Kanal (App-Seite).

Sicherheitsmodell: Die App fuehrt NIEMALS Docker aus und hat keinen Docker-Socket.
Sie prueft nur read-only die Release-API und schreibt bei Bedarf eine *Anforderung*
(Ziel-Tag) in eine Datei. Die privilegierte Aktion (verify -> pull -> up ->
healthcheck -> rollback) uebernimmt ein host-seitiger Updater (systemd), also
ausserhalb der Container-Vertrauensgrenze.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

def _secret(name: str, default: str = "") -> str:
    """Wert aus Docker-Secret-Datei (<NAME>_FILE) lesen, sonst aus Env (M4)."""
    path = os.environ.get(name + "_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return os.environ.get(name, default)


REPO = os.environ.get("UPDATE_REPO", "")
TOKEN = _secret("UPDATE_TOKEN", "")   # optional (nur fuer private Repos/Pakete)
CURRENT = os.environ.get("APP_VERSION", "dev")
# manual  = GUI zeigt nur den Befehl an (kein Automatismus)
# host-agent = GUI schreibt eine Anforderung fuer den host-seitigen Updater
MODE = os.environ.get("UPDATE_MODE", "manual").lower()
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "false").lower() == "true"
AUTO_SCHEDULE = os.environ.get("AUTO_UPDATE_SCHEDULE", "")
UPDATE_DIR = Path(os.environ.get("UPDATE_DIR", "/app/update"))
CHECK_FILE = UPDATE_DIR / "check.json"
REQUEST_FILE = UPDATE_DIR / "request.json"
RESULT_FILE = UPDATE_DIR / "result.json"
PREVIOUS_FILE = UPDATE_DIR / "previous-version"   # vom Host-Updater geschrieben
# M7: App-Audit (Container, weniger vertrauenswuerdig) getrennt vom Host-Audit.
# Der Host-Updater schreibt in host-audit.log (append-only via chattr +a); der
# Container kann es dank cap_drop ALL nicht umschreiben/loeschen, nur die App
# schreibt in app-audit.log. Fuers GUI werden beide zusammengefuehrt (nur Lesen).
AUDIT_LOG = UPDATE_DIR / "app-audit.log"
HOST_AUDIT_LOG = UPDATE_DIR / "host-audit.log"
CACHE_TTL = int(os.environ.get("UPDATE_CHECK_TTL", "21600"))  # 6 h

MANUAL_COMMAND = "docker compose pull && docker compose up -d"


# ---------------------------------------------------------------------------
# Versionsvergleich
# ---------------------------------------------------------------------------
def _parse(version: str):
    nums = re.findall(r"\d+", version or "")
    return tuple(int(x) for x in nums[:3]) if nums else None


def is_newer(latest: str, current: str) -> bool:
    lp, cp = _parse(latest), _parse(current)
    if not lp:
        return False
    if not cp:
        return True
    return lp > cp


# ---------------------------------------------------------------------------
# Release-Pruefung (read-only)
# ---------------------------------------------------------------------------
def fetch_latest_release() -> dict:
    # Fehler werden als Uebersetzungs-Keys (+ optionalem "error_arg") zurueckgegeben;
    # die Uebersetzung erfolgt an der Anzeigestelle (Sprache des Nutzers).
    if not REPO:
        return {"error": "update.err_no_repo"}
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {
            "tag": data.get("tag_name"),
            "name": data.get("name"),
            "url": data.get("html_url"),
            "body": (data.get("body") or "")[:2000],
            "published_at": data.get("published_at"),
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": "update.err_no_releases"}
        if e.code in (401, 403):
            return {"error": "update.err_forbidden", "error_arg": e.code}
        return {"error": "update.err_http", "error_arg": e.code}
    except Exception as e:  # noqa: BLE001
        return {"error": "update.err_network", "error_arg": str(e)}


def check(force: bool = False) -> dict:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    if not force and CHECK_FILE.exists():
        try:
            cached = json.loads(CHECK_FILE.read_text(encoding="utf-8"))
            if time.time() - cached.get("checked_at", 0) < CACHE_TTL:
                return cached
        except (OSError, ValueError):
            pass

    latest = fetch_latest_release()
    result = {"current": CURRENT, "mode": MODE, "repo": REPO,
              "auto_update": AUTO_UPDATE, "auto_schedule": AUTO_SCHEDULE,
              "manual_command": MANUAL_COMMAND, "checked_at": time.time()}
    result.update(latest)
    result["update_available"] = bool(latest.get("tag")) and is_newer(latest["tag"], CURRENT)
    try:
        CHECK_FILE.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


# ---------------------------------------------------------------------------
# Audit-Log (append-only). Auf dem Server zusaetzlich per `chattr +a` haerten.
# ---------------------------------------------------------------------------
def audit(actor: str, action: str, detail: str = "") -> None:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} | app | actor={actor} | {action}"
    if detail:
        line += f" | {detail}"
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Anforderung an den host-seitigen Updater
# ---------------------------------------------------------------------------
def previous_version() -> str | None:
    """Vom Host-Updater gemerkte vorherige Version (fuer 1-Klick-Rollback)."""
    try:
        val = PREVIOUS_FILE.read_text(encoding="utf-8").strip()
        return val or None
    except OSError:
        return None


def request_update(target: str, actor: str, action: str = "UPDATE_REQUESTED") -> None:
    """Schreibt die (Update-/Rollback-)Anforderung. Fuehrt selbst KEINE Docker-Aktion aus."""
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        RESULT_FILE.unlink()
    except OSError:
        pass
    payload = {"target": target, "requested_by": actor, "ts": time.time()}
    tmp = REQUEST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(REQUEST_FILE)
    audit(actor, action, f"target={target}")


def request_rollback(target: str, actor: str) -> None:
    request_update(target, actor, action="ROLLBACK_REQUESTED")


def read_audit(lines: int = 50) -> str:
    """Fuehrt App- und Host-Audit fuers GUI zusammen (nach Zeitstempel sortiert)."""
    entries = []
    for path in (AUDIT_LOG, HOST_AUDIT_LOG):
        try:
            entries += path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
    if not entries:
        return ""   # leer -> die Anzeige uebersetzt (update.audit_empty)
    # Zeilen beginnen mit ISO-Zeitstempel -> lexikografische Sortierung = chronologisch.
    entries.sort()
    return "\n".join(entries[-lines:])


def update_state() -> dict:
    pending = REQUEST_FILE.exists()
    result = None
    if RESULT_FILE.exists():
        try:
            result = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = None
    return {"pending": pending, "result": result, "audit": read_audit(40),
            "mode": MODE, "previous": previous_version()}
