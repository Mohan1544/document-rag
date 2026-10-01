"""Request-scoped hybrid search over an in-memory vector index."""

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.schemas import Passage


def _terms(text: str) -> list[str]:
    return re.findall(r"[\w-]+", text.lower())


def _unit(vector: list[float]) -> tuple[float, ...]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return tuple(0.0 for _ in vector)
    return tuple(value / magnitude for value in vector)


@dataclass(frozen=True)
class SearchIndex:
    """Per-document search data for semantic matches and exact terminology."""

    passages: tuple[Passage, ...]
    vectors: tuple[tuple[float, ...], ...]
    term_counts: tuple[Counter[str], ...]
    document_frequencies: Counter[str]
    average_length: float

    @classmethod
    def build(cls, passages: list[Passage], embeddings: list[list[float]]) -> "SearchIndex":
        """Precompute normalized vectors and term statistics once per document."""
        if len(passages) != len(embeddings) or not passages:
            raise ValueError("Each passage needs one embedding")
        width = len(embeddings[0])
        if not width or any(len(vector) != width for vector in embeddings):
            raise ValueError("Embeddings have inconsistent dimensions")
        counts = tuple(Counter(_terms(passage.text)) for passage in passages)
        frequencies: Counter[str] = Counter()
        for count in counts:
            frequencies.update(count.keys())
        average_length = sum(sum(count.values()) for count in counts) / len(counts)
        return cls(
            passages=tuple(passages),
            vectors=tuple(_unit(vector) for vector in embeddings),
            term_counts=counts,
            document_frequencies=frequencies,
            average_length=average_length,
        )

    def search(self, question: str, embedding: list[float], top_k: int) -> list[Passage]:
        """Return citable passages ranked by cosine similarity and BM25 term matches."""
        if len(embedding) != len(self.vectors[0]):
            raise ValueError("Question embedding has the wrong dimension")
        query_vector = _unit(embedding)
        semantic = [
            sum(left * right for left, right in zip(query_vector, vector, strict=True))
            for vector in self.vectors
        ]
        query_terms = set(_terms(question))
        lexical: list[float] = []
        count_documents = len(self.passages)
        for counts in self.term_counts:
            length = sum(counts.values())
            score = 0.0
            for term in query_terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                doc_frequency = self.document_frequencies[term]
                inverse_frequency = math.log(
                    1 + (count_documents - doc_frequency + 0.5) / (doc_frequency + 0.5)
                )
                normalization = frequency + 1.2 * (
                    0.25 + 0.75 * length / max(self.average_length, 1)
                )
                score += inverse_frequency * frequency * 2.2 / normalization
            lexical.append(score)

        semantic_order = sorted(range(count_documents), key=lambda i: semantic[i], reverse=True)
        lexical_order = sorted(
            (i for i in range(count_documents) if lexical[i] > 0),
            key=lambda i: lexical[i],
            reverse=True,
        )
        # Reciprocal rank fusion keeps exact terms useful without relying on raw score scales.
        fused = [0.0] * count_documents
        for rank, index in enumerate(semantic_order, start=1):
            fused[index] += 1 / (60 + rank)
        for rank, index in enumerate(lexical_order, start=1):
            fused[index] += 1 / (60 + rank)
        ranked = sorted(range(count_documents), key=lambda i: fused[i], reverse=True)
        return [self.passages[index] for index in ranked[:top_k]]
