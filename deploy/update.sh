#!/usr/bin/env bash
# mc-hamma auto-updater
# Checks if origin/main has new commits; if so, pulls and restarts the service.
# Run by mc-hamma-updater.service via a systemd timer.
set -euo pipefail

REPO_DIR="/home/pi/mc-hamma"
VENV_DIR="${REPO_DIR}/.venv"
SERVICE_NAME="mc-hamma.service"

log() { echo "[mc-hamma-updater] $*"; }

cd "${REPO_DIR}"

log "Fetching origin/main..."
git fetch origin main 2>&1

LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse origin/main)

if [ "${LOCAL_HEAD}" = "${REMOTE_HEAD}" ]; then
    log "Already up to date (${LOCAL_HEAD:0:8}). Nothing to do."
    exit 0
fi

log "Update available: ${LOCAL_HEAD:0:8} -> ${REMOTE_HEAD:0:8}"

# Snapshot requirements hash before pull to detect dependency changes
REQ_HASH_BEFORE=$(sha256sum "${REPO_DIR}/requirements.txt" | awk '{print $1}')

# Fast-forward only — fails safely if repo has unexpected local commits
git pull --ff-only origin main 2>&1

REQ_HASH_AFTER=$(sha256sum "${REPO_DIR}/requirements.txt" | awk '{print $1}')

if [ "${REQ_HASH_BEFORE}" != "${REQ_HASH_AFTER}" ]; then
    log "requirements.txt changed — reinstalling dependencies..."
    "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt" --quiet 2>&1
    log "Dependencies reinstalled."
else
    log "requirements.txt unchanged — skipping pip install."
fi

log "Restarting ${SERVICE_NAME}..."
sudo systemctl restart "${SERVICE_NAME}"

log "Update complete. Now at $(git rev-parse --short HEAD)."
