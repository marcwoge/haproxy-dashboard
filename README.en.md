# Acme Platform – HAProxy Reverse Proxy + Dashboard

**Language / Sprache:** English (this document) · [Deutsch](README.md)

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Release](https://img.shields.io/badge/release-v1.11.0-brightgreen.svg)
![HAProxy](https://img.shields.io/badge/proxy-HAProxy-cf3d1e.svg)
![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ed.svg)

> A lean **HAProxy reverse proxy** with a tile dashboard and admin GUI –
> routing, TLS, live backend status and secure updates, all configurable
> from the interface.

![Dashboard](docs/screenshot-dashboard.png)

Reverse proxy based on **HAProxy** with:

- TLS termination on a **freely configurable domain** (`PLATFORM_DOMAIN`)
- **Tile dashboard** with configurable appearance
- **Path-based routing**: `https://platform.example.local/abfallmanager` → container “Example Service”
- **Admin GUI** to create/edit the tiles, backends and design
- per service: **container name** (shared Docker network) **or** host:port / ip:port
- per service: http/https to the backend, **SSL check on/off** (self-signed backends ok)
- automatic **reload** on config change (validated beforehand)
- **multilingual interface** via language packs (German & English, extensible)

<details>
<summary>🖥️ View the admin GUI</summary>

![Admin GUI](docs/screenshot-admin.png)

</details>

---

## 1. Quick start

```powershell
# 1) (optional) adjust domain / admin password in .env
notepad .env

# 2) place the SSL certificate (see certs\README.txt)
#    -> certs\platform.crt  and  certs\platform.key
#    (without a certificate, a self-signed one is generated automatically)

# 3) start
docker compose up -d --build
```

Then in the browser:

- Dashboard: `https://platform.example.local/`
- Admin GUI: `https://platform.example.local/admin`

> **Name resolution note:** for `platform.example.local` to resolve on the server/client,
> set a DNS record or extend the `hosts` file, e.g.:
> `127.0.0.1   platform.example.local`
> (Windows: `C:\Windows\System32\drivers\etc\hosts`)

> **Data directory ownership:** the containers run as a fixed **non-root UID `1000`**
> (hardened with `cap_drop: ALL`). The bind-mounted `data/` / `certs/` folders must
> therefore be owned by that UID, otherwise the containers won't start (they print a
> clear error). Fix it once on the host:
> ```bash
> sudo chown -R 1000:1000 data certs
> ```
> The host-side updater and `updater/install-linux.sh` do this automatically; the
> one-liner is only needed for manual installs / first starts.
>
> **Upgrading from ≤ v1.11 to ≥ v1.12:** there the containers still ran as `root`, so
> `data/`/`certs/` are root-owned. Run the `chown` above once before first starting
> the new images (in auto-update mode the updater handles it).

---

## 2. Configuration

All central values live in **`.env`**:

| Variable          | Meaning                                              |
|-------------------|------------------------------------------------------|
| `PLATFORM_DOMAIN` | Domain HAProxy responds to                           |
| `ADMIN_USER`      | Username for the admin GUI                           |
| `ADMIN_PASSWORD`  | Password for the admin GUI (empty = admin GUI **locked**) |
| `TZ`              | Time zone                                            |

Services & design are easiest to manage in the **admin GUI**. Alternatively edit
`data\config\services.yaml` directly (translated into a `haproxy.cfg` on save/start).

### Domain aliases & multiple certificates (SNI)

In the admin GUI under **"Domains & certificates"**:

- **Domain aliases (301):** redirect old domains to a platform path with a 301 (path
  + query preserved), e.g. `wiki.example.local /wiki keep`. "keep" avoids a double
  prefix when the path already starts with the target. Optionally add `root=/target`
  (`host /wiki keep root=/wiki/home`) to send the alias domain's "/" to a dedicated
  target, independently of path preservation.
- **Additional certificates (SNI):** besides the primary certificate (in `certs/`),
  upload more (certificate + key, or a combined PEM). HAProxy selects by SNI. Subject,
  issuer, SANs and expiry are shown; **private keys are never shown or served**.
  Before every reload HAProxy validates the config (`haproxy -c`) – a broken
  certificate is rejected and the running config stays active.
- **Expiry warning:** the notification monitor alerts 30/14/7 days before expiry
  (event "Certificate expiring", via email/webhook).

**Internal CA:** place the certificate + intermediate chain as a PEM (primary in
`certs/` or uploaded additionally) and distribute the root CA to the clients (e.g. via
GPO).

**ACME / Let's Encrypt (DNS-01, optional):** for internal-only hosts, issue the
certificates **externally** with `acme.sh`/`lego` via your provider's DNS API and
upload/renew the resulting `fullchain.pem` (including the key) via cron. Provider
credentials belong on the host as a secret, not in `services.yaml`.

### Verifying backend certificates (custom/internal CA)

The counterpart to the front side: to have HAProxy verify an **https backend**
(`verify SSL`), pick the **backend CA** per service under **"Advanced"**:

- **Public CAs** (default) – the container's CA bundle.
- **Internal CA (step-ca)** – the platform's own CA (compose profile `ca`, see
  [`ca/README.md`](ca/README.md)).
