#!/bin/sh
# ============================================================
#  Backend certificate agent (internal CA, stage 3)
#  Obtains a short-lived certificate for THIS backend from the platform's
#  internal step-ca via ACME, installs it into a shared volume for the backend
#  to serve, and renews it automatically. Run it as a sidecar that shares the
#  backend's network namespace, so the ACME HTTP-01 challenge (:80) is answered
#  under the backend's own name. See README.md.
# ============================================================
set -eu

# --- configuration (from env) ----------------------------------------------
: "${ACME_DIRECTORY:?set ACME_DIRECTORY, e.g. https://ca.example.local/acme/acme/directory}"
: "${CERT_DOMAIN:?set CERT_DOMAIN (the backend name = certificate SAN, must match HAProxy SNI)}"
CA_BUNDLE="${CA_BUNDLE:-/ca/root_ca.crt}"   # internal CA root, to trust the ACME server
OUT_DIR="${OUT_DIR:-/certs}"                # shared with the backend container
ACME_EMAIL="${ACME_EMAIL:-admin@localhost}"
CHALLENGE="${CHALLENGE:-standalone}"        # standalone (:80 in this netns) | webroot
WEBROOT="${WEBROOT:-/var/www}"
# RELOAD_CMD (optional): how the backend picks up a renewed cert, e.g. touch a
# file it watches, or curl its reload endpoint. The agent shares the netns, not
# the PID namespace, so it cannot signal the backend process directly.

bundle_opt=""
[ -f "$CA_BUNDLE" ] && bundle_opt="--ca-bundle $CA_BUNDLE"
mkdir -p "$OUT_DIR"

acme.sh --register-account -m "$ACME_EMAIL" --server "$ACME_DIRECTORY" $bundle_opt || true

if ! acme.sh --list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$CERT_DOMAIN"; then
    if [ "$CHALLENGE" = "webroot" ]; then
        acme.sh --issue -w "$WEBROOT" -d "$CERT_DOMAIN" --server "$ACME_DIRECTORY" $bundle_opt --accountemail "$ACME_EMAIL"
    else
        acme.sh --issue --standalone -d "$CERT_DOMAIN" --server "$ACME_DIRECTORY" $bundle_opt --accountemail "$ACME_EMAIL"
    fi
fi

# Export the issued cert into the shared volume (re-applied automatically on renewal).
acme.sh --install-cert -d "$CERT_DOMAIN" --ecc \
    --key-file "$OUT_DIR/key.pem" \
    --fullchain-file "$OUT_DIR/fullchain.pem" \
    ${RELOAD_CMD:+--reloadcmd "$RELOAD_CMD"}

echo "cert-agent: certificate for '$CERT_DOMAIN' in $OUT_DIR; entering auto-renew loop."
# acme.sh installs its own daily cron on --issue; crond runs the renewal (which
# re-installs into OUT_DIR and runs RELOAD_CMD). Keep the container in the foreground.
exec crond -n -s
