#!/usr/bin/env bash
# ============================================================
#  Host-seitiger Updater (laeuft als systemd-Service, NICHT im Container).
#
#  Sicherheitsmodell:
#   - Die App loest nur aus (schreibt request.json). Sie hat KEINEN Docker-Socket.
#   - Dieser Updater ist die einzige privilegierte Stelle und laeuft ausserhalb
#     der Container-Vertrauensgrenze.
#   - Selbst wer request.json faelscht, kann hoechstens ein KORREKT SIGNIERTES
#     Release ausrollen (Signaturpruefung), keinen beliebigen Code.
#
#  Ablauf: Signatur pruefen -> pull -> nur geaenderte Container -> Healthcheck
#          -> Rollback auf vorherigen Digest bei Fehler -> Audit-Log.
# ============================================================
set -uo pipefail

# --- Konfiguration (per systemd EnvironmentFile / .env ueberschreibbar) ------
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
UPDATE_DIR="${UPDATE_DIR:-$PROJECT_DIR/data/update}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
REQ="$UPDATE_DIR/request.json"
RES="$UPDATE_DIR/result.json"
AUDIT="$UPDATE_DIR/audit.log"

UPDATE_REPO="${UPDATE_REPO:-your-org/haproxy-dashboard}"
DOMAIN="${PLATFORM_DOMAIN:-platform.example.local}"
HEALTH_URL="${HEALTH_URL:-https://localhost/healthz}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"

VERIFY_SIGNATURES="${VERIFY_SIGNATURES:-true}"
COSIGN_IDENTITY_REGEXP="${COSIGN_IDENTITY_REGEXP:-https://github.com/${UPDATE_REPO}/.*}"
COSIGN_OIDC_ISSUER="${COSIGN_OIDC_ISSUER:-https://token.actions.githubusercontent.com}"

audit()  { printf '%s | host | %s\n' "$(date -u +%FT%TZ)" "$*" >> "$AUDIT"; }
result() { printf '{"state":"%s","ts":%s,"detail":"%s"}\n' "$1" "$(date +%s)" "${2:-}" > "$RES.tmp" && mv "$RES.tmp" "$RES"; }

# IMAGE_TAG dauerhaft in .env schreiben, damit Neustarts die Version behalten.
persist_tag() {
    local tag="$1"
    [ -f "$ENV_FILE" ] || return 0
    if grep -q '^IMAGE_TAG=' "$ENV_FILE"; then
        sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=${tag}|" "$ENV_FILE"
    else
        printf '\nIMAGE_TAG=%s\n' "$tag" >> "$ENV_FILE"
    fi
}

# Vorherige Image-IDs unter den Ziel-Refs wiederherstellen und neu starten.
do_rollback() {
    local reason="$1"
    audit "ROLLBACK reason=$reason target=${TARGET:-?}"
    [ -n "${prev_config:-}" ]  && docker tag "$prev_config"  "$CONFIG_REF"  >>"$AUDIT" 2>&1
    [ -n "${prev_haproxy:-}" ] && docker tag "$prev_haproxy" "$HAPROXY_REF" >>"$AUDIT" 2>&1
    docker compose up -d config-app haproxy >>"$AUDIT" 2>&1
    # Diese Version nicht automatisch erneut anfordern (verhindert Rollback-Schleife).
    [ -n "${TARGET:-}" ] && ! grep -qxF "$TARGET" "$UPDATE_DIR/auto-skip" 2>/dev/null \
        && echo "$TARGET" >> "$UPDATE_DIR/auto-skip"
    result "rolled_back" "$reason"
}

# --- Nur handeln, wenn eine Anforderung vorliegt ----------------------------
[ -f "$REQ" ] || exit 0
cd "$PROJECT_DIR" || { audit "FEHLER: PROJECT_DIR $PROJECT_DIR fehlt"; exit 1; }

TARGET="$(sed -n 's/.*"target"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQ")"
REQBY="$(sed -n 's/.*"requested_by"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQ")"
[ -n "$TARGET" ] || TARGET="latest"
rm -f "$REQ"   # Anforderung entgegengenommen

