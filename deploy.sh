#!/usr/bin/env bash
# Deploy the service on the host (run ON the server, e.g. ai-seller, as root):
#
#   ssh ai-seller 'cd /var/www/browser-as-a-service && ./deploy.sh'
#
# Pulls the latest code, syncs Python deps, and restarts the systemd service.
# Requires: the `browser-as-a-service` systemd unit (see README "Деплой") and a
# repo-root .env with ASOCKS_API_KEY. Logs: journalctl -u browser-as-a-service -f
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="browser-as-a-service"
PORT=8077

cd "$REPO_DIR"

echo "==> git pull (--ff-only)"
git pull --ff-only

echo "==> sync Python deps"
./.venv/bin/pip install -q -r service/requirements.txt

echo "==> restart $SERVICE"
systemctl restart "$SERVICE"

echo "==> wait for :$PORT"
for _ in $(seq 1 20); do
    ss -ltn 2>/dev/null | grep -q ":$PORT" && break
    sleep 1
done

echo "==> verify"
systemctl is-active --quiet "$SERVICE" || { echo "service not active"; exit 1; }
curl -fsS -m 20 "localhost:$PORT/admin/status" >/dev/null \
    || { echo "health check failed"; journalctl -u "$SERVICE" --no-pager -n 20 -o cat; exit 1; }
echo "==> done: $SERVICE active on :$PORT"
