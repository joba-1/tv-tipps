#!/usr/bin/env bash
# undeploy.sh — remove a previously installed tv-tipps (or legacy tv-tips) install.
#
# Stops + disables the systemd service, removes app dir, env dir, and the
# database directory. By default targets the new name (tv-tipps); pass --legacy
# to clean up the old tv-tips install left over from a v1.x deployment.
#
# Run with sudo.
set -euo pipefail

NAME="tv-tipps"
PREFIX="/usr/local"
PURGE_DB=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --legacy)  NAME="tv-tips"; shift ;;
    --name)    NAME="$2"; shift 2 ;;
    --prefix)  PREFIX="$2"; shift 2 ;;
    --purge-db) PURGE_DB=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: sudo $0 [--legacy] [--name NAME] [--prefix PREFIX] [--purge-db]

  --legacy     Target the old 'tv-tips' install (default: tv-tipps)
  --name NAME  Custom service/path name
  --prefix DIR Install prefix (default: /usr/local)
  --purge-db   Also delete /var/lib/<name> (the SQLite database)
EOF
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

SERVICE="$NAME.service"
UNIT_FILE="/etc/systemd/system/$SERVICE"
APP_DIR="$PREFIX/lib/$NAME"
CONF_DIR="/etc/$NAME"
DATA_DIR="/var/lib/$NAME"

echo "Stopping $SERVICE if running…"
systemctl stop "$SERVICE" 2>/dev/null || true
systemctl disable "$SERVICE" 2>/dev/null || true

if [[ -f "$UNIT_FILE" ]]; then
  echo "Removing $UNIT_FILE"
  rm -f "$UNIT_FILE"
  systemctl daemon-reload
fi

for d in "$APP_DIR" "$CONF_DIR"; do
  if [[ -d "$d" ]]; then
    echo "Removing $d"
    rm -rf "$d"
  fi
done

if [[ $PURGE_DB -eq 1 ]]; then
  if [[ -d "$DATA_DIR" ]]; then
    echo "Purging $DATA_DIR (DB)"
    rm -rf "$DATA_DIR"
  fi
else
  if [[ -d "$DATA_DIR" ]]; then
    echo "Kept $DATA_DIR (run with --purge-db to delete)"
  fi
fi

echo "Done. $NAME uninstalled."
