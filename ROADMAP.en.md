# Roadmap

**Language / Sprache:** English (this document) · [Deutsch](ROADMAP.md)

## Done
- [x] HAProxy reverse proxy with TLS on a configurable domain
- [x] Tile dashboard with configurable design + logo
- [x] Path-based routing (container name **or** host:port), SSL check toggleable
- [x] Admin GUI: manage services & design, auto-reload with validation
- [x] Status/error display, live backend health, log view in admin

## Update channel (this feature)
- [x] Version stamp in the image (`APP_VERSION`) + `VERSION` as the source of truth
- [x] GitHub Action builds private images to GHCR **and signs them** (cosign keyless)
- [x] Update check (read-only) against the GitHub release API
- [x] "Update available" message in the admin area
- [x] **Secure execution model**:
  - the app has **no** Docker socket; it only writes a request
  - a **host-side updater** (systemd path+service) outside the container boundary:
    verify signature → pull → only changed containers → healthcheck → **rollback**
  - **RBAC** (platform admin), **two-step confirmation**, **append-only audit log**
  - `manual` mode (only show the command) as an uncompromising fallback
- [x] Secure access: read-only, single-repo, ideally via a dedicated machine account

## Ideas / open
- [x] Automatic update schedule (systemd timer) – opt-in via `AUTO_UPDATE=true`,
      with signature check/healthcheck/rollback and rollback-loop protection (auto-skip)
- [x] Backend status on the dashboard too (status dot per tile, live + toggle)
- [x] One-click rollback to the previous version (admin button, host-agent, same secure path)
- [x] Health/update notification via email/webhook (SMTP relay OR direct MX
      delivery, + webhook; events individually toggleable, in admin)
- [x] **Platform connector** (v1.1.0) – a sidecar for self-service onboarding of
      external compose projects (without a tunnel, since same host):
  - [x] **Auto-registration** (`/api/v1/register|deregister`, `source=connector`):
        the tile appears/disappears with the project; heartbeat + reaping of orphaned
        entries (`CONNECTOR_TTL`).
  - [x] **Gateway/proxy mode**: TCP ambassador, the app stays isolated on its own
        network (backend = `connector:port`); alternative `register` mode.
  - [x] **Publish** as a signed GHCR image (`connector`, in the release workflow).
  - [x] "via connector" badge on the dashboard + admin.

  **Connector security** (auto-registration is an attack surface):
  - [x] **Dedicated machine token** (`CONNECTOR_TOKEN`, separate from the admin
        password, opt-in; empty = API disabled).
  - [x] **Strict validation** of the registration data (K2: path/backend against
        config injection), `/` reserved, backend required.
  - [x] **Rate limiting** + **audit-log entry** per registration/deregistration/reaping.
  - [x] **No Docker socket** – the sidecar declares itself via HTTPS.
  - [x] Registration API **separate from the human admin auth** (`/api/v1/*`,
        token only, CSRF-exempt).
  - [x] **TLS verification** connector→config-app (`CA_BUNDLE`/`INSECURE`).
  - [ ] open: backend **whitelist** against SSRF (the target is K2-validated but not
        restricted to allowed hosts – the token is treated as trusted infrastructure).

## Security & hardening (from the security audit)

**Critical**
- [x] CSRF protection for all admin POST routes (session token, `before_request`) — K1
- [x] Strict input validation of `path`/`backend` against HAProxy config injection
      (reject newlines/special characters) + renderer defense — K2

**High**
- [x] No working default password; an empty password LOCKS the admin GUI,
      warning on a weak password — H1
- [x] Rate limiting / brute-force protection on the admin login (IP lock after N
      failed attempts, real client IP via HAProxy `X-Client-IP`) — H2
- [x] Strictly validate `target`/tag (GUI + host-updater + check-latest) — prevents
      injection into request.json/`.env` — H3

**Medium**
- [x] Harden containers: `cap_drop: [ALL]`, `no-new-privileges`, config-app `read_only`
      +tmpfs; haproxy only `NET_BIND_SERVICE` — M1
      (non-root/UID drop optional, still open due to bind-mount ownership)
- [x] Constant-time password comparison (`hmac.compare_digest`) — M2
- [x] Enforce `SECRET_KEY` instead of a hardcoded default (persistently generated) — M3
- [x] Secrets via Docker/Compose *secrets* instead of `environment:` (opt-in, `*_FILE`) — M4
- [x] `waitress >= 3.0.1` (CVE fixes) — raised to 3.0.2 — M5
- [x] Backend SSL default `verify required` (GUI checkbox on by default, warning on "none") — M6
- [x] Audit-log integrity: app audit (`app-audit.log`) separated from the host audit
      (`host-audit.log`, append-only via `chattr +a`, not rewritable by the cap_drop container) — M7

**Low**
- [x] Secure SVG logo upload: active content rejected + `/logo` with CSP `sandbox`
      and `X-Content-Type-Options: nosniff` — N1
- [x] TLS hardening: `ssl-min-ver TLSv1.2`, modern ciphers/ciphersuites, HSTS (opt-in) — N2
- [x] Internal stats endpoint (`:8404`) secured with Basic auth (shared secret) — N3
- [x] Generic error messages in the GUI (internal details only to the log) — N4
- [x] Persist the self-signed certificate (stable fingerprint across restarts) — N5
