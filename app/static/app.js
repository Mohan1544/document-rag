const uploadForm = document.getElementById("upload-form");
const followupForm = document.getElementById("followup-form");
const documentInput = document.getElementById("document-file");
const questionsInput = document.getElementById("questions-file");
const resultsPanel = document.getElementById("results-panel");
const resultsContainer = document.getElementById("results");
const summary = document.getElementById("document-summary");
const progress = document.getElementById("progress");
const errorBox = document.getElementById("error");
const answerButton = document.getElementById("answer-button");
const followupButton = document.getElementById("followup-button");
const exportButton = document.getElementById("export-button");
const followupInput = document.getElementById("followup-question");
const cancelButton = document.getElementById("cancel-button");

// Keep the current upload and reviewed answers in this tab for follow-ups and export.
let activeDocument = null;
let answers = [];
let busy = false;
let operation = 0;
let controller = null;

function beginOperation(message) {
  controller?.abort();
  controller = new AbortController();
  operation += 1;
  setBusy(true, message);
  return { id: operation, signal: controller.signal };
}

function finishOperation(id) {
  if (id !== operation) return;
  controller = null;
  setBusy(false);
}

async function readPayload(response) {
  try {
    const payload = await response.json();
    if (!response.ok) {
      const message = payload.error?.message || "The request failed. Try again.";
      const reference = payload.request_id ? ` Reference: ${payload.request_id}` : "";
      throw new Error(message + reference);
    }
    return payload;
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}). Try again.`);
    }
    throw error;
  }
}

function setBusy(value, message = "") {
  busy = value;
  answerButton.disabled = value;
  followupButton.disabled = value;
  exportButton.disabled = value || answers.length === 0;
  progress.textContent = message;
  documentInput.disabled = value;
  questionsInput.disabled = value;
  followupInput.disabled = value;
  cancelButton.hidden = !value;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

function locationText(citation) {
  const pieces = [citation.source];
  if (citation.page !== null && citation.page !== undefined) pieces.push(`page ${citation.page}`);
  if (citation.json_path) pieces.push(citation.json_path);
  return pieces.join(" · ");
}

function renderAnswers() {
  resultsContainer.replaceChildren();
  answers.forEach((result, index) => {
    const card = document.createElement("article");
    card.className = "result-card";

    const top = document.createElement("div");
    top.className = "result-top";
    const number = document.createElement("span");
    number.className = "result-number";
    number.textContent = String(index + 1).padStart(2, "0");
    const status = document.createElement("span");
    status.className = `status ${result.status === "answered" ? "answered" : "not-found"}`;
    status.textContent = { answered: "Answered", not_found: "Not found", error: "Failed" }[result.status];
    const strength = document.createElement("span");
    strength.className = "strength";
    strength.textContent = result.confidence ? `Evidence: ${result.confidence}` : "Processing error";
    top.append(number, strength, status);

    const question = document.createElement("h3");
    question.textContent = result.question;
    const answer = document.createElement("p");
    answer.className = "answer";
    answer.textContent = result.status === "error" ? result.error.message : result.answer;
    const comment = document.createElement("p");
    comment.className = "comment";
    comment.textContent = result.status === "error" ? `Error code: ${result.error.code}` : result.comments;
    card.append(top, question, answer, comment);

    if (result.citations.length) {
      const evidence = document.createElement("details");
      evidence.className = "evidence";
      const title = document.createElement("summary");
      title.textContent = `View evidence (${result.citations.length})`;
      evidence.append(title);
      result.citations.forEach((citation) => {
        const item = document.createElement("div");
        item.className = "citation";
        const location = document.createElement("strong");
        location.textContent = locationText(citation);
        const excerpt = document.createElement("p");
        excerpt.textContent = citation.excerpt;
        if (citation.quote) {
          const quote = document.createElement("blockquote");
          quote.textContent = citation.quote;
          item.append(location, quote, excerpt);
        } else {
          item.append(location, excerpt);
        }
        evidence.append(item);
      });
      card.append(evidence);
    }
    resultsContainer.append(card);
  });
  exportButton.disabled = busy || answers.length === 0;
}

async function requestAnswers(documentFile, questionsFile, signal) {
  const form = new FormData();
  form.append("document_file", documentFile);
  form.append("questions_file", questionsFile);
  const response = await fetch("/api/v1/answer", { method: "POST", body: form, signal });
  return readPayload(response);
}

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  showError("");
  const documentFile = documentInput.files[0];
  const questionsFile = questionsInput.files[0];
  if (!documentFile || !questionsFile) return;
  const request = beginOperation("Reading the document and answering questions…");
  try {
    const payload = await requestAnswers(documentFile, questionsFile, request.signal);
    if (request.id !== operation) return;
    activeDocument = documentFile;
    answers = payload.results;
    summary.textContent = `${payload.document.name} · ${payload.document.passages} searchable passages · ${answers.length} results`;
    resultsPanel.hidden = false;
    renderAnswers();
    resultsPanel.scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    if (request.id === operation && error.name !== "AbortError") showError(error.message);
  } finally {
    finishOperation(request.id);
  }
});

followupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!activeDocument || busy) return;
  showError("");
  const text = followupInput.value.trim();
  if (!text) return;
  // The API accepts question files, so a follow-up uses the same contract as an upload.
  const questionFile = new File(
    [JSON.stringify({ questions: [{ id: `followup-${crypto.randomUUID()}`, question: text }] })],
    "follow-up.json",
    { type: "application/json" },
  );
  const request = beginOperation("Answering follow-up question…");
  try {
    const payload = await requestAnswers(activeDocument, questionFile, request.signal);
    if (request.id !== operation) return;
    answers.push(...payload.results);
    summary.textContent = `${payload.document.name} · ${payload.document.passages} searchable passages · ${answers.length} results`;
    renderAnswers();
    followupInput.value = "";
  } catch (error) {
    if (request.id === operation && error.name !== "AbortError") showError(error.message);
  } finally {
    finishOperation(request.id);
  }
});

exportButton.addEventListener("click", async () => {
  if (!answers.length || busy) return;
  showError("");
  const request = beginOperation("Preparing Excel download…");
  try {
    const response = await fetch("/api/v1/export.xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: answers }),
      signal: request.signal,
    });
    if (!response.ok) await readPayload(response);
    const blob = await response.blob();
    if (request.id !== operation) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "answers.xlsx";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  } catch (error) {
    if (request.id === operation && error.name !== "AbortError") showError(error.message);
  } finally {
    finishOperation(request.id);
  }
});

function resetFiles() {
  operation += 1;
  controller?.abort();
  controller = null;
  setBusy(false);
  activeDocument = null;
  answers = [];
  resultsPanel.hidden = true;
  showError("");
}

documentInput.addEventListener("change", resetFiles);
questionsInput.addEventListener("change", resetFiles);
cancelButton.addEventListener("click", () => {
  operation += 1;
  controller?.abort();
  controller = null;
  setBusy(false, "Request cancelled.");
});
