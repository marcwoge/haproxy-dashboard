"""#27: Verwaltung des Plattform-Zertifikats (Standard-Zertifikat des HTTPS-
Frontends) ueber das Admin-GUI.

Das aktive Plattform-Zertifikat stammt aus einer von drei Quellen (Prioritaet):
  1. "managed" - im GUI hochgeladen, liegt als kombiniertes PEM unter
     CERTS_DIR/_platform.pem (vom haproxy-Container als /certs-extra/_platform.pem
     gelesen). Hat Vorrang.
  2. "host"    - klassischer Weg: Dateien in ./certs auf dem Host (vom
     config-app-Container read-only unter HOST_CERT_DIR sichtbar).
  3. "self-signed" - vom haproxy-Entrypoint erzeugtes Fallback-Zertifikat,
     persistiert unter dem geteilten Ausgabeordner (selfsigned.pem).

Der private Schluessel wird NIE ueber das GUI angezeigt. Austausch im GUI schreibt
ein neues _platform.pem und bumpt die Config (Reload ueber den Watcher) - kein
Container-Neustart. Die vorherige Fassung wird als .bak gesichert und ist
wiederherstellbar. Metadaten/Ablauf (inkl. Fingerprint) werden fuer alle drei
Quellen angezeigt und fliessen in die Ablaufwarnung ein.
"""
import datetime
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from certs import CERTS_DIR

_log = logging.getLogger("config-app")

# GUI-verwaltetes Plattform-Zertifikat (reservierter Name im SNI-Ordner; wird von
# certs.py aus der SNI-Liste ausgeschlossen, damit es dort nicht doppelt auftaucht).
MANAGED = CERTS_DIR / "_platform.pem"
BACKUP = CERTS_DIR / "_platform.pem.bak"
# Quelle "managed" aus Sicht des haproxy-Containers (CERTS_DIR -> /certs-extra).
MANAGED_HAPROXY_PATH = "/certs-extra/_platform.pem"
# Pfad des vom Entrypoint gebauten Host-/Self-signed-PEM (haproxy-Sicht).
DEFAULT_HAPROXY_PATH = "/usr/local/etc/haproxy/platform.pem"

# Host-Zertifikate (./certs) read-only im config-app-Container (docker-compose.yml).
HOST_CERT_DIR = Path(os.environ.get("PLATFORM_CERT_DIR_LOCAL", "/app/platform-certs"))
# Vom haproxy-Entrypoint persistiertes selbstsigniertes Fallback-Zertifikat.
HAPROXY_OUT = Path(os.environ.get("HAPROXY_OUT", "/app/haproxy_out"))
SELFSIGNED = HAPROXY_OUT / "selfsigned.pem"


def _host_cert_path():
    """Erstes Host-Zertifikat (gleiche Reihenfolge wie der haproxy-Entrypoint)."""
    try:
        for name in ("fullchain.pem",):
            p = HOST_CERT_DIR / name
            if p.is_file():
                return p
        for pat in ("*.crt", "*.pem"):
            for p in sorted(HOST_CERT_DIR.glob(pat)):
                if p.is_file():
                    return p
    except OSError:
        pass
    return None


def active_source() -> str:
    """Welche Quelle liefert gerade das Plattform-Zertifikat?"""
    if MANAGED.is_file():
        return "managed"
    if _host_cert_path() is not None:
        return "host"
    if SELFSIGNED.is_file():
        return "self-signed"
    return "none"


def _active_local_path():
    """Lokal (im config-app-Container) lesbarer Pfad des aktiven Zertifikats,
    zum Parsen der Metadaten - oder None, wenn nichts lesbar ist."""
    src = active_source()
    if src == "managed":
        return MANAGED
    if src == "host":
        return _host_cert_path()
    if src == "self-signed":
        return SELFSIGNED
    return None


def haproxy_crt_path() -> str:
    """Pfad, den die generierte Config fuer das Plattform-Zertifikat verwendet
    (als LETZTER crt-Eintrag der :443-Bind-Zeile und fuer TCP-TLS-Streams)."""
    return MANAGED_HAPROXY_PATH if MANAGED.is_file() else DEFAULT_HAPROXY_PATH


def _load_cert(data: bytes):
    """Zertifikat aus PEM oder DER laden (erstes/Leaf-Zertifikat)."""
    if b"-----BEGIN CERTIFICATE-----" in data:
        return x509.load_pem_x509_certificate(data)
    return x509.load_der_x509_certificate(data)   # DER (.cer)


def _fingerprint(cert) -> str:
    fp = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in fp)


def _info_from_cert(cert, source: str) -> dict:
    info = {"source": source, "subject": "", "issuer": "", "sans": [],
            "not_after": "", "days_left": None, "fingerprint": "", "ok": True}

    def _cn(name):
        try:
            attrs = name.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
            if attrs:
                return attrs[0].value
            org = name.get_attributes_for_oid(x509.oid.NameOID.ORGANIZATION_NAME)
            return org[0].value if org else ""
        except Exception:  # noqa: BLE001
            return ""

    info["subject"] = _cn(cert.subject)
    info["issuer"] = _cn(cert.issuer)
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        info["sans"] = ext.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        pass
    exp = cert.not_valid_after_utc
    info["not_after"] = exp.strftime("%b %d %H:%M:%S %Y UTC")
    info["days_left"] = (exp - datetime.datetime.now(datetime.timezone.utc)).days
    info["fingerprint"] = _fingerprint(cert)
    return info


