"""Probe a running container without credentials; uses only the Python standard library."""

import argparse
import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile


def run(base_url: str) -> None:
    def request(path, method="GET", body=None, content_type=None):
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            response = urlopen(
                Request(base_url + path, data=body, headers=headers, method=method), timeout=10
            )
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    for path in ("/", "/healthz", "/docs", "/openapi.json", "/static/app.js"):
        status, headers, _ = request(path)
        assert status == 200, (path, status)
        assert headers.get("X-Request-ID"), path

    status, _, body = request("/readyz")
    assert status == 503 and json.loads(body)["error"]["code"] == "missing_api_key"

    status, _, body = request("/api/v1/answer", "POST", b"{}", "application/json")
    assert status == 422 and json.loads(body)["error"]["code"] == "validation_error"

    boundary = "document-rag-smoke"
    multipart = (
        "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: application/json\r\n\r\n{content}\r\n"
            for name, filename, content in (
                ("questions_file", "questions.json", '["Which cloud?"]'),
                ("document_file", "source.json", '{"cloud":"GCP"}'),
            )
        )
        + f"--{boundary}--\r\n"
    )
    status, headers, body = request(
        "/api/v1/answer", "POST", multipart.encode(), f"multipart/form-data; boundary={boundary}"
    )
    error = json.loads(body)
    assert status == 503 and error["error"]["code"] == "missing_api_key"
    assert error["request_id"] == headers["X-Request-ID"]

    payload = {
        "results": [
            {
                "id": "q",
                "question": "Unknown policy?",
                "answer": "Data-Not-Found",
                "comments": "No supporting evidence",
                "status": "not_found",
                "confidence": "low",
                "citations": [],
            }
        ]
    }
    status, _, body = request(
        "/api/v1/export.xlsx", "POST", json.dumps(payload).encode(), "application/json"
    )
    assert status == 200
    with ZipFile(BytesIO(body)) as workbook:
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()
    print(
        "Container smoke passed: UI/assets, API docs, health/readiness, validation, multipart upload, and Excel export."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    run(parser.parse_args().base_url.rstrip("/"))
