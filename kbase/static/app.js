const get = id => document.getElementById(id);
const librarySelect = get("library");
const queryForm = get("query-form");
const libraryDialog = get("library-dialog");
const fileInput = get("file-input");
const queryStatus = get("query-status");
const uploadLog = get("upload-log");
const transcript = get("conversation");
const questionInput = get("question");
const knowledgeSwitch = get("enable-knowledge");
let libraries = [];
let history = [];
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
  get("current-view").textContent = view === "query" ? "对话" : "文档管理";
  get("new-chat").hidden = view !== "query";
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
  get("side-upload").disabled = !library || uploading;
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
  get("side-files").replaceChildren(...data.map(item => {
    const row = element("li", undefined, "side-file");
    const suffix = item.filename.split(".").pop().slice(0, 5).toUpperCase();
    const link = downloadLink(library.id, item.id, item.filename);
    link.title = item.filename;
    row.append(element("span", suffix, "file-type"), link);
    return row;
  }));
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
  notice(get("side-notice"), error.message, true);
}

function queryPending(pending) {
  queryForm.querySelectorAll("button").forEach(button => { button.disabled = pending; });
  queryForm.setAttribute("aria-busy", String(pending));
}

function resetConversation() {
  requestNumber += 1;
  history = [];
  transcript.replaceChildren();
  questionInput.value = "";
  queryPending(false);
  notice(queryStatus);
  get("welcome").hidden = false;
}

function showReply(result, library, searchOnly) {
  const reply = element("article", undefined, "message assistant-message");
  const heading = searchOnly ? "检索结果" : "KBase";
  reply.append(element("h2", heading, "message-author"));
  const cards = new Map();
  const sources = element("div", undefined, "sources");
  for (const source of result.sources) {
    const card = element("details", undefined, "source");
    card.open = searchOnly;
    const ranks = [];
    if (source.keyword_rank) ranks.push(`关键词第 ${source.keyword_rank} 名`);
    if (source.semantic_rank) ranks.push(`语义第 ${source.semantic_rank} 名`);
    card.append(element("summary", `[${source.reference}] ${source.filename}`),
      element("p", source.location), element("p", ranks.join(" · ")),
      element("blockquote", source.text), downloadLink(library, source.document_id, "下载原文"));
    cards.set(String(source.reference), card);
    sources.append(card);
  }
  if (result.answer) {
    const answer = element("div", undefined, "answer-text");
    const blocks = result.answer.split(/```[^\n]*\n([\s\S]*?)```/g);
    for (const [index, block] of blocks.entries()) {
      if (index % 2) {
        const pre = element("pre");
        pre.append(element("code", block.trimEnd()));
        answer.append(pre);
        continue;
      }
      for (const part of block.split(/(\[\d+\])/g)) {
        const target = cards.get(part.slice(1, -1));
        if (!target || !/^\[\d+\]$/.test(part)) {
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
    reply.append(answer);
  }
  if (result.notice) reply.append(element("p", result.notice, "notice error"));
  if (cards.size) {
    const title = element("p", `知识库片段 · ${cards.size}`, "source-title");
    reply.append(title, sources);
  } else if (searchOnly || (result.searched && !result.notice)) {
    reply.append(element("p", "当前知识库没有检索到匹配片段。", "hint"));
  }
  transcript.append(reply);
}

function scrollToLatest() {
  get("composer-dock").scrollIntoView({block: "end"});
}

queryForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (queryForm.getAttribute("aria-busy") === "true") return;
  const library = currentLibrary();
  const question = questionInput.value.trim();
  const searchOnly = event.submitter?.value === "search";
  if (!question || (searchOnly && (!library || question.length > 500))) {
    const message = !question ? "请输入消息。" : !library ? "请先选择知识库。" : "检索词最多 500 字。";
    notice(queryStatus, message, true);
    return;
  }
  const ticket = ++requestNumber;
  const mode = get("mode").value;
  const request = searchOnly
    ? api(`/libraries/${library.id}/query`, jsonRequest({question, mode, answer: false}))
    : api("/chat", jsonRequest({question, mode, history, library_id: knowledgeSwitch.checked ? library?.id : null}));
  const userMessage = element("p", question, "message user-message");
  transcript.append(userMessage);
  questionInput.value = "";
  queryPending(true);
  get("welcome").hidden = true;
  notice(queryStatus, searchOnly ? "正在检索资料…" : "KBase 正在回答…");
  scrollToLatest();
  try {
    const result = await request;
    if (ticket !== requestNumber) return;
    if (!searchOnly && result.answer) {
      history = [...history,
        {role: "user", content: question.slice(0, 1200)},
        {role: "assistant", content: result.answer.slice(0, 1200)},
      ].slice(-8);
    }
    showReply(result, library?.id, searchOnly);
    notice(queryStatus);
    scrollToLatest();
  } catch (error) {
    if (ticket === requestNumber) {
      userMessage.remove();
      if (!questionInput.value) questionInput.value = question;
      get("welcome").hidden = transcript.childElementCount > 0;
      notice(queryStatus, error.message, true);
    }
  } finally {
    if (ticket === requestNumber) queryPending(false);
  }
});

async function importFiles(files) {
  const library = currentLibrary();
  if (!library || !files.length || uploading) return;
  uploading = true;
  get("choose-files").disabled = true;
  get("side-upload").disabled = true;
  notice(get("side-notice"), `正在导入 ${files.length} 份文件…`);
  uploadLog.hidden = false;
  uploadLog.replaceChildren();
  let imported = 0;
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
        imported += 1;
      } catch (error) {
        notice(entry, `${file.name} · ${error.message}`, true);
      }
    }
    if (imported) await refreshDocuments();
    const failed = files.length - imported;
    notice(get("side-notice"), failed ? `已导入 ${imported} 份，失败 ${failed} 份。详情见文档管理。`
      : `已导入 ${imported} 份文件。`, Boolean(failed));
  } catch (error) {
    reportDocumentError(error);
  } finally {
    uploading = false;
    get("choose-files").disabled = false;
    get("side-upload").disabled = !currentLibrary();
    fileInput.value = "";
  }
}

