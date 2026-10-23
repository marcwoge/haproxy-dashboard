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
  auf `0600` gesetzt. Beide Container laufen als `root`, der haproxy-Container
  kann die Config also weiterhin lesen.
- **Health-Check-Header-Secret-Dateien müssen `root` (uid 0) gehören.** Die
  Container laufen als root, aber mit `cap_drop: ALL` – ohne `CAP_DAC_OVERRIDE`
  kann der Container-root eine `0600`-Datei nur lesen, wenn sie ihm gehört. Legt
  man sie als normaler Benutzer an, kann config-app sie nicht lesen und lässt den
  Header **weg** (mit einer Warnung im App-Log); der betroffene Health-Check
  scheitert dann (z. B. 401 → der Dienst liefert 503). Also auf dem Host als root
  anlegen, z. B. `sudo install -o root -g root -m600 token data/config/secrets/`.
- Der Header-**Wert** wird nie im Admin-GUI, in `status.txt` oder in den Logs
  angezeigt – nur der Dateiname.
- **`data/haproxy/haproxy.cfg` gehört nicht unverschlüsselt in Backups oder in
  Support-Anfragen.** Wer Zugriff auf den Datenordner auf dem Host hat, kann die
  dort enthaltenen Geheimnisse lesen.
