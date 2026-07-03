import os
import time
import asyncio
import random
from fastapi import FastAPI, Response, HTTPException, Request
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

SERVICE_NAME = "order-service"
FAULT_MODE = os.getenv("FAULT_MODE", "false").lower()

REQUEST_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["service", "endpoint", "status_code"],
)
REQUEST_ERRORS = Counter(
    "http_request_errors_total",
    "Total HTTP errors",
    ["service", "endpoint"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request duration in seconds",
    ["service", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

MOCK_ORDERS = [
    {
        "id": i,
        "customer_id": f"cust_{i}",
        "total": round(random.uniform(10, 500), 2),
        "status": "pending",
        "items": [f"item_{j}" for j in range(random.randint(1, 5))],
    }
    for i in range(1, 21)
]

app = FastAPI(title="Order Service")


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    if request.url.path in ("/metrics", "/health"):
        return await call_next(request)
    start = time.time()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration = time.time() - start
        endpoint = request.url.path
        REQUEST_TOTAL.labels(SERVICE_NAME, endpoint, str(status_code)).inc()
        REQUEST_LATENCY.labels(SERVICE_NAME, endpoint).observe(duration)
        if status_code >= 500:
            REQUEST_ERRORS.labels(SERVICE_NAME, endpoint).inc()


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "fault_mode": FAULT_MODE}


@app.get("/orders")
async def list_orders():
    if FAULT_MODE == "n_plus_one":
        # Simulate N+1 query: separate DB call per order instead of a JOIN
        for _ in MOCK_ORDERS:
            await asyncio.sleep(0.1)
    return MOCK_ORDERS


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    if FAULT_MODE == "n_plus_one":
        await asyncio.sleep(0.15)
    order = next((o for o in MOCK_ORDERS if o["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.post("/orders")
async def create_order(order: dict):
    if FAULT_MODE == "n_plus_one":
        await asyncio.sleep(0.3)
    new_order = {"id": len(MOCK_ORDERS) + 1, **order, "status": "created"}
    MOCK_ORDERS.append(new_order)
    return new_order
