# Roadmap

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
- [ ] Backend-Status auch auf dem Dashboard (nicht nur Admin)
- [ ] Rollback auf vorherige Version per Klick
- [ ] Health-/Update-Benachrichtigung per E-Mail/Webhook

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
- [ ] Backend-SSL-Default auf `verify required` (statt `verify none`) — M6
- [ ] Audit-Log-Integrität: App- vom Host-Audit trennen, append-only erzwingen — M7

**Niedrig**
- [ ] SVG-Logo-Upload absichern (sanitizen / `Content-Disposition` / CSP) — N1
- [ ] TLS-Härtung: `ssl-min-ver TLSv1.2`, moderne Ciphers, HSTS-Header — N2
- [ ] Interne Stats-Schnittstelle (`:8404`) mit Auth / lokal binden — N3
- [ ] Generische Fehlermeldungen im GUI (Details nur ins Log) — N4
- [ ] Selbstsigniertes Zertifikat persistieren (nicht bei jedem Neustart neu) — N5
