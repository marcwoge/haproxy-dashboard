#!/usr/bin/env bash
# ============================================================
#  Auto-Update-Check (host-seitig, per systemd-Timer).
#
#  Fragt read-only die Release-API ab und schreibt - NUR wenn AUTO_UPDATE=true
#  und ein NEUERES Release vorliegt - eine Anforderung (request.json). Die
#  eigentliche privilegierte Aktion (Signatur pruefen -> pull -> Healthcheck ->
#  Rollback) uebernimmt danach wie gehabt der host-seitige Updater ueber
#  acme-updater.path/.service. Der Zeitplan ist also nur ein automatischer "Klick".
#
#  Schutz gegen Endlosschleifen: eine Version, die einmal per Rollback
#  zurueckgenommen wurde, wird nicht automatisch erneut angefordert (auto-skip).
#  Manuelles Update aus dem Admin-GUI umgeht diese Sperre.
# ============================================================
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
UPDATE_DIR="${UPDATE_DIR:-$PROJECT_DIR/data/update}"
REQ="$UPDATE_DIR/request.json"
AUDIT="$UPDATE_DIR/audit.log"
SKIP="$UPDATE_DIR/auto-skip"

AUTO_UPDATE="${AUTO_UPDATE:-false}"
UPDATE_REPO="${UPDATE_REPO:-your-org/haproxy-dashboard}"
UPDATE_TOKEN="${UPDATE_TOKEN:-}"

audit() { mkdir -p "$UPDATE_DIR"; printf '%s | check | %s\n' "$(date -u +%FT%TZ)" "$*" >> "$AUDIT"; }

# Numerischer, portabler Semver-Vergleich (ohne sort -V).
nums() { printf '%s' "${1#v}" | grep -oE '[0-9]+'; }
is_newer() {  # $1 latest  $2 current  -> 0 wenn latest strikt neuer
    local la ca i l c
    la=($(nums "$1")); ca=($(nums "$2"))
    for i in 0 1 2; do
        l=${la[i]:-0}; c=${ca[i]:-0}
        [ "$l" -gt "$c" ] && return 0
        [ "$l" -lt "$c" ] && return 1
    done
    return 1
}

[ "$AUTO_UPDATE" = "true" ] || { audit "SKIP AUTO_UPDATE!=true"; exit 0; }

# Schon eine Anforderung offen? Dann nichts tun.
[ -f "$REQ" ] && { audit "SKIP Anforderung bereits offen"; exit 0; }

# Laufende Version aus dem Container lesen (APP_VERSION ist im Image gebacken).
current="$(docker inspect acme-config-app \
    --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | sed -n 's/^APP_VERSION=//p' | head -1)"
if [ -z "$current" ] || ! printf '%s' "$current" | grep -qE '[0-9]'; then
    audit "SKIP laufende Version unbekannt (current='${current:-}')"; exit 0
fi

# Neuestes Release ermitteln (read-only). Test-Hook: TEST_LATEST setzt latest.
if [ -n "${TEST_LATEST:-}" ]; then
    latest="$TEST_LATEST"
else
    hdr_auth=()
    [ -n "$UPDATE_TOKEN" ] && hdr_auth=(-H "Authorization: Bearer $UPDATE_TOKEN")
    latest="$(curl -fsS "${hdr_auth[@]}" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        "https://api.github.com/repos/${UPDATE_REPO}/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
fi

[ -n "$latest" ] || { audit "SKIP kein Release gefunden"; exit 0; }

if ! is_newer "$latest" "$current"; then
    audit "OK aktuell (laufend=$current, neuestes=$latest)"; exit 0
fi

# Bereits per Rollback verworfene Version nicht automatisch erneut anfordern.
if [ -f "$SKIP" ] && grep -qxF "$latest" "$SKIP"; then
    audit "SKIP $latest steht auf auto-skip (fehlgeschlagenes Update) - nur manuell"
    exit 0
fi

mkdir -p "$UPDATE_DIR"
printf '{"target": "%s", "requested_by": "auto-schedule", "ts": %s}\n' "$latest" "$(date +%s)" \
    > "$REQ.tmp" && mv "$REQ.tmp" "$REQ"
audit "AUTO_REQUEST target=$latest (laufend=$current) -> Updater uebernimmt"
