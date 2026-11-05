"""Unit tests mock inference/DB. The explicitly enabled model test uses real ONNX assets."""

import asyncio
import hashlib
import math
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.embedding_model import EmbeddingError, LocalEmbedder, get_embedder, validate_vector
from app.services import embedding_service, retrieval_service


def unit_vector():
    return [1.0] + [0.0] * 383


@pytest.mark.parametrize("vector", [[0.0]*384, [1.0]*3, [float('nan')]*384,
                                  [float('inf')]*384, [1.0]*384, [1e308]*384, ['invalid']*384])
def test_invalid_embeddings_are_rejected(vector):
    with pytest.raises(EmbeddingError, match="invalid_vector"):
        validate_vector(vector)


def test_masked_pooling_excludes_padding_and_normalizes():
    import numpy as np
    model = LocalEmbedder()
    model._load = lambda: None
    model.tokenizer = SimpleNamespace(encode_batch=lambda texts, **kwargs: [SimpleNamespace(
        ids=[101, 102, 0], attention_mask=[1,1,0], type_ids=[0,0,0])])
    hidden = np.zeros((1,3,384))
    hidden[0,0,0] = 2
    hidden[0,1,1] = 2
    hidden[0,2,2] = 1000  # must not affect pooling
    model.session = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name='input_ids'),
        SimpleNamespace(name='attention_mask')], run=lambda *args: [hidden])
    result = model.embed_sync(["fixture"])[0]
    assert result[0] == pytest.approx(1/math.sqrt(2))
    assert result[1] == pytest.approx(1/math.sqrt(2)) and result[2] == 0
    model.executor.shutdown()


def test_token_limit_is_explicit_and_never_silently_truncated():
    model = LocalEmbedder()
    model._load = lambda: None
    model.tokenizer = SimpleNamespace(encode_batch=lambda texts, **kwargs: [SimpleNamespace(ids=[1]*513)])
    with pytest.raises(EmbeddingError, match="input_too_long"):
        model.embed_sync(["oversize fixture"])
    model.executor.shutdown()


def test_worker_failure_retries_existing_memory_and_stale_result_is_discarded(monkeypatch):
    async def run():
        job = dict(id="memory-1", tenant_id="tenant-1", room_id="room-1", message_id="source-1",
                   content="Original fixture", content_hash="hash-1", claim_token="claim-1")
        queued = [[job], [job], []]
        completed, calls = [], []

        async def claim(settings):
            return queued.pop(0)

        async def finish(settings, row, **kwargs):
            completed.append((row["id"], row["content_hash"], kwargs))
            return len(completed) == 1  # second claim's result is stale

        async def embed(texts):
            calls.append(texts)
            if len(calls) == 1:
                raise EmbeddingError("inference_failed")
            return [unit_vector()]

        monkeypatch.setattr(embedding_service, "claim_embeddings", claim)
        monkeypatch.setattr(embedding_service, "finish_embedding", finish)
        monkeypatch.setattr(embedding_service, "get_embedder", lambda: SimpleNamespace(embed=embed))
        worker = embedding_service.EmbeddingWorker()
        settings = Settings(_env_file=None)
        assert await worker.process_once(settings) == 1
        assert await worker.process_once(settings) == 1
        assert await worker.process_once(settings) == 0
        assert completed[0][2] == {"error":"inference_failed"}
        assert completed[1][0:2] == ("memory-1", "hash-1")
        assert completed[1][2]["vector"] == unit_vector()
    asyncio.run(run())


def test_semantic_retrieval_preserves_source_and_never_uses_keyword_label(monkeypatch):
    async def run():
        seen = []

        async def embed(texts):
            return [unit_vector()]

        async def search(settings, vector, **kwargs):
            seen.append(kwargs)
            return [dict(id="memory-1",message_id="source-1",content="Planning Friday",similarity=.9)]

        monkeypatch.setattr(retrieval_service, "get_embedder", lambda: SimpleNamespace(embed=embed))
        monkeypatch.setattr(retrieval_service, "search_embeddings", search)
        async def context(*args):
            return {"commitments":[],"decisions":[],"messages":[]}
        monkeypatch.setattr(retrieval_service, "source_context", context)
        settings = Settings(_env_file=None).model_copy(update={"local_embeddings_enabled":True})
        result = await retrieval_service.query_team_memory(settings, search_query="When do we plan?",switch_room_id="test-room")
        assert result["memories"][0]["message_id"] == "source-1"
        assert result["retrieval"][0]["mode"] == "semantic"
        assert seen == [{"switch_room_id":"test-room"}]
    asyncio.run(run())


def test_embedding_unavailable_uses_explicit_keyword_fallback_and_scope_failure_does_not(monkeypatch):
    async def run():
        async def embed(texts):
            raise EmbeddingError("model_not_installed")

        async def keyword(settings, **kwargs):
            return {"memories": [{"message_id":"source-1","content":"Friday"}]}

        monkeypatch.setattr(retrieval_service,"get_embedder",lambda: SimpleNamespace(embed=embed))
        monkeypatch.setattr(retrieval_service,"keyword_query",keyword)
        settings = Settings(_env_file=None).model_copy(update={"local_embeddings_enabled":True})
        result = await retrieval_service.query_team_memory(settings,search_query="When?")
        assert result["retrieval"] == [{"mode":"keyword_fallback","reason":"model_not_installed"}]

        async def valid(texts):
            return [unit_vector()]

        async def denied(*args,**kwargs):
            raise HTTPException(403,"denied")

        monkeypatch.setattr(retrieval_service,"get_embedder",lambda: SimpleNamespace(embed=valid))
        monkeypatch.setattr(retrieval_service,"search_embeddings",denied)
        with pytest.raises(HTTPException) as error:
            await retrieval_service.query_team_memory(settings,search_query="When?")
        assert error.value.status_code == 403
    asyncio.run(run())


@pytest.mark.skipif(os.environ.get("RUN_LOCAL_MODEL_TESTS") != "1", reason="Set RUN_LOCAL_MODEL_TESTS=1 after public asset setup")
def test_real_pinned_onnx_model_vectors_padding_similarity_and_reuse():
    model = get_embedder()
    texts = ["The backend web API will use FastAPI.","Which framework powers our backend API?",
             "The cafeteria serves noodles for lunch."]
    vectors = model.embed_sync(texts)
    session = model.session
    repeated = model.embed_sync([texts[0]])[0]
    assert model.session is session
    assert all(len(v)==384 and abs(sum(x*x for x in v)-1)<1e-5 for v in vectors)
    assert repeated == pytest.approx(vectors[0],abs=1e-5)
    positive = sum(a*b for a,b in zip(vectors[0],vectors[1]))
    negative = sum(a*b for a,b in zip(vectors[0],vectors[2]))
    assert positive > negative+.05
    with pytest.raises(EmbeddingError,match="input_too_long"):
        model.embed_sync(["word "*600])
