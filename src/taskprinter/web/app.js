"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  jobs: new Map(),
  printer: null,
  settings: null,
  previewAction: null,
  previewUrl: null,
};

/* ---------------- helpers ---------------- */

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = "GET", json, form, raw = false } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (json !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(json);
  } else if (form) {
    opts.body = form;
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError(0, "Can't reach the server");
  }
  if (res.status === 401) {
    showLogin();
    throw new ApiError(401, "Please sign in");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* not JSON */ }
    throw new ApiError(res.status, msg);
  }
  return raw ? res : res.json();
}

function toast(message, kind = "info", ms = 3200) {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  $("#toasts").append(node);
  setTimeout(() => node.remove(), ms);
}

const fail = (err) => { if (err.status !== 401) toast(err.message, "error", 5000); };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c != null && c !== false));
  return node;
}

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

const store = {  // per-device conveniences only; failures are harmless
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
};

const copiesOf = (input) => Math.max(1, Math.min(20, +(input?.value || 1)));

/* ---------------- navigation ---------------- */

const VIEWS = ["print", "history", "printer"];

function go(view) {
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  $$(".tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.go === view));
  history.replaceState(null, "", view === "print" ? location.pathname + location.search : `#${view}`);
  if (view === "history") loadJobs();
  if (view === "printer") { loadSettings(); loadLog(); }
  if (view === "print") loadTemplates();
  window.scrollTo({ top: 0 });
}

document.addEventListener("click", (e) => {
  const target = e.target.closest("[data-go]");
  if (target) go(target.dataset.go);
});

window.addEventListener("hashchange", () => {
  const view = location.hash.slice(1);
  go(VIEWS.includes(view) ? view : "print");
});

/* ---------------- auth ---------------- */

function showLogin() {
  const dlg = $("#loginDialog");
  if (!dlg.open) dlg.showModal();
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const token = new FormData(e.target).get("token");
  try {
    await api("/api/login", { method: "POST", json: { token } });
    $("#loginDialog").close();
    $("#loginError").hidden = true;
    boot();
  } catch (err) {
    $("#loginError").textContent = err.message;
    $("#loginError").hidden = false;
  }
});

$("#logoutBtn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.reload();
});

/* ---------------- printer status ---------------- */

const STATE_LABEL = {
  disconnected: "Idle", connecting: "Connecting…", connected: "Connected",
  printing: "Printing…", scanning: "Scanning…", error: "Error",
};

function renderPrinter(p) {
  state.printer = p;
  const pill = $("#statusPill");
  pill.dataset.state = p.state;
  let text = STATE_LABEL[p.state] || p.state;
  if (p.status?.battery) text += ` · ${p.status.battery}%`;
  if (p.status?.paper_out) text = "Out of paper";
  $("#statusText").textContent = text;

  $("#printerName").textContent = p.name || (p.backend === "mock" ? "Mock printer" : "Printer");
  const meta = [p.address || "no printer selected", p.backend === "mock" ? "mock backend" : "Bluetooth LE"];
  if (p.status?.battery) meta.push(`battery ${p.status.battery}%`);
  $("#printerMeta").textContent = meta.join(" · ");
  $("#printerError").hidden = !p.error || p.state !== "error";
  $("#printerError").textContent = p.error || "";
  $("#connectBtn").disabled = ["connected", "printing", "connecting"].includes(p.state);
  $("#disconnectBtn").disabled = !["connected", "error"].includes(p.state);
}

$("#connectBtn").addEventListener("click", async () => {
  try {
    renderPrinter(await api("/api/printer/connect", { method: "POST", json: {} }));
    toast("Printer connected");
  } catch (err) { fail(err); }
});

$("#disconnectBtn").addEventListener("click", () =>
  api("/api/printer/disconnect", { method: "POST" }).then(renderPrinter).catch(fail));

