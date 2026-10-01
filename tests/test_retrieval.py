from app.retrieval import SearchIndex
from app.schemas import Passage


def test_hybrid_ranking_finds_exact_provider_term() -> None:
    passages = [
        Passage("p1", "Employee holidays and travel policy", "source.pdf", page=1),
        Passage("p2", "GCP is the cloud hosting provider", "source.pdf", page=2),
        Passage("p3", "Network equipment maintenance", "source.pdf", page=3),
    ]
    embeddings = [[0.1, 1], [1, 0], [0.2, 0.8]]
    index = SearchIndex.build(passages, embeddings)
    assert index.search("Which cloud provider uses GCP?", [1, 0], top_k=1)[0].id == "p2"
