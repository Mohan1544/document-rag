const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const { File } = require("node:buffer");
const { webcrypto } = require("node:crypto");

class Element {
  constructor() { this.children = []; this.handlers = {}; this.files = []; this.hidden = true; this.value = ""; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  scrollIntoView() {}
  remove() {}
  click() {}
}

function app(fetch) {
  const elements = {};
  const document = {
    getElementById(id) { return elements[id] ||= new Element(); },
    createElement() { return new Element(); }, body: new Element(),
  };
  const context = vm.createContext({ document, fetch, AbortController, File, FormData, crypto: webcrypto, URL, setTimeout, SyntaxError });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../app/static/app.js"), "utf8"), context);
  elements["document-file"].files = [new File(["{}"], "source.json")];
  elements["questions-file"].files = [new File(['["Cloud?"]'], "q.json")];
  return elements;
}

function result(status = "answered") {
  return { document: { name: "source.json", passages: 1 }, results: [{
    id: "q", question: "Cloud?", status, answer: status === "answered" ? "GCP" : null,
    confidence: status === "answered" ? "high" : null, comments: "", citations: [],
    error: status === "error" ? { code: "model_timeout", message: "Timed out" } : null,
  }] };
}
function response(body) { return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } }); }
const event = { preventDefault() {} };

test("a changed source ignores a late response, even when transport ignores abort", async () => {
  let resolve;
  const ui = app(() => new Promise(r => { resolve = r; }));
  const upload = ui["upload-form"].handlers.submit(event);
  assert.equal(ui["document-file"].disabled, true);
  ui["document-file"].handlers.change();
  resolve(response(result()));
  await upload;
  assert.equal(ui["results-panel"].hidden, true);
  assert.equal(ui["results"].children.length, 0);
  assert.equal(ui["answer-button"].disabled, false);
});

test("failed questions show an error without fabricated confidence", async () => {
  const ui = app(async () => response(result("error")));
  await ui["upload-form"].handlers.submit(event);
  const card = ui["results"].children[0];
  assert.equal(card.children[0].children[1].textContent, "Processing error");
  assert.equal(card.children[0].children[2].textContent, "Failed");
  assert.equal(card.children[2].textContent, "Timed out");
});

test("a non-JSON upstream response shows a friendly failure", async () => {
  const ui = app(async () => new Response("Bad gateway", { status: 502 }));
  await ui["upload-form"].handlers.submit(event);
  assert.match(ui.error.textContent, /unexpected response \(HTTP 502\)/);
  assert.equal(ui["answer-button"].disabled, false);
});

test("cancelling a follow-up preserves existing results", async () => {
  let resolve;
  let calls = 0;
  let signal;
  const ui = app((url, options) => {
    if (++calls === 1) return Promise.resolve(response(result()));
    signal = options.signal;
    return new Promise(r => { resolve = r; });
  });
  await ui["upload-form"].handlers.submit(event);
  ui["followup-question"].value = "Backup?";
  const followup = ui["followup-form"].handlers.submit(event);
  ui["cancel-button"].handlers.click();
  assert.equal(signal.aborted, true);
  resolve(response(result()));
  await followup;
  assert.equal(ui["results"].children.length, 1);
});