$("#scanBtn").addEventListener("click", async () => {
  const btn = $("#scanBtn");
  btn.disabled = true;
  btn.textContent = "Scanning…";
  const list = $("#deviceList");
  list.replaceChildren();
  try {
    const devices = await api(`/api/printer/scan?show_all=${$("#scanAll").checked}`, { method: "POST" });
    if (!devices.length) list.append(el("li", { class: "muted" }, "No printers found. Is it on and not connected to your phone?"));
    for (const d of devices) {
      list.append(el("li", { class: "device" },
        el("div", {}, d.name, el("small", {}, `${d.address} · RSSI ${d.rssi ?? "?"}`)),
        el("button", {
          class: "btn", onclick: async () => {
            try {
              renderPrinter(await api("/api/printer/connect", { method: "POST", json: { address: d.address } }));
              toast(`Connected to ${d.name}`);
              loadSettings();
            } catch (err) { fail(err); }
          },
        }, "Use"),
      ));
    }
  } catch (err) { fail(err); }
  btn.disabled = false;
  btn.textContent = "Scan for printers";
});

async function loadLog() {
  try {
    const entries = await api("/api/printer/log");
    $("#printerLog").textContent = entries
      .map((e) => `${new Date(e.t * 1000).toLocaleTimeString()}  ${e.msg}`).join("\n") || "(empty)";
  } catch (err) { fail(err); }
}
$("#refreshLog").addEventListener("click", loadLog);

/* ---------------- settings ---------------- */

const TUNING_KEYS = ["energy", "feed", "feed_mode", "encoding", "speed", "backlash", "printer_address",
  "rotate180", "rewind", "mirror"];
const PAPER_KEYS = ["paper", "gap", "separator", "label_width", "label_height", "label_gap", "label_offset"];

function fillForm(f, values) {
  for (const [k, v] of Object.entries(values)) {
    const input = f.elements[k];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!v;
    else input.value = v ?? "";
  }
}

function readForm(f, keys) {
  const out = {};
  for (const k of keys) {
    const input = f.elements[k];
    if (!input) continue;
    out[k] = input.type === "checkbox" ? input.checked : input.type === "number" || input.type === "range"
      ? +input.value : input.value;
  }
  return out;
}

async function loadSettings() {
  try {
    const s = state.settings = await api("/api/settings");
    fillForm($("#settingsForm"), Object.fromEntries(TUNING_KEYS.map((k) => [k, s[k]])));
    fillForm($("#layoutForm"), Object.fromEntries(PAPER_KEYS.map((k) => [k, s[k]])));
    showPaper(s.paper || "continuous");
    syncValues($("#settingsForm"));
    return s;
  } catch (err) { fail(err); return null; }
}

async function saveSettings(values, message) {
  try {
    state.settings = await api("/api/settings", { method: "PUT", json: values });
    toast(message);
    schedulePreview();
  } catch (err) { fail(err); }
}

$("#settingsForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const body = readForm(e.target, TUNING_KEYS);
  body.printer_address = String(body.printer_address || "").trim();
  saveSettings(body, "Settings saved");
});

function showPaper(paper) {
  $("#layoutForm").paper.value = paper;
  $$("#paperMode .chip").forEach((c) => c.classList.toggle("active", c.dataset.paper === paper));
  $$("[data-for-paper]").forEach((n) => (n.hidden = n.dataset.forPaper !== paper));
}
$("#paperMode").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-paper]");
  if (chip) showPaper(chip.dataset.paper);
});
$("#layoutForm").addEventListener("submit", (e) => {
  e.preventDefault();
  saveSettings(readForm(e.target, PAPER_KEYS), "Paper saved");
});

/* live values beside range sliders */
function syncValues(root = document) {
  $$(".val[data-for]", root).forEach((span) => {
    const input = span.closest("label").querySelector("input");
    if (input) span.textContent = input.value;
  });
}
document.addEventListener("input", (e) => {
  if (e.target.type === "range") syncValues(e.target.closest("form") || document);
});

/* ---------------- preview dialog ---------------- */

async function openPreview(fetchPreview, printAction) {
  try {
    const res = await fetchPreview();
    const blob = await res.blob();
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = URL.createObjectURL(blob);
    $("#previewImg").src = state.previewUrl;
    state.previewAction = printAction;
    $("#previewDialog").showModal();
  } catch (err) { fail(err); }
}

$("#closePreview").addEventListener("click", () => $("#previewDialog").close());
$("#previewPrint").addEventListener("click", async () => {
  $("#previewDialog").close();
  if (state.previewAction) await state.previewAction();
});

function queued(job) {
  state.jobs.set(job.id, job);
  toast(job.copies > 1 ? `Queued ${job.copies} copies` : `Queued: ${job.title}`);
  return job;
}

/* ---------------- print modes ---------------- */

