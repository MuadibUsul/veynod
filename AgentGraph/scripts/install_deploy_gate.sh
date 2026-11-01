#!/usr/bin/env bash
# Install (or reinstall) the Veynod deploy gate on the production VPS.
#
#   sudo scripts/install_deploy_gate.sh                 # install or update, start the timer
#   sudo scripts/install_deploy_gate.sh --uninstall     # stop and remove the units
#
# Run from the deployment checkout (default /home/deploy/agentgraph). It is safe
# to run again: units are re-rendered from deploy/deploy-gate/ and the timer is
# restarted. Secrets stay in the deployment .env and are never printed.
# See docs/DEPLOY_GATE.md for the GitHub webhook and the console switches.

set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
WORK_DIR="${DEPLOY_GATE_WORKDIR:-/var/lib/veynod-deploy-gate}"
LOG_DIR="${DEPLOY_GATE_LOG_DIR:-/var/log/veynod-deploy-gate}"
SERVICE=veynod-deploy-gate

log() { printf '\033[1m[gate]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[gate] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run with sudo: units go to $UNIT_DIR"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now "$SERVICE.timer" 2>/dev/null || true
  rm -f "$UNIT_DIR/$SERVICE.service" "$UNIT_DIR/$SERVICE.timer"
  systemctl daemon-reload
  log "removed; $WORK_DIR and $LOG_DIR were kept"
  exit 0
fi

RUN_USER="${RUN_USER:-$(stat -c %U "$DEPLOY_DIR")}"
[ "$RUN_USER" != root ] || fail "the deployment checkout is owned by root; set RUN_USER"
[ -f "$DEPLOY_DIR/.env" ] || fail "$DEPLOY_DIR/.env not found"
[ -x "$DEPLOY_DIR/scripts/deploy_tlines.sh" ] || fail "scripts/deploy_tlines.sh is missing"
command -v docker >/dev/null || fail "docker is not installed"
command -v git >/dev/null || fail "git is not installed"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || fail "python3 3.10 or newer is required"
id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx docker \
  || fail "$RUN_USER is not in the docker group (usermod -aG docker $RUN_USER)"
grep -Eq '^DEPLOY_GATE_RUNNER_TOKEN=.+' "$DEPLOY_DIR/.env" \
  || fail "set DEPLOY_GATE_RUNNER_TOKEN in $DEPLOY_DIR/.env (see .env.example)"
grep -Eq '^DEPLOY_GATE_WEBHOOK_SECRET=.+' "$DEPLOY_DIR/.env" \
  || log "DEPLOY_GATE_WEBHOOK_SECRET is empty: only the timer will find new merges"

install -d -m 750 -o "$RUN_USER" -g "$RUN_USER" "$WORK_DIR" "$LOG_DIR"
for unit in "$SERVICE.service" "$SERVICE.timer"; do
  source_file="$DEPLOY_DIR/deploy/deploy-gate/$unit"
  [ -f "$source_file.in" ] && source_file="$source_file.in"
  sed -e "s#@DEPLOY_DIR@#$DEPLOY_DIR#g" -e "s#@RUN_USER@#$RUN_USER#g" "$source_file" \
    > "$UNIT_DIR/$unit"
  chmod 644 "$UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable "$SERVICE.timer" >/dev/null
systemctl restart "$SERVICE.timer"
log "installed for $RUN_USER in $DEPLOY_DIR"
log "  next cycle:  systemctl list-timers $SERVICE.timer"
log "  run now:     sudo systemctl start $SERVICE.service"
log "  logs:        $LOG_DIR (gate.log, one file per run)"
log "the gate starts unauthorized: authorize automatic deploys in the console"
