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

// Keep the current upload and reviewed answers in this tab for follow-ups and export.
let activeDocument = null;
let answers = [];
let busy = false;

function setBusy(value, message = "") {
  busy = value;
  answerButton.disabled = value;
  followupButton.disabled = value;
  exportButton.disabled = value || answers.length === 0;
  progress.textContent = message;
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
    status.textContent = result.status === "answered" ? "Answered" : "Not found";
    const strength = document.createElement("span");
    strength.className = "strength";
    strength.textContent = `Evidence: ${result.confidence}`;
    top.append(number, strength, status);

    const question = document.createElement("h3");
    question.textContent = result.question;
    const answer = document.createElement("p");
    answer.className = "answer";
    answer.textContent = result.answer;
    const comment = document.createElement("p");
    comment.className = "comment";
    comment.textContent = result.comments;
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
        item.append(location, excerpt);
        evidence.append(item);
      });
      card.append(evidence);
    }
    resultsContainer.append(card);
  });
  exportButton.disabled = busy || answers.length === 0;
}

async function requestAnswers(documentFile, questionsFile) {
  const form = new FormData();
  form.append("document_file", documentFile);
  form.append("questions_file", questionsFile);
  const response = await fetch("/api/v1/answer", { method: "POST", body: form });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error?.message || payload.detail?.[0]?.msg || "Could not answer questions.");
  }
  return payload;
}

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const documentFile = documentInput.files[0];
  const questionsFile = questionsInput.files[0];
  if (!documentFile || !questionsFile) return;
  setBusy(true, "Reading the document and answering questions…");
  try {
    const payload = await requestAnswers(documentFile, questionsFile);
    activeDocument = documentFile;
    answers = payload.results;
    summary.textContent = `${payload.document.name} · ${payload.document.passages} searchable passages · ${answers.length} results`;
    resultsPanel.hidden = false;
    renderAnswers();
    resultsPanel.scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy(false);
  }
});

followupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!activeDocument) return;
  showError("");
  const text = followupInput.value.trim();
  if (!text) return;
  // The API accepts question files, so a follow-up uses the same contract as an upload.
  const questionFile = new File(
    [JSON.stringify({ questions: [{ id: `followup-${crypto.randomUUID()}`, question: text }] })],
    "follow-up.json",
    { type: "application/json" },
  );
  setBusy(true, "Answering follow-up question…");
  try {
    const payload = await requestAnswers(activeDocument, questionFile);
    answers.push(...payload.results);
    renderAnswers();
    followupInput.value = "";
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy(false);
  }
});

exportButton.addEventListener("click", async () => {
  if (!answers.length) return;
  showError("");
  setBusy(true, "Preparing Excel download…");
  try {
    const response = await fetch("/api/v1/export.xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: answers }),
    });
    if (!response.ok) throw new Error("Could not create the Excel file.");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "answers.xlsx";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy(false);
  }
});

documentInput.addEventListener("change", () => {
  activeDocument = null;
  answers = [];
  resultsPanel.hidden = true;
  showError("");
});