function showMode(mode) {
  $$("#printMode .chip").forEach((c) => c.classList.toggle("active", c.dataset.mode === mode));
  $$("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== mode));
  store.set("tp-mode", mode);
  if (mode === "doc") schedulePreview();
}
$("#printMode").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-mode]");
  if (chip) showMode(chip.dataset.mode);
});

/* ---------------- formatting editor ---------------- */

const editor = $("#editor");
try {
  document.execCommand("defaultParagraphSeparator", false, "p");
  document.execCommand("styleWithCSS", false, false);
} catch { /* older browsers */ }

const ALIGN = { left: "left", center: "center", right: "right", start: "left", end: "right", justify: "left" };

function closestIn(node, selector) {
  const elNode = node?.nodeType === 1 ? node : node?.parentElement;
  const found = elNode?.closest(selector);
  return found && editor.contains(found) ? found : null;
}

function selectionNode() {
  const sel = getSelection();
  return sel.rangeCount && editor.contains(sel.anchorNode) ? sel.anchorNode : null;
}

/* toolbar */
function runCommand(cmd) {
  editor.focus();
  const node = selectionNode();
  if (cmd === "code") return toggleInlineCode();
  if (cmd === "checklist") {
    const list = closestIn(node, "ul");
    if (list?.classList.contains("checklist")) { list.classList.remove("checklist"); return; }
    if (!list) document.execCommand("insertUnorderedList");
    closestIn(selectionNode(), "ul")?.classList.add("checklist");
    return;
  }
  if ((cmd === "indent" || cmd === "outdent") && !closestIn(node, "li")) return;  // only lists nest
  if (cmd === "removeFormat") {
    document.execCommand("removeFormat");
    document.execCommand("formatBlock", false, "<p>");
    return;
  }
  document.execCommand(cmd);
}

function toggleInlineCode() {
  const sel = getSelection();
  if (!sel.rangeCount) return;
  const existing = closestIn(sel.anchorNode, "code");
  if (existing && !closestIn(sel.anchorNode, "pre")) {
    existing.replaceWith(...existing.childNodes);
    return;
  }
  const range = sel.getRangeAt(0);
  if (range.collapsed) return;
  const code = document.createElement("code");
  code.append(range.extractContents());
  range.insertNode(code);
  sel.removeAllRanges();
  const after = document.createRange();
  after.selectNodeContents(code);
  sel.addRange(after);
}

$("#toolbar").addEventListener("mousedown", (e) => {
  if (e.target.closest("button")) e.preventDefault();  // keep the editor selection
});
$("#toolbar").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-cmd]");
  if (!btn) return;
  runCommand(btn.dataset.cmd);
  editorChanged();
});
$("#blockType").addEventListener("change", (e) => {
  editor.focus();
  document.execCommand("formatBlock", false, `<${e.target.value}>`);
  editorChanged();
});

function updateToolbar() {
  const node = selectionNode();
  if (!node) return;
  for (const btn of $$("#toolbar [data-cmd]")) {
    const cmd = btn.dataset.cmd;
    let on = false;
    if (["bold", "italic", "underline", "strikeThrough"].includes(cmd)) {
      try { on = document.queryCommandState(cmd); } catch { /* unsupported */ }
    } else if (cmd === "code") on = !!closestIn(node, "code");
    else if (cmd === "checklist") on = !!closestIn(node, "ul.checklist");
    else if (cmd === "insertUnorderedList") on = !!closestIn(node, "ul:not(.checklist)");
    else if (cmd === "insertOrderedList") on = !!closestIn(node, "ol");
    else if (cmd.startsWith("justify")) {
      const block = closestIn(node, "p,h1,h2,h3,blockquote,div");
      const align = ALIGN[block?.style.textAlign || "left"] || "left";
      on = cmd === { left: "justifyLeft", center: "justifyCenter", right: "justifyRight" }[align];
    }
    btn.classList.toggle("on", on);
  }
  const block = closestIn(node, "h1,h2,h3,blockquote,pre");
  $("#blockType").value = block ? block.tagName.toLowerCase() : "p";
}
document.addEventListener("selectionchange", updateToolbar);

/* tick boxes in checklists: click the box area of an item */
editor.addEventListener("click", (e) => {
  const li = e.target.closest("ul.checklist > li");
  if (!li || e.target !== li) return;
  const left = li.getBoundingClientRect().left;
  if (e.clientX - left > 28) return;
  li.dataset.checked = li.dataset.checked === "true" ? "false" : "true";
  editorChanged();
});

