from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _find_node_runtime() -> str | None:
    for candidate in (
        Path("/opt/homebrew/bin/node"),
        Path("/usr/local/bin/node"),
        Path("/usr/bin/node"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("node")


NODE = _find_node_runtime()


_HARNESS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class ClassList {
  constructor() { this.values = new Set(); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  toggle(name, force) {
    if (force === false) this.values.delete(name);
    else if (force === true) this.values.add(name);
    else if (this.values.has(name)) this.values.delete(name);
    else this.values.add(name);
  }
  contains(name) { return this.values.has(name); }
}

class Element {
  constructor(id = "") {
    this.id = id;
    this.hidden = true;
    this.innerHTML = "";
    this.textContent = "";
    this.href = "";
    this.download = "";
    this.onclick = null;
    this.clicked = false;
    this.classList = new ClassList();
    this.listeners = {};
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  appendChild(child) { children.push(child); return child; }
  click() { this.clicked = true; if (this.onclick) return this.onclick({ preventDefault() {} }); }
  remove() { this.removed = true; }
  querySelector() { return null; }
}

const children = [];
const elements = new Map();
for (const id of [
  "fmPreviewModal", "fmPreviewTitle", "fmPreviewBody", "fmPreviewDownload",
  "fmPreviewMove", "fmPreviewTrash", "fmPreviewShare",
]) elements.set(id, new Element(id));

global.document = {
  cookie: "",
  body: new Element("body"),
  getElementById(id) { return elements.get(id) || null; },
  createElement() { return new Element(); },
};
global.location = { origin: "https://osc.invalid", pathname: "/osc", href: "https://osc.invalid/osc" };
global.window = { location: global.location };
const sessionValues = new Map();
global.sessionStorage = {
  getItem(key) { return sessionValues.has(key) ? sessionValues.get(key) : null; },
  setItem(key, value) { sessionValues.set(key, String(value)); },
};
const NativeURL = URL;
NativeURL.createObjectURL = () => "blob:synthetic-preview";
NativeURL.revokeObjectURL = () => {};
global.URL = NativeURL;
global.navigator = { clipboard: { async writeText() {} } };
global.showToast = (...args) => { global.lastToast = args; };
global.state = { sort: {} };
global.fetchCalls = [];

vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"), { filename: process.argv[2] });

function response({ status = 200, type = "basic", contentType = "application/json", jsonBody = {}, blobSize = 0, textBody = "" } = {}) {
  return {
    status,
    type,
    ok: status >= 200 && status < 300,
    redirected: false,
    url: "",
    statusText: String(status),
    headers: { get(name) { return String(name).toLowerCase() === "content-type" ? contentType : ""; } },
    async json() { return jsonBody; },
    async blob() { return { size: blobSize }; },
    async text() { return textBody; },
  };
}
"""


def _run_node(tmp_path: Path, scenario: str) -> None:
    if not NODE:
        pytest.skip("node runtime unavailable")
    script = tmp_path / "osc_file_frontend_runtime.cjs"
    script.write_text(_HARNESS + "\n" + scenario, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script), str(ROOT / "static/osc/osc-utils.js")],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_pdf_preview_uses_unified_route_and_authenticated_content_url(tmp_path: Path) -> None:
    _run_node(
        tmp_path,
        r"""
(async () => {
  global.fetch = async (url, options) => {
    fetchCalls.push({ url, options });
    if (options.method === "HEAD") return response({ contentType: "application/pdf" });
    return response({ jsonBody: {
      ok: true,
      kind: "pdf",
      content_url: "/api/osc/files/content?path=%2Fsynthetic%2Fsample.pdf&inline=1",
    }});
  };
  const ok = await openFilePreview("/synthetic/sample.pdf", "sample.pdf");
  assert.equal(ok, true);
  assert.equal(fetchCalls[0].url, "/api/osc/files/preview?path=%2Fsynthetic%2Fsample.pdf");
  assert.equal(fetchCalls[0].options.redirect, "manual");
  assert.equal(fetchCalls[1].options.method, "HEAD");
  assert.match(elements.get("fmPreviewBody").innerHTML, /fm-preview-pdf/);
  assert.match(elements.get("fmPreviewBody").innerHTML, /inline=1/);
  assert.equal(elements.get("fmPreviewModal").hidden, false);
  assert.equal(elements.get("fmPreviewMove").hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
    )


def test_office_and_structured_previews_render_in_shared_modal(tmp_path: Path) -> None:
    _run_node(
        tmp_path,
        r"""
(async () => {
  const queue = [
    response({ contentType: "application/pdf", blobSize: 24 }),
    response({ jsonBody: {
      ok: true,
      kind: "csv",
      headers: ["欄位"],
      rows: [["<script>blocked</script>"]],
    }}),
  ];
  global.fetch = async (url, options) => {
    fetchCalls.push({ url, options });
    return queue.shift();
  };
  assert.equal(await openFilePreview("/synthetic/form.docx", "form.docx"), true);
  assert.equal(fetchCalls[0].url, "/api/osc/files/preview?path=%2Fsynthetic%2Fform.docx");
  assert.match(elements.get("fmPreviewBody").innerHTML, /blob:synthetic-preview/);
  assert.match(elements.get("fmPreviewBody").innerHTML, /fm-preview-pdf/);

  assert.equal(await openFilePreview("/synthetic/table.csv", "table.csv"), true);
  assert.match(elements.get("fmPreviewBody").innerHTML, /fm-preview-table/);
  assert.doesNotMatch(elements.get("fmPreviewBody").innerHTML, /<script>blocked<\/script>/);
  assert.match(elements.get("fmPreviewBody").innerHTML, /&lt;script&gt;blocked&lt;\/script&gt;/);
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
    )


@pytest.mark.parametrize(
    "response_expression",
    [
        'response({ status: 0, type: "opaqueredirect" })',
        'response({ status: 401, jsonBody: { ok: false, error: "authentication_required" } })',
    ],
    ids=["live_302_opaque_redirect", "future_json_401"],
)
def test_session_expiry_never_renders_a_fake_preview(tmp_path: Path, response_expression: str) -> None:
    _run_node(
        tmp_path,
        rf"""
(async () => {{
  global.fetch = async () => {response_expression};
  const ok = await openFilePreview("/synthetic/private.pdf", "private.pdf");
  assert.equal(ok, false);
  assert.match(location.href, /^\/login\?next=/);
  assert.doesNotMatch(elements.get("fmPreviewBody").innerHTML, /fm-preview-pdf/);
  assert.match(elements.get("fmPreviewBody").innerHTML, /登入已逾時/);
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
""",
    )


def test_download_probes_session_before_clicking_real_content_route(tmp_path: Path) -> None:
    _run_node(
        tmp_path,
        r"""
(async () => {
  global.fetch = async (url, options) => {
    fetchCalls.push({ url, options });
    return response({ contentType: "application/octet-stream" });
  };
  const ok = await startFileDownload("/synthetic/report.pdf", "report.pdf");
  assert.equal(ok, true);
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].options.method, "HEAD");
  assert.equal(fetchCalls[0].options.redirect, "manual");
  assert.equal(children.length, 1);
  assert.equal(children[0].clicked, true);
  assert.equal(children[0].href, "/api/osc/files/content?path=%2Fsynthetic%2Freport.pdf");
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
    )


def test_file_download_error_ui_does_not_render_server_trace_text(tmp_path: Path) -> None:
    _run_node(
        tmp_path,
        r"""
(async () => {
  global.fetch = async () => { throw new Error("trace_id=secret-123 /internal/path"); };
  const ok = await startFileDownload("/synthetic/report.pdf", "report.pdf");
  assert.equal(ok, false);
  assert.match(global.lastToast[0], /下載暫時無法完成/);
  assert.doesNotMatch(global.lastToast[0], /trace_id|internal\/path/);
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
    )


def test_readonly_api_retries_transient_fetch_failure_without_raw_browser_error(tmp_path: Path) -> None:
    _run_node(
        tmp_path,
        r"""
(async () => {
  let attempts = 0;
  global.fetch = async () => {
    attempts += 1;
    if (attempts < 3) throw new TypeError("Failed to fetch");
    return response({ textBody: JSON.stringify({ ok: true, folder_path: "/synthetic/case" }) });
  };
  const data = await api("/api/osc/cases/case-1/folder-path");
  assert.equal(attempts, 3);
  assert.equal(data.ok, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
    )


def test_api_does_not_retry_mutation_and_never_exposes_failed_to_fetch(tmp_path: Path) -> None:
    _run_node(
        tmp_path,
        r"""
(async () => {
  let attempts = 0;
  global.fetch = async () => {
    attempts += 1;
    throw new TypeError("Failed to fetch trace_id=private");
  };
  let message = "";
  try {
    await api("/api/osc/cases/case-1", "PUT", { status: "closed" });
  } catch (error) {
    message = String(error && error.message || error);
  }
  assert.equal(attempts, 1);
  assert.match(message, /網路連線暫時中斷/);
  assert.doesNotMatch(message, /Failed to fetch|trace_id|private/i);
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
    )


def test_documents_and_cases_use_shared_preview_download_actions() -> None:
    documents = (ROOT / "static/osc/tabs/documents.js").read_text(encoding="utf-8")
    cases = (ROOT / "static/osc/tabs/cases.js").read_text(encoding="utf-8")
    events = (ROOT / "static/osc/osc-events.js").read_text(encoding="utf-8")

    assert "fileContentUrl(" not in documents
    assert "fileContentUrl(" not in cases
    assert documents.count('data-act="osc-file-preview"') >= 4
    assert documents.count('data-act="osc-file-download"') >= 2
    assert cases.count('data-act="osc-file-preview"') >= 2
    assert cases.count('data-act="osc-file-download"') >= 3
    assert 'act === "osc-file-preview"' in events
    assert 'act === "osc-file-download"' in events


def test_file_manager_disables_expensive_directory_summaries() -> None:
    file_manager = (ROOT / "static/osc/tabs/file_manager.js").read_text(encoding="utf-8")

    assert "&summarize_dirs=0" in file_manager


def test_file_manager_retries_one_transient_nas_read_and_preserves_recent_listing() -> None:
    file_manager = (ROOT / "static/osc/tabs/file_manager.js").read_text(encoding="utf-8")

    assert "FM_DIRECTORY_RETRY_DELAY_MS = 900" in file_manager
    assert "return /directory_io_busy|NAS 正在處理其他資料夾/i.test(message);" in file_manager
    assert "listdir_failed has already exhausted the helper/cache contract" in file_manager
    assert "return apiDirectoryRead(url);" in file_manager
    assert "entryCache: new Map()" in file_manager
    assert "treeCache: new Map()" in file_manager
    assert "目前顯示最近一次成功內容" in file_manager


def test_file_manager_initialization_cannot_duplicate_drop_uploads() -> None:
    file_manager = (ROOT / "static/osc/tabs/file_manager.js").read_text(encoding="utf-8")

    assert "if (!main || !dz || main._fmDropBound) return;" in file_manager
    assert "main._fmDropBound = true;" in file_manager
    assert "FM._initialized = true;" in file_manager


def test_reentrant_file_manager_drop_executes_one_upload_and_finishes(tmp_path: Path) -> None:
    if not NODE:
        pytest.skip("node runtime unavailable")
    scenario = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const elements = new Map();
class Element {
  constructor(id = "") {
    this.id = id;
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this.children = [];
    this.textContent = "";
    this.innerHTML = "";
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  appendChild(child) { this.children.push(child); if (child.id) elements.set(child.id, child); return child; }
  querySelector(selector) {
    if (selector === ".fm-queue-bar-fill") return (this._fill ||= { style: {} });
    if (selector === ".fm-queue-status") return (this._status ||= { textContent: "" });
    return null;
  }
  querySelectorAll() { return []; }
  scrollIntoView() {}
  closest() { return null; }
}

const main = new Element("fmMain");
for (const id of ["fmDropZone", "fmUploadQueue", "fmUploadQueueBody", "fmStatus"]) {
  elements.set(id, new Element(id));
}
global.document = {
  readyState: "complete",
  cookie: "",
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) { return selector === "#fileManager .fm-main" ? main : null; },
  querySelectorAll() { return []; },
  createElement() { return new Element(); },
  addEventListener() {},
};
global.window = {
  matchMedia() { return { matches: false }; },
  requestAnimationFrame(callback) { callback(); },
  location: { pathname: "/osc" },
};
global.localStorage = { getItem() { return null; }, setItem() {} };
global.api = async url => url.includes("/roots")
  ? { ok: true, items: [] }
  : { ok: true, folders: [], files: [], hidden_count: 0 };
global._csrfToken = () => "";
global.csrfHeaders = () => ({});
global.FormData = class { append() {} };
global.fetch = async () => ({ ok: true, async json() { return { ok: true }; } });

let sends = 0;
let responseMode = "success";
global.XMLHttpRequest = class {
  constructor() { this.upload = {}; this.status = 200; this.responseText = ""; }
  open() {}
  setRequestHeader() {}
  send() {
    sends += 1;
    if (this.upload.onprogress) this.upload.onprogress({ lengthComputable: true, loaded: 1, total: 1 });
    setTimeout(() => {
      this.responseText = responseMode === "success"
        ? JSON.stringify({ ok: true, results: [{ ok: true, path: "/synthetic/report.pdf" }] })
        : "not-json";
      this.onload();
    }, 5);
  }
};

vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"), { filename: process.argv[2] });
window.FileManager.basePath = "/synthetic";
window.FileManager.init();
window.FileManager.init();
window.FileManager.init();
assert.equal(main.listeners.drop.length, 1);

const file = { name: "report.pdf", size: 123, lastModified: 456, type: "application/pdf" };
const event = {
  preventDefault() {},
  dataTransfer: { files: [file, file] },
};
(async () => {
  await Promise.all(main.listeners.drop.map(handler => handler(event)));
  assert.equal(sends, 1);
  const rows = elements.get("fmUploadQueueBody").children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0]._fill.style.width, "100%");
  assert.equal(rows[0]._status.textContent, "完成");
  responseMode = "invalid-json";
  const failedFile = { name: "retry.pdf", size: 123, lastModified: 789, type: "application/pdf" };
  await main.listeners.drop[0]({ preventDefault() {}, dataTransfer: { files: [failedFile] } });
  assert.equal(sends, 2);
  assert.equal(rows.length, 2);
  assert.equal(rows[1]._fill.style.width, "0%");
  assert.match(rows[1]._status.textContent, /伺服器回覆無法辨識.*再上傳/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    script = tmp_path / "file_manager_drop_runtime.cjs"
    script.write_text(scenario, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script), str(ROOT / "static/osc/tabs/file_manager.js")],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
