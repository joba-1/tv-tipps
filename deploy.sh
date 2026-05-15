#!/usr/bin/env bash
# deploy.sh — install tv-tips as a systemd service
#
# Usage: sudo ./deploy.sh [--prefix DIR] [--port PORT] [--user USER]
#   --prefix DIR   install root (default: /usr/local)
#   --port   PORT  listening port  (default: 8844)
#   --user   USER  service user    (default: current user)
#
# Layout after install:
#   $PREFIX/lib/tv-tips/   — app code + venv
#   /etc/tv-tips/env       — environment / config file (created once)
#   /var/lib/tv-tips/      — runtime data (SQLite DB)
#   /etc/systemd/system/tv-tips.service

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
PREFIX=/usr/local
PORT=8844
SERVICE_USER=${SUDO_USER:-$(id -un)}

# ── argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --port)   PORT="$2";   shift 2 ;;
        --user)   SERVICE_USER="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,10p' "$0"
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

APP_DIR="$PREFIX/lib/tv-tips"
DATA_DIR="/var/lib/tv-tips"
CONF_DIR="/etc/tv-tips"
ENV_FILE="$CONF_DIR/env"
SERVICE_FILE="/etc/systemd/system/tv-tips.service"
VERSION=$(cat VERSION 2>/dev/null || echo "?")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

# ── root check ────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi

echo "==> tv-tips v${VERSION} deploy"
echo "    prefix : $APP_DIR"
echo "    port   : $PORT"
echo "    user   : $SERVICE_USER"
echo "    data   : $DATA_DIR"
echo "    config : $ENV_FILE"
echo

# ── install app files ─────────────────────────────────────────────────────────
echo "--> Installing app files …"
install -d "$APP_DIR"
rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.db' \
    --exclude='*.db-shm' \
    --exclude='*.db-wal' \
    --exclude='.env' \
    --exclude='.git' \
    --exclude='deploy.sh' \
    "$SCRIPT_DIR/" "$APP_DIR/"
chown -R "$SERVICE_USER:" "$APP_DIR"

# ── Python venv ───────────────────────────────────────────────────────────────
echo "--> Setting up Python venv …"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ── data directory ────────────────────────────────────────────────────────────
echo "--> Creating data directory …"
install -d -o "$SERVICE_USER" "$DATA_DIR"

# ── env / config file (created once, never overwritten) ───────────────────────
install -d "$CONF_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "--> Creating config file $ENV_FILE …"
    cat > "$ENV_FILE" <<EOF
# tv-tips configuration — edit before starting the service
# Restart the service after changes: systemctl restart tv-tips
# Add receivers and users via the Admin page in the web UI.

# ── Ollama ───────────────────────────────────────────────────────────────────
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:9b

# ── EPG & poller ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC=45
MIN_WATCH_SEC=300
EPG_FULL_REFRESH_HOUR=3
EPG_RETENTION_DAYS=30
TIMEZONE=Europe/Berlin

# ── Prime-time window ────────────────────────────────────────────────────────
PRIME_START_HOUR=20
PRIME_END_HOUR=23

# ── IntertechnoGateway (optional — only for power_method=intertechno) ─────────
#INTERTECHNO_URL=http://intertechnogw
#INTERTECHNO_FAMILY=A
#INTERTECHNO_DEVICE=1

# ── Misc ─────────────────────────────────────────────────────────────────────
DB_PATH=$DATA_DIR/tv_tips.db
LOG_LEVEL=INFO
MOCK_RECEIVERS=false
EOF
    chmod 640 "$ENV_FILE"
    chown "root:$SERVICE_USER" "$ENV_FILE"
else
    echo "--> Config file already exists — skipping (edit $ENV_FILE to change settings)"
    # Warn if DB_PATH is still a relative path (common when migrating from a dev .env)
    existing_db=$(grep -E '^DB_PATH=' "$ENV_FILE" | cut -d= -f2-)
    if [[ -n "$existing_db" && "$existing_db" != /* ]]; then
        echo "WARNING: DB_PATH='$existing_db' is a relative path."
        echo "         The service WorkingDirectory is $APP_DIR, so the DB will be"
        echo "         created there, not in $DATA_DIR."
        echo "         Fix: update DB_PATH=$DATA_DIR/tv_tips.db in $ENV_FILE"
    fi
fi

# ── systemd service ───────────────────────────────────────────────────────────
echo "--> Installing systemd service …"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=tv-tips TV recommendation server
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/uvicorn main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable tv-tips
systemctl restart tv-tips

# ── success ───────────────────────────────────────────────────────────────────
echo
echo "✓ tv-tips v${VERSION} installed and started on port ${PORT}"
echo
echo "Next steps:"
echo "  1. Edit config:   $ENV_FILE"
echo "     - Set OLLAMA_MODEL to a model you have pulled (ollama pull qwen3.5:9b)"
echo "     - Adjust TIMEZONE to your local timezone"
echo "     - Optionally set INTERTECHNO_URL if using an RF power switch"
echo "  2. Restart after config changes:"
echo "     sudo systemctl restart tv-tips"
echo "  3. Open in browser:"
echo "     http://$(hostname -I | awk '{print $1}'):${PORT}/"
echo "  4. Check logs:"
echo "     journalctl -u tv-tips -f"
echo "  5. Update later:"
echo "     cd $SCRIPT_DIR && git pull && sudo ./deploy.sh --prefix $PREFIX --port $PORT --user $SERVICE_USER"
