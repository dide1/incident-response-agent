#!/usr/bin/env bash
# heal.sh — restore a service to its healthy state
# Usage: ./scripts/heal.sh [payments-service|order-service]

set -euo pipefail

SERVICE="${1:-payments-service}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT_DIR/.env"

echo ""
echo "  Healing $SERVICE..."

# Reset all fault modes to false
cat > "$ENV_FILE" <<EOF
ORDER_FAULT_MODE=false
PAYMENT_FAULT_MODE=false
API_FAULT_MODE=false
EOF

# Restart only the affected service
(cd "$ROOT_DIR" && docker compose up -d --no-deps "$SERVICE")

echo "  $SERVICE restored to healthy state."
echo "  Alertmanager will auto-resolve the alert within ~5 minutes."
echo ""
