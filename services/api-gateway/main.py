import os
import time
import httpx
from fastapi import FastAPI, Response, HTTPException, Request
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

SERVICE_NAME = "api-gateway"
FAULT_MODE = os.getenv("FAULT_MODE", "false").lower() == "true"
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8000")
PAYMENTS_SERVICE_URL = os.getenv("PAYMENTS_SERVICE_URL", "http://payments-service:8000")

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

app = FastAPI(title="API Gateway")


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


@app.get("/api/orders")
async def get_orders():
    if FAULT_MODE:
        raise HTTPException(
            status_code=500,
            detail="NullPointerException in OrderController.listOrders()",
        )
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(f"{ORDER_SERVICE_URL}/orders")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"order-service unavailable: {e}")


@app.post("/api/orders")
async def create_order(order: dict):
    if FAULT_MODE:
        raise HTTPException(
            status_code=500,
            detail="NullPointerException in OrderController.createOrder()",
        )
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(f"{ORDER_SERVICE_URL}/orders", json=order)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"order-service unavailable: {e}")


@app.get("/api/payments")
async def get_payments():
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(f"{PAYMENTS_SERVICE_URL}/payments")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"payments-service unavailable: {e}")


@app.post("/api/payments/process")
async def process_payment(payment: dict):
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"{PAYMENTS_SERVICE_URL}/payments/process", json=payment
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"payments-service unavailable: {e}")
