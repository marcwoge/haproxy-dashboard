# Platform Connector

A tiny sidecar you drop into another compose project. It **self-registers** the
project's service with the platform (dashboard tile + HAProxy route) on start and
**deregisters** it on stop — so the tile appears when your project comes up and
disappears when it goes down. No Docker socket, pure Python standard library.

> Enable the API on the platform first: set `CONNECTOR_TOKEN` in the platform's
> `.env` (see the main README). Empty token = the API is disabled.

## Modes
- **`register`**: your app joins the shared platform network and the route points
  directly at `SERVICE_TARGET`.
- **`proxy`**: the connector listens on the shared network and forwards TCP to
  `SERVICE_TARGET`, so your app can **stay on its own network**. The registered
  backend becomes the connector itself (`CONNECTOR_HOST:LISTEN_PORT`).

## Usage (register mode)

```yaml
# docker-compose.yml of your project
services:
  billing-app:
    image: yourorg/billing
    networks: [default, acme-platform]   # so HAProxy can reach it by name

  platform-connector:
    image: ghcr.io/your-org/haproxy-dashboard/connector:latest
    environment:
      PLATFORM_API:   https://platform.example.local
      PLATFORM_TOKEN: ${CONNECTOR_TOKEN}    # must match the platform's CONNECTOR_TOKEN
      MODE:           register
      SERVICE_NAME:   "Billing"
      SERVICE_PATH:   /billing
      SERVICE_TARGET: billing-app:8080      # <app-container>:<port>
      SERVICE_ICON:   "💳"
      INSECURE:       "true"                # only if the platform uses a self-signed cert
    networks: [acme-platform]
    restart: unless-stopped

networks:
  acme-platform: { external: true }
```

## Usage (proxy mode)

Your app stays on its own network; only the connector joins the shared network and
forwards traffic to it. **Your app service needs no changes.**

```yaml
services:
  billing-app:
    image: yourorg/billing                 # no network changes needed

  platform-connector:
    image: ghcr.io/your-org/haproxy-dashboard/connector:latest
    container_name: billing-connector      # HAProxy reaches the connector by this name
    environment:
      PLATFORM_API:   https://platform.example.local
      PLATFORM_TOKEN: ${CONNECTOR_TOKEN}
      MODE:           proxy
      SERVICE_NAME:   "Billing"
      SERVICE_PATH:   /billing
      SERVICE_TARGET: billing-app:8080      # where the connector forwards (project network)
      LISTEN_PORT:    "8080"                # port HAProxy connects to on the shared network
      CONNECTOR_HOST: billing-connector     # must match container_name (or a network alias)
      SERVICE_ICON:   "💳"
      INSECURE:       "true"                # only if the platform uses a self-signed cert
    networks: [default, acme-platform]      # default reaches the app, acme-platform the platform
    restart: unless-stopped

networks:
  acme-platform: { external: true }
```

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `PLATFORM_API` | ✅ | – | Base URL of the platform (config-app), e.g. `https://platform.example.local` |
| `PLATFORM_TOKEN` | ✅ | – | Must match the platform's `CONNECTOR_TOKEN` |
| `SERVICE_NAME` | ✅ | – | Tile label |
| `SERVICE_PATH` | ✅ | – | Route path, e.g. `/billing` |
| `SERVICE_TARGET` | ✅ | – | register: backend `host:port` HAProxy connects to · proxy: where the connector forwards |
| `SERVICE_SCHEME` | | `http` | `http` or `https` (platform → backend) |
| `SERVICE_SSL_VERIFY` | | `false` | Verify the backend certificate (only for `https`) |
| `SERVICE_STRIP_PATH` | | `false` | Strip the path prefix before forwarding |
| `SERVICE_ICON` | | – | Emoji/character for the tile |
| `SERVICE_COLOR` | | – | Tile color (hex or CSS name) |
| `SERVICE_DESCRIPTION` | | – | Tile description |
| `SERVICE_ENABLED` | | `true` | Register the service as enabled |
| `MODE` | | `register` | `register` or `proxy` |
| `LISTEN_PORT` | proxy | `8080` | Port the connector listens on (shared network) |
| `CONNECTOR_HOST` | | hostname | Name HAProxy uses to reach the connector in proxy mode; match `container_name` |
| `CONNECTOR_ID` | | `SERVICE_NAME` | Identifier used as the audit actor |
| `HEARTBEAT` | | `60` | Re-register interval in seconds (`0` = register once) |
| `RETRY_INTERVAL` | | `5` | Backoff while the platform is unreachable |
| `INSECURE` | | `false` | Skip TLS verification (self-signed platform cert) |
| `CA_BUNDLE` | | – | Path to a CA bundle for TLS verification |

The connector needs network access to the platform and to be reachable by HAProxy
by name — i.e. it (and, in register mode, your app) must join the external
`acme-platform` network.
