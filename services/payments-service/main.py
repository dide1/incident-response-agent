import os
import time
import random
from fastapi import FastAPI, Response, HTTPException, Request
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

SERVICE_NAME = "payments-service"
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

MOCK_PAYMENTS = [
    {
        "id": i,
        "order_id": i,
        "amount": round(random.uniform(10, 500), 2),
        "status": "completed",
        "transaction_id": f"txn_{random.randint(100000, 999999)}",
    }
    for i in range(1, 11)
]

app = FastAPI(title="Payments Service")


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


@app.get("/payments")
def list_payments():
    if FAULT_MODE == "exception":
        if random.random() < 0.8:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Unhandled exception: PaymentProcessorException: "
                    "Connection to payment gateway timed out after 30s"
                ),
            )
    return MOCK_PAYMENTS


@app.post("/payments/process")
def process_payment(payment: dict):
    if FAULT_MODE == "exception":
        if random.random() < 0.8:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Unhandled exception: PaymentProcessorException: "
                    "NullPointerException in PaymentTokenValidator.validate()"
                ),
            )
    result = {
        "id": len(MOCK_PAYMENTS) + 1,
        "order_id": payment.get("order_id"),
        "amount": payment.get("amount", 0),
        "status": "completed",
        "transaction_id": f"txn_{random.randint(100000, 999999)}",
    }
    MOCK_PAYMENTS.append(result)
    return result
