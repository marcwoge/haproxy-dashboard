"""#21: Vertrauenswuerdige CA-/Self-signed-Zertifikate zum VERIFIZIEREN von
Backends (Richtung HAProxy->Backend). Hier gehoeren NUR oeffentliche Zertifikate
hin (kein privater Schluessel): eine Firmen-/AD-Root-CA oder das self-signed
Zertifikat eines Ziels. Daraus wird ein Bundle gebaut, das HAProxy als ca-file
nutzt, wenn ein Dienst `backend_ca: custom` hat. Reines Stdlib-Parsing (ssl),
keine zusaetzliche Abhaengigkeit.
"""
import os
import re
import ssl
from pathlib import Path

from werkzeug.utils import secure_filename

from certs import _parse, _SAFE_NAME

TRUST_DIR = Path(os.environ.get("EXTRA_CA_DIR_LOCAL", "/app/config/ca"))
BUNDLE = TRUST_DIR / "ca-bundle.pem"
_RE_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----")
# Jede Art privater Schluessel (RSA/EC/ENCRYPTED/PKCS8) -> strikt ablehnen.
_RE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY-----")


def _pem_files() -> list:
    try:
        return sorted(p for p in TRUST_DIR.glob("*.pem")
                      if p.is_file() and p.name != BUNDLE.name)
    except OSError:
        return []


def list_cas() -> list:
    """Metadaten aller vertrauenswuerdigen Backend-CAs (fuers Admin-GUI)."""
    return [_parse(p) for p in _pem_files()]


def ca_files() -> list:
    """Dateinamen (fuer den cfg-Hash des Renderers -> Reload bei Aenderung)."""
    return [p.name for p in _pem_files()]


def min_days_left():
    """Kleinste Rest-Laufzeit (Tage) ueber alle Trust-Certs, sonst None."""
    vals = [c["days_left"] for c in list_cas() if isinstance(c.get("days_left"), int)]
    return min(vals) if vals else None


def _rebuild_bundle() -> None:
    """Alle Trust-Certs zu ca-bundle.pem zusammenfuehren (HAProxy-ca-file)."""
    files = _pem_files()
    if not files:
        try:
            BUNDLE.unlink(missing_ok=True)
        except OSError:
            pass
        return
    parts = []
    for p in files:
        try:
            parts.append(p.read_bytes().rstrip())
        except OSError:
            pass
    tmp = BUNDLE.with_suffix(".pem.tmp")
    tmp.write_bytes(b"\n".join(parts) + b"\n")
    try:
        os.chmod(tmp, 0o644)   # nur oeffentliche Certs -> lesbar fuer haproxy
    except OSError:
        pass
    tmp.replace(BUNDLE)


def save_ca(name: str, data: bytes) -> tuple:
    """CA-/Self-signed-Cert (ohne Key) ablegen + Bundle neu bauen. (ok, info/key)."""
    data = data or b""
    if _RE_KEY.search(data):
        return False, "flash.ca_has_key"         # privater Schluessel -> nie hier
    if not _RE_CERT.search(data):                 # evtl. DER -> PEM wandeln
        try:
            data = ssl.DER_cert_to_PEM_cert(data).encode("ascii")
        except Exception:  # noqa: BLE001
            return False, "flash.ca_invalid"
    if not _RE_CERT.search(data):
        return False, "flash.ca_invalid"
    base = re.sub(r"[^A-Za-z0-9._-]+", "-",
                  os.path.splitext(os.path.basename(name or ""))[0]).strip("-.")
    fname = secure_filename((base or "ca") + ".pem")
    if not fname or not _SAFE_NAME.match(fname) or fname == BUNDLE.name:
        return False, "flash.cert_bad_name"
    TRUST_DIR.mkdir(parents=True, exist_ok=True)
    dest = TRUST_DIR / fname
    tmp = dest.with_suffix(".pem.tmp")
    tmp.write_bytes(data)
    if not _parse(tmp).get("ok"):                 # unparsbar -> verwerfen
        tmp.unlink(missing_ok=True)
        return False, "flash.ca_invalid"
    tmp.replace(dest)
    _rebuild_bundle()
    return True, fname


def delete_ca(name: str) -> bool:
    fname = secure_filename(name or "")
    if not fname or not _SAFE_NAME.match(fname) or fname == BUNDLE.name:
        return False
    try:
        p = TRUST_DIR / fname
        if p.is_file():
            p.unlink()
            _rebuild_bundle()
            return True
    except OSError:
        pass
    return False
