#!/usr/bin/env bash
# inject_fault.sh — simulate a bad commit being deployed to a service
# Usage:
#   ./scripts/inject_fault.sh payments-service   # 80% 5xx (exception fault)
#   ./scripts/inject_fault.sh order-service       # 2s+ P99 latency (N+1 fault)

set -euo pipefail

SERVICE="${1:-payments-service}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT_DIR/.env"

case "$SERVICE" in
  payments-service)
    FAULT_VAR="PAYMENT_FAULT_MODE"
    FAULT_VALUE="exception"
    ALERT_TYPE="HighErrorRate"
    DESCRIPTION="80% of payment requests will return 500 (unhandled exception)"
    ;;
  order-service)
    FAULT_VAR="ORDER_FAULT_MODE"
    FAULT_VALUE="n_plus_one"
    ALERT_TYPE="HighLatency"
    DESCRIPTION="N+1 query: 20 x 100ms = ~2s per request, P99 will spike"
    ;;
  *)
    echo "Unknown service: $SERVICE"
    echo "Valid targets: payments-service, order-service"
    exit 1
    ;;
esac

echo ""
echo "  Injecting fault into $SERVICE"
echo "  Type   : $FAULT_VALUE"
echo "  Effect : $DESCRIPTION"
echo "  Expect : $ALERT_TYPE alert within ~2 minutes"
echo ""

# Rewrite .env with the fault enabled
cat > "$ENV_FILE" <<EOF
ORDER_FAULT_MODE=false
PAYMENT_FAULT_MODE=false
API_FAULT_MODE=false
EOF

# Override the target service's fault mode
sed -i.bak "s/${FAULT_VAR}=false/${FAULT_VAR}=${FAULT_VALUE}/" "$ENV_FILE"
rm -f "${ENV_FILE}.bak"

# Restart only the affected service (no rebuild needed, env var drives behavior)
(cd "$ROOT_DIR" && docker compose up -d --no-deps "$SERVICE")

echo ""
echo "  Service restarted with fault active."
echo ""
echo "  Monitor:"
echo "    Prometheus  -> http://localhost:9090/alerts"
echo "    Alertmanager-> http://localhost:9093"
echo "    Webhook logs-> docker compose logs -f webhook-receiver"
echo ""
echo "  Run traffic generator: ./scripts/generate_traffic.sh"
echo "  To heal: ./scripts/heal.sh $SERVICE"
echo ""
