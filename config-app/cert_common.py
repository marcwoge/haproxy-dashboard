"""#28: Gemeinsame Verarbeitung hochgeladener Zertifikate/Schluessel (SNI + Plattform).

Normalisiert die verschiedenen Upload-Formate zu EINEM kombinierten PEM, das
HAProxy laden kann: Zertifikatskette (Leaf zuerst) + UNVERSCHLUESSELTER
PKCS#8-Schluessel. Unterstuetzt:
  - Zertifikat als PEM oder DER (.cer),
  - Schluessel als PEM (PKCS#1/PKCS#8, unverschluesselt oder mit 3DES/AES
    verschluesselt) oder DER (PKCS#8), jeweils optional mit Passphrase,
  - PKCS#12 (.pfx/.p12) als eine Datei - Zertifikat, Kette und Schluessel werden
    daraus entnommen, auch bei Legacy-Verschluesselung (RC2/3DES), die die
    cryptography-Bibliothek selbst entschluesselt (kein openssl-CLI noetig).

Die Passphrase dient AUSSCHLIESSLICH zum Entschluesseln beim Upload. Sie wird
nicht gespeichert, nicht geloggt und nicht zurueckgegeben; gespeichert wird immer
der unverschluesselte Schluessel, weil HAProxy verschluesselte Schluessel nicht
laden kann.

Rueckgabe: (True, combined_pem_bytes) oder (False, flash-key). Flash-Keys:
  flash.cert_need_both       - Zertifikat und/oder Schluessel fehlen
  flash.cert_invalid         - Zertifikat/Datei nicht parsebar
  flash.cert_key_passphrase  - Schluessel braucht/hat falsche Passphrase
  flash.cert_key_mismatch    - Schluessel passt nicht zum Zertifikat
"""
import re

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import (
    load_der_private_key, load_pem_private_key, pkcs12)

_PEM_CERT = b"-----BEGIN CERTIFICATE-----"
# Alle PEM-Schluesselvarianten inkl. "ENCRYPTED PRIVATE KEY" (PKCS#8 verschluesselt).
_PEM_KEY = re.compile(rb"-----BEGIN (?:RSA |EC |)(?:ENCRYPTED )?PRIVATE KEY-----")


def _looks_pkcs12(filename: str) -> bool:
    return (filename or "").lower().endswith((".pfx", ".p12"))


def _load_certs(data: bytes) -> list:
    """Zertifikatskette (Leaf zuerst) aus PEM-Bundle oder DER-Einzelzertifikat."""
    if _PEM_CERT in data:
        return x509.load_pem_x509_certificates(data)   # Reihenfolge wie in der Datei
    return [x509.load_der_x509_certificate(data)]       # DER (.cer) = ein Zertifikat


def _load_key(data: bytes, passphrase):
    """Privaten Schluessel aus PEM (ggf. verschluesselt) oder DER-PKCS#8 laden."""
    if b"-----BEGIN" in data:
        return load_pem_private_key(data, password=passphrase)
    return load_der_private_key(data, password=passphrase)


def _keys_match(leaf, key) -> bool:
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    pem = serialization.Encoding.PEM
    return (leaf.public_key().public_bytes(pem, spki)
            == key.public_key().public_bytes(pem, spki))


def _combine(chain: list, key) -> bytes:
    chain_pem = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain)
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return chain_pem + key_pem


def _from_pkcs12(data: bytes, passphrase):
    """Cert, Kette und Schluessel aus einer PKCS#12-Datei (.pfx/.p12)."""
    for pw in (passphrase, b""):   # manche Tools exportieren mit leerem Passwort
        try:
            key, leaf, addl = pkcs12.load_key_and_certificates(data, pw)
            break
        except (TypeError, ValueError):
            key = leaf = None
            continue
        except Exception:  # noqa: BLE001 - defekte/kein PKCS#12
            return False, "flash.cert_invalid"
    else:
        return False, "flash.cert_key_passphrase"
    if leaf is None or key is None:
        return False, "flash.cert_invalid"
    chain = [leaf] + list(addl or [])
    if not _keys_match(leaf, key):
        return False, "flash.cert_key_mismatch"
    return True, _combine(chain, key)


def build_combined_pem(cert_bytes: bytes, key_bytes: bytes = b"",
                       passphrase: bytes | None = None, filename: str = "") -> tuple:
    """Hochgeladenes Material zu einem kombinierten PEM normalisieren.
    Liefert (True, combined_pem_bytes) oder (False, flash-key)."""
    cert_bytes = cert_bytes or b""
    key_bytes = key_bytes or b""

    # --- PKCS#12 (.pfx/.p12): alles in einer Datei ---
    if _looks_pkcs12(filename):
        if not cert_bytes:
            return False, "flash.cert_need_both"
        return _from_pkcs12(cert_bytes, passphrase)

    # --- Getrennte Dateien: Zertifikat (PEM/DER) + Schluessel (PEM/DER) ---
    # Kombiniertes PEM erlauben: liegt der Schluessel im Zertifikatsupload, von dort nehmen.
    if not key_bytes and _PEM_KEY.search(cert_bytes):
        key_bytes = cert_bytes
    if not cert_bytes or not key_bytes:
        return False, "flash.cert_need_both"
    try:
        chain = _load_certs(cert_bytes)
    except Exception:  # noqa: BLE001
        return False, "flash.cert_invalid"
    if not chain:
        return False, "flash.cert_invalid"
    try:
        key = _load_key(key_bytes, passphrase)
    except (TypeError, ValueError):
        # TypeError: Passphrase fehlt/ueberfluessig; ValueError: falsch/defekt.
        return False, "flash.cert_key_passphrase"
    except Exception:  # noqa: BLE001 - unlesbares Schluesselformat
        return False, "flash.cert_invalid"
    if not _keys_match(chain[0], key):
        return False, "flash.cert_key_mismatch"
    return True, _combine(chain, key)
