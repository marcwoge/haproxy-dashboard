# Security Policy

## Unterstützte Versionen

Sicherheitskorrekturen gibt es nur für das jeweils neueste Release (`vX.Y.Z`,
siehe [Releases](https://github.com/marcwoge/haproxy-dashboard/releases)) bzw.
den aktuellen Stand von `main`. Bitte vor einer Meldung auf die neueste Version
aktualisieren.

## Schwachstellen melden

Bitte **keine** öffentlichen Issues für Sicherheitslücken anlegen. Stattdessen
privat über GitHub melden:
[Security Advisories → Report a vulnerability](https://github.com/marcwoge/haproxy-dashboard/security/advisories/new)

Hilfreich sind: betroffene Komponente (config-app, haproxy, connector, updater),
Version, Reproduktionsschritte und mögliche Auswirkungen.

Wir bestätigen den Eingang in der Regel innerhalb von 5 Werktagen und geben
innerhalb von 14 Tagen eine erste Einschätzung. Bitte bis zur Veröffentlichung
eines Fixes keine Details öffentlich machen.

## Geltungsbereich

- Admin-/Dashboard-Weboberfläche (config-app), Login, RBAC, Connector-API
- generierte HAProxy-Konfiguration und Zertifikatsbehandlung
- Host-seitiger Updater (`updater/`) und die veröffentlichten GHCR-Images

## Geheimnisse in der generierten Konfiguration

Die generierte `data/haproxy/haproxy.cfg` enthält **Klartext-Geheimnisse** –
den Wert eines optionalen Health-Check-Headers (`header_file`, z. B. ein
`Authorization`-Token) sowie die internen Stats-Zugangsdaten (`stats auth`).

- config-app schreibt die Datei mit `0600`; der Secret-Ordner
  `data/config/secrets/` wird (sofern vorhanden) auf `0700`, die Dateien darin
  auf `0600` gesetzt. Beide Container laufen als dieselbe Non-Root-UID (`1000`),
  der haproxy-Container kann die Config also weiterhin lesen.
- **Health-Check-Header-Secret-Dateien müssen der App-UID `1000` gehören.** Die
  Container laufen als Non-Root (uid 1000) mit `cap_drop: ALL` – ohne
  `CAP_DAC_OVERRIDE` kann der Container eine `0600`-Datei nur lesen, wenn sie ihm
  gehört. Legt man sie einem anderen Benutzer an, kann config-app sie nicht lesen
  und lässt den Header **weg** (mit einer Warnung im App-Log); der betroffene
  Health-Check scheitert dann (z. B. 401 → der Dienst liefert 503). Also auf dem
  Host z. B. `sudo install -o 1000 -g 1000 -m600 token data/config/secrets/`.
- Der Header-**Wert** wird nie im Admin-GUI, in `status.txt` oder in den Logs
  angezeigt – nur der Dateiname.
- **`data/haproxy/haproxy.cfg` gehört nicht unverschlüsselt in Backups oder in
  Support-Anfragen.** Wer Zugriff auf den Datenordner auf dem Host hat, kann die
  dort enthaltenen Geheimnisse lesen.

## Interne CA (opt-in, Profil `ca`)

Wird die interne Zertifizierungsstelle (step-ca) genutzt, ist sie ein
**Vertrauensanker**: Zertifikate aus dieser CA werden von HAProxy für
Backend-Verbindungen akzeptiert.

- Die CA-**Private-Keys** liegen verschlüsselt unter `data/step-ca/secrets/`
  (nur auf dem Host, `data/` ist git-ignored) und werden **nicht** in den
  haproxy-Container gemountet – HAProxy bekommt nur die öffentlichen Certs.
- `STEPCA_PASSWORD` schützt diese Keys; geheim halten, nicht in Backups/Support.
- Leaf-Zertifikate sind **kurzlebig** (≈24 h) → keine CRL/OCSP nötig.
- Ein Backend darf nur ein Zertifikat für **seinen eigenen** Namen erhalten
  (ACME-Validierung gegen den Backend-Namen); so kann ein kompromittiertes
  Backend kein anderes imitieren. Siehe [`ca/README.md`](ca/README.md) und
  [`cert-agent/README.md`](cert-agent/README.md).
