"""Verwaltung zusaetzlicher TLS-Zertifikate (SNI) inkl. Metadaten/Ablauf.

Zertifikate liegen als kombiniertes PEM (Zertifikatskette + privater Schluessel)
unter CERTS_DIR und werden vom haproxy-Container per SNI ausgewaehlt. Der private
Schluessel wird NIE ueber das GUI angezeigt oder ausgeliefert - es werden nur
geparste Zertifikatsfelder (Subject/SAN/Aussteller/Ablauf) zurueckgegeben.
Reines Standardbibliothek-Parsing (ssl), keine zusaetzliche Abhaengigkeit.
"""
import datetime
import os
import re
import ssl
from pathlib import Path

CERTS_DIR = Path(os.environ.get("EXTRA_CERT_DIR_LOCAL", "/app/config/certs"))
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RE_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----")
_RE_KEY = re.compile(rb"-----BEGIN (?:RSA |EC |)PRIVATE KEY-----")


def _flat(rdns):
    return {k: v for rdn in (rdns or ()) for (k, v) in rdn}


def _parse(path: Path) -> dict:
    """Zertifikatsmetadaten (ohne Schluessel). ok=False bei Parsefehler."""
    info = {"file": path.name, "subject": "", "issuer": "", "sans": [],
            "not_after": "", "days_left": None, "ok": False}
    try:
        d = ssl._ssl._test_decode_cert(str(path))
    except Exception:  # noqa: BLE001 - defekte/unlesbare Datei
        return info
    subj, iss = _flat(d.get("subject")), _flat(d.get("issuer"))
    info["subject"] = subj.get("commonName") or subj.get("organizationName") or ""
    info["issuer"] = iss.get("commonName") or iss.get("organizationName") or ""
    info["sans"] = [v for (t, v) in d.get("subjectAltName", ()) if t == "DNS"]
    info["not_after"] = d.get("notAfter", "")
    try:
        exp = datetime.datetime.strptime(info["not_after"], "%b %d %H:%M:%S %Y %Z") \
            .replace(tzinfo=datetime.timezone.utc)
        info["days_left"] = (exp - datetime.datetime.now(datetime.timezone.utc)).days
    except ValueError:
        pass
    info["ok"] = True
    return info


def _pem_files() -> list:
    try:
        return sorted(p for p in CERTS_DIR.glob("*.pem") if p.is_file())
    except OSError:
        return []


def list_certs() -> list:
    """Metadaten aller zusaetzlichen Zertifikate (fuers Admin-GUI)."""
    return [_parse(p) for p in _pem_files()]


def cert_files() -> list:
    """Dateinamen der vorhandenen Zertifikate (fuer den Renderer/SNI-Bind)."""
    return [p.name for p in _pem_files()]


def min_days_left():
    """Kleinste Rest-Laufzeit (Tage) ueber alle Zertifikate, sonst None."""
    vals = [c["days_left"] for c in list_certs() if isinstance(c.get("days_left"), int)]
    return min(vals) if vals else None


def save_cert(name: str, cert_bytes: bytes, key_bytes: bytes = b"") -> tuple:
    """Kombiniertes PEM (Cert[+Chain]+Key) speichern, 0600. Liefert (ok, info/key)."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.splitext(os.path.basename(name or ""))[0]).strip("-.")
    fname = (base or "cert") + ".pem"
    if not _SAFE_NAME.match(fname):
        return False, "flash.cert_bad_name"
    data = cert_bytes or b""
    if key_bytes:
        data = data.rstrip() + b"\n" + key_bytes.lstrip()
    if not _RE_CERT.search(data) or not _RE_KEY.search(data):
        return False, "flash.cert_need_both"
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = CERTS_DIR / fname
    tmp = dest.with_suffix(".pem.tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    if not _parse(tmp).get("ok"):          # unparsbares Zertifikat -> verwerfen
        tmp.unlink(missing_ok=True)
        return False, "flash.cert_invalid"
    tmp.replace(dest)
    return True, fname


def delete_cert(name: str) -> bool:
    fname = os.path.basename(name or "")
    if not _SAFE_NAME.match(fname):
        return False
    try:
        p = CERTS_DIR / fname
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False
