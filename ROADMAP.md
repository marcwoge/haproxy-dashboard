# Roadmap

**Sprache / Language:** Deutsch (dieses Dokument) · [English](ROADMAP.en.md)

## Erledigt
- [x] HAProxy Reverse Proxy mit TLS auf konfigurierbarer Domain
- [x] Kachel-Dashboard mit konfigurierbarem Design + Logo
- [x] Pfad-basiertes Routing (Container-Name **oder** Host:Port), SSL-Prüfung abschaltbar
- [x] Admin-GUI: Services & Design verwalten, Auto-Reload mit Validierung
- [x] Status-/Fehleranzeige, Live-Backend-Health, Log-Anzeige im Admin

## Update-Kanal (dieses Feature)
- [x] Versionsstempel im Image (`APP_VERSION`) + `VERSION` als Quelle der Wahrheit
- [x] GitHub Action baut private Images nach GHCR **und signiert sie** (cosign keyless)
- [x] Update-Prüfung (read-only) gegen die GitHub-Release-API
- [x] Meldung „Update verfügbar" im Admin-Bereich
- [x] **Sicheres Ausführungsmodell** (wie Trading-Projekt):
  - App hat **keinen** Docker-Socket; sie schreibt nur eine Anforderung
  - **host-seitiger Updater** (systemd path+service) außerhalb der Container-Grenze:
    Signatur prüfen → pull → nur geänderte Container → Healthcheck → **Rollback**
  - **RBAC** (Plattform-Admin), **Zwei-Schritt-Bestätigung**, **append-only Audit-Log**
  - `manual`-Modus (nur Befehl anzeigen) als kompromissloser Fallback
- [x] Sicherer Zugang: read-only, single-repo, ideal via dediziertem Maschinen-Konto

## Ideen / offen
- [x] Automatischer Update-Zeitplan (systemd-Timer) – opt-in via `AUTO_UPDATE=true`,
      mit Signaturprüfung/Healthcheck/Rollback und Rollback-Schleifen-Schutz (auto-skip)
- [x] Backend-Status auch auf dem Dashboard (Status-Punkt je Kachel, live + Toggle) 
- [x] Rollback auf vorherige Version per Klick (Admin-Knopf, host-agent, gleicher sicherer Pfad)
- [x] Health-/Update-Benachrichtigung per E-Mail/Webhook (SMTP-Relay ODER
      Direktversand via MX, + Webhook; Ereignisse einzeln schaltbar, im Admin)
- [ ] **Platform-Connector** – Sidecar zum Self-Service-Einbinden fremder
      Compose-Projekte (Cloudflare-Tunnel-Gefühl, aber ohne Tunnel, da gleicher Host):
  - [ ] **Auto-Registrierung**: meldet den Service beim Start per API bei der
        config-app an (Name/Pfad/Backend/Icon aus env) und **beim Stop wieder ab**
        → Kachel erscheint/verschwindet mit dem Projekt.
  - [ ] **Optionaler Gateway-/Proxy-Modus**: sitzt auf Plattform- und Projekt-Netz
        und proxyt TCP zum App-Container, sodass die App dem geteilten Netz **nicht**
        beitreten muss (mehr Isolation; Backend = `connector:port`).
  - [ ] **Veröffentlichung** als Image auf GHCR/Docker Hub
        (`image: …/connector:latest`), Konfiguration rein über env – Drop-in.
  - [ ] Alternativen zuerst prüfen: bei wenigen Services genügt das dokumentierte
        „externes Netz + GUI"-Verfahren; bei **anderem Host/NAT** kein Eigenbau,
        sondern frp/inlets/WireGuard/cloudflared.

  **Sicherheit des Connectors** (zwingend – Auto-Registrierung ist eine Angriffsfläche):
  - [ ] **Dediziertes Maschinen-Token** (nicht das Admin-Passwort), scoped nur auf
        „Service registrieren/abmelden", pro Connector widerrufbar.
  - [ ] **Strikte Validierung** der Registrierungsdaten mit derselben Logik wie im GUI
        (K2: Pfad/Backend gegen Config-Injection); Ziel gegen eine Whitelist prüfen
        (kein Biegen auf beliebige interne Hosts / SSRF).
  - [ ] **Rate-Limit** + **Audit-Log-Eintrag** je Registrierung/Abmeldung (Actor = Token-ID).
  - [ ] **Kein Docker-Socket** – der Sidecar deklariert sich selbst per HTTPS
        (konsistent zum „kein Socket in netz-/app-Container"-Prinzip).
  - [ ] Registrierungs-API **getrennt von der Menschen-Admin-Auth**: eigener Endpunkt
        `POST /api/register` / `/deregister`, nur Token (kein CSRF/Session).
  - [ ] **TLS-Verifikation** Connector→config-app (bei self-signed: CA-Bundle/Pinning).

## Sicherheit & Härtung (aus Security-Audit)

**Kritisch**
- [x] CSRF-Schutz für alle Admin-POST-Routen (Session-Token, `before_request`) — K1
- [x] Strikte Eingabevalidierung von `path`/`backend` gegen HAProxy-Config-Injection
      (Newline/Sonderzeichen ablehnen) + Renderer-Defense — K2

**Hoch**
- [x] Kein funktionierendes Default-Passwort; leeres Passwort SPERRT das Admin-GUI,
      Warnung bei schwachem Passwort — H1
- [x] Rate-Limiting / Brute-Force-Schutz am Admin-Login (IP-Sperre nach N Fehlversuchen,
      echte Client-IP via HAProxy `X-Client-IP`) — H2
- [x] `target`/Tag strikt validieren (GUI + host-updater + check-latest) — verhindert
      Injection in request.json/`.env` — H3

**Mittel**
- [x] Container härten: `cap_drop: [ALL]`, `no-new-privileges`, config-app `read_only`
      +tmpfs; haproxy nur `NET_BIND_SERVICE` — M1
      (non-root/UID-Drop optional, wegen Bind-Mount-Ownership noch offen)
- [x] Constant-time Passwortvergleich (`hmac.compare_digest`) — M2
- [x] `SECRET_KEY` erzwingen statt hartkodiertem Default (persistent generiert) — M3
- [x] Secrets via Docker/Compose *secrets* statt `environment:` (opt-in, `*_FILE`) — M4
- [x] `waitress >= 3.0.1` (CVE-Fixes) — auf 3.0.2 angehoben — M5
- [x] Backend-SSL-Default `verify required` (GUI-Checkbox default an, Warnung bei „none") — M6
- [x] Audit-Log-Integrität: App-Audit (`app-audit.log`) vom Host-Audit (`host-audit.log`,
      append-only via `chattr +a`, vom cap_drop-Container nicht umschreibbar) getrennt — M7

**Niedrig**
- [x] SVG-Logo-Upload absichern: aktive Inhalte abgelehnt + `/logo` mit CSP `sandbox`
      und `X-Content-Type-Options: nosniff` — N1
- [x] TLS-Härtung: `ssl-min-ver TLSv1.2`, moderne Ciphers/Ciphersuites, HSTS (opt-in) — N2
- [x] Interne Stats-Schnittstelle (`:8404`) mit Basic-Auth (Shared Secret) abgesichert — N3
- [x] Generische Fehlermeldungen im GUI (interne Details nur ins Log) — N4
- [x] Selbstsigniertes Zertifikat persistieren (stabiler Fingerprint über Neustarts) — N5
