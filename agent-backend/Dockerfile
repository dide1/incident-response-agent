# Stage 1: build Rust log-analyzer binary
FROM rust:1-slim AS rust-builder
WORKDIR /build
COPY log-analyzer/ .
RUN cargo build --release

# Stage 2: Python app
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev && rm -rf /var/lib/apt/lists/*
COPY agent-backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Pre-download the ONNX embedding model so first ingest doesn't need internet
RUN python -c "from fastembed import TextEmbedding; list(TextEmbedding('BAAI/bge-small-en-v1.5').embed(['warmup']))"
COPY agent-backend/ .
COPY --from=rust-builder /build/target/release/log-analyzer /usr/local/bin/log-analyzer
ENV LOG_ANALYZER_BIN=/usr/local/bin/log-analyzer
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
