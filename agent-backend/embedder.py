"""Lightweight local embedding using fastembed (ONNX, no PyTorch required)."""
import logging

from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

_model: TextEmbedding | None = None


def get_model() -> TextEmbedding:
    global _model
    if _model is None:
        logger.info("Loading embedding model %s", MODEL_NAME)
        _model = TextEmbedding(model_name=MODEL_NAME)
        logger.info("Embedding model ready")
    return _model


def embed(text: str) -> list[float]:
    model = get_model()
    vectors = list(model.embed([text]))
    return vectors[0].tolist()
