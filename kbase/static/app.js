const get = id => document.getElementById(id);
const librarySelect = get("library");
const queryForm = get("query-form");
const libraryDialog = get("library-dialog");
const fileInput = get("file-input");
const dropZone = get("drop-zone");
const queryStatus = get("query-status");
const uploadLog = get("upload-log");
let libraries = [];
let requestNumber = 0;
let uploading = false;

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
}

async function api(path, options) {
  const response = await fetch(`/api${path}`, options);
  if (response.status === 204) return null;
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof result.detail === "string" ? result.detail : "请求失败，请检查输入后重试。");
  }
  return result;
}

function jsonRequest(payload) {
  return {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)};
}

function notice(node, message = "", error = false) {
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = !message;
}

function showView(view) {
  get("query-view").hidden = view !== "query";
  get("documents-view").hidden = view !== "documents";
  get("current-view").textContent = view === "query" ? "检索与问答" : "文档管理";
  document.querySelectorAll(".nav").forEach(button => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

function currentLibrary() {
  return libraries.find(item => item.id === Number(librarySelect.value));
}

function showLibrary() {
  const library = currentLibrary();
  get("current-library").textContent = library?.name ?? "知识库";
  get("document-count").textContent = library?.documents ?? 0;
  get("welcome-text").textContent = library?.documents
    ? "输入问题或关键词，开始检索当前知识库。"
    : "导入第一份文档，即可开始检索。";
}

async function loadLibraries(preferredId) {
  const data = await api("/libraries");
  const previous = preferredId ?? currentLibrary()?.id;
  libraries = data;
  librarySelect.replaceChildren(...data.map(library => new Option(library.name, library.id)));
  if (data.some(library => library.id === previous)) librarySelect.value = String(previous);
  showLibrary();
}

function downloadLink(library, document, text) {
  const link = element("a", text, "text-link");
  link.href = `/api/libraries/${library}/documents/${document}/download`;
  return link;
}

async function loadDocuments() {
  const library = currentLibrary();
  if (!library) return;
  const data = await api(`/libraries/${library.id}/documents`);
  if (currentLibrary()?.id !== library.id) return;
  const date = new Intl.DateTimeFormat("zh-CN", {dateStyle: "short", timeStyle: "short"});
  const rows = data.map(item => {
    const row = element("tr");
    const actions = element("td");
    const remove = element("button", "删除");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`删除“${item.filename}”及其检索索引？`)) return;
      remove.disabled = true;
      try {
        await api(`/libraries/${library.id}/documents/${item.id}`, {method: "DELETE"});
        resetQuery();
        await refreshDocuments();
      } catch (error) {
        reportDocumentError(error);
        remove.disabled = false;
      }
    });
    actions.append(downloadLink(library.id, item.id, "下载"), remove);
    row.append(element("td", item.filename), element("td", item.chunks),
      element("td", date.format(new Date(item.imported_at))), actions);
    return row;
  });
  get("documents").replaceChildren(...rows);
  get("table-count").textContent = `${rows.length} 份`;
  get("table-wrap").hidden = !rows.length;
  get("empty-documents").hidden = Boolean(rows.length);
}

async function refreshDocuments() {
  await loadLibraries();
  await loadDocuments();
}

function reportDocumentError(error) {
  const entry = element("li");
  notice(entry, error.message, true);
  uploadLog.append(entry);
  uploadLog.hidden = false;
}

function queryPending(pending) {
  queryForm.querySelectorAll("button").forEach(button => { button.disabled = pending; });
}

function resetQuery() {
  requestNumber += 1;
  queryPending(false);
  notice(queryStatus);
  get("results").hidden = true;
  get("welcome").hidden = false;
}

function showResults(result, library, answerRequested) {
  const container = get("results");
  const sources = get("sources");
  sources.replaceChildren();
  get("welcome").hidden = true;
  const showAnswer = answerRequested && Boolean(result.answer);
  get("answer-panel").hidden = !showAnswer;
  container.classList.toggle("with-answer", showAnswer);
  get("sources-title").textContent = `文档出处 · ${result.sources.length}`;
  result.sources.forEach(source => {
    const card = element("details", undefined, "source");
    card.id = `source-${source.reference}`;
    card.open = source.reference === 1 || !answerRequested;
    const ranks = [];
    if (source.keyword_rank) ranks.push(`关键词第 ${source.keyword_rank} 名`);
    if (source.semantic_rank) ranks.push(`语义第 ${source.semantic_rank} 名`);
    card.append(element("summary", `[${source.reference}] ${source.filename}`),
      element("p", source.location), element("p", ranks.join(" · ")),
      element("blockquote", source.text), downloadLink(library, source.document_id, "下载原文"));
    sources.append(card);
  });
  if (!result.sources.length) sources.append(element("p", "当前知识库没有匹配的片段。", "hint"));
  const answer = get("answer-text");
  answer.replaceChildren();
  if (showAnswer) {
    for (const part of result.answer.split(/(\[\d+\])/g)) {
      if (!/^\[\d+\]$/.test(part)) {
        answer.append(document.createTextNode(part));
        continue;
      }
      const target = get(`source-${part.slice(1, -1)}`);
      if (!target) {
        answer.append(document.createTextNode(part));
        continue;
      }
      const button = element("button", part, "reference");
      button.type = "button";
      button.addEventListener("click", () => {
        sources.querySelectorAll(".selected").forEach(card => card.classList.remove("selected"));
        target.open = true;
        target.classList.add("selected");
        target.querySelector("summary").focus({preventScroll: true});
        target.scrollIntoView({block: "nearest"});
      });
      answer.append(button);
    }
  }
  notice(queryStatus, result.notice ?? "", Boolean(result.notice));
  container.hidden = false;
}

