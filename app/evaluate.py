"""Reference checks and optional live RAG evaluation; never call a model by default."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import platform
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.api import create_app
from app.config import Settings
from app.errors import ServiceError
from app.llm import ANSWER_MODEL, EMBEDDING_MODEL, ModelProvider, OpenAIProvider
from app.observability import current_metrics
from app.parsing import parse_document
from app.schemas import AnswerResult

ROOT = Path(__file__).resolve().parent.parent


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: str
    question: str
    expected_status: Literal["answered", "not_found"]
    answer_groups: list[list[str]]
    evidence_groups: list[list[str]]
    reference_pages: list[int] = Field(default_factory=list)


def normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def matches(text: str, groups: list[list[str]]) -> bool:
    haystack = normalized(text)
    return all(any(normalized(term) in haystack for term in group) for group in groups)


def source_bytes(case: EvaluationCase, source_override: Path | None = None) -> tuple[Path, bytes]:
    # Only an explicit CLI override can select a private/local PDF outside the repo.
    path = source_override.resolve() if source_override else (ROOT / case.source).resolve()
    if source_override is None and not path.is_relative_to(ROOT):
        raise ValueError("Evaluation source must be inside the project")
    with path.open("rb") as stream:
        data = stream.read(Settings().max_document_bytes + 1)
    if len(data) > Settings().max_document_bytes:
        raise ValueError("Evaluation source exceeds the upload limit")
    return path, data


def check_references(cases: list[EvaluationCase], source_override: Path | None = None) -> dict:
    documents = {}
    outcomes = []
    for case in cases:
        if case.source not in documents:
            path, data = source_bytes(case, source_override)
            documents[case.source] = parse_document(data, path.name, Settings())[1]
        passages = documents[case.source]
        if case.reference_pages:
            passages = [passage for passage in passages if passage.page in case.reference_pages]
        found = case.expected_status == "not_found" or matches(
            "\n".join(passage.text for passage in passages), case.evidence_groups
        )
        outcomes.append({"id": case.id, "reference_valid": found})
    return {
        "mode": "reference_check",
        "cases": len(cases),
        "valid": sum(outcome["reference_valid"] for outcome in outcomes),
        "results": outcomes,
        "limitations": "Checks reference evidence locations only. Unsupported cases are manually curated; this is not a model accuracy measurement.",
    }


def score_result(case: EvaluationCase, result: AnswerResult) -> dict:
    expected_state = result.status == case.expected_status
    phrase_coverage = expected_state and (
        case.expected_status == "not_found" or matches(result.answer or "", case.answer_groups)
    )
    quotes_valid = result.status != "answered" or all(
        citation.quote and normalized(citation.quote) in normalized(citation.excerpt)
        for citation in result.citations
    )
    reference_hit = bool(case.reference_pages) and any(
        citation.page in case.reference_pages for citation in result.citations
    )
    return {
        "id": case.id,
        "expected_state": expected_state,
        "phrase_coverage": phrase_coverage,
        "quotes_valid": bool(quotes_valid),
        "reference_page_hit": reference_hit,
        "result": result.model_dump(),
    }


def estimated_cost(usage: dict) -> float:
    """Standard USD rates per million tokens, verified October 13, 2026.

    Estimate ignores cached-input discounts, taxes and unreported retry usage;
    it is not an invoice or a model accuracy metric.
    """
    return round(
        (
            usage.get("embedding_tokens", 0) * 0.02
            + usage.get("prompt_tokens", 0) * 0.15
            + usage.get("completion_tokens", 0) * 0.60
        )
        / 1_000_000,
        8,
    )


class _RequestMeasurements(logging.Handler):
    """Collect existing HTTP summary events without logging prompts or credentials."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: dict[str, dict] = {}

    def emit(self, record: logging.LogRecord) -> None:
        metrics = current_metrics.get()
        if record.getMessage() == "http_request" and metrics is not None:
            self.requests[metrics.request_id] = record.summary


