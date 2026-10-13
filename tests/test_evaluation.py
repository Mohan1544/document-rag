import json

import pytest

from app.evaluate import (
    ROOT,
    EvaluationCase,
    check_references,
    estimated_cost,
    evaluate_live,
    markdown_report,
    matches,
    score_result,
    source_bytes,
)
from app.schemas import AnswerResult, Citation


def test_curated_reference_locations_match_bundled_documents() -> None:
    cases = [
        EvaluationCase.model_validate(case)
        for case in json.loads((ROOT / "evaluations/cases.json").read_text())
    ]
    report = check_references(cases)
    assert report["cases"] == 20
    assert report["valid"] == 20


def test_evaluation_requires_all_answer_parts_and_counts_errors_as_failures() -> None:
    assert matches("Five minutes for SEV-1 and 15 minutes for SEV-2", [["five", "5"], ["15"]])
    assert not matches("Five minutes for SEV-1", [["five", "5"], ["15"]])
    case = EvaluationCase(
        id="q",
        source="x",
        question="Cloud?",
        expected_status="answered",
        answer_groups=[["GCP"]],
        evidence_groups=[["GCP"]],
    )
    result = AnswerResult(
        id="q",
        question="Cloud?",
        answer="AWS",
        comments="",
        status="answered",
        confidence="high",
        citations=[Citation(passage_id="p", source="x", excerpt="GCP", quote="GCP")],
    )
    assert score_result(case, result)["phrase_coverage"] is False


def test_evaluation_rejects_paths_outside_project() -> None:
    case = EvaluationCase(
        id="q",
        source="../private.json",
        question="?",
        expected_status="not_found",
        answer_groups=[],
        evidence_groups=[],
    )
    with pytest.raises(ValueError, match="inside"):
        source_bytes(case)


@pytest.mark.asyncio
async def test_live_runner_measures_real_api_cache_and_leaves_accuracy_unreviewed(fake_provider):
    case = EvaluationCase(
        id="cloud",
        source="examples/basic-json/source.json",
        question="Which cloud?",
        expected_status="answered",
        answer_groups=[["Google Cloud", "GCP"]],
        evidence_groups=[["GCP"]],
    )
    report = await evaluate_live([case], repeat=2, provider=fake_provider)
    assert [run["usage"]["cache_status"] for run in report["runs"]] == ["miss", "hit"]
    assert all(run["status"] == 200 and run["latency_ms"] > 0 for run in report["runs"])
    assert fake_provider.embedding_batches == [3, 1, 1]
    assert report["completed_results"] == 2
    assert all(result["manual_verdict"] == "not_reviewed" for result in report["results"])
    markdown = markdown_report(report)
    assert "Not reviewed" in markdown and "Google Cloud Platform" in markdown
    assert "GCP" in markdown and "Manual verdict" in markdown


def test_cost_estimate_uses_separate_embedding_input_and_output_rates():
    # One million tokens in each category provides a scale/rate sanity check.
    assert estimated_cost(
        {"embedding_tokens": 1_000_000, "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    ) == pytest.approx(0.77)


def test_explicit_local_source_override_is_bounded(tmp_path):
    path = tmp_path / "source.json"
    path.write_bytes(b'{"cloud":"GCP"}')
    case = EvaluationCase(
        id="q",
        source="placeholder.pdf",
        question="?",
        expected_status="answered",
        answer_groups=[["GCP"]],
        evidence_groups=[["GCP"]],
    )
    assert source_bytes(case, path)[0] == path.resolve()
    assert check_references([case], path)["valid"] == 1