- **Custom / uploaded CAs** – under **"Trusted backend CAs"** upload a
  **company/AD root CA** or a target's **self-signed certificate** (PEM or DER,
  **without** a private key). HAProxy then verifies backends against it. Also
  applies to the health checks of external link tiles.

This verifies, e.g., an internal target with a self-signed cert or a service whose
cert comes from the corporate PKI, instead of talking to it unverified with
`verify SSL` off. Expiry warning as with the SNI certificates.

### Behind a proxy/tunnel (real client IP)

When HAProxy sits behind a reverse proxy or tunnel (e.g. a **Cloudflare Tunnel**),
it only sees the proxy as the source — so the login lockout and the logs use that
single IP (one attacker locks everyone out). With `REAL_IP_HEADER` +
`TRUSTED_PROXIES` (in `.env`) HAProxy takes the real client IP from the header —
**only** when the connection comes from a trusted network (otherwise the header
would be spoofable):

```bash
REAL_IP_HEADER=CF-Connecting-IP
TRUSTED_PROXIES=173.245.48.0/20 103.21.244.0/22   # your proxy's/tunnel's networks
```

### Host-based routing & tunnel mode

In addition to path routing, a service can be routed by **hostname**: fill the
**"Host routing"** field in the service form with e.g. `app.example.com`. HAProxy
then routes by the Host header — **without a path prefix** (ideal for SPAs, Django,
WebSockets). Host and path can also be combined (this host **and** this path). This
makes the platform a central entry point for several (sub)domains, e.g. behind a
wildcard tunnel.

**Tunnel mode (`FRONTEND_MODE=http`):** when an upstream tunnel (e.g. Cloudflare)
already terminates TLS, HAProxy routes directly on **:80** — **no** HTTPS redirect
and no own `:443`. Then publish `:80` (instead of `:443`) to the tunnel and usually
set `REAL_IP_HEADER` (see above). The default stays `https` (HAProxy terminates TLS
on :443 itself).

---

## 3. Adding a container as a tile

In the admin GUI under **“Add service”**:

| Field         | Example                | Explanation                                           |
|---------------|------------------------|-------------------------------------------------------|
| Name          | `Example Service`        | tile label                                            |
| Path          | `/abfallmanager`       | URL path → `…/abfallmanager`                          |
| Backend       | `abfallmanager:8080`   | container-name:port **or** `192.168.1.50:8080`        |
| Scheme        | `http` / `https`       | how HAProxy talks to the backend                      |
| SSL check     | off                    | for https: do **not** verify the backend certificate  |
| Strip path    | usually off            | cut off `/abfallmanager` before forwarding            |

### Backend by container name (shared Docker network)

For HAProxy to reach a container by name, it must be on the `acme-platform` network.
In your app's `docker-compose.yml`:

```yaml
services:
  abfallmanager:
    image: your/abfallmanager
    networks: [acme-platform]

networks:
  acme-platform:
    external: true
```

Then in the backend field: `abfallmanager:8080`.