/* paste as plain text so web pages don't bring their styling along */
editor.addEventListener("paste", (e) => {
  const text = e.clipboardData?.getData("text/plain");
  if (text == null || [...(e.clipboardData.files || [])].length) return;
  e.preventDefault();
  document.execCommand("insertText", false, text);
});

editor.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); printDoc(); }
  if (e.key === "Tab" && closestIn(selectionNode(), "li")) {
    e.preventDefault();
    document.execCommand(e.shiftKey ? "outdent" : "indent");
    editorChanged();
  }
});

editor.addEventListener("input", () => {
  // new items split from a ticked one shouldn't start ticked
  for (const li of $$("ul.checklist > li[data-checked='true']", editor)) {
    if (!li.textContent.trim()) li.dataset.checked = "false";
  }
  editorChanged();
});

function editorChanged() {
  editor.classList.toggle("empty", !editor.textContent.trim() && !$("hr,img", editor));
  store.set("tp-draft", editor.innerHTML);
  schedulePreview();
}

/* editor DOM -> print blocks */
const INLINE = { B: "b", STRONG: "b", I: "i", EM: "i", U: "u", S: "s", STRIKE: "s", DEL: "s", CODE: "code" };

function inlineRuns(node, style = {}, runs = []) {
  for (const child of node.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) {
      if (child.data) runs.push({ text: child.data.replace(/ /g, " "), ...style });
      continue;
    }
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const tag = child.tagName;
    if (tag === "BR") { runs.push({ text: "\n", ...style }); continue; }
    if (tag === "UL" || tag === "OL") continue;  // nested lists are separate blocks
    const s = { ...style };
    if (INLINE[tag]) s[INLINE[tag]] = true;
    const css = child.style;
    if (css) {
      if (css.fontWeight === "bold" || +css.fontWeight >= 600) s.b = true;
      if (css.fontStyle === "italic") s.i = true;
      const deco = css.textDecorationLine || css.textDecoration || "";
      if (deco.includes("underline")) s.u = true;
      if (deco.includes("line-through")) s.s = true;
    }
    if ((tag === "P" || tag === "DIV") && runs.length) runs.push({ text: "\n", ...style });
    inlineRuns(child, s, runs);
  }
  return runs;
}

function trimRuns(runs) {
  while (runs.length && /^\n*$/.test(runs.at(-1).text)) runs.pop();  // browsers add a trailing <br>
  return runs;
}

const alignOf = (node) => ALIGN[node.style?.textAlign] || "";

const BLOCK_TAGS = new Set(["P", "DIV", "UL", "OL", "H1", "H2", "H3", "H4", "H5", "H6", "BLOCKQUOTE", "PRE", "HR"]);

/* Browsers sometimes nest blocks in paragraphs (Chrome: <p><ul>…</ul></p>). Lift them out so
   every block sits at the top level; inline content around them becomes its own paragraph. */
function flattenBlocks(root) {
  for (const node of [...root.children]) {
    if (!["P", "DIV"].includes(node.tagName) || ![...node.children].some((c) => BLOCK_TAGS.has(c.tagName))) continue;
    const parts = [];
    let para = null;
    for (const child of [...node.childNodes]) {
      if (child.nodeType === Node.ELEMENT_NODE && BLOCK_TAGS.has(child.tagName)) {
        para = null;
        parts.push(child);
      } else {
        if (!para) {
          para = document.createElement("p");
          para.style.textAlign = node.style.textAlign;
          parts.push(para);
        }
        para.append(child);
      }
    }
    node.replaceWith(...parts.filter((p) => p.tagName !== "P" || p.textContent.trim() || p.querySelector("br")));
    flattenBlocks(root);  // the lifted blocks may themselves contain nested blocks
    return;
  }
}

