import json
from io import BytesIO

import pytest
from reportlab.pdfgen import canvas

from app.config import Settings
from app.errors import ServiceError
from app.parsing import parse_document, parse_questions


def test_questions_accept_strings_and_objects_and_preserve_order() -> None:
    payload = json.dumps(
        {"questions": ["First?", {"id": "cloud", "question": "Which cloud?"}]}
    ).encode()
    questions = parse_questions(payload, Settings())
    assert [(question.id, question.text) for question in questions] == [
        ("1", "First?"),
        ("cloud", "Which cloud?"),
    ]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"{broken", "invalid_questions_json"),
        (b'{"questions": []}', "invalid_questions"),
        (b'{"questions": [""]}', "empty_question"),
        (
            b'{"questions": [{"id":"x","question":"A"},{"id":"x","question":"B"}]}',
            "duplicate_question_id",
        ),
        (
            json.dumps({"questions": [{"id": "x" * 101, "question": "A"}]}).encode(),
            "question_id_too_long",
        ),
    ],
)
def test_questions_validation(payload: bytes, code: str) -> None:
    with pytest.raises(ServiceError) as error:
        parse_questions(payload, Settings())
    assert error.value.code == code


def test_json_source_keeps_qa_record_and_path() -> None:
    source = json.dumps(
        {"policies": [{"id": "p1", "question": "Cloud?", "answer": "GCP", "comments": "US"}]}
    ).encode()
    kind, passages = parse_document(source, "source.json", Settings())
    assert kind == "json"
    assert passages[0].json_path == "$.policies[0]"
    assert "Cloud?" in passages[0].text
    assert "GCP" in passages[0].text


def test_json_source_rejects_oversized_citation_path() -> None:
    source = json.dumps({"x" * 501: {"answer": "value"}}).encode()
    with pytest.raises(ServiceError) as error:
        parse_document(source, "source.json", Settings())
    assert error.value.code == "json_path_too_long"


def test_pdf_citation_uses_one_based_page(pdf_bytes: bytes) -> None:
    kind, passages = parse_document(pdf_bytes, "report.pdf", Settings())
    assert kind == "pdf"
    assert any(passage.page == 2 and "GCP" in passage.text for passage in passages)


def test_invalid_pdf_has_clear_error() -> None:
    with pytest.raises(ServiceError) as error:
        parse_document(b"not a pdf", "report.pdf", Settings())
    assert error.value.code == "invalid_pdf"


def test_scanned_pdf_reports_missing_text() -> None:
    output = BytesIO()
    page = canvas.Canvas(output)
    page.showPage()
    page.save()
    with pytest.raises(ServiceError) as error:
        parse_document(output.getvalue(), "scan.pdf", Settings())
    assert error.value.code == "no_extractable_text"


def test_pdf_page_limit(pdf_bytes: bytes) -> None:
    with pytest.raises(ServiceError) as error:
        parse_document(pdf_bytes, "report.pdf", Settings(max_pdf_pages=1))
    assert error.value.code == "too_many_pages"
