# Document RAG

A small API and upload interface that answers a JSON list of questions from one uploaded PDF or JSON document. Answers use `gpt-4o-mini` and include citations to extracted PDF pages or JSON paths. The UI can add follow-up questions and download the reviewed results as an Excel workbook.

## Requirements

- Python 3.11 or later, or Docker
- An OpenAI API key with access to `gpt-4o-mini` and `text-embedding-3-small`

Create a key at the [OpenAI API keys page](https://platform.openai.com/api-keys), or use a challenge key if you are authorized to use it. Keep the key server side. Never place it in source code, browser requests, screenshots, or commits.

## Run locally on Windows (PowerShell)

1. Open PowerShell in the project folder (the folder containing this README).

2. This workspace already has `.venv`, so you can skip installation here. For a fresh copy of the project, create it and install dependencies once:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

3. Start the app with one command:

```powershell
.\.venv\Scripts\python.exe -m app.run_local
```

When the terminal says `OpenAI API key (input hidden, then press Enter):`, paste your key and press Enter. Nothing appears while you type; this is expected. Do not put the key into the command itself. If a key was exposed in terminal history, a screenshot, or a chat, revoke it in the [OpenAI API keys dashboard](https://platform.openai.com/api-keys) and create a new one before continuing.

4. Open <http://127.0.0.1:8000>. Select `examples/basic-json/source.json` for **Source document** and `examples/basic-json/questions.json` for **Questions**, then click **Answer questions**. The first two questions have answers in the source; the backup-location question should be marked **Not found**. Expand **View evidence**, ask a follow-up, and download the Excel file.

Keep PowerShell open while using the app. Press `Ctrl+C` to stop it. The key is only used by this server process; the launcher will ask again next time unless you already set `OPENAI_API_KEY` in the environment.

## Run locally on macOS or Linux (Bash)

From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m app.run_local
```

Paste your API key when prompted; input is hidden. Open <http://127.0.0.1:8000> for the UI or <http://127.0.0.1:8000/docs> for the OpenAPI documentation.

## Run with Docker

Copy `.env.example` to `.env` (`Copy-Item .env.example .env` in PowerShell, or `cp .env.example .env` in Bash), then replace `OPENAI_API_KEY=` with your key in `.env`. The file is ignored by Git. Docker reads this file through `--env-file`; the local launcher above prompts for the key instead. From the repository root, run:

```text
docker build -t document-rag .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env document-rag
```

For shared deployments, inject the key through a secret manager and put authentication and TLS at the ingress. The container runs as a non-root user.

## API contract

`POST /api/v1/answer` accepts multipart form data with exactly two file fields:

| Field | Accepted content |
| --- | --- |
| `questions_file` | UTF-8 `.json` file containing an array of strings or objects, or `{ "questions": [...] }` |
| `document_file` | Text-based `.pdf` or UTF-8 `.json` source document |

An object question has an optional `id` and a required `question` string:

```json
{
  "questions": [
    {"id": "q1", "question": "Which cloud provider is used?"},
    {"id": "q2", "question": "What is the backup location?"}
  ]
}
```

Example request from the repository root (PowerShell or Bash; `curl.exe` selects the real curl binary on Windows):

```text
curl.exe -X POST http://localhost:8000/api/v1/answer -F "questions_file=@examples/basic-json/questions.json;type=application/json" -F "document_file=@examples/basic-json/source.json;type=application/json"
```

On macOS or Linux, use `curl` in place of `curl.exe`.

The bundled JSON and NIST folders contain matching source and question files. Choose both files from the same folder. The Nave folder contains questions for its publisher-hosted PDF:

| Folder | Source document | Questions | Answer checklist |
| --- | --- | --- | --- |
| `basic-json/` | [`source.json`](examples/basic-json/source.json) | [`questions.json`](examples/basic-json/questions.json) | The third question should be `not_found`. |
| `noc-json/` | [`noc-operations-source.json`](examples/noc-json/noc-operations-source.json) | [`noc-operations-questions.json`](examples/noc-json/noc-operations-questions.json) | [Review checklist](examples/noc-json/noc-operations-review.md) |
| `nist-continuous-monitoring/` | [`NIST SP 800-137`](examples/nist-continuous-monitoring/nist-sp-800-137-continuous-monitoring.pdf), 80 pages | [`nist-sp-800-137-questions.json`](examples/nist-continuous-monitoring/nist-sp-800-137-questions.json) | [Review checklist](examples/nist-continuous-monitoring/nist-sp-800-137-review.md) |
| `nist-network-forensics/` | [`NIST SP 800-86`](examples/nist-network-forensics/nist-sp-800-86-forensics-incident-response.pdf), 121 pages | [`nist-sp-800-86-questions.json`](examples/nist-network-forensics/nist-sp-800-86-questions.json) | [Review checklist](examples/nist-network-forensics/nist-sp-800-86-review.md) |
| `nave-soc2/` | [Publisher-hosted Nave report](https://getnave.com/assets2/docs/Nave-SOC2-Type-2-Report.pdf) | [`nave-soc2-questions.json`](examples/nave-soc2/nave-soc2-questions.json) | [Review checklist](examples/nave-soc2/nave-soc2-review.md) |

The small JSON and NOC sets are synthetic. The NIST PDFs are real public security guidance documents, not SOC 2 attestation reports. Start long-document testing with the 80-page file. The Nave source is not bundled because its publisher may block downloads.

If the UI says OpenAI rejected an embedding request (HTTP 400), check the PowerShell window running the server for `embedding_api_failure`. The log includes `upstream_message`, `upstream_code`, `upstream_param`, `batch_start` (the zero-based passage index of the rejected batch), `api_host`, and response identifiers. Try the small `examples/basic-json/source.json` and `examples/basic-json/questions.json` pair as a control. If both pairs fail, check the API key, API host, and upstream response; if only a PDF fails, the batch index helps locate the rejected input. Restart the server after updating code so the improved diagnostic logging takes effect. Do not paste an API key when sharing logs.

If a shared key works elsewhere but this app reports `invalid_api_key`, clear any inherited OpenAI settings and restart the server. This resolved a local setup issue during development. Never share the key itself.

PowerShell:

```powershell
Remove-Item Env:OPENAI_API_KEY,Env:OPENAI_ORG_ID,Env:OPENAI_PROJECT_ID,Env:OPENAI_BASE_URL,Env:OPENAI_CUSTOM_HEADERS -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m app.run_local
```

On macOS or Linux, use `unset OPENAI_API_KEY OPENAI_ORG_ID OPENAI_PROJECT_ID OPENAI_BASE_URL OPENAI_CUSTOM_HEADERS` followed by `python -m app.run_local` from the project root. Paste the full key at the hidden prompt. Test the small JSON pair before a long PDF.

Example response shape:

```json
{
  "request_id": "4e12...",
  "document": {"name": "source.pdf", "type": "pdf", "passages": 48},
  "results": [
    {
      "id": "q1",
      "question": "Which cloud provider is used?",
      "answer": "Google Cloud Platform.",
      "comments": "The source explicitly names GCP as the hosting platform.",
      "confidence": "high",
      "status": "answered",
      "citations": [
        {"passage_id": "p16-c1", "source": "source.pdf", "page": 16, "json_path": null, "excerpt": "..."}
      ]
    }
  ]
}
```

When evidence is insufficient, `status` is `not_found`, `answer` is `Data-Not-Found`, `confidence` is `low`, and citations are empty. `comments` describes the gap. The confidence label is a qualitative assessment of the cited evidence, not a calibrated probability.

`POST /api/v1/export.xlsx` accepts `{ "results": [...] }` using the result objects above and returns an Excel file. Its columns are `id`, `question`, `answer`, `comments`, `confidence`, `status`, and `citations`. Spreadsheet formula prefixes in user-supplied text are escaped.

## How retrieval works

1. PDF extraction preserves one-based page numbers. JSON extraction preserves paths such as `$[0]` or `$.policies.incidents` and keeps Q&A records together.
2. Long records are split into overlapping passages with LangChain's text splitter. Request limits cap bytes, PDF pages, extracted characters, chunks, and questions.
3. `text-embedding-3-small` embeds passages in batches. An in-memory vector index and BM25 term ranking are combined with reciprocal rank fusion. This is request scoped; a short-lived, bounded process cache avoids re-embedding an unchanged upload for a follow-up question. Simultaneous requests for the same upload share one indexing task.
4. Only the top retrieved passages go to `gpt-4o-mini`. The model returns structured JSON with passage IDs. The service discards unknown IDs and returns `not_found` if it has no valid citation.
5. Identical questions in one request share an answer call. Distinct questions are answered concurrently with a process-wide semaphore. Model calls have timeouts; PDF extraction and passage ranking run in worker threads.

The uploaded document is data, not an instruction source. The model is prompted to ignore instructions embedded in it. Source excerpts and citation locations are returned so the user can check an answer; citations are not a guarantee that every generated statement is correct.

### Retrieval design choice

The API searches one uploaded document at a time, with a limit of 500 passages. A local index avoids requiring a database service for the evaluator to run the app. LangChain is used only to split text; embedding and hybrid ranking are explicit in `app/llm.py` and `app/retrieval.py`. For a persistent collection of many documents or multiple server replicas, replace the `SearchIndex.build` and `search` boundary with a vector database such as Chroma or pgvector and store document identity with each passage. The current cache is process-local and expires after 15 minutes.

## Test and review

Tests do not need an API key. After installing the development dependencies, run:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
```

macOS or Linux with the virtual environment active:

```bash
python -m pytest -q
python -m ruff check app tests
```

Tests use a fake model provider, so no paid calls are needed. They cover PDF and JSON parsing, invalid uploads, retrieval, multi-question ordering, citation validation, the HTTP endpoint, and Excel export. A live smoke test should use a few questions only. The challenge brief links to an [84-page sample PDF](https://getnave.com/assets2/docs/Nave-SOC2-Type-2-Report.pdf) and an [answered questionnaire workbook](https://docs.google.com/spreadsheets/d/1u7z18yNKsL8cMLV6OxYI1-8ageRfFG1j/edit); the workbook is illustrative and is not required as an input format.

## Operational limits

Defaults (configurable with uppercase environment variables matching `app/config.py`): 10 MB document, 100 KB questions file, 50 questions, 150 PDF pages, 500,000 extracted characters, 500 passages, four concurrent answers, and a 120-second request timeout. The API returns structured validation errors for unsupported or malformed inputs. Scanned PDFs without extractable text require OCR before upload. The in-process cache is bounded to four documents for 15 minutes and is not shared between container replicas.

## Evaluation coverage

| Rubric area | Evidence in this repository |
| --- | --- |
| Backend correctness | Two upload fields; PDF and JSON source readers; ordered multi-question JSON response |
| Error handling | File, count, page, text, and time limits with clear 4xx/5xx responses |
| Code structure | Separate parsing, retrieval, model, orchestration, API, and export modules |
| Tests | Offline unit and mocked endpoint tests |
| Performance | Batched embeddings, bounded concurrent answers, hybrid search, short-lived cache |
| Containerization and observability | Non-root Dockerfile, request IDs, JSON logs, model token usage logging |
| Grounding | Citable passages, structured model output, ID validation, `not_found` status |
| Frontend | Two-file upload, results with evidence, follow-up question, Excel download |

## Submission contents

Submit the GitHub repository containing `app/`, `tests/`, `pyproject.toml`, `README.md`, `Dockerfile`, `.dockerignore`, `.env.example`, `.gitignore`, and the example inputs in `examples/`. The small JSON pair and one real PDF pair are enough to demonstrate both required source formats; the other examples are optional review material. The challenge document and answered spreadsheet are references, not inputs to the app.

Keep `.venv/`, caches, `*.egg-info/`, generated Excel files, and any `.env` file out of Git. Never commit an API key. A demo video is optional under the challenge instructions.
