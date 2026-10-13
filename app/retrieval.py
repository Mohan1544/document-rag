"""Request-scoped hybrid search over an in-memory vector index."""

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

from app.schemas import Passage


def _terms(text: str) -> list[str]:
    return re.findall(r"[\w-]+", text.lower())


def _unit(vector: list[float]) -> tuple[float, ...]:
    magnitude = math.hypot(*vector)
    return tuple(value / magnitude for value in vector)


def validate_embeddings(embeddings: list[list[float]], count: int, width: int | None = None) -> int:
    """Reject unusable vectors before they can silently corrupt evidence ranking."""
    if not isinstance(embeddings, list) or len(embeddings) != count or not embeddings:
        raise ValueError("Each input needs one embedding")
    if not isinstance(embeddings[0], list):
        # A uniform validation exception lets adapters safely map malformed upstream data.
        raise ValueError("Embeddings must be numeric vectors")  # noqa: TRY004
    expected_width = width if width is not None else len(embeddings[0])
    for vector in embeddings:
        if not isinstance(vector, list) or not expected_width or len(vector) != expected_width:
            raise ValueError("Embeddings have inconsistent dimensions")
        try:
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                raise ValueError("Embeddings must contain finite numbers")
            magnitude = math.hypot(*vector)
            if not magnitude or not math.isfinite(magnitude):
                raise ValueError("Embeddings must have a finite nonzero magnitude")
        except (TypeError, OverflowError) as exc:
            raise ValueError("Embeddings must contain finite numbers") from exc
    return expected_width


@dataclass(frozen=True)
class SearchIndex:
    """Per-document search data for semantic matches and exact terminology."""

    passages: tuple[Passage, ...]
    vectors: np.ndarray
    term_counts: tuple[Counter[str], ...]
    document_frequencies: Counter[str]
    average_length: float

    @classmethod
    def build(cls, passages: list[Passage], embeddings: list[list[float]]) -> "SearchIndex":
        """Precompute normalized vectors and term statistics once per document."""
        validate_embeddings(embeddings, len(passages))
        counts = tuple(Counter(_terms(passage.text)) for passage in passages)
        frequencies: Counter[str] = Counter()
        for count in counts:
            frequencies.update(count.keys())
        average_length = sum(sum(count.values()) for count in counts) / len(counts)
        vectors = np.asarray([_unit(vector) for vector in embeddings], dtype=np.float32)
        vectors.flags.writeable = False
        return cls(
            passages=tuple(passages),
            vectors=vectors,
            term_counts=counts,
            document_frequencies=frequencies,
            average_length=average_length,
        )

    def search(self, question: str, embedding: list[float], top_k: int) -> list[Passage]:
        """Return citable passages ranked by cosine similarity and BM25 term matches."""
        validate_embeddings([embedding], 1, len(self.vectors[0]))
        query_vector = _unit(embedding)
        # Native vector math releases the GIL and stores a dense float32 matrix
        # instead of hundreds of thousands of Python float objects.
        semantic = self.vectors @ np.asarray(query_vector, dtype=np.float32)
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
