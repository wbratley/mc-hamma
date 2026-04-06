#!/usr/bin/env bash
# mc-hamma first-time setup
# Run as the user who will own the service (e.g. pi), NOT as root.
# Requires sudo for: usermod, sudoers, systemd unit installation.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="${REPO_DIR}/deploy"
VENV_DIR="${REPO_DIR}/.venv"
SERVICE_USER="${USER}"
SYSTEMD_SYSTEM="/etc/systemd/system"

log()  { echo "[setup] $*"; }
warn() { echo "[setup] WARNING: $*" >&2; }
die()  { echo "[setup] ERROR: $*" >&2; exit 1; }

log "mc-hamma setup"
log "  Repo:  ${REPO_DIR}"
log "  User:  ${SERVICE_USER}"
echo ""

# ── Sanity checks ─────────────────────────────────────────────────────────────
[ "$(id -u)" -ne 0 ] || die "Run this script as your normal user, not root."
[ -f "${REPO_DIR}/server.py" ] || die "server.py not found. Run this from inside the mc-hamma repo."
command -v python3 &>/dev/null || die "python3 not found."
command -v git &>/dev/null    || die "git not found."

# ── 1. dialout group (serial port access) ─────────────────────────────────────
if groups "${SERVICE_USER}" | grep -qw "dialout"; then
    log "1. User already in dialout group."
else
    log "1. Adding ${SERVICE_USER} to dialout group..."
    sudo usermod -aG dialout "${SERVICE_USER}"
    warn "Group change takes effect on next login/reboot. The systemd service"
    warn "will use the group from /etc/group directly, so it works immediately."
fi

# ── 2. Python venv ────────────────────────────────────────────────────────────
log "2. Setting up Python venv..."
if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    log "   Created venv at ${VENV_DIR}"
else
    log "   Venv already exists."
fi

"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"
log "   Dependencies installed."

# ── 3. Env file ───────────────────────────────────────────────────────────────
ENV_FILE="${DEPLOY_DIR}/mc-hamma.env"
log "3. Environment config..."
if [ ! -f "${ENV_FILE}" ]; then
    cp "${DEPLOY_DIR}/mc-hamma.env.example" "${ENV_FILE}"
    log ""
    log "   *** Created ${ENV_FILE} ***"
    log "   IMPORTANT: Edit this file and set MC_SERIAL_PORT to your device"
    log "   e.g.  MC_SERIAL_PORT=/dev/ttyUSB0"
    log "   Run: nano ${ENV_FILE}"
    log ""
else
    log "   ${ENV_FILE} already exists — not overwriting."
fi

# ── 4. update.sh executable ───────────────────────────────────────────────────
log "4. Making update.sh executable..."
chmod +x "${DEPLOY_DIR}/update.sh"

# ── 5. Sudoers rule (only allows restarting this one service) ─────────────────
SUDOERS_FILE="/etc/sudoers.d/mc-hamma"
log "5. Sudoers rule..."
if [ ! -f "${SUDOERS_FILE}" ]; then
    {
        echo "${SERVICE_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart mc-hamma.service"
        echo "${SERVICE_USER} ALL=(ALL) NOPASSWD: /bin/systemctl start mc-hamma.service"
        echo "${SERVICE_USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop mc-hamma.service"
    } | sudo tee "${SUDOERS_FILE}" > /dev/null
    sudo chmod 440 "${SUDOERS_FILE}"
    log "   Installed ${SUDOERS_FILE}"
else
    log "   Sudoers rule already exists."
fi

# ── 6. Install systemd units ──────────────────────────────────────────────────
log "6. Installing systemd units..."
for UNIT in mc-hamma.service mc-hamma-updater.service mc-hamma-updater.timer; do
    SRC="${DEPLOY_DIR}/${UNIT}"
    DST="${SYSTEMD_SYSTEM}/${UNIT}"
    # Substitute placeholder user/path with actual values
    sudo sed \
        -e "s|/home/pi/mc-hamma|${REPO_DIR}|g" \
        -e "s|User=pi|User=${SERVICE_USER}|g" \
        -e "s|Group=pi|Group=${SERVICE_USER}|g" \
        "${SRC}" | sudo tee "${DST}" > /dev/null
    log "   ${DST}"
done

sudo systemctl daemon-reload
log "   systemd daemon reloaded."

# ── 7. Enable units ───────────────────────────────────────────────────────────
log "7. Enabling units..."
sudo systemctl enable mc-hamma.service
sudo systemctl enable mc-hamma-updater.timer
log "   Units enabled (will start on next boot automatically)."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
log "════════════════════════════════════════════════"
log "Setup complete!"
log ""
log "Next steps:"
log "  1. Confirm your serial port in: ${ENV_FILE}"
log "     nano ${ENV_FILE}"
log ""
log "  2. Start the app:"
log "     sudo systemctl start mc-hamma.service"
log ""
log "  3. Start the auto-updater timer:"
log "     sudo systemctl start mc-hamma-updater.timer"
log ""
log "  4. Check status:"
log "     sudo systemctl status mc-hamma.service"
log "     sudo systemctl status mc-hamma-updater.timer"
log ""
log "  5. Watch live logs:"
log "     journalctl -fu mc-hamma"
log ""
log "  6. Watch updater runs:"
log "     journalctl -fu mc-hamma-updater"
log "════════════════════════════════════════════════"