export IMAGE_TAG="$TARGET"
CONFIG_REF="ghcr.io/${UPDATE_REPO}/config-app:${TARGET}"
HAPROXY_REF="ghcr.io/${UPDATE_REPO}/haproxy:${TARGET}"

audit "UPDATE_START target=$TARGET requested_by=${REQBY:-?}"
result "running" "pull"

# --- Vorherige Image-IDs fuer Rollback merken -------------------------------
prev_config="$(docker inspect -f '{{.Image}}' acme-config-app 2>/dev/null || true)"
prev_haproxy="$(docker inspect -f '{{.Image}}' acme-haproxy 2>/dev/null || true)"

# --- Images ziehen ----------------------------------------------------------
if ! docker compose pull config-app haproxy >>"$AUDIT" 2>&1; then
    audit "PULL_FAILED"; result "failed" "pull"; exit 1
fi

# --- Signaturpruefung (fail-closed) -----------------------------------------
verify_one() {
    local ref="$1" digestref
    digestref="$(docker inspect --format '{{index .RepoDigests 0}}' "$ref" 2>/dev/null)"
    if [ -z "$digestref" ]; then audit "SIGNATURE_NO_DIGEST ref=$ref"; return 1; fi
    cosign verify \
        --certificate-identity-regexp "$COSIGN_IDENTITY_REGEXP" \
        --certificate-oidc-issuer "$COSIGN_OIDC_ISSUER" \
        "$digestref" >>"$AUDIT" 2>&1
}

if [ "$VERIFY_SIGNATURES" = "true" ]; then
    if ! command -v cosign >/dev/null 2>&1; then
        audit "SIGNATURE_ABORT cosign fehlt (VERIFY_SIGNATURES=true)"; result "failed" "signature"; exit 1
    fi
    for ref in "$CONFIG_REF" "$HAPROXY_REF"; do
        if ! verify_one "$ref"; then
            audit "SIGNATURE_FAILED ref=$ref -> Abbruch, kein Deploy"; result "failed" "signature"; exit 1
        fi
    done
    audit "SIGNATURE_OK"
else
    audit "SIGNATURE_SKIPPED (VERIFY_SIGNATURES=false)"
fi

# --- Nur tatsaechlich geaenderte Container neu starten (graceful) -----------
new_config="$(docker inspect -f '{{.Id}}' "$CONFIG_REF" 2>/dev/null || true)"
new_haproxy="$(docker inspect -f '{{.Id}}' "$HAPROXY_REF" 2>/dev/null || true)"
changed=""
[ -n "$new_config" ]  && [ "$new_config"  != "$prev_config" ]  && changed="$changed config-app"
[ -n "$new_haproxy" ] && [ "$new_haproxy" != "$prev_haproxy" ] && changed="$changed haproxy"
changed="$(echo "$changed" | xargs)"

if [ -z "$changed" ]; then
    audit "NO_CHANGE bereits auf $TARGET"; result "ok" "no-change"
    persist_tag "$TARGET"; exit 0
fi

audit "RECREATE services=[$changed]"
result "running" "deploy"
if ! docker compose up -d $changed >>"$AUDIT" 2>&1; then
    audit "DEPLOY_FAILED -> rollback"; do_rollback "deploy-error"; exit 1
fi

# --- Healthcheck ------------------------------------------------------------
healthy=0
for _ in $(seq 1 "$HEALTH_RETRIES"); do
    if curl -ksf -H "Host: $DOMAIN" "$HEALTH_URL" >/dev/null 2>&1; then healthy=1; break; fi
    sleep 2
done

if [ "$healthy" -eq 1 ]; then
    audit "HEALTHCHECK_OK -> Update auf $TARGET erfolgreich"
    result "ok" "$TARGET"
    persist_tag "$TARGET"
else
    audit "HEALTHCHECK_FAILED -> rollback"
    do_rollback "healthcheck"
fi
exit 0