function serialize(source = editor) {
  const root = source.cloneNode(true);
  flattenBlocks(root);
  const blocks = [];
  const walkList = (list, level) => {
    const type = list.tagName === "OL" ? "number" : list.classList.contains("checklist") ? "check" : "bullet";
    for (const item of list.children) {
      if (item.tagName === "UL" || item.tagName === "OL") { walkList(item, level + 1); continue; }
      if (item.tagName !== "LI") continue;
      blocks.push({ type, level: Math.min(level, 3), runs: trimRuns(inlineRuns(item)),
        checked: item.dataset.checked === "true" });
      for (const sub of item.children) {
        if (sub.tagName === "UL" || sub.tagName === "OL") walkList(sub, level + 1);
      }
    }
  };
  for (const node of root.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.data.trim()) blocks.push({ type: "p", runs: [{ text: node.data }] });
      continue;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) continue;
    const tag = node.tagName;
    if (/^H[1-6]$/.test(tag)) {
      blocks.push({ type: `h${Math.min(3, +tag[1])}`, runs: trimRuns(inlineRuns(node)), align: alignOf(node) });
    } else if (tag === "BLOCKQUOTE") {
      blocks.push({ type: "quote", runs: trimRuns(inlineRuns(node)), align: alignOf(node) });
    } else if (tag === "PRE") {
      blocks.push({ type: "code", text: node.innerText.replace(/\n$/, "") });
    } else if (tag === "HR") {
      blocks.push({ type: "hr" });
    } else if (tag === "UL" || tag === "OL") {
      walkList(node, 0);
    } else if (node.childNodes.length) {  // a typed blank line is <p><br></p>; bare <p></p> is parser debris
      blocks.push({ type: "p", runs: trimRuns(inlineRuns(node)), align: alignOf(node) });
    }
  }
  while (blocks.length && blocks.at(-1).type === "p" && !blocks.at(-1).runs.some((r) => r.text.trim())) blocks.pop();
  return blocks;
}

/* print blocks -> editor DOM (for templates) */
function runsToNodes(runs = []) {
  return runs.map((r) => {
    let node = document.createTextNode(r.text);
    if (r.text === "\n") node = document.createElement("br");
    for (const [key, tag] of [["code", "code"], ["s", "s"], ["u", "u"], ["i", "i"], ["b", "b"]]) {
      if (r[key]) { const wrap = document.createElement(tag); wrap.append(node); node = wrap; }
    }
    return node;
  });
}

function deserialize(blocks = []) {
  const frag = document.createDocumentFragment();
  let stack = [];  // open lists: [{level, type, el}]
  for (const b of blocks) {
    const listType = { bullet: "UL", number: "OL", check: "UL" }[b.type];
    if (!listType) {
      stack = [];
      let node;
      if (b.type === "hr") node = document.createElement("hr");
      else if (b.type === "code") { node = document.createElement("pre"); node.textContent = b.text || ""; }
      else {
        node = document.createElement({ h1: "h1", h2: "h2", h3: "h3", quote: "blockquote" }[b.type] || "p");
        node.append(...runsToNodes(b.runs));
        if (!node.childNodes.length) node.append(document.createElement("br"));
        if (b.align) node.style.textAlign = b.align;
      }
      frag.append(node);
      continue;
    }
    const level = b.level || 0;
    while (stack.length && (stack.at(-1).level > level ||
      (stack.at(-1).level === level && stack.at(-1).type !== b.type))) stack.pop();
    if (!stack.length || stack.at(-1).level < level) {
      const list = document.createElement(listType);
      if (b.type === "check") list.className = "checklist";
      const parentItem = stack.length ? stack.at(-1).el.lastElementChild : null;
      (parentItem || frag).append(list);
      stack.push({ level, type: b.type, el: list });
    }
    const li = document.createElement("li");
    li.append(...runsToNodes(b.runs));
    if (b.type === "check") li.dataset.checked = String(!!b.checked);
    stack.at(-1).el.append(li);
  }
  return frag;
}

function setEditor(blocks) {
  editor.replaceChildren(deserialize(blocks));
  editorChanged();
}

/* format panel */
const formatForm = $("#formatForm");
const FORMAT_KEYS = ["font", "size", "align", "line_spacing", "paragraph_spacing", "margin", "border",
  "invert", "timestamp"];
const formatBody = () => readForm(formatForm, FORMAT_KEYS);

function applyFormat(fmt) {
  fillForm(formatForm, fmt);
  syncValues(formatForm);
  editor.dataset.font = fmt.font || "sans";
  editor.style.textAlign = fmt.align || "left";
}