def info() -> dict:
    """Metadaten des aktiven Plattform-Zertifikats fuers Admin-GUI (ohne Schluessel).
    Enthaelt immer "source"; bei Parsefehler/none ist ok=False."""
    src = active_source()
    p = _active_local_path()
    if p is None:
        return {"source": src, "ok": False, "subject": "", "issuer": "",
                "sans": [], "not_after": "", "days_left": None, "fingerprint": ""}
    try:
        cert = _load_cert(p.read_bytes())
        return _info_from_cert(cert, src)
    except Exception as e:  # noqa: BLE001
        _log.warning("Plattform-Zertifikat (%s) nicht lesbar/parsebar: %s", src, e)
        return {"source": src, "ok": False, "subject": "", "issuer": "",
                "sans": [], "not_after": "", "days_left": None, "fingerprint": ""}


def days_left():
    """Rest-Laufzeit (Tage) des aktiven Plattform-Zertifikats oder None."""
    d = info().get("days_left")
    return d if isinstance(d, int) else None


def has_backup() -> bool:
    return BACKUP.is_file()


def _cert_pem_chain(data: bytes) -> bytes:
    """Zertifikatskette als PEM zurueckgeben (DER -> PEM konvertieren)."""
    if b"-----BEGIN CERTIFICATE-----" in data:
        return data.strip() + b"\n"   # PEM (Leaf [+ Kette]) unveraendert uebernehmen
    cert = x509.load_der_x509_certificate(data)
    return cert.public_bytes(serialization.Encoding.PEM)


def save(cert_bytes: bytes, key_bytes: bytes, passphrase: bytes | None = None) -> tuple:
    """Plattform-Zertifikat austauschen. Prueft: Zertifikat parsebar, Schluessel
    (ggf. mit Passphrase) ladbar, Schluessel passt zum Zertifikat. Erst dann wird
    das kombinierte PEM atomar geschrieben (0600); die vorherige Fassung wandert
    nach .bak. Liefert (ok, flash-key). Bei Fehler bleibt alles unveraendert."""
    cert_bytes = cert_bytes or b""
    key_bytes = key_bytes or b""
    if not cert_bytes or not key_bytes:
        return False, "flash.cert_need_both"
    # 1) Zertifikat (Leaf) laden
    try:
        leaf = _load_cert(cert_bytes)
    except Exception:  # noqa: BLE001
        return False, "flash.cert_invalid"
    # 2) Privaten Schluessel laden (PEM, ggf. verschluesselt mit Passphrase)
    try:
        key = load_pem_private_key(key_bytes, password=passphrase)
    except (TypeError, ValueError):
        # TypeError: Passphrase fehlt/ueberfluessig; ValueError: falsch/defekt.
        return False, "flash.cert_key_passphrase"
    # 3) Schluessel muss zum Zertifikat passen
    try:
        pub_cert = leaf.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        pub_key = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    except Exception:  # noqa: BLE001
        return False, "flash.cert_invalid"
    if pub_cert != pub_key:
        return False, "flash.cert_key_mismatch"
    # 4) Kombiniertes PEM bauen: Kette zuerst, dann UNVERSCHLUESSELTER Schluessel
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    combined = _cert_pem_chain(cert_bytes) + key_pem
    # 5) Atomar schreiben, vorherige Fassung sichern
    try:
        CERTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MANAGED.with_suffix(".pem.new")
        tmp.write_bytes(combined)
        os.chmod(tmp, 0o600)
        if MANAGED.is_file():
            MANAGED.replace(BACKUP)
            try:
                os.chmod(BACKUP, 0o600)
            except OSError:
                pass
        tmp.replace(MANAGED)
    except OSError as e:
        _log.warning("Plattform-Zertifikat konnte nicht gespeichert werden: %s", e)
        return False, "flash.cert_write_failed"
    return True, "flash.platform_cert_saved"


def restore_backup() -> bool:
    """Gesicherte vorherige Fassung wiederherstellen (1-Klick-Rollback im GUI)."""
    if not BACKUP.is_file():
        return False
    try:
        # aktuelle managed-Fassung mit dem Backup tauschen (Backup bleibt erhalten)
        data = BACKUP.read_bytes()
        tmp = MANAGED.with_suffix(".pem.new")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        tmp.replace(MANAGED)
        return True
    except OSError as e:
        _log.warning("Plattform-Zertifikat: Wiederherstellung fehlgeschlagen: %s", e)
        return False


def delete_managed() -> bool:
    """GUI-verwaltetes Zertifikat entfernen -> zurueck auf Host/Self-signed."""
    ok = False
    for p in (MANAGED, BACKUP):
        try:
            if p.is_file():
                p.unlink()
                ok = True
        except OSError:
            pass
    return ok
