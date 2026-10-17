"""Policy clause retrieval with citations.

  * VertexSearchRetriever - Vertex AI Search (Discovery Engine) over policies indexed in a data
    store with layout-based chunking (scripts/index_policies.py). Indexing takes minutes, so it
    serves the pre-indexed sample policies; hits are matched to the uploaded policy by file name,
    and a policy with no hits (a fresh upload) goes to the local retriever.
  * LocalRetriever - BM25 over the clauses parsed from the uploaded policy.
Both return Clause objects with clause id, title, page and source for the citation.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter

from ..models import Clause, PolicyDoc

TOKEN = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "of", "to", "and", "or", "is", "are", "in", "for", "on", "by", "be", "any", "this", "under", "per"}


def _tokens(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP]


class LocalRetriever:
    name = "BM25 over parsed clauses (local)"

    def __init__(self, min_score: float | None = None) -> None:
        configured = os.getenv("CLAIMSHIELD_RETRIEVAL_MIN_SCORE", "0.5")
        self.min_score = float(configured) if min_score is None else float(min_score)
        if self.min_score < 0:
            raise ValueError("retrieval minimum score must be non-negative")

    def search(self, policy: PolicyDoc, query: str, top_k: int = 3) -> list[Clause]:
        docs = [(c, _tokens(f"{c.title} {c.title} {c.text}")) for c in policy.clauses]
        if not docs:
            return []
        avg = sum(len(d) for _, d in docs) / len(docs)
        df = Counter(t for _, d in docs for t in set(d))
        q = _tokens(query)
        k1, b, n = 1.4, 0.75, len(docs)
        scored = []
        for clause, toks in docs:
            tf = Counter(toks)
            score = 0.0
            for term in q:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(toks) / avg))
            if score >= self.min_score and score > 0:
                scored.append(clause.model_copy(update={"score": round(score, 3)}))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:top_k]


class VertexSearchRetriever:
    name = "Vertex AI Search"

    def __init__(self, project: str, location: str, datastore_id: str) -> None:
        from google.api_core.client_options import ClientOptions
        from google.cloud import discoveryengine_v1 as de

        self._de = de
        opts = ClientOptions(api_endpoint=f"{location}-discoveryengine.googleapis.com") if location != "global" else None
        self._client = de.SearchServiceClient(client_options=opts)
        self._serving_config = (
            f"projects/{project}/locations/{location}/collections/default_collection/"
            f"dataStores/{datastore_id}/servingConfigs/default_search"
        )
        self._fallback = LocalRetriever()
        self.last_backend = self.name

    def search(self, policy: PolicyDoc, query: str, top_k: int = 3) -> list[Clause]:
        de = self._de
        request = de.SearchRequest(
            serving_config=self._serving_config,
            query=query,
            page_size=top_k * 4,
            content_search_spec=de.SearchRequest.ContentSearchSpec(
                search_result_mode=de.SearchRequest.ContentSearchSpec.SearchResultMode.CHUNKS,
            ),
        )
        out: list[Clause] = []
        for result in self._client.search(request).results:
            chunk = result.chunk
            # Filenames are not document identities. Vertex hits must map back to the
            # selected parsed document's exact clause text before they can be used.
            clause = self._to_clause(policy, chunk.content or "", chunk)
            if clause is not None:
                out.append(clause)
        # A policy that isn't indexed (a fresh upload) gets no hits: use the local index for it.
        self.last_backend = self.name if out else f"{self._fallback.name} (not indexed in Vertex AI Search)"
        return out[:top_k] or self._fallback.search(policy, query, top_k)

    def _to_clause(self, policy: PolicyDoc, text: str, chunk) -> Clause | None:
        """Map a chunk back onto the parsed clause it came from, so citations carry clause ids."""
        m = re.search(r"(\d+\.\d+)\s+[A-Z]", text)
        if m:
            for c in policy.clauses:
                normalized_chunk = " ".join(text.split()).casefold()
                normalized_clause = " ".join(c.text.split()).casefold()
                if (c.clause_id == m.group(1) and normalized_chunk and normalized_clause
                        and (normalized_chunk in normalized_clause or normalized_clause in normalized_chunk)):
                    return c.model_copy(update={"score": 1.0})
        return None