formatForm.addEventListener("input", () => {
  editor.dataset.font = formatForm.font.value;
  editor.style.textAlign = formatForm.align.value;
  store.set("tp-format", JSON.stringify(formatBody()));
  schedulePreview();
});
$("#formatSave").addEventListener("click", () => saveSettings({ format: formatBody() }, "Saved as your default format"));
$("#formatReset").addEventListener("click", async () => {
  const s = await loadSettings();
  if (s) { applyFormat(s.format); store.set("tp-format", ""); schedulePreview(); }
});

/* live preview + print */
const docBody = () => ({ blocks: serialize(), format: formatBody() });

let previewTimer, previewSeq = 0;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 450);
}

async function refreshPreview() {
  if ($("[data-pane='doc']").hidden) return;
  const body = docBody();
  const img = $("#docPreview");
  $("#docPreviewEmpty").hidden = body.blocks.length > 0;
  img.hidden = !body.blocks.length;
  if (!body.blocks.length) return;
  const seq = ++previewSeq;
  try {
    const res = await api("/api/preview/doc", { method: "POST", json: body, raw: true });
    const blob = await res.blob();
    if (seq !== previewSeq) return;  // a newer preview is on its way
    if (img.src) URL.revokeObjectURL(img.src);
    img.src = URL.createObjectURL(blob);
  } catch (err) { if (err.status !== 422) fail(err); }
}

async function printDoc() {
  const body = docBody();
  if (!body.blocks.length) { toast("Nothing to print yet"); editor.focus(); return; }
  try {
    queued(await api("/api/print/doc", { method: "POST", json: { ...body, copies: copiesOf($("#docCopies")) } }));
  } catch (err) { fail(err); }
}
$("#docPrint").addEventListener("click", printDoc);
$("#docClear").addEventListener("click", () => {
  if (editor.textContent.trim() && !confirm("Clear the editor?")) return;
  editor.replaceChildren();
  editorChanged();
  editor.focus();
});
$("#docSaveTemplate").addEventListener("click", () => saveTemplate("doc", docBody()));

/* ---------------- QR and banner ---------------- */

const SIMPLE_MODES = {
  qr: {
    form: "#qrForm",
    body: (f) => ({ data: f.data.value, caption: f.caption.value, scale: +f.scale.value }),
    fill: (f, p) => { f.data.value = p.data || ""; f.caption.value = p.caption || ""; f.scale.value = p.scale || 1; },
  },
  banner: {
    form: "#bannerForm",
    body: (f) => ({ text: f.text.value, font: f.font.value }),
  },
};

for (const [mode, m] of Object.entries(SIMPLE_MODES)) {
  const f = $(m.form);
  const print = async () => {
    try { queued(await api(`/api/print/${mode}`, { method: "POST", json: { ...m.body(f), copies: copiesOf(f.copies) } })); }
    catch (err) { fail(err); }
  };
  f.addEventListener("submit", (e) => { e.preventDefault(); print(); });
  $("[data-preview]", f).addEventListener("click", () => {
    if (!f.reportValidity()) return;
    openPreview(() => api(`/api/preview/${mode}`, { method: "POST", json: m.body(f), raw: true }), print);
  });
  $("[data-save-template]", f)?.addEventListener("click", () => {
    if (f.reportValidity()) saveTemplate(mode, m.body(f));
  });
}

/* ---------------- templates ---------------- */

async function saveTemplate(kind, payload) {
  if (kind === "doc" && !payload.blocks.length) { toast("Nothing to save yet"); return; }
  const name = prompt("Template name");
  if (!name?.trim()) return;
  try {
    await api("/api/templates", { method: "POST", json: { name: name.trim(), kind, payload } });
    toast(`Saved template "${name.trim()}"`);
    loadTemplates();
  } catch (err) { fail(err); }
}

function loadTemplate(t) {
  if (t.kind === "doc") {
    showMode("doc");
    setEditor(t.payload.blocks);
    if (t.payload.format) applyFormat({ ...formatBody(), ...t.payload.format });
  } else if (SIMPLE_MODES[t.kind]?.fill) {
    showMode(t.kind);
    SIMPLE_MODES[t.kind].fill($(SIMPLE_MODES[t.kind].form), t.payload);
    syncValues($(SIMPLE_MODES[t.kind].form));
  } else {
    toast("This template can only be printed");
    return;
  }
  toast(`Loaded "${t.name}"`);
}

const KIND_LABEL = { doc: "text", note: "note", qr: "QR label" };

