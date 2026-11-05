"""Pinned public ONNX assets and one CPU model instance per backend process."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
import math
from threading import Lock

from app.core.config import BACKEND_DIR

MODEL_ID = "Supabase/gte-small"
REVISION = "93b36ff09519291b77d6000d2e86bd8565378086"
RECIPE = f"{REVISION}:onnx-fp32:masked-mean:l2:v1"
DIMENSION = 384
MAX_TOKENS = 512
BATCH_SIZE = 8
MODEL_DIR = BACKEND_DIR / ".local" / "models" / "gte-small" / REVISION
ASSETS = {
    "onnx/model.onnx": "398a29991324e0b383afa13375d681ced3079c83e097fb1ebd9290d7498523b3",
    "tokenizer.json": "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0",
}


class EmbeddingError(ValueError):
    """Safe category only, with no source/model/connection content."""


def validate_vector(vector):
    try:
        numbers = [float(x) for x in vector]
    except (ValueError,TypeError,OverflowError):
        raise EmbeddingError("invalid_vector") from None
    if len(numbers) != DIMENSION or any(not math.isfinite(x) for x in numbers):
        raise EmbeddingError("invalid_vector")
    norm = math.hypot(*numbers)
    if norm <= 0 or not math.isfinite(norm) or abs(norm - 1) > 0.001:
        raise EmbeddingError("invalid_vector")
    return numbers


def verify_assets(directory=MODEL_DIR):
    for name, expected in ASSETS.items():
        path = directory / name
        if not path.is_file():
            raise EmbeddingError("model_not_installed")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise EmbeddingError("model_checksum_failed")


class LocalEmbedder:
    def __init__(self, directory=MODEL_DIR):
        self.directory = directory
        self.session = self.tokenizer = None
        self.lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-embedding")

    def _load(self):
        if self.session is not None:
            return
        verify_assets(self.directory)
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError:
            raise EmbeddingError("dependencies_missing") from None
        tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
        tokenizer.no_truncation()
        tokenizer.enable_padding(direction="right", pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.log_severity_level = 4
        session = ort.InferenceSession(str(self.directory / "onnx/model.onnx"),
                                       sess_options=options, providers=["CPUExecutionProvider"])
        self.tokenizer, self.session = tokenizer, session

    def embed_sync(self, texts):
        if not 1 <= len(texts) <= BATCH_SIZE or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise EmbeddingError("invalid_input")
        if any(len(t) > 64000 for t in texts):
            raise EmbeddingError("input_too_long")
        with self.lock:
            self._load()
            import numpy as np
            # The repository tokenizer supplies WordPiece normalization and CLS/SEP.
            # No prefix, stripping, lowercasing or truncation is added by our code.
            encoded = self.tokenizer.encode_batch(texts, add_special_tokens=True)
            if any(len(e.ids) > MAX_TOKENS for e in encoded):
                raise EmbeddingError("input_too_long")
            arrays = {
                "input_ids": np.asarray([e.ids for e in encoded], dtype=np.int64),
                "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype=np.int64),
                "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype=np.int64),
            }
            feeds = {item.name: arrays[item.name] for item in self.session.get_inputs()}
            hidden = self.session.run(None, feeds)[0]
            if hidden.shape != (*arrays["input_ids"].shape, DIMENSION):
                raise EmbeddingError("invalid_vector")
            mask = arrays["attention_mask"][..., None]
            pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            if not np.all(np.isfinite(pooled)) or np.any(norms <= 0):
                raise EmbeddingError("invalid_vector")
            return [validate_vector(v) for v in (pooled / norms).tolist()]

    async def embed(self, texts):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self.executor, self.embed_sync, texts)
        except EmbeddingError:
            raise
        except Exception:
            raise EmbeddingError("inference_failed") from None


@lru_cache(maxsize=1)
def get_embedder():
    return LocalEmbedder()
