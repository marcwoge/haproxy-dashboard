# Internal certificate authority (opt-in)

For multi-host setups where HAProxy talks to backends across networks you don't
fully trust, the platform can run its own small certificate authority
([step-ca](https://smallstep.com/docs/step-ca/)). Each backend obtains a
short-lived certificate from it, and HAProxy verifies the backend's certificate
against the internal root — so every **HAProxy → backend** connection is TLS with
a trust chain that means something (not `verify none`).

This is the reverse direction of Let's Encrypt: Let's Encrypt secures
*client → HAProxy* (the public front), the internal CA secures
*HAProxy → backend* (the private back).

> **Status: foundation (stage 1).** This sets up the CA and lets HAProxy trust it.
> Per-service enforcement in the admin GUI, the backend auto-renew agent (acme.sh)
> and cross-host reachability follow in later stages.

## Enable it

The CA is **opt-in** via a Compose profile — without it the platform runs exactly
as before and no CA is started.

1. Set a CA key password in `.env` (encrypts the CA private keys) **before** the
   first start — it initializes the CA:

   ```bash
   # .env
   STEPCA_PASSWORD=<a long random secret>
   CA_NAME=Acme Internal CA
   CA_DNS_NAMES=step-ca,localhost          # add a routable name for other hosts
   ```

2. Start the CA alongside the platform:

   ```bash
   docker compose --profile ca up -d
   ```

   On first start step-ca generates a **root + intermediate CA** and an **ACME
   provisioner**, and prints the root fingerprint:

   ```bash
   docker compose logs step-ca | grep -i fingerprint
   ```

## What lives where

- `data/step-ca/` — CA config, the public certs and the **encrypted** private keys.
  It is under `data/` (git-ignored); the private keys never leave the host and are
  **not** mounted into HAProxy.
- `data/step-ca/certs/root_ca.crt` — the internal root. It is mounted read-only
  into the HAProxy container at `/certs-ca/root_ca.crt`, so HAProxy can use it as
  the `ca-file` when verifying backend certificates.
- ACME directory URL (inside the platform network):
  `https://step-ca:9000/acme/acme/directory`.

## Trust chain (verified in stage 1)

A backend presenting a step-ca-issued certificate is reached by HAProxy with:

```
server <name>:<port> ssl verify required ca-file /certs-ca/root_ca.crt \
       sni str(<name>) check-sni <name> check ...
```

A certificate from any other CA is rejected (the backend goes DOWN), so the
verification is real, not cosmetic.

## Reaching the CA from other hosts

Backends on other hosts need to reach step-ca's ACME endpoint. step-ca speaks TLS
with its own certificate, so expose it with **TLS passthrough** — don't terminate
it at HAProxy. Use a TCP stream (admin GUI → "TCP streams", the v1.5 feature):

- **TCP stream** `ca`: port `9000`, target `step-ca:9000`, **TLS off** (passthrough,
  so step-ca keeps its own cert end to end).
- Publish the port in `docker-compose.override.yml` (the GUI shows the line):

  ```yaml
  services:
    haproxy:
      ports:
        - "9000:9000"
  ```

- Set `CA_DNS_NAMES` (in `.env`, before first CA init) to include the name the
  backends use to reach the platform, e.g. `step-ca,localhost,ca.example.local`,
  so step-ca's certificate is valid for that name.

Backends then point the agent at
`ACME_DIRECTORY=https://ca.example.local:9000/acme/acme/directory` and trust the
internal root via `CA_BUNDLE` (see [`../cert-agent/README.md`](../cert-agent/README.md)).

The ACME **HTTP-01 challenge** runs the other way: step-ca connects back to the
backend on `:80` under its name, so that name must resolve and be reachable from
the platform host (routable network / VPN). If inbound `:80` to backends is not
possible, switch the provisioner to DNS-01 or use the token-based (JWK) flow
instead — see the step-ca docs.

## Security notes

- Keep `STEPCA_PASSWORD` secret; it encrypts the CA keys.
- Leaf certificates are short-lived by design, which avoids CRL/OCSP complexity.
- A backend must only be able to obtain a certificate for **its own** name; the
  later issuance flow enforces that server-side (ties into the SSRF backend
  allowlist), so a compromised backend cannot impersonate another service.
