# Autonomous Incident Response Agent

An end-to-end portfolio project: simulates production outages in dockerized microservices, detects them via real Prometheus/Alertmanager alerting, and uses a Claude-powered LLM agent to diagnose the likely bad commit, retrieve relevant runbooks, estimate user impact, post a Slack brief, and generate a postmortem.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Mock Production Environment                                    │
│                                                                 │
│  api-gateway :8080  ──►  order-service :8081                   │
│        │           └──►  payments-service :8082                │
│        │                       │                               │
│   Postgres :5432 ◄─────────────┘  (Phase 2+)                  │
└─────────────────────────────────────────────────────────────────┘
         │ /metrics
         ▼
┌─────────────────────┐     fires webhook    ┌──────────────────┐
│  Prometheus :9090   │ ──────────────────►  │  Alertmanager    │
│  (scrapes every 15s)│                      │  :9093           │
└─────────────────────┘                      └─────────┬────────┘
                                                       │ POST /webhook
                                                       ▼
                                             ┌──────────────────┐
                                             │  webhook-receiver│
                                             │  :9000           │
                                             │  (→ agent in P2) │
                                             └──────────────────┘
```

**Build phases:**

| Phase | What it adds | Status |
|-------|-------------|--------|
| 1 | Mock prod + real alerting | ✅ Done |
| 2 | Deploy tracker + commit correlation agent | — |
| 3 | Runbook RAG (pgvector) | — |
| 4 | Impact estimation + Slack brief | — |
| 5 | Postmortem generation | — |

---

## Phase 1: Quick start

### Prerequisites

- Docker Desktop (with Compose v2)
- `bash` (macOS/Linux)

### 1. Start the full stack

```bash
docker compose up --build -d
```

Wait ~30 seconds for all services to become healthy. Verify:

```bash
curl http://localhost:8080/health   # api-gateway
curl http://localhost:8081/health   # order-service
curl http://localhost:8082/health   # payments-service
curl http://localhost:9000/health   # webhook-receiver
```

Open Prometheus → **http://localhost:9090** and Alertmanager → **http://localhost:9093**.

### 2. Start the traffic generator (required for rate-based alerts)

In a separate terminal:

```bash
./scripts/generate_traffic.sh
```

This sends a mix of requests every 0.5 s so Prometheus has enough data to compute error rates.

### 3. Inject a fault

```bash
# Trigger HighErrorRate: payments-service returns 500 on ~80% of requests
./scripts/inject_fault.sh payments-service

# OR trigger HighLatency: order-service simulates N+1 query (20 x 100ms = 2s P99)
./scripts/inject_fault.sh order-service
```

### 4. Watch the alert fire

```bash
docker compose logs -f webhook-receiver
```

Within ~2 minutes you'll see Alertmanager deliver the webhook and the receiver log the alert name, job, severity, description, and full payload.

Also visible in:
- Prometheus Alerts tab → `http://localhost:9090/alerts`
- Alertmanager UI → `http://localhost:9093`

### 5. Heal the service

```bash
./scripts/heal.sh payments-service   # or order-service
```

Alertmanager sends a `resolved` webhook within ~5 minutes.

---

## Services

| Service | Port | Fault env var | Fault value | Symptom |
|---------|------|--------------|-------------|---------|
| api-gateway | 8080 | `API_FAULT_MODE` | `true` | all routes 500 |
| order-service | 8081 | `ORDER_FAULT_MODE` | `n_plus_one` | P99 latency > 2 s |
| payments-service | 8082 | `PAYMENT_FAULT_MODE` | `exception` | 80% error rate |

Each service exposes `GET /health` and `GET /metrics`.

---

## Alert rules

| Alert | Expression | Threshold | `for` window |
|-------|-----------|-----------|-------------|
| HighErrorRate | `errors / requests` per job | > 50% | 30 s |
| HighLatency | P99 latency per job | > 2 s | 30 s |

---

## Project layout

```
.
├── docker-compose.yml
├── .env                        # fault mode env vars (rewritten by scripts)
├── services/
│   ├── api-gateway/            # FastAPI + prometheus_client
│   ├── order-service/
│   └── payments-service/
├── prometheus/
│   ├── prometheus.yml          # scrape targets
│   └── alert_rules.yml        # HighErrorRate + HighLatency
├── alertmanager/
│   └── alertmanager.yml       # routes alerts to webhook-receiver:9000
├── webhook-receiver/           # logs alert payloads (becomes agent in Phase 2)
└── scripts/
    ├── inject_fault.sh
    ├── heal.sh
    └── generate_traffic.sh
```