queryForm.addEventListener("submit", async event => {
  event.preventDefault();
  const library = currentLibrary();
  const question = get("question").value.trim();
  if (!library || !question) {
    notice(queryStatus, library ? "请输入搜索词或问题。" : "请先选择知识库。", true);
    return;
  }
  const ticket = ++requestNumber;
  const answer = event.submitter?.value === "answer";
  queryPending(true);
  get("results").hidden = true;
  get("welcome").hidden = true;
  notice(queryStatus, answer ? "正在检索资料并生成回答…" : "正在检索资料…");
  try {
    const result = await api(`/libraries/${library.id}/query`, jsonRequest({question, mode: get("mode").value, answer}));
    if (ticket === requestNumber) showResults(result, library.id, answer);
  } catch (error) {
    if (ticket === requestNumber) notice(queryStatus, error.message, true);
  } finally {
    if (ticket === requestNumber) queryPending(false);
  }
});

async function importFiles(files) {
  const library = currentLibrary();
  if (!library || !files.length || uploading) return;
  uploading = true;
  get("choose-files").disabled = true;
  uploadLog.hidden = false;
  uploadLog.replaceChildren();
  let changed = false;
  try {
    for (const file of files) {
      const entry = element("li");
      uploadLog.append(entry);
      notice(entry, `${file.name} · 正在导入到 ${library.name}…`);
      try {
        const data = new FormData();
        data.append("file", file);
        const document = await api(`/libraries/${library.id}/documents`, {method: "POST", body: data});
        notice(entry, `${file.name} · 已导入 ${document.chunks} 个片段`);
        changed = true;
      } catch (error) {
        notice(entry, `${file.name} · ${error.message}`, true);
      }
    }
    if (changed) {
      resetQuery();
      await refreshDocuments();
    }
  } catch (error) {
    reportDocumentError(error);
  } finally {
    uploading = false;
    get("choose-files").disabled = false;
    fileInput.value = "";
  }
}

async function checkModels() {
  const output = get("model-status");
  const detail = get("model-detail");
  const dot = get("model-dot");
  try {
    const models = await api("/models");
    const ready = models.embedding.ready && models.chat.ready;
    const cloud = models.chat.provider === "deepseek";
    get("cloud-note").hidden = !cloud;
    dot.classList.toggle("ready", ready);
    output.textContent = ready ? (cloud ? "DeepSeek 已配置" : "本地模型就绪")
      : models.connected ? "模型待配置" : "Ollama 未连接";
    detail.textContent = [
      `向量 · ${models.embedding.name}${models.embedding.ready ? "" : "（未就绪）"}`,
      `问答 · ${models.chat.name}${models.chat.ready ? "" : "（未就绪）"}`,
    ].join("\n");
    detail.style.whiteSpace = "pre-line";
  } catch {
    dot.classList.remove("ready");
    output.textContent = "无法获取模型状态";
  }
}

document.querySelectorAll("[data-view]").forEach(button => {
  button.addEventListener("click", () => {
    showView(button.dataset.view);
    if (button.dataset.view === "documents") loadDocuments().catch(reportDocumentError);
  });
});
librarySelect.addEventListener("change", () => {
  showLibrary();
  resetQuery();
  loadDocuments().catch(reportDocumentError);
});
get("check-models").addEventListener("click", checkModels);
get("choose-files").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => importFiles([...fileInput.files]));
dropZone.addEventListener("dragover", event => {
  event.preventDefault();
  dropZone.classList.add("dragging");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
dropZone.addEventListener("drop", event => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  importFiles([...event.dataTransfer.files]);
});

get("new-library").addEventListener("click", () => {
  get("library-form").reset();
  notice(get("library-error"));
  libraryDialog.showModal();
  get("library-name").focus();
});
get("cancel-library").addEventListener("click", () => libraryDialog.close());
get("library-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const library = await api("/libraries", jsonRequest({name: get("library-name").value}));
    await loadLibraries(library.id);
    libraryDialog.close();
    resetQuery();
    showView("documents");
    await loadDocuments().catch(reportDocumentError);
  } catch (error) {
    notice(get("library-error"), error.message, true);
  } finally {
    button.disabled = false;
  }
});

loadLibraries().catch(error => notice(queryStatus, error.message, true));
checkModels();
