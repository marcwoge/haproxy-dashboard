"""Tests fuer die Konfigurationserzeugung (haproxy_render).

Ausfuehren (aus dem Repo-Root):
    python -m unittest discover -s config-app/tests -v

Optional zusaetzlich mit echter Syntaxpruefung durch `haproxy -c` in einem
Container: HAPROXY_IMAGE auf ein HAProxy-Image setzen (z. B. haproxy:3.4.7 oder
das lokal gebaute Plattform-Image). Ohne die Variable wird dieser Teil uebersprungen.
"""
import datetime
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import haproxy_render  # noqa: E402

SAMPLE_CONFIG = {
    "domain": "platform.example.local",
    "services": [
        {"name": "App", "path": "/app", "backend": "app:8080"},
        {"name": "Wiki", "host": "wiki.example.local", "backend": "10.0.0.5:80"},
    ],
    "tcp_streams": [
        {"name": "DB", "port": 5432, "backend": "db:5432"},
    ],
}


def _reload(**env):
    """haproxy_render mit gesetzten Umgebungsvariablen neu laden (die Optionen
    werden beim Import gelesen)."""
    with mock.patch.dict(os.environ, env):
        return importlib.reload(haproxy_render)


def _frontend_block(cfg: str, name: str) -> list:
    """Zeilen eines Frontends bis zur naechsten Leerzeile."""
    lines = cfg.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln == f"frontend {name}" or ln.startswith(f"frontend {name} "))
    block = []
    for ln in lines[start + 1:]:
        if not ln.strip():
            break
        block.append(ln.strip())
    return block


class PrometheusFrontendTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("PROMETHEUS_EXPORTER", None)
        importlib.reload(haproxy_render)

    def test_default_enabled(self):
        os.environ.pop("PROMETHEUS_EXPORTER", None)
        hr = importlib.reload(haproxy_render)
        cfg = hr.render(SAMPLE_CONFIG)
        block = _frontend_block(cfg, "prometheus")
        self.assertIn("bind :8405", block)
        self.assertIn("mode http", block)
        self.assertIn("no log", block)
        self.assertIn("http-request use-service prometheus-exporter if { path /metrics }", block)
        # Alles ausser /metrics wird abgewiesen (kein Routing in Backends).
        self.assertIn("http-request deny deny_status 404", block)
        self.assertFalse(any(ln.startswith(("default_backend", "use_backend")) for ln in block))
        # Eigene Defaults ohne Logging (keine 'log format ignored'-Warnung) und vor
        # den unbenannten HTTP-Defaults (sonst waeren diese nicht mehr implizit).
        self.assertIn("frontend prometheus from promex", cfg)
        lines = cfg.splitlines()
        self.assertLess(lines.index("defaults promex"), lines.index("defaults"))

    def test_enabled_in_tunnel_mode(self):
        hr = _reload(FRONTEND_MODE="http")
        try:
            self.assertIn("frontend prometheus", hr.render(SAMPLE_CONFIG))
        finally:
            os.environ.pop("FRONTEND_MODE", None)

    def test_can_be_disabled(self):
        for val in ("false", "0", "no", "off", "FALSE"):
            with self.subTest(val=val):
                hr = _reload(PROMETHEUS_EXPORTER=val)
                cfg = hr.render(SAMPLE_CONFIG)
                self.assertNotIn("frontend prometheus", cfg)
                self.assertNotIn("prometheus-exporter", cfg)
                self.assertNotIn(":8405", cfg)
                self.assertNotIn("defaults promex", cfg)

    def test_explicit_true(self):
        hr = _reload(PROMETHEUS_EXPORTER="true")
        self.assertIn("frontend prometheus", hr.render(SAMPLE_CONFIG))

    def test_port_reserved_for_tcp_streams(self):
        hr = importlib.reload(haproxy_render)
        self.assertEqual(hr._valid_port(8405), 0)
        self.assertEqual(hr._valid_port("8405"), 0)
        cfg = hr.render({"tcp_streams": [{"name": "x", "port": 8405, "backend": "db:1"}]})
        self.assertEqual(cfg.count("bind :8405"), 1)   # nur der Prometheus-Bind
        # Auch bei abgeschaltetem Exporter bleibt der Port reserviert (Compose-Mapping).
        hr = _reload(PROMETHEUS_EXPORTER="false")
        self.assertEqual(hr._valid_port(8405), 0)


def _make_pem(path: Path):
    """Selbstsigniertes Zertifikat + Schluessel als kombiniertes PEM (nur Test)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "platform.example.local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM)
                     + key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.TraditionalOpenSSL,
                                         serialization.NoEncryption()))


@unittest.skipUnless(os.environ.get("HAPROXY_IMAGE") and shutil.which("docker"),
                     "HAPROXY_IMAGE nicht gesetzt oder kein docker -> haproxy -c uebersprungen")
class HaproxyCheckTest(unittest.TestCase):
    """Gerenderte Konfiguration mit `haproxy -c` im Container validieren."""

    def _check(self, cfg_text: str):
        image = os.environ["HAPROXY_IMAGE"]
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            _make_pem(dp / "platform.pem")
            (dp / "haproxy.cfg").write_text(cfg_text, encoding="utf-8")
            # Lesbar fuer den Non-Root-User im Plattform-Image (uid 1000).
            for p in (dp, dp / "platform.pem", dp / "haproxy.cfg"):
                p.chmod(0o755 if p.is_dir() else 0o644)
            res = subprocess.run(
                ["docker", "run", "--rm", "--entrypoint", "haproxy",
                 "-v", f"{dp}:/chk:ro", image, "-c", "-f", "/chk/haproxy.cfg"],
                capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, msg=out)
        # Keine Warnungen/Alerts (NOTICEs zu nicht aufloesbaren Test-Hosts sind ok).
        self.assertNotIn("[WARNING]", out)
        self.assertNotIn("[ALERT]", out)
        return out

    def test_promex_compiled_in(self):
        res = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "haproxy",
             os.environ["HAPROXY_IMAGE"], "-vv"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("prometheus-exporter", res.stdout + res.stderr)

    def test_rendered_config_valid(self):
        hr = importlib.reload(haproxy_render)
        cfg = hr.render(SAMPLE_CONFIG, platform_crt="/chk/platform.pem")
        self.assertIn("frontend prometheus", cfg)
        self._check(cfg)

    def test_rendered_config_valid_without_exporter(self):
        hr = _reload(PROMETHEUS_EXPORTER="false")
        try:
            self._check(hr.render(SAMPLE_CONFIG, platform_crt="/chk/platform.pem"))
        finally:
            os.environ.pop("PROMETHEUS_EXPORTER", None)
            importlib.reload(haproxy_render)


if __name__ == "__main__":
    unittest.main()