async def evaluate_live(
    cases: list[EvaluationCase],
    repeat: int = 1,
    source_override: Path | None = None,
    provider: ModelProvider | None = None,
) -> dict:
    settings = Settings.from_env()
    active_provider = provider or OpenAIProvider(settings)
    app = create_app(settings, active_provider)
    measurements = _RequestMeasurements()
    logger = logging.getLogger("document_rag")
    logger.addHandler(measurements)
    outcomes = []
    runs = []
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://evaluation"
            ) as client,
        ):
            for source in dict.fromkeys(case.source for case in cases):
                group = [case for case in cases if case.source == source]
                path, data = source_bytes(group[0], source_override)
                questions = json.dumps(
                    {"questions": [{"id": case.id, "question": case.question} for case in group]}
                ).encode()
                for iteration in range(repeat):
                    started = time.perf_counter()
                    response = await client.post(
                        "/api/v1/answer",
                        files={
                            "document_file": (path.name, data),
                            "questions_file": ("questions.json", questions, "application/json"),
                        },
                    )
                    latency_ms = round((time.perf_counter() - started) * 1000, 2)
                    payload = response.json()
                    request_id = response.headers["X-Request-ID"]
                    usage = measurements.requests.pop(request_id, {})
                    run = {
                        "source": source,
                        "filename": path.name,
                        "source_sha256": sha256(data).hexdigest(),
                        "source_bytes": len(data),
                        "iteration": iteration + 1,
                        "request_id": request_id,
                        "status": response.status_code,
                        "latency_ms": latency_ms,
                        "usage": usage,
                        "estimated_cost_usd": estimated_cost(usage),
                    }
                    runs.append(run)
                    if response.status_code != 200:
                        run["error"] = payload["error"]
                        break  # Correct a shared request failure before paying for a repeat.
                    run["document"] = payload["document"]
                    results = [AnswerResult.model_validate(result) for result in payload["results"]]
                    outcomes.extend(
                        {
                            **score_result(case, result),
                            "iteration": iteration + 1,
                            "source": source,
                            "question": case.question,
                            "reference_answer_groups": case.answer_groups,
                            "reference_pages": case.reference_pages,
                            "manual_verdict": "not_reviewed",
                        }
                        for case, result in zip(group, results, strict=True)
                    )
    finally:
        logger.removeHandler(measurements)
    expected_results = len(cases) * repeat
    return {
        "mode": "live",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "models": {"answer": ANSWER_MODEL, "embedding": EMBEDDING_MODEL},
        "cases": len(cases),
        "repeat": repeat,
        "completed_results": len(outcomes),
        "expected_state_rate": sum(item["expected_state"] for item in outcomes) / expected_results,
        "reference_phrase_rate": sum(item["phrase_coverage"] for item in outcomes)
        / expected_results,
        "runs": runs,
        "results": outcomes,
        "limitations": "Latency includes in-process multipart API validation, parsing, retrieval, OpenAI network calls and response serialization; excludes client upload network, local file reads and interpreter startup. Cost uses visible usage and standard rates, ignores cached-input discounts/taxes/unreported retries. Phrase coverage is a diagnostic proxy, not semantic correctness. Manual verdicts remain not_reviewed until answers and citations are checked against the source.",
    }