### Backend by host/IP

If the app runs elsewhere (another host, a fixed port on the Docker host, another VM),
simply enter `host:port` or `ip:port`, e.g. `192.168.1.50:8080`.

### Reaching a container from ANOTHER Docker project

Docker bridge networks are isolated from each other – a container from another
`docker compose` project is **not** automatically reachable by name. Two ways:

**A) Via the port published on the host** (simplest): if the other container publishes
a port (`ports: ["8080:80"]`), enter `host.docker.internal:8080` in the backend field.
Thanks to `extra_hosts: host-gateway` (already in `docker-compose.yml`) this works on
Linux too.

**B) Shared network** (no published port, routing by container name): make the other
container join this network –

```bash
docker network connect acme-platform <other-container>
```

or permanently add the external `acme-platform` network to its `docker-compose.yml`
(see above). Then enter `<container-name>:<port>` as backend.

### “Strip path” – when?

- **off** (default): the backend receives the full path (`/abfallmanager/...`).
  Correct when the app knows it runs under a sub-path (base path configured).
- **on**: HAProxy cuts off `/abfallmanager`, the backend sees `/...`.
  Correct for apps that insist on running at `/`.

### Advanced routing options (per service)

In the service form under **"Advanced (optional)"** (or directly in `services.yaml`):

- **Multiple servers & load balancing** – instead of a single backend, enter
  several addresses (one per line, `host:port`). From two servers on, traffic is
  balanced; the algorithm is selectable (`roundrobin` default, or `leastconn`).
  The service table shows a `k/n` badge for the healthy servers.
- **Path match** – `Exact` (default: `/path` and `/path/…`) or `Prefix`
  (`path_beg /path`, also matches e.g. `/pathsomething` – handy for apps like NiFi
  that respond on `/nifi` without a trailing `/`).
- **Additional paths** – extra paths (one per line) routed to the **same** backend,
  without ever getting their own tile.
