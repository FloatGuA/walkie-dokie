"""Retrieval provider contracts and the local BM25 diagnostic baseline."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

import jieba
from rank_bm25 import BM25Okapi

from .domain import normalize_text

jieba.setLogLevel(30)
_EXACT_TOKEN_RE = re.compile(
    r"第[〇零一二三四五六七八九十百千万两0-9]+条|[A-Za-z]+[-_/A-Za-z0-9]*|\d+(?:\.\d+)?%?"
)


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    retrieval_id: str
    evidence_id: str
    text: str
    metadata: dict


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    rank: int
    retrieval_id: str
    evidence_id: str
    score: float
    stage_scores: dict[str, float]
    text: str
    metadata: dict


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    provider_name: str
    provider_version: str
    query: str
    query_tokens: tuple[str, ...]
    candidates: tuple[RetrievalCandidate, ...]


@runtime_checkable
class RetrievalProvider(Protocol):
    name: str
    version: str

    def search(
        self,
        *,
        query: str,
        documents: Iterable[RetrievalDocument],
        top_k: int,
    ) -> RetrievalResult: ...


def chinese_search_tokens(text: str) -> tuple[str, ...]:
    normalized = normalize_text(text).casefold()
    segmented = [
        token.strip()
        for token in jieba.lcut(normalized, cut_all=False)
        if token.strip() and not token.isspace()
    ]
    exact = [match.group(0) for match in _EXACT_TOKEN_RE.finditer(normalized)]
    # Preserve order for trace readability and avoid overweighting exact tokens already
    # emitted by jieba.  Token policy itself is versioned with the provider.
    return tuple(dict.fromkeys([*segmented, *exact]))


class LocalChineseBm25Retriever:
    """Transparent diagnostic baseline, not the final Hybrid RAG implementation."""

    name = "local_chinese_bm25"
    version = "0.1-jieba-rank_bm25"

    def search(
        self,
        *,
        query: str,
        documents: Iterable[RetrievalDocument],
        top_k: int = 10,
    ) -> RetrievalResult:
        if not 1 <= top_k <= 50:
            raise ValueError("top_k 必须位于 1 到 50")
        query_tokens = chinese_search_tokens(query)
        corpus = tuple(documents)
        if not query_tokens or not corpus:
            return RetrievalResult(
                self.name, self.version, query, query_tokens, ()
            )
        tokenized_corpus = [list(chinese_search_tokens(item.text)) for item in corpus]
        ranker = BM25Okapi(tokenized_corpus)
        scores = ranker.get_scores(list(query_tokens))
        ranked_indexes = sorted(
            range(len(corpus)), key=lambda index: (-float(scores[index]), index)
        )[:top_k]
        candidates = tuple(
            RetrievalCandidate(
                rank=rank,
                retrieval_id=corpus[index].retrieval_id,
                evidence_id=corpus[index].evidence_id,
                score=float(scores[index]),
                stage_scores={"bm25": float(scores[index])},
                text=corpus[index].text,
                metadata=corpus[index].metadata,
            )
            for rank, index in enumerate(ranked_indexes, start=1)
        )
        return RetrievalResult(
            self.name, self.version, query, query_tokens, candidates
        )
