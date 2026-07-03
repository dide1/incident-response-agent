#!/usr/bin/env bash
# generate_traffic.sh — send continuous requests so Prometheus has rate data to evaluate
# Must be running when you inject a fault, or error-rate alerts won't fire
# Usage: ./scripts/generate_traffic.sh

set -euo pipefail

GW="http://localhost:8080"
ORDERS_DIRECT="http://localhost:8081"
PAYMENTS_DIRECT="http://localhost:8082"

echo "Sending traffic (Ctrl+C to stop)..."
echo "  api-gateway     -> $GW"
echo "  order-service   -> $ORDERS_DIRECT  (direct, for faster latency alert)"
echo "  payments-service-> $PAYMENTS_DIRECT (direct, for faster error-rate alert)"
echo ""

i=0
while true; do
  i=$((i + 1))

  # Via api-gateway (realistic path)
  curl -sf "$GW/api/orders"                                       > /dev/null 2>&1 || true
  curl -sf "$GW/api/payments"                                     > /dev/null 2>&1 || true
  curl -sf -X POST "$GW/api/payments/process"                     \
       -H "Content-Type: application/json"                        \
       -d "{\"order_id\": $((RANDOM % 10 + 1)), \"amount\": $(( RANDOM % 200 + 10 )).99}" \
       > /dev/null 2>&1 || true

  # Direct hits (ensures the per-job metrics accumulate even if gateway is down)
  curl -sf "$ORDERS_DIRECT/orders"                                > /dev/null 2>&1 || true
  curl -sf "$PAYMENTS_DIRECT/payments"                            > /dev/null 2>&1 || true
  curl -sf -X POST "$PAYMENTS_DIRECT/payments/process"           \
       -H "Content-Type: application/json"                        \
       -d "{\"order_id\": $((RANDOM % 10 + 1)), \"amount\": $(( RANDOM % 200 + 10 )).99}" \
       > /dev/null 2>&1 || true

  # Print a heartbeat every 10 cycles so the terminal shows progress
  if (( i % 10 == 0 )); then
    echo "  [$(date +%H:%M:%S)] sent $i request batches"
  fi

  sleep 0.5
done
