# Backend certificate agent (internal CA)

A tiny sidecar that gives one backend a TLS certificate from the platform's
internal CA ([step-ca](../ca/README.md)) and keeps it renewed — so HAProxy can
reach the backend over verified TLS (`backend_ca: internal`). It wraps
[acme.sh](https://github.com/acmesh-official/acme.sh): obtain via ACME, install
into a shared volume, auto-renew.

This is the moving part that makes the internal CA usable; it is **opt-in** and
lives next to your backend, not on the platform host.

## How it works

```
step-ca (platform)  ◄──ACME HTTP-01──  cert-agent ──writes──►  cert-data volume
                                         (shares myapp's                │
                                          network namespace)            ▼
HAProxy ──https, verify required (internal root), SNI=myapp──►  myapp serves TLS from /certs
```

The agent runs with `network_mode: "service:<backend>"`, so during the HTTP-01
challenge it answers on `:80` **under the backend's own name** — which is also
the certificate SAN and the SNI HAProxy uses. The issued cert + key land in a
volume the backend reads.

## Compose example (in your backend's project)

```yaml
services:
  myapp:
    image: your/app                 # serves HTTPS on :8443 reading /certs/{fullchain,key}.pem
    networks: [platform]            # shared platform network (external)
    volumes:
      - cert-data:/certs:ro

  cert-agent:
    image: ghcr.io/your-org/haproxy-dashboard/cert-agent:latest
    network_mode: "service:myapp"   # share myapp's network identity for HTTP-01
    environment:
      ACME_DIRECTORY: https://ca.example.local/acme/acme/directory
      CERT_DOMAIN:    myapp         # = certificate SAN = the SNI HAProxy uses
      ACME_EMAIL:     ops@example.com
      # RELOAD_CMD:   "touch /certs/.reload"   # optional, see below
    volumes:
      - cert-data:/certs            # shares the issued cert with myapp
      - ./internal-root.crt:/ca/root_ca.crt:ro   # one-time trust bootstrap (see below)
    restart: unless-stopped

volumes:
  cert-data:
networks:
  platform: { external: true, name: acme-platform }
```

On the platform, add the tile for `myapp` with **scheme https**, **verify SSL
certificate**, and **backend CA = internal** (admin GUI). HAProxy then reaches
`myapp:8443` and verifies its cert against the internal root.

## Environment

| Variable | Meaning |
|---|---|
| `ACME_DIRECTORY` | step-ca ACME directory URL (reachable from the backend host) |
| `CERT_DOMAIN` | the backend name = certificate SAN = HAProxy SNI |
| `ACME_EMAIL` | ACME account email (default `admin@localhost`) |
| `CA_BUNDLE` | path to the internal root to trust the ACME server (default `/ca/root_ca.crt`) |
| `OUT_DIR` | where to write `fullchain.pem` + `key.pem` (default `/certs`) |
| `CHALLENGE` | `standalone` (default, uses `:80`) or `webroot` |
| `WEBROOT` | webroot path when `CHALLENGE=webroot` (default `/var/www`) |
| `RELOAD_CMD` | optional command run after issue/renew (see below) |

## Trust bootstrap (one-time)

The agent must trust step-ca's own TLS to talk ACME to it. Copy the internal
root to the backend host once and mount it as `CA_BUNDLE`:

```bash
docker compose cp step-ca:/home/step/certs/root_ca.crt ./internal-root.crt   # on the platform host
# transfer internal-root.crt to the backend host, next to its compose file
```

## Renewal & reload

acme.sh installs a daily cron on first issue; the agent runs it in the
foreground (`crond`). step-ca leaf certs are short-lived (≈24 h), so renewal is
frequent and silent. On each renewal the cert is re-written to `OUT_DIR` and, if
set, `RELOAD_CMD` runs.

Because the agent shares the backend's **network** namespace but not its **PID**
namespace, it cannot signal the backend process directly. Pick one:

- Have the backend **watch** `/certs/fullchain.pem` (mtime) and reload itself.
- Set `RELOAD_CMD` to touch a file the backend watches, or to call a reload
  endpoint (e.g. `wget -qO- http://localhost:8443/-/reload`).
