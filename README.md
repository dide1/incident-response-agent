# Autonomous Incident Response Agent

A portfolio project that simulates production outages in a dockerized microservice environment, detects them via real Prometheus/Alertmanager alerting, and uses a Claude-powered LLM agent to autonomously diagnose the root cause, retrieve relevant runbooks via vector similarity search, estimate user impact from live Prometheus metrics, post a Slack brief, and generate a full postmortem — all without human involvement.

---

## What it does

When an alert fires, the agent:

1. **Identifies the bad commit** — queries a deploy tracker, fetches diffs for every recent commit, and reasons about which change caused the alert
2. **Retrieves the right runbook** — embeds the incident description and runs cosine similarity search over 11 runbooks stored in pgvector
3. **Estimates real impact** — queries Prometheus for live error rate and request rate using a window matched to the incident duration
4. **Posts a Slack brief** — Block Kit message with severity, impact metrics, blamed commit, and immediate action
5. **Generates a postmortem** — when the alert resolves, produces a structured Markdown doc with executive summary, timeline, root cause, action items (with owners), contributing factors, and lessons learned

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Mock Production Environment                         │
│                                                      │
│  api-gateway :8080 ──► order-service :8081           │
│       │          └──►  payments-service :8082        │
│       │                                              │
│  Each service exposes GET /metrics (Prometheus fmt)  │
└──────────────────────────────────────────────────────┘
        │ scrape every 15s
        ▼
┌─────────────────────┐   alert fires    ┌─────────────────────┐
│  Prometheus :9090   │ ───────────────► │  Alertmanager :9093 │
│  alert_rules.yml    │                  │  routes to agent    │
└─────────────────────┘                  └──────────┬──────────┘
                                                    │ POST /webhook
                                                    ▼
                                    ┌───────────────────────────┐
                                    │   agent-backend :9000     │
                                    │                           │
                                    │  FastAPI + Claude agent   │
                                    │  ┌─────────────────────┐  │
                                    │  │  Tools              │  │
                                    │  │  • get_recent_deploys│  │
                                    │  │  • get_commit_diff  │  │
                                    │  │  • search_runbooks  │  │
                                    │  │  • query_prometheus │  │
                                    │  └─────────────────────┘  │
                                    │                           │
                                    │  pgvector (runbook RAG)   │
                                    │  fastembed BAAI/bge-small │
                                    │  Slack Block Kit notifier │
                                    │  Postmortem generator     │
                                    └───────────────────────────┘
                                                    │
                                    ┌───────────────┴───────────────┐
                                    │                               │
                                    ▼                               ▼
                              Slack channel                  Postgres :5432
                              • incident brief               • deploy_tracker
                              • resolved notice              • commit_diffs
                              • postmortem preview           • runbooks (vector)
                                                             • postmortems
```

---

## Stack

| Component | Technology |
|-----------|-----------|
| Microservices | Python / FastAPI / uvicorn |
| Observability | Prometheus + Alertmanager |
| Agent | Claude (`claude-sonnet-4-6`) via Anthropic tool use API |
| Vector search | pgvector (HNSW index) + fastembed `BAAI/bge-small-en-v1.5` (384-dim, ONNX) |
| Database | PostgreSQL 15 (`pgvector/pgvector:pg15`) |
| Notifications | Slack Incoming Webhooks + Block Kit |
| Infrastructure | Docker Compose |

---

## Project layout

```
.
├── docker-compose.yml
├── .env                          # ANTHROPIC_API_KEY, SLACK_WEBHOOK_URL, fault mode vars
├── agent-backend/
│   ├── main.py                   # FastAPI app, webhook handler, endpoints
│   ├── agent.py                  # Claude tool-use agentic loop
│   ├── tools.py                  # Tool definitions + dispatch
│   ├── db.py                     # Postgres: deploys, diffs, runbooks, postmortems
│   ├── embedder.py               # fastembed singleton
│   ├── prometheus_client.py      # PromQL HTTP client
│   ├── slack_notifier.py         # Block Kit formatter + webhook poster
│   ├── postmortem.py             # Claude postmortem generator
│   └── requirements.txt
├── runbooks/                     # 11 Markdown runbooks (ingested into pgvector)
│   ├── api-latency-spike.md
│   ├── bad-deploy-rollback.md
│   ├── db-connection-pool-exhaustion.md
│   ├── downstream-service-failure.md
│   ├── high-error-rate-investigation.md
│   ├── missing-retry-logic.md
│   ├── n-plus-one-query.md
│   ├── payment-gateway-timeout.md
│   ├── rate-limiting-ddos.md
│   ├── service-memory-leak.md
│   └── unhandled-exception-triage.md
├── services/
│   ├── api-gateway/
│   ├── order-service/            # fault: N+1 query → HighLatency
│   └── payments-service/         # fault: unhandled exception → HighErrorRate
├── prometheus/
│   ├── prometheus.yml
│   └── alert_rules.yml           # HighErrorRate (>50% errors, 30s) + HighLatency (P99>2s, 30s)
├── alertmanager/
│   └── alertmanager.yml
└── scripts/
    ├── generate_traffic.sh       # continuous request loop for Prometheus rate data
    ├── inject_fault.sh           # sets FAULT_MODE, restarts service, seeds bad commit
    ├── heal.sh                   # clears fault, restores service
    ├── record_bad_commit.py      # called by inject_fault.sh to seed deploy tracker
    ├── ingest_runbooks.py        # POST /admin/ingest-runbooks
    ├── probe_similarity.py       # adversarial retrieval quality check (7 queries)
    ├── test_phase2.py
    ├── test_phase3.py
    ├── test_phase4.py
    └── test_phase5.py