async function updateCloudNotice() {
  try {
    const models = await api("/models");
    get("cloud-note").hidden = models.chat.provider !== "deepseek";
  } catch {
    get("cloud-note").hidden = false;
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
  resetConversation();
  get("side-files").replaceChildren();
  notice(get("side-notice"));
  loadDocuments().catch(reportDocumentError);
});
get("new-chat").addEventListener("click", () => {
  resetConversation();
  questionInput.focus();
});
knowledgeSwitch.addEventListener("change", () => {
  get("search-options").hidden = !knowledgeSwitch.checked;
});
questionInput.addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  queryForm.requestSubmit(queryForm.querySelector('button[value="answer"]'));
});
document.querySelectorAll("[data-prompt]").forEach(button => {
  button.addEventListener("click", () => {
    questionInput.value = button.dataset.prompt;
    questionInput.focus();
  });
});
get("choose-files").addEventListener("click", () => fileInput.click());
get("side-upload").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => importFiles([...fileInput.files]));
document.querySelectorAll(".drop-zone").forEach(zone => {
  zone.addEventListener("dragover", event => {
    event.preventDefault();
    zone.classList.add("dragging");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
  zone.addEventListener("drop", event => {
    event.preventDefault();
    zone.classList.remove("dragging");
    importFiles([...event.dataTransfer.files]);
  });
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
    resetConversation();
    showView("documents");
    await loadDocuments().catch(reportDocumentError);
  } catch (error) {
    notice(get("library-error"), error.message, true);
  } finally {
    button.disabled = false;
  }
});

loadLibraries().then(loadDocuments).catch(reportDocumentError);
updateCloudNotice();
