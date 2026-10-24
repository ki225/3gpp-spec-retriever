"""Optional BM25 + BGE-M3 ANN retrieval; SQLite owns the source evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from uuid import uuid4

from spec_retriever import __version__
from spec_retriever.spec_store.store import Corpus


class BGEEmbedder:
    dimensions = 1024

    def __init__(self, model: str = "BAAI/bge-m3", device: str = "cpu"):
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as error:
            raise ValueError("install the 'elastic' extra to use BGE-M3") from error
        self.model_name = model
        self.model = BGEM3FlagModel(model, devices=device, use_fp16=device.startswith("cuda"))

    def encode(self, texts: list[str]) -> list[list[float]]:
        lengths = self._validate_lengths(texts)
        vectors = [None] * len(texts)
        # Small paragraphs can share a batch; long tables stay in small batches
        # to avoid padding every short passage to a multi-thousand-token table.
        for minimum, maximum, batch_size in [(0, 256, 16), (257, 1024, 4), (1025, 8192, 1)]:
            indexes = [index for index, length in enumerate(lengths) if minimum <= length <= maximum]
            if not indexes:
                continue
            encoded = self.model.encode([texts[index] for index in indexes], batch_size=batch_size, max_length=8192,
                                        return_dense=True, return_sparse=False,
                                        return_colbert_vecs=False)["dense_vecs"].tolist()
            if len(encoded) != len(indexes):
                raise ValueError("embedding output shape does not match inputs")
            for index, vector in zip(indexes, encoded):
                vectors[index] = vector
        return vectors

    def _validate_lengths(self, texts: list[str]):
        lengths = []
        for text in texts:
            length = len(self.model.tokenizer(text, truncation=False)["input_ids"])
            if length > 8192:
                raise ValueError("BGE-M3 input exceeds 8192 tokens; use lexical retrieval or smaller source tables")
            lengths.append(length)
        return lengths

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        self._validate_lengths([query, *texts])
        query_vectors = self.model.encode([query], return_dense=False, return_sparse=False,
                                          return_colbert_vecs=True, max_length=8192)["colbert_vecs"][0]
        vectors = self.model.encode(texts, batch_size=2, return_dense=False, return_sparse=False,
                                    return_colbert_vecs=True, max_length=8192)["colbert_vecs"]
        return [float(self.model.colbert_score(query_vectors, vector)) for vector in vectors]


def create_client():
    try:
        from elasticsearch import Elasticsearch
    except ImportError as error:
        raise ValueError("install the 'elastic' extra to use Elasticsearch") from error
    url = os.environ.get("ELASTICSEARCH_URL")
    if not url:
        raise ValueError("ELASTICSEARCH_URL is not configured")
    options = {"api_key": os.environ["ELASTIC_API_KEY"]} if os.environ.get("ELASTIC_API_KEY") else {}
    return Elasticsearch(url, **options)


def _fingerprint(status: dict, model_name: str) -> str:
    payload = {"corpus": status, "parser_version": __version__, "model": model_name}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ElasticBackend:
    def __init__(self, client, embedder, *, prefix: str = "3gpp-specs"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", prefix):
            raise ValueError("Elasticsearch prefix must be 1-40 lowercase letters, digits, underscores or hyphens")
        self.client, self.embedder, self.prefix = client, embedder, prefix

    def _alias(self, baseline: str) -> str:
        return self.prefix + "-" + hashlib.sha256(baseline.encode()).hexdigest()[:16]

    def sync(self, corpus: Corpus, baseline: str) -> dict:
        from elasticsearch.helpers import bulk

        status = corpus.status(baseline)
        fingerprint = _fingerprint(status, self.embedder.model_name)
        alias = self._alias(baseline)
        staging = alias + "-" + uuid4().hex
        self.client.indices.create(index=staging, mappings={
            "_meta": {"fingerprint": fingerprint}, "dynamic": "strict",
            "properties": {
                "baseline": {"type": "keyword"}, "spec": {"type": "keyword"},
                "version": {"type": "keyword"}, "text": {"type": "text"},
                "embedding": {"type": "dense_vector", "dims": self.embedder.dimensions,
                              "index": True, "similarity": "cosine", "index_options": {"type": "hnsw"}},
                "evidence": {"type": "object", "enabled": False},
            }})

        def actions():
            batch = []
            for evidence in corpus.export_chunks(baseline):
                batch.append(evidence)
                if len(batch) == 32:
                    yield from self._actions(staging, batch)
                    batch = []
            if batch:
                yield from self._actions(staging, batch)

        publication_started = False
        try:
            count, _ = bulk(self.client, actions(), refresh="wait_for")
            # An alias switch makes only a fully populated generation visible.
            changes = []
            if self.client.indices.exists_alias(name=alias):
                changes = [{"remove": {"index": name, "alias": alias}}
                           for name in self.client.indices.get_alias(name=alias)]
            changes.append({"add": {"index": staging, "alias": alias}})
            publication_started = True
            self.client.indices.update_aliases(actions=changes)
        except Exception:
            # A network error during the alias request does not prove it failed
            # on the server. Keep that generation for inspection/retry.
            if not publication_started:
                self.client.indices.delete(index=staging)
            raise
        return {"backend": "elasticsearch_bge_m3", "baseline": baseline,
                "indexed_chunks": count, "fingerprint": fingerprint, "corpus_status": status}

    def _actions(self, index: str, batch: list[dict]):
        vectors = self.embedder.encode([item["search_text"] for item in batch])
        if len(vectors) != len(batch) or any(len(vector) != self.embedder.dimensions for vector in vectors):
            raise ValueError("embedding output shape does not match source chunks")
        for item, vector in zip(batch, vectors):
            yield {"_index": index, "_id": item["chunk_id"], "_source": {
                "baseline": item["baseline"], "spec": item["spec"], "version": item["version"],
                "text": item["search_text"], "embedding": vector, "evidence": item}}

    def search(self, corpus: Corpus, query: str, baseline: str, *, limit: int = 20,
               spec: str | None = None, candidates: int = 200, rerank: bool = False,
               rerank_candidates: int = 40) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise ValueError("query must contain 1-4096 characters")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        if type(candidates) is not int or not limit <= candidates <= 1000:
            raise ValueError("candidates must be between limit and 1000")
        if rerank and (type(rerank_candidates) is not int or not limit <= rerank_candidates <= 200):
            raise ValueError("rerank_candidates must be between limit and 200")
        status = corpus.status(baseline)
        alias = self._alias(baseline)
        mappings = self.client.indices.get_mapping(index=alias)
        expected = _fingerprint(status, self.embedder.model_name)
        if len(mappings) != 1 or any(value["mappings"].get("_meta", {}).get("fingerprint") != expected
                                     for value in mappings.values()):
            raise ValueError("Elasticsearch snapshot is stale; run sync-elastic again")
        filters = [{"term": {"baseline": baseline}}]
        if spec is not None:
            filters.append({"term": {"spec": spec}})
        lexical = self.client.search(index=alias, size=candidates, source_excludes=["embedding"],
                                     query={"bool": {"must": [{"match": {"text": query}}], "filter": filters}})
        vector = self.embedder.encode([query])[0]
        dense = self.client.search(index=alias, size=candidates, source_excludes=["embedding"], knn={
            "field": "embedding", "query_vector": vector, "k": candidates,
            "num_candidates": min(10000, max(candidates * 4, 100)),
            "filter": {"bool": {"filter": filters}}})
        scores, evidence = {}, {}
        hits_per_channel = []
        for response in (lexical, dense):
            hits = response["hits"]["hits"]
            hits_per_channel.append(len(hits))
            for rank, hit in enumerate(hits, 1):
                chunk_id = hit["_id"]
                scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
                evidence[chunk_id] = hit["_source"]["evidence"]
        ranked = sorted(scores, key=lambda item: (-scores[item], item))
        rerank_scores = {}
        if rerank and ranked:
            selected = ranked[:rerank_candidates]
            values = self.embedder.rerank(query, [evidence[item]["search_text"] for item in selected])
            if len(values) != len(selected):
                raise ValueError("reranking output shape does not match candidates")
            rerank_scores = dict(zip(selected, values))
            ranked = sorted(selected, key=lambda item: (-rerank_scores[item], item))
        results, seen = [], set()
        for chunk_id in ranked:
            item = dict(evidence[chunk_id])
            address = (item["spec"], item["version"], item["clause"])
            if address in seen:
                continue
            seen.add(address)
            item.pop("search_text", None)
            item["score"] = scores[chunk_id]
            if rerank:
                item["rerank_score"] = rerank_scores[chunk_id]
            results.append(item)
        return {"query": query, "backend": "elasticsearch_bge_m3",
                "score_meaning": "rrf_rank_not_confidence", "results": results[:limit],
                "truncated": len(results) > limit or (rerank and len(scores) > rerank_candidates)
                    or any(count == candidates for count in hits_per_channel),
                "reranker": "bge_m3_colbert" if rerank else "none",
                "candidate_limit_per_channel": candidates, "approximate_vector_search": True,
                "corpus_status": status}