def markdown_report(report: dict) -> str:
    def cell(value) -> str:
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = [
        "# Live PDF evaluation evidence",
        "",
        report["limitations"],
        "",
        f"Generated: {report['generated_at_utc']}. Runtime: `{report['runtime']}`.",
        "",
        "## Measured API requests",
        "",
        "| Source / run | HTTP | Cache | Latency (ms) | Embedding tokens | Prompt tokens | Output tokens | Estimated USD |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in report["runs"]:
        usage = run["usage"]
        lines.append(
            f"| {cell(run['filename'])} / {run['iteration']} | {run['status']} | {usage.get('cache_status', 'unknown')} | {run['latency_ms']} | {usage.get('embedding_tokens', 0)} | {usage.get('prompt_tokens', 0)} | {usage.get('completion_tokens', 0)} | {run['estimated_cost_usd']:.8f} |"
        )
        if run.get("error"):
            lines.extend(
                [
                    "",
                    f"Request failed: `{cell(run['error']['code'])}` — {cell(run['error']['message'])}",
                    "",
                ]
            )
    lines.extend(
        [
            "",
            "## Answer and citation review",
            "",
            "Check each answer's factual correctness, completeness, and cited support in the PDF. Mark the verdict Correct or Incorrect and explain failures. Do not equate phrase matches with semantic accuracy. Include every question, including processing failures and unsupported questions.",
            "",
            "| Question / run | Expected reference phrases / pages | Live answer | Citation and supporting quote | Reference check | Manual verdict |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for outcome in report["results"]:
        result = outcome["result"]
        citations = "; ".join(
            f"{citation['source']} page {citation['page']} ({citation['passage_id']}): {citation.get('quote') or ''}"
            if citation["page"]
            else f"{citation['source']} {citation['json_path']}: {citation.get('quote') or ''}"
            for citation in result["citations"]
        )
        answer = (
            result["answer"]
            if result["status"] != "error"
            else f"Processing error: {result['error']['code']}"
        )
        check = (
            "State and phrases match" if outcome["phrase_coverage"] else "Mismatch; inspect source"
        )
        reference = (
            "; ".join(" or ".join(group) for group in outcome["reference_answer_groups"])
            or "Data-Not-Found"
        )
        reference += f" / PDF pages {outcome['reference_pages']}"
        lines.append(
            f"| {cell(outcome['question'])} / {outcome['iteration']} | {cell(reference)} | {cell(answer)} | {cell(citations or 'None')} | {check} | Not reviewed |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluations/cases.json")
    parser.add_argument("--live", action="store_true", help="Use OpenAI; incurs API usage")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path, help="Write a live-answer manual review table")
    parser.add_argument(
        "--repeat",
        type=int,
        choices=(1, 2, 3),
        default=1,
        help="Repeat each source in the same cache; 2 measures cold then cached",
    )
    parser.add_argument("--source-filter", help="Select one source path from the dataset")
    parser.add_argument(
        "--source",
        type=Path,
        help="Explicit local source override; requires a single-source dataset or filter",
    )
    args = parser.parse_args()
    cases = [
        EvaluationCase.model_validate(item)
        for item in json.loads(args.dataset.read_text(encoding="utf-8"))
    ]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise SystemExit("Evaluation cases must be nonempty and have unique IDs")
    if args.source_filter:
        cases = [case for case in cases if case.source == args.source_filter]
        if not cases:
            raise SystemExit("Source filter does not match the dataset")
    if args.source and len({case.source for case in cases}) != 1:
        raise SystemExit("--source requires exactly one dataset source")
    if args.markdown and not args.live:
        raise SystemExit("--markdown requires --live; reference checks are not live answers")
    # Validate the actual source/reference anchors before any paid model calls.
    try:
        references = check_references(cases, args.source)
    except (OSError, ServiceError) as exc:
        message = exc.message if isinstance(exc, ServiceError) else str(exc)
        raise SystemExit(f"Cannot validate evaluation source: {message}") from None
    if references["valid"] != references["cases"]:
        raise SystemExit(
            "Reference evidence does not match this source; review the dataset before a live run"
        )
    if args.live and not os.getenv("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = getpass.getpass("OpenAI API key (input hidden): ").strip()
    report = (
        asyncio.run(evaluate_live(cases, args.repeat, args.source)) if args.live else references
    )
    serialized = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown_report(report), encoding="utf-8")
    if not args.live and report["valid"] != report["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
