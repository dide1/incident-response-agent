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

# Update only fault mode vars — preserve ANTHROPIC_API_KEY and anything else in .env
_set_env_var() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

_set_env_var "ORDER_FAULT_MODE"   "false"
_set_env_var "PAYMENT_FAULT_MODE" "false"
_set_env_var "API_FAULT_MODE"     "false"

# Restart only the affected service
(cd "$ROOT_DIR" && docker compose up -d --no-deps "$SERVICE")

echo "  $SERVICE restored to healthy state."
echo "  Alertmanager will auto-resolve the alert within ~5 minutes."
echo ""
