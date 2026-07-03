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

# 1. Record the bad commit in the deploy tracker BEFORE restarting the service
echo "  Recording bad commit..."
python3 "$SCRIPT_DIR/record_bad_commit.py" "$SERVICE"

# 2. Rewrite .env with the fault enabled
cat > "$ENV_FILE" <<EOF
ORDER_FAULT_MODE=false
PAYMENT_FAULT_MODE=false
API_FAULT_MODE=false
EOF

sed -i.bak "s/${FAULT_VAR}=false/${FAULT_VAR}=${FAULT_VALUE}/" "$ENV_FILE"
rm -f "${ENV_FILE}.bak"

# 3. Restart only the affected service (env var drives fault behavior)
(cd "$ROOT_DIR" && docker compose up -d --no-deps "$SERVICE")

echo ""
echo "  Service restarted with fault active."
echo ""
echo "  Monitor:"
echo "    Agent logs  -> docker compose logs -f agent-backend"
echo "    Prometheus  -> http://localhost:9090/alerts"
echo "    Alertmanager-> http://localhost:9093"
echo ""
echo "  Run traffic generator (if not running): ./scripts/generate_traffic.sh"
echo "  To heal: ./scripts/heal.sh $SERVICE"
echo ""