- **Health-check header from a secret file** – if the HTTP health check should send
  a header (e.g. `Authorization`), only the **header name** is stored in the config;
  the **value** lives as a file under `data/config/secrets/<file>` and is read only
  when the config is generated – never stored in `services.yaml`. The file must be
  **owned by the app UID `1000`** (the containers run as non-root with `cap_drop:
  ALL`, so they cannot read files they don't own): `sudo install -o 1000 -g 1000
  -m600 token data/config/secrets/`. If it is unreadable, the header is dropped with
  a warning in the log.
- **Hide the tile** – the route stays active but no tile appears on the dashboard
  (`hidden`). For alias domains you can also set the target of "/" independently of
  path preservation (`root_to`, see domain aliases above).

**Logging (E6):** in addition to the in-container file log, HAProxy can write to a
**syslog** target (`SYSLOG_ADDRESS`, e.g. `10.0.0.5:514` or `/dev/log`). The file
log's retention is tunable via `LOG_ROTATE_AT`/`LOG_KEEP_LINES`. All three live in
`.env` – see [section 5](#5-status-health--logs-admin-gui).

### TCP streams (non-HTTP services)

For services that don't speak HTTP (database, SSH, SMTP, …) the admin GUI has a
**"TCP streams"** card. A stream listens on its **own port** and forwards plain TCP
(`mode tcp`) to one or more targets:

- **Dedicated listen port** (1–65535, not 80/443/8404) — unique per stream.
- **Multiple targets + load balancing** — one `host:port` or several (one per line);
  from two targets on, traffic is balanced (`leastconn` default, or `roundrobin`).
- **Optional TLS termination** with the **platform certificate** — HAProxy decrypts,
  the backend receives plain TCP.
- **Timeouts** (client/server, default `1h`) and **`clitcpka`** (TCP keepalive to the client).

> **Important:** each stream port must also be **published** on the `haproxy`
> container (`- "5432:5432"`), otherwise HAProxy only binds it inside the container.
> Put the lines in a **`docker-compose.override.yml`** (template:
> `docker-compose.override.yml.example`) and run `docker compose up -d`. Compose loads
> the override automatically; it is not in the repo and is left untouched by updates
> (the host-side updater does not modify it) — unlike entries in `docker-compose.yml`,
> which an update can overwrite. The admin GUI shows the exact lines to add.

### External link tiles

For services you only want to **link to** from the dashboard (SaaS, internet
portals, other sites), there are tiles without a proxy route. Fill the **"External
link"** field in the service form with an absolute URL:

- The tile opens the URL in a **new tab** (`target="_blank"`, ↗) and shows the host
  instead of a path.
- **No route:** no ACL, no `use_backend`; path/backend/servers/timeouts are ignored,
  a path is not needed.
- **Optional health check:** if enabled, HAProxy probes the target URL (its host
  header + SNI) and the tile shows UP/DOWN; a DOWN triggers the normal "backend down"
  notification. Without a health check the dot stays grey.

```yaml
services:
  - name: "Ticket portal"
    link_url: "https://portal.example.com/login"
    icon: "🎫"
    healthcheck: { expect_status: "200|302", interval: 30s }   # optional
```

### Onboard automatically: the Platform Connector (sidecar)

Instead of adding the service manually in the admin GUI, you can drop a small
**connector container** into your other project's compose. It registers the service
itself on start (tile + route) and deregisters on stop — so the tile appears and
disappears together with your project.

Prerequisite: set a **`CONNECTOR_TOKEN`** in the platform's `.env` (empty = the API
is disabled). The recommended **`proxy` mode** keeps your app on its own network, so
you change **nothing** about it:

```yaml
services:
  billing-app:
    image: yourproject/billing            # unchanged

  platform-connector:
    image: ghcr.io/your-org/haproxy-dashboard/connector:latest
    container_name: billing-connector
    environment:
      PLATFORM_API:   https://platform.example.local
      PLATFORM_TOKEN: ${CONNECTOR_TOKEN}
      MODE:           proxy
      SERVICE_NAME:   "Billing"
      SERVICE_PATH:   /billing
      SERVICE_TARGET: billing-app:8080     # your app on the project network
      CONNECTOR_HOST: billing-connector    # = container_name
      SERVICE_ICON:   "💳"
      INSECURE:       "true"               # only if the platform uses a self-signed cert
    networks: [default, acme-platform]
    restart: unless-stopped

networks:
  acme-platform: { external: true }
```

There is also a **`register` mode** (your app joins the shared network, no extra
hop). For all variables, both modes and details see
[`connector/README.md`](connector/README.md).

---

## 4. Appearance

In the admin GUI under **“Appearance & domain”**: title, subtitle, background
gradient, tile color/text, accent color and number of columns. Each tile can
additionally have its own color and an icon (emoji).

**Logo:** uploadable in the same form (png, jpg, svg, gif, webp, ico) including an
adjustable display height. It is shown above the title on the dashboard, stored
persistently under `data\config\` and can be replaced or removed at any time.

### Language (language packs)

The interface is multilingual. In the admin GUI under **“Appearance & domain”** you
can switch the **language** (shipped: German, English). The choice applies to both
dashboard and admin and is stored in `services.yaml` (key `language`).

**Adding another language:** drop a JSON file at `config-app/lang/<code>.json`
(easiest: copy `en.json` and translate). The `_name` key holds the display name; if a
translation is missing, it falls back to English and then to the key name. The new
language appears in the selector automatically, no code change needed.

---

## 5. Status, health & logs (admin GUI)

The admin GUI refreshes live (polling every ~3 s):

- The **validation banner** at the top shows whether the last generated config was
  accepted by HAProxy:
  - 🟢 *valid and active*
  - 🟠 *validation in progress …* (HAProxy is checking the new config right now)
  - 🔴 *rejected* – including the original error text from `haproxy -c`; the proxy
    keeps running with the **last valid** config.
- **Backend status per tile** (dot in the service table):
  🟢 UP · 🔴 DOWN · 🟠 e.g. name not resolvable · grey disabled/no backend.
  Source is an internal HAProxy stats endpoint (port **8404**, only within the Docker
  network, **not** published on the host).
- **Log view** at the bottom: HAProxy or config-app log, selectable line count,
  auto-refresh. Both logs are also written to files under `data\haproxy\` (with
  rotation) and remain visible via `docker compose logs`.

### Notifications (email / webhook)

Configurable under **Admin → Notifications**. Triggered on *update available*,
*update result* and *backend DOWN* (each toggleable). A background monitor detects the
events and reports once per event.

- **Email – SMTP host** (recommended): delivery via a relay server (STARTTLS/SSL,
  auth). Reliable.
- **Email – direct delivery**: the server does its own MX lookup and delivers over
  port 25 – convenient, but often blocked by providers (port 25 closed) or spam
  filters (missing PTR/SPF/DKIM).
- **Webhook**: POST of a JSON payload per event to a URL of your choice.

Use “Send test message” to verify the configuration immediately.

---

## 6. Architecture

```
                      :443 (TLS)            internal routes
   Browser  ─────────────────────►  HAProxy  ──────────────►  config-app  (/ , /admin, dashboard)
   platform.example.local                  │     ──────────────►  abfallmanager:8080   (/abfallmanager)
                                        │     ──────────────►  another-app:9000     (/...)
                                        │
   certs/*.crt + *.key  ──►  combined PEM (built at start)
```

- **haproxy** (`./haproxy`): TLS, routing, auto-reload watcher.
- **config-app** (`./config-app`, Flask): dashboard + admin GUI, writes
  `services.yaml` and generates `haproxy.cfg`.
- Shared volume `data\haproxy` holds the generated `haproxy.cfg`, the log files and
  `status.txt`; the watcher in the haproxy container validates the config, reloads on
  change (`SIGUSR2`, master-worker) and writes the validation result to `status.txt`,
  which the GUI reads. An **invalid** config is discarded – the running proxy stays up.
- **Internal CA (optional, `--profile ca`):** for multi-host setups the platform can
  run its own certificate authority (step-ca) that issues short-lived certificates to
  the backends — so the **HAProxy → backend** leg is TLS-encrypted and verified too.
  Details: [`ca/README.md`](ca/README.md).

---

## 7. Update channel & secure GitHub access

Security guidelines for update access:

- The server holds **read-only rights** to exactly this one repo/package – never write
  rights, never access to other repos.
- **No `/var/run/docker.sock` in any network-/app-facing container.** The privileged
  action (pulling images, replacing containers) runs **outside the container trust
  boundary** in a host-side updater (systemd).
- The app **only triggers** – it writes a request to a file and never runs Docker
  itself.

### Flow

```
 Admin GUI (config-app, no socket)                   Host (systemd, privileged)
 ─────────────────────────────────                   ────────────────────────────
 “Request update” + type target version   request.json   acme-updater.path detects file
 (RBAC: platform admin, 2-step)          ───────────────▶ acme-updater.service starts
                                                          → cosign verify (signature)
                                                          → docker compose pull
                                                          → only changed containers up -d
                                                          → healthcheck /healthz
                                                          → rollback to old digest on error
 shows result + audit log  ◀── result.json / audit.log ──┘
```

1. Push tag `vX.Y.Z` → the [`release.yml`](.github/workflows/release.yml) action builds
   the images, pushes them to **GHCR** and **signs** them keyless with cosign. Only the
   built-in `GITHUB_TOKEN` – **no** personal token needed.
2. Then publish a **GitHub release** for the tag (only then does the release API report
   the version).
3. The server checks the release API read-only → “update available” message in admin.
4. Applying depends on `UPDATE_MODE`:
   - **`manual`** (default): the GUI shows the command, you run it:
     `docker compose pull && docker compose up -d`
   - **`host-agent`**: GUI button (RBAC + type the target version) writes the request;
     the host-side updater does verify → pull → healthcheck → rollback.

**One-click rollback:** after a successful update the updater remembers the previous
version. In admin (in `host-agent` mode) a button “↩ Roll back to vX.Y.Z” then appears,
using exactly the same secure path as an update (signature check → pull of the old
version → healthcheck).

### Install the host-side updater (Linux/systemd)

```bash
sudo ./updater/install-linux.sh          # installs path+service, hardens audit.log
# install cosign (signature verification):  https://docs.sigstore.dev/cosign/installation/
# ONLY for PRIVATE GHCR packages, additionally log in once:
# echo <READ-ONLY-TOKEN> | docker login ghcr.io -u <GHCR_USER> --password-stdin
```

The service ([host-updater.sh](updater/host-updater.sh)) runs as root but is hardened
(`NoNewPrivileges`, `ProtectSystem`), and **verifies every signature**: even someone
who forges the request file can at most roll out a **correctly signed release** – not
arbitrary code.

### Auto-update schedule (optional)

The installer also sets up a **systemd timer**
([acme-update-check.timer](updater/systemd/acme-update-check.timer), default: daily
03:30 ±30 min). It only becomes active once you set **`AUTO_UPDATE=true`** in `.env`.

Flow: the timer starts [check-latest.sh](updater/check-latest.sh) – checks the release
API read-only, and **only if a newer release exists** does it write the request. From
there the exact same secure path runs (signature check → pull → healthcheck →
rollback). The schedule is therefore just an automatic “click” – the security
guarantees stay identical.

- **Rollback loop protection**: a version that was once discarded via rollback lands in
  `data/update/auto-skip` and is **not** requested again automatically (a manual update
  from the GUI bypasses the block).
- Change the schedule: `sudo systemctl edit acme-update-check.timer` (`OnCalendar=…`).
- Test immediately: `sudo systemctl start acme-update-check.service`.

```bash
# Enable in .env:
AUTO_UPDATE=true
AUTO_UPDATE_SCHEDULE=daily 03:30    # display text in the admin GUI only
```

### RBAC & confirmation

- `system.update` requires the **platform admin role** (`PLATFORM_ADMIN_USER` /
  `PLATFORM_ADMIN_PASSWORD` in `.env`). Empty = at least the normal admin.
- **Two-step confirmation**: the target version must be typed exactly.
- **Append-only audit log** (`data/update/audit.log`, hardened via `chattr +a`) with
  actor, time and every step including signature check, deploy, rollback.

### GitHub access: public (token-free) vs. private

**Public repo + public GHCR packages = no token needed.** Leave `UPDATE_TOKEN` and
`GHCR_USER` empty in `.env`:
- The release check runs **anonymously** (GitHub API limit 60 requests/h per IP – more
  than enough for the periodic check).
- `docker compose pull` pulls **public** images without `docker login`.
- The cosign signature check needs **no** token anyway (Sigstore/Rekor, public).

> **Important:** GHCR packages are initially **private** even with a public repo.
> Make them public once: GitHub → your profile/org → **Packages** → `config-app` or
> `haproxy` → *Package settings* → **Change visibility → Public**.
> (The packages only exist after the first release.)

**Private repo/packages:** then you need read-only access. Safest is a **dedicated
machine/bot account** as a read-only collaborator on just this repo – if the server is
compromised, your personal account stays untouched (2FA on both):

| Purpose | Variable | Permission |
|---------|----------|------------|
| Update **check** (release API) | `UPDATE_TOKEN` | Fine-grained, this repo only, *Contents:Read* + *Metadata:Read*, with expiry |
| Image **pull** (GHCR, `docker login`) | `GHCR_USER` + token | Only **`read:packages`** / package *Read* |

> **Never** a classic token with `repo` scope on the server – it can read *and write*
> all your repos. Always tightly scoped and read-only.

### Cutting a release (development machine)

```bash
# adjust VERSION, commit, then:
git tag v1.1.0 && git push origin v1.1.0
# -> the action builds, pushes & signs; then publish a release in the GitHub UI
```

---

## 8. Useful commands

```powershell
docker compose up -d --build            # start / rebuild (local)
docker compose logs -f haproxy          # HAProxy logs (incl. reload/watcher)
docker compose logs -f config-app       # GUI logs
docker compose restart haproxy          # after a certificate change
docker compose pull; docker compose up -d   # manual update (GHCR)
docker compose down                     # stop
```

View the generated config: `data\haproxy\haproxy.cfg`
