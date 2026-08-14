#!/usr/bin/env bash
# ============================================================
#  Installiert den host-seitigen Updater als systemd path+service.
#  Auf dem Linux-Server als root ausfuehren, aus dem Projektverzeichnis:
#      sudo ./updater/install-linux.sh
# ============================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte als root ausfuehren (sudo)." >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="/etc/systemd/system"
UPDATE_DIR="$PROJECT_DIR/data/update"

echo "[install] Projektverzeichnis: $PROJECT_DIR"

# 1) Update-Verzeichnis + append-only Audit-Log vorbereiten
mkdir -p "$UPDATE_DIR"
touch "$UPDATE_DIR/audit.log"
if command -v chattr >/dev/null 2>&1; then
    chattr +a "$UPDATE_DIR/audit.log" 2>/dev/null \
        && echo "[install] audit.log auf append-only (chattr +a) gesetzt" \
        || echo "[install] WARN: chattr +a nicht moeglich (Dateisystem?)"
fi

# 2) Skripte ausfuehrbar machen
chmod 750 "$PROJECT_DIR/updater/host-updater.sh" "$PROJECT_DIR/updater/check-latest.sh"

# 3) Units mit echtem Pfad installieren
for unit in acme-updater.service acme-updater.path \
            acme-update-check.service acme-update-check.timer; do
    sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        "$PROJECT_DIR/updater/systemd/$unit" > "$UNIT_DIR/$unit"
    echo "[install] $UNIT_DIR/$unit geschrieben"
done

# 4) Aktivieren
systemctl daemon-reload
systemctl enable --now acme-updater.path
echo "[install] acme-updater.path aktiviert."
# Auto-Update-Timer aktivieren (bleibt inaktiv, solange AUTO_UPDATE!=true in .env).
systemctl enable --now acme-update-check.timer
echo "[install] acme-update-check.timer aktiviert (Auto-Update via AUTO_UPDATE=true)."

cat <<EOF

Fertig. Naechste Schritte:
  1) GHCR-Login als root, damit private Images gezogen werden koennen:
       echo <READ-ONLY-TOKEN> | docker login ghcr.io -u <GHCR_USER> --password-stdin
  2) cosign installieren (fuer Signaturpruefung):
       https://docs.sigstore.dev/cosign/installation/
     (oder in .env  VERIFY_SIGNATURES=false  setzen - NICHT empfohlen)
  3) Auto-Update (optional): in .env  AUTO_UPDATE=true  setzen.
     Zeitplan anpassen:  sudo systemctl edit acme-update-check.timer
     Sofort testen:      sudo systemctl start acme-update-check.service
  4) Status/Logs:
       systemctl status acme-updater.path acme-update-check.timer
       systemctl list-timers acme-update-check.timer
       journalctl -u acme-updater.service -u acme-update-check.service -f
EOF
