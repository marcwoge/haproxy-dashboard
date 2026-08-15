#!/bin/sh
# ============================================================
#  HAProxy Entrypoint
#  - baut kombiniertes PEM aus /certs/*.crt + *.key
#    (Fallback: selbstsigniertes Zertifikat fuer PLATFORM_DOMAIN)
#  - startet HAProxy, schreibt Logs in eine Datei (+ Container-stdout)
#  - Watcher: validiert Config, laedt bei Aenderung neu, schreibt status.txt
# ============================================================
set -u

DOMAIN="${PLATFORM_DOMAIN:-platform.example.local}"
CERT_DIR="/certs"
PEM="/usr/local/etc/haproxy/platform.pem"
DYN="/etc/haproxy/dynamic"
CFG="$DYN/haproxy.cfg"
LOG="$DYN/haproxy.log"
STATUS="$DYN/status.txt"
ROTATE_AT=5000   # Zeilen: ab hier Logdatei kuerzen
KEEP_LINES=600   # auf so viele Zeilen kuerzen

mkdir -p /usr/local/etc/haproxy "$DYN"
: >> "$LOG"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [entrypoint] $*" >> "$LOG"
}

write_status() {
    # $1 ok(1/0)   $2 action   $3 cfg_md5   $4 message(multiline)
    {
        echo "ok=$1"
        echo "action=$2"
        echo "ts=$(date +%s)"
        echo "cfg_md5=$3"
        echo "---MESSAGE---"
        printf '%s\n' "$4"
    } > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
}

# ------------------------------------------------------------
# Kombiniertes PEM (Zertifikat + privater Schluessel) bauen
# ------------------------------------------------------------
build_pem() {
    crt=""
    key=""
    for f in "$CERT_DIR"/fullchain.pem "$CERT_DIR"/*.crt "$CERT_DIR"/*.pem; do
        if [ -f "$f" ]; then crt="$f"; break; fi
    done
    for f in "$CERT_DIR"/privkey.pem "$CERT_DIR"/*.key; do
        if [ -f "$f" ]; then key="$f"; break; fi
    done

    # Persistiertes selbstsigniertes Zertifikat (N5): stabiler Fingerprint ueber
    # Neustarts hinweg (kein Trust-on-first-use-Bruch / Pinning-Chaos).
    SELF_PEM="$DYN/selfsigned.pem"

    if [ -n "$crt" ] && [ -n "$key" ] && [ "$crt" != "$key" ]; then
        log "Verwende Zertifikat: $crt + Schluessel: $key"
        cat "$crt" "$key" > "$PEM"
    elif [ -f "$SELF_PEM" ]; then
        log "Verwende persistiertes selbstsigniertes Zertifikat (stabiler Fingerprint)"
        cp "$SELF_PEM" "$PEM"
    else
        log "Kein Zertifikat in $CERT_DIR -> erzeuge selbstsigniertes Zertifikat fuer $DOMAIN (wird persistiert)"
        openssl req -x509 -newkey rsa:2048 -nodes \
            -keyout /tmp/selfsigned.key -out /tmp/selfsigned.crt -days 825 \
            -subj "/CN=$DOMAIN" \
            -addext "subjectAltName=DNS:$DOMAIN" >/dev/null 2>&1
        cat /tmp/selfsigned.crt /tmp/selfsigned.key > "$SELF_PEM"
        chmod 600 "$SELF_PEM"
        cp "$SELF_PEM" "$PEM"
        rm -f /tmp/selfsigned.key /tmp/selfsigned.crt
    fi
    chmod 600 "$PEM"
}
build_pem

# ------------------------------------------------------------
# Auf generierte Config warten (config-app schreibt sie beim Start)
# ------------------------------------------------------------
log "Warte auf Config: $CFG"
i=0
while [ ! -f "$CFG" ]; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
        log "Timeout -> schreibe Bootstrap-Config"
        cat > "$CFG" <<EOF
global
    log stdout format raw local0
defaults
    log global
    mode http
    timeout connect 5s
    timeout client 60s
    timeout server 60s
    default-server init-addr last,libc,none
frontend fe_http
    bind :80
    http-request redirect scheme https code 301 unless { ssl_fc }
frontend fe_https
    bind :443 ssl crt $PEM
    default_backend bk_dashboard
backend bk_dashboard
    server dashboard config-app:5000 init-addr last,libc,none
EOF
        break
    fi
    sleep 1
done

# ------------------------------------------------------------
# HAProxy starten (Master-Worker), Logs in Datei
# ------------------------------------------------------------
log "Starte HAProxy (master-worker)"
haproxy -W -db -f "$CFG" >> "$LOG" 2>&1 &
HAP_PID=$!

# Logdatei zusaetzlich auf Container-stdout spiegeln (damit `docker logs` weiter geht)
tail -n +1 -F "$LOG" 2>/dev/null &
TAIL_PID=$!

# Sauberes Herunterfahren: Signale an HAProxy weiterreichen
trap 'kill -TERM "$HAP_PID" 2>/dev/null; kill "$TAIL_PID" 2>/dev/null' TERM INT

# ------------------------------------------------------------
# Watcher: Config validieren, neu laden, Status + Log-Rotation
# ------------------------------------------------------------
last=""
while kill -0 "$HAP_PID" 2>/dev/null; do
    if [ -f "$CFG" ]; then
        cur="$(md5sum "$CFG" | cut -d' ' -f1)"
        if [ -n "$cur" ] && [ "$cur" != "$last" ]; then
            if out="$(haproxy -c -f "$CFG" 2>&1)"; then
                if [ -n "$last" ]; then
                    log "Config geaendert und gueltig -> reload"
                    kill -USR2 "$HAP_PID" 2>/dev/null
                    write_status 1 reloaded "$cur" "$out"
                else
                    write_status 1 initial "$cur" "$out"
                fi
            else
                log "WARN: neue Config UNGUELTIG -> laufende Config bleibt aktiv"
                write_status 0 rejected "$cur" "$out"
            fi
            last="$cur"
        fi
    fi

    # Log-Rotation in-place (haelt den Inode fuer offene Schreib-fds)
    nlines="$(wc -l < "$LOG" 2>/dev/null || echo 0)"
    if [ "$nlines" -gt "$ROTATE_AT" ]; then
        tail -n "$KEEP_LINES" "$LOG" > "$LOG.r" 2>/dev/null && cat "$LOG.r" > "$LOG" && rm -f "$LOG.r"
    fi

    sleep 2
done

wait "$HAP_PID" 2>/dev/null