```

---

## Quick start

### Prerequisites

- Docker Desktop (Compose v2)
- Python 3.11+ (for test scripts, run outside containers)
- An [Anthropic API key](https://console.anthropic.com/)
- A Slack incoming webhook URL (optional — briefs log to stdout if unset)

### 1. Configure environment

```bash
cp .env.example .env   # or create .env manually
```

`.env` minimum:
```
ANTHROPIC_API_KEY=sk-ant-...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   # optional
ORDER_FAULT_MODE=false
PAYMENT_FAULT_MODE=false
API_FAULT_MODE=false
```

### 2. Start the stack

```bash
docker compose up --build -d
```

Wait ~30s for all services to become healthy:

```bash
curl http://localhost:9000/health    # agent-backend
curl http://localhost:8080/health    # api-gateway
curl http://localhost:9090/-/ready   # prometheus
```

### 3. Ingest runbooks into pgvector

```bash
python3 scripts/ingest_runbooks.py
# → 11 runbooks embedded (BAAI/bge-small-en-v1.5, 384 dims) and stored
```

### 4. Start the traffic generator

In a separate terminal — required for Prometheus to compute error and latency rates:

```bash
./scripts/generate_traffic.sh
```

### 5. Inject a fault

```bash
# HighErrorRate: payments-service throws PaymentProcessorException on ~80% of requests
./scripts/inject_fault.sh payments-service

# HighLatency: order-service N+1 query regression, P99 spikes to ~2s
./scripts/inject_fault.sh order-service
```

Within ~2 minutes Alertmanager fires the webhook, the agent runs, and you'll see:
- A Slack brief in your channel
- Agent logs: `docker compose logs -f agent-backend`
- Prometheus alerts: `http://localhost:9090/alerts`

### 6. Trigger the postmortem

```bash
./scripts/heal.sh payments-service
```

When the resolved webhook arrives (~5 min), the agent generates and stores a postmortem:

```bash
curl http://localhost:9000/postmortems/latest | python3 -m json.tool
```

---

## Alert rules

| Alert | Condition | Window |
|-------|-----------|--------|
| `HighErrorRate` | 5xx rate > 50% of total requests per job | `for: 30s` |
| `HighLatency` | P99 latency > 2s per job | `for: 30s` |

---

## Agent tools

| Tool | What it does |
|------|-------------|
| `get_recent_deploys` | Returns all commits deployed to a service in the last N minutes |
| `get_commit_diff` | Fetches the stored diff for a specific SHA |
| `search_runbooks` | Embeds the query and runs cosine similarity search (HNSW) over pgvector |
| `query_prometheus` | Runs PromQL against Prometheus; uses `{window}` placeholder computed from alert start time to avoid diluting impact with pre-incident baseline |

---

## Key API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook` | Alertmanager webhook receiver (firing + resolved) |
| `POST` | `/deploys` | Record a deployment with optional diff |
| `GET` | `/incidents/latest` | Most recent completed agent analysis |
| `GET` | `/postmortems/latest` | Most recent generated postmortem |
| `POST` | `/admin/ingest-runbooks` | Embed and upsert all runbooks from `/runbooks/` |
| `POST` | `/runbooks/search` | Direct vector search (for debugging retrieval) |
| `DELETE` | `/admin/clear-deploys` | Truncate deploy tables (test setup) |

---

## Running the tests

Each test is self-contained and cleans up after itself:

```bash
python3 scripts/test_phase2.py   # commit correlation + confidence calibration
python3 scripts/test_phase3.py   # runbook ingestion, similarity, agent e2e
python3 scripts/test_phase4.py   # Prometheus impact, Slack brief structure + webhook
python3 scripts/test_phase5.py   # postmortem generation on alert resolution
```

Phase 4 runs `generate_traffic.sh` internally for 90s before injecting the fault, so Prometheus has baseline data. Expected runtime: ~8 minutes total for all four suites.

---

## Design notes

**Why fastembed over OpenAI embeddings?** No external API call, no per-token cost, runs in the container at ~10ms/embed. `BAAI/bge-small-en-v1.5` (384 dims, ONNX runtime) is sufficient for 11 keyword-rich runbooks and adversarial retrieval separation is clean — db-connection-pool-exhaustion and downstream-service-failure resolve to distinct top-1 results at cosine similarity 0.763 vs 0.798.

**Why HNSW over IVFFLAT?** IVFFLAT with `lists=10` requires ~30 training vectors minimum; with only 11 runbooks, 2 of 7 adversarial queries returned empty results. HNSW has no minimum dataset size and gives exact results at this scale.

**Why dynamic `{window}` in PromQL?** A fixed `[2m]` window on a 90-second incident dilutes the error rate with ~30s of pre-incident normal traffic. The agent passes `since=starts_at` and the Prometheus client computes `window = elapsed + 30s`, ensuring the rate captures the full spike.

**Vector serialization without register_vector:** `psycopg2.extras.RealDictCursor` is incompatible with `pgvector`'s `register_vector()` (which calls `row[0]` internally on a dict). Vectors are passed as `'[f1,f2,...]'::vector` string literals instead — no adapter needed.