async function loadTemplates() {
  try {
    const templates = await api("/api/templates");
    $("#templatesCard").hidden = !templates.length;
    $("#templateList").replaceChildren(...templates.map((t) => el("li", { class: "template" },
      el("button", { class: "template-name", title: "Load into the editor", onclick: () => loadTemplate(t) },
        el("small", {}, KIND_LABEL[t.kind] || t.kind), t.name),
      el("button", { class: "btn ghost", title: "Print now", onclick: async () => {
        try { queued(await api(`/api/templates/${t.id}/print`, { method: "POST" })); } catch (err) { fail(err); }
      } }, "Print"),
      el("button", { class: "icon-btn", "aria-label": "Delete template", onclick: async () => {
        if (!confirm(`Delete template "${t.name}"?`)) return;
        try { await api(`/api/templates/${t.id}`, { method: "DELETE" }); loadTemplates(); } catch (err) { fail(err); }
      } }, "✕"),
    )));
  } catch (err) { fail(err); }
}

/* ---------------- image ---------------- */

const imageForm = $("#imageForm");
function imageFormData() {
  const f = imageForm;
  const fd = new FormData();
  fd.append("file", f.file.files[0]);
  for (const k of ["dither", "threshold", "brightness", "contrast"]) fd.append(k, f[k].value);
  fd.append("autolevel", f.autolevel.checked);
  fd.append("rotate", f.rotate.checked);
  fd.append("copies", copiesOf(f.copies));
  return fd;
}

let imageTimer;
function scheduleImagePreview() {
  clearTimeout(imageTimer);
  if (!imageForm.file.files[0]) return;
  imageTimer = setTimeout(async () => {
    try {
      const res = await api("/api/preview/image", { method: "POST", form: imageFormData(), raw: true });
      const img = $("#imagePreview");
      if (img.src) URL.revokeObjectURL(img.src);
      img.src = URL.createObjectURL(await res.blob());
      img.hidden = false;
    } catch (err) { fail(err); }
  }, 350);
}
imageForm.addEventListener("change", scheduleImagePreview);
imageForm.addEventListener("input", (e) => { if (e.target.type === "range") scheduleImagePreview(); });
imageForm.file.addEventListener("change", () => {
  const file = imageForm.file.files[0];
  $("#dropText").textContent = file ? file.name : "Choose, drop or paste a picture";
});
imageForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!imageForm.file.files[0]) return;
  try { queued(await api("/api/print/image", { method: "POST", form: imageFormData() })); } catch (err) { fail(err); }
});

const drop = $("#dropZone");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  if (!e.dataTransfer.files.length) return;
  imageForm.file.files = e.dataTransfer.files;
  imageForm.file.dispatchEvent(new Event("change", { bubbles: true }));
});

/* paste an image anywhere on the Print tab */
document.addEventListener("paste", (e) => {
  if ($("#view-print").hidden) return;
  const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith("image/"));
  if (!file) return;
  e.preventDefault();
  const dt = new DataTransfer();
  dt.items.add(file);
  imageForm.file.files = dt.files;
  showMode("image");
  imageForm.file.dispatchEvent(new Event("change", { bubbles: true }));
});

$("#testBtn").addEventListener("click", () => openPreview(
  () => api("/api/preview/test", { raw: true }),
  async () => { try { queued(await api("/api/print/test", { method: "POST" })); } catch (err) { fail(err); } },
));
$("#feedBtn").addEventListener("click", async () => {
  try { queued(await api("/api/print/feed", { method: "POST", json: { amount: 20 } })); } catch (err) { fail(err); }
});

/* ---------------- history ---------------- */

async function loadJobs() {
  try {
    const jobs = await api("/api/jobs?limit=60");
    state.jobs = new Map(jobs.map((j) => [j.id, j]));
    renderJobs();
  } catch (err) { fail(err); }
}

function renderJobs() {
  const jobs = [...state.jobs.values()].sort((a, b) => b.id - a.id);
  $("#jobList").replaceChildren(...jobs.map(jobItem));
  $("#jobsEmpty").hidden = jobs.length > 0;
}

function jobItem(job) {
  const actions = [];
  if (job.status === "queued") {
    actions.push(el("button", { class: "btn ghost", onclick: () =>
      api(`/api/jobs/${job.id}/cancel`, { method: "POST" }).then(updateJob).catch(fail) }, "Cancel"));
  } else if (job.kind !== "feed" && job.status !== "printing") {
    actions.push(el("button", { class: "btn ghost", onclick: () =>
      api(`/api/jobs/${job.id}/reprint`, { method: "POST" }).then(queued).then(renderJobs).catch(fail) }, "Reprint"));
  }
  const thumb = job.kind === "feed"
    ? el("div", { class: "muted" }, "⇣")
    : el("img", { src: `/api/jobs/${job.id}/preview.png`, alt: "", loading: "lazy" });
  return el("li", { class: "job", id: `job-${job.id}` },
    thumb,
    el("div", {},
      el("div", { class: "job-title" }, job.title),
      el("div", { class: "job-meta" },
        el("span", { class: `status ${job.status}` }, job.status), ` · ${job.kind} · ${timeAgo(job.created_at)}`),
      job.error ? el("div", { class: "error-text" }, job.error) : null,
      job.status === "printing" ? el("div", { class: "progress" }, el("div", { style: `width:${Math.round((job.progress || 0) * 100)}%` })) : null,
    ),
    el("div", { class: "actions" }, actions),
  );
}

function updateJob(job) {
  const prev = state.jobs.get(job.id);
  state.jobs.set(job.id, { ...prev, ...job });
  if (!$("#view-history").hidden) renderJobs();
  if (prev && prev.status !== job.status) {
    if (job.status === "done") toast(`Printed: ${job.title}`);
    if (job.status === "failed") toast(`Print failed: ${job.error || job.title}`, "error", 6000);
  }
}

/* ---------------- live events ---------------- */

let source;
function connectEvents() {
  if (source) source.close();
  source = new EventSource("/api/events");
  source.addEventListener("hello", (e) => renderPrinter(JSON.parse(e.data).printer));
  source.addEventListener("printer", (e) => renderPrinter(JSON.parse(e.data)));
  source.addEventListener("job", (e) => updateJob(JSON.parse(e.data)));
  source.onerror = () => {
    // EventSource retries by itself; if we're logged out, stop hammering.
    if (source.readyState === EventSource.CLOSED) setTimeout(connectEvents, 5000);
  };
}

/* ---------------- share target ---------------- */

async function handleShareTarget() {
  const params = new URLSearchParams(location.search);
  if (params.has("shared")) toast("Shared item queued for printing");
  if (!params.has("share-target") || !("caches" in window)) return;
  try {
    const cache = await caches.open("share-target");
    const res = await cache.match("/shared-payload");
    if (!res) return;
    await cache.delete("/shared-payload");
    const fd = await res.formData();
    const r = await fetch("/share", { method: "POST", body: fd, credentials: "same-origin" });
    if (r.ok && !r.url.includes("login")) toast("Shared item queued for printing");
    else showLogin();
  } catch (err) { toast(`Share failed: ${err.message}`, "error"); }
}

/* ---------------- PWA install ---------------- */

let installPrompt;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installPrompt = e;
  $("#installBtn").hidden = false;
});
$("#installBtn").addEventListener("click", async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  $("#installBtn").hidden = true;
});

function updateOnline() { $("#offlineBanner").hidden = navigator.onLine; }
window.addEventListener("online", () => { updateOnline(); boot(); });
window.addEventListener("offline", updateOnline);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((err) => console.warn("SW registration failed", err));
}

/* ---------------- boot ---------------- */

async function boot() {
  updateOnline();
  let me;
  try {
    me = await api("/api/me");
  } catch (err) { fail(err); return; }
  $("#versionText").textContent = `v${me.version}`;
  $("#logoutBtn").hidden = !me.auth_required;
  if (!me.authenticated) { showLogin(); return; }

  connectEvents();
  const s = await loadSettings();
  let fmt = s?.format || {};
  try { fmt = { ...fmt, ...JSON.parse(store.get("tp-format") || "{}") }; } catch { /* ignore */ }
  applyFormat(fmt);
  const draft = store.get("tp-draft");
  if (draft && !editor.childNodes.length) editor.innerHTML = draft;
  editorChanged();
  loadTemplates();
  await handleShareTarget();

  const params = new URLSearchParams(location.search);
  if (params.size) history.replaceState(null, "", location.pathname + location.hash);
  const view = location.hash.slice(1);
  if (VIEWS.includes(view) && view !== "print") go(view);
  else showMode(store.get("tp-mode") || "doc");
}

boot();
