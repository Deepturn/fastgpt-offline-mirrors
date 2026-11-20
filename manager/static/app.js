const byId = (id) => document.getElementById(id);
const pageSize = 12;
const state = { kind: "all", view: "catalog", catalog: null, projects: {}, jobs: [],
  job: null, detail: null, submitting: false, expanded: new Set(), page: 0, file: null, loading: false };
const labels = { checking: "预检中", verified: "待发布", rejected: "预检失败", publishing: "发布中",
  published: "已发布", partial: "发布未完成", discarded: "已丢弃", existing: "已存在",
  new: "新增", conflict: "冲突" };
const kinds = { pypi: "PyPI", npm: "NPM" };
const tones = { published: "good", existing: "good", new: "good", verified: "warn",
  checking: "warn", publishing: "warn", partial: "bad", rejected: "bad", conflict: "bad" };
const date = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" });
const collator = new Intl.Collator("en", { numeric: true, sensitivity: "base" });
const icons = {
  layers: ["M12 2 2 7l10 5 10-5-10-5Z", "m2 12 10 5 10-5", "m2 17 10 5 10-5"],
  book: ["M4 19.5A2.5 2.5 0 0 1 6.5 17H20", "M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"],
  lock: ["M3 11h18v11H3z", "M7 11V7a5 5 0 0 1 10 0v4"],
  logout: ["M10 17l5-5-5-5", "M15 12H3", "M14 3h5a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-5"],
  download: ["M12 3v12", "m7 10 5 5 5-5", "M4 16v4h16v-4"],
  upload: ["M12 16V4", "m7 9 5-5 5 5", "M4 16v4h16v-4"],
  refresh: ["M20 7v5h-5", "M4 17v-5h5", "M6 7a7 7 0 0 1 11.5-2L20 8", "m4 16 2.5 3A7 7 0 0 0 18 17"],
  boxes: ["m12 2 8 5-8 5-8-5 8-5Z", "M4 7v10l8 5 8-5V7", "M12 12v10"],
  search: ["M21 21l-4.3-4.3", "M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z"],
  left: ["m15 18-6-6 6-6"], right: ["m9 18 6-6-6-6"],
  copy: ["M8 8h12v12H8z", "M16 4H4v12"], check: ["m5 12 4 4L19 6"],
  file: ["M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z", "M13 2v7h7"],
};

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined && content !== null) node.textContent = content;
  if (className) node.className = className;
  return node;
}

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round",
    "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false" })) svg.setAttribute(key, value);
  for (const path of icons[name]) {
    const part = document.createElementNS(ns, "path");
    part.setAttribute("d", path);
    svg.append(part);
  }
  return svg;
}
document.querySelectorAll("[data-icon]").forEach((node) => node.append(icon(node.dataset.icon)));

function status(label, tone, dot = false) {
  const node = element("span", null, "state");
  if (tone) node.dataset.tone = tone;
  if (dot) node.append(element("span", null, "state-dot"));
  node.append(label);
  return node;
}

function message(id, text = "", tone = "") {
  const node = byId(id);
  node.textContent = text;
  node.hidden = !text;
  node.dataset.tone = tone;
}

async function api(url, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "X-Mirror-Action": "console", "Content-Type": "application/json" }, body,
  };
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || response.statusText);
    error.status = response.status;
    throw error;
  }
  return data;
}

function groupPackages(entries, kind) {
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry.name)) groups.set(entry.name, []);
    groups.get(entry.name).push(entry);
  }
  return [...groups].map(([name, artifacts]) => ({ name, kind, artifacts }));
}

function sourceBadge(kind) {
  const badge = element("span", null, "source-badge");
  badge.dataset.source = kind;
  badge.append(icon("boxes"), kinds[kind]);
  return badge;
}

function renderPackages() {
  if (!state.catalog) return;
  const query = byId("filter").value.trim().toLowerCase();
  const normalize = (name, kind) => kind === "pypi" ? name.toLowerCase().replace(/[-_.]+/g, "-") : name.toLowerCase();
  const matches = state.projects[state.kind].filter(({ name, kind, artifacts }) =>
    normalize(name, kind).includes(normalize(query, kind)) ||
    artifacts.some(({ version }) => version.toLowerCase().includes(query)));
  state.page = Math.min(state.page, Math.max(0, Math.ceil(matches.length / pageSize) - 1));
  const start = state.page * pageSize;
  byId("packages").replaceChildren(...matches.slice(start, start + pageSize).flatMap(({ name, kind, artifacts }, index) => {
    const row = element("tr");
    const cell = element("td");
    const toggle = element("button", null, "package-toggle");
    const detail = element("tr", null, "version-row");
    const key = kind + "/" + name;
    row.dataset.source = detail.dataset.source = kind;
    detail.id = "versions-" + index;
    detail.hidden = !state.expanded.has(key);
    toggle.type = "button";
    toggle.setAttribute("aria-controls", detail.id);
    toggle.setAttribute("aria-expanded", String(!detail.hidden));
    toggle.append(element("span", name), icon("right"));
    toggle.onclick = () => {
      detail.hidden = !detail.hidden;
      toggle.setAttribute("aria-expanded", String(!detail.hidden));
      if (detail.hidden) state.expanded.delete(key);
      else state.expanded.add(key);
    };
    cell.append(toggle);
    const source = element("td");
    source.append(sourceBadge(kind));
    row.append(cell, source, element("td", new Set(artifacts.map(({ version }) => version)).size, "number"),
      element("td", artifacts.length, "number"));
    const info = element("td");
    info.colSpan = 4;
    const list = element("ul", null, "version-list");
    for (const entry of [...artifacts].sort((a, b) => collator.compare(b.version, a.version))) {
      const item = element("li");
      const filename = element("span", null, "artifact-name");
      filename.append(icon("file"), element("code", entry.file));
      item.append(element("code", entry.version, "version-label"), filename);
      list.append(item);
    }
    info.append(list);
    detail.append(info);
    return [row, detail];
  }));
  byId("catalog-total").textContent = "共 " + matches.length + " 个包";
  message("catalog-state", matches.length ? "" : (query ? "没有匹配的包" : "这个源暂无制品"));
  byId("pagination").hidden = !matches.length;
  byId("page-summary").textContent = matches.length ?
    (start + 1) + "–" + Math.min(start + pageSize, matches.length) + " / " + matches.length : "";
  byId("previous").disabled = state.page === 0;
  byId("next").disabled = start + pageSize >= matches.length;
}

function sourceCard(kind, source) {
  const card = element("article", null, "source");
  card.dataset.source = kind;
  const endpoint = element("div", null, "endpoint");
  const address = element("code", source.url);
  const copy = element("button", null, "control square");
  const label = "复制 " + kinds[kind] + " 地址";
  copy.type = "button";
  copy.title = label;
  copy.setAttribute("aria-label", label);
  copy.append(icon("copy"));
  copy.onclick = async () => {
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(source.url);
      copy.replaceChildren(icon("check"));
      message("overview-status", "已复制 " + kinds[kind] + " 地址");
      setTimeout(() => copy.replaceChildren(icon("copy")), 1400);
    } catch {
      const range = document.createRange();
      range.selectNodeContents(address);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      message("overview-status", "地址已选中，请手动复制。");
    }
  };
  endpoint.append(address, copy);
  card.append(sourceBadge(kind), endpoint, status(source.healthy ? "正常" : "不可用", source.healthy ? "good" : "bad", true));
  return card;
}

function showView(view) {
  if (view === "imports" && !state.catalog?.admin) return;
  state.view = view;
  for (const name of ["catalog", "imports"]) byId(name).hidden = name !== view;
  byId("imports-nav").setAttribute("aria-pressed", String(view === "imports"));
  document.querySelectorAll("[data-rail-kind]").forEach((button) =>
    button.setAttribute("aria-pressed", String(view === "catalog" && button.dataset.railKind === state.kind)));
  if (view === "imports" && state.catalog?.admin) loadJobs().catch(handleJobError);
}

function renderAccess(admin) {
  byId("imports-nav").hidden = !admin;
  byId("imports-content").hidden = !admin;
  if (!admin) showView("catalog");
  byId("session-label").textContent = admin ? "退出登录" : "管理员登录";
  byId("session").querySelector("[data-icon]").replaceChildren(icon(admin ? "logout" : "lock"));
}

async function loadCatalog() {
  if (state.loading) return;
  state.loading = true;
  byId("refresh").disabled = true;
  try {
    const catalog = await api("/api/catalog");
    state.catalog = catalog;
    const projects = Object.keys(kinds).flatMap((kind) => groupPackages(catalog.packages[kind], kind))
      .sort((a, b) => collator.compare(a.name, b.name) || collator.compare(a.kind, b.kind));
    state.projects = { all: projects, pypi: projects.filter(({ kind }) => kind === "pypi"),
      npm: projects.filter(({ kind }) => kind === "npm") };
    byId("sources").replaceChildren(...Object.entries(catalog.sources).map(([kind, source]) => sourceCard(kind, source)));
    renderAccess(catalog.admin);
    renderPackages();
    message("overview-status");
  } catch (error) {
    message("overview-status", "无法读取镜像源：" + error.message, "bad");
    if (!state.catalog) message("catalog-state", "目录暂不可用");
    throw error;
  } finally {
    state.loading = false;
    byId("refresh").disabled = false;
  }
}

function formatTime(seconds) {
  return seconds ? date.format(new Date(seconds * 1000)) : "";
}

function renderJobs() {
  const jobs = state.jobs;
  byId("job-count").textContent = jobs.length + " 条";
  if (!jobs.length) {
    byId("jobs").replaceChildren(element("p", "暂无导入记录", "empty"));
    return;
  }
  byId("jobs").replaceChildren(...jobs.map((job) => {
    const button = element("button", null, "job");
    const info = element("span", null, "job-info");
    info.append(element("strong", [kinds[job.kind] || "离线包", job.profile].filter(Boolean).join(" · ")),
      element("span", formatTime(job.created), "muted"));
    button.append(info, status(labels[job.status] || job.status, tones[job.status]));
    button.type = "button";
    button.setAttribute("aria-pressed", job.id === state.job);
    button.onclick = () => {
      if (job.id === state.job) return;
      state.job = job.id;
      state.detail = null;
      byId("report").hidden = true;
      for (const entry of byId("jobs").children) entry.setAttribute("aria-pressed", String(entry === button));
      tick(job.id);
    };
    return button;
  }));
}

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  renderJobs();
}

function renderJob(job) {
  state.detail = job;
  byId("report").hidden = false;
  byId("download").href = "/api/jobs/" + job.id + "/report";
  const summary = byId("job-summary");
  summary.replaceChildren(status(labels[job.status] || job.status, tones[job.status]),
    element("span", [kinds[job.kind], job.profile, formatTime(job.created)].filter(Boolean).join(" · "), "muted"));
  if (job.sha256) {
    const checksum = element("details");
    checksum.append(element("summary", "SHA256"), element("code", job.sha256));
    summary.append(checksum);
  }
  byId("error").textContent = job.error || "";
  byId("error").hidden = !job.error;
  const counts = new Map();
  const items = job.preview || Object.entries(job.files || {}).map(([file, entry]) => ({ file, ...entry }));
  byId("results").replaceChildren(...items.map((item) => {
    const row = element("tr");
    const action = job.published?.[item.file] || item.action || "checking";
    counts.set(action, (counts.get(action) || 0) + 1);
    const cell = element("td");
    cell.append(status(labels[action] || action, tones[action]));
    row.append(element("td", item.name), element("td", item.version, "number"), cell);
    return row;
  }));
  byId("result-counts").replaceChildren(...[...counts].map(([action, count]) =>
    status((labels[action] || action) + " " + count, tones[action])));
  byId("result-table").hidden = !items.length;
  const publish = byId("publish");
  publish.hidden = !["verified", "partial", "publishing"].includes(job.status) || counts.has("conflict");
  publish.disabled = state.submitting;
  publish.textContent = job.status === "verified" ? "确认发布" : "重试发布";
  byId("discard").hidden = !["verified", "partial"].includes(job.status) || state.submitting;
}

function handleJobError(error) {
  if (error.status === 401) {
    state.catalog.admin = false;
    renderAccess(false);
    state.job = null;
    byId("report").hidden = true;
    openLogin();
    message("auth-error", "登录已过期，请重新登录。", "bad");
  } else {
    message("notice", error.message, "bad");
  }
  state.submitting = false;
}

async function tick(id) {
  if (id !== state.job) return;
  try {
    const job = await api("/api/jobs/" + id);
    if (id !== state.job) return;
    const pending = job.status === "checking" || job.status === "publishing" ||
      (state.submitting && job.status === "verified");
    if (!pending) state.submitting = false;
    const changed = state.detail?.status !== job.status;
    renderJob(job);
    if (changed) await loadJobs();
    if (pending) setTimeout(() => tick(id), 1300);
    if (job.status === "published" && changed) await loadCatalog();
  } catch (error) {
    handleJobError(error);
    if (state.detail) renderJob(state.detail);
  }
}

function openLogin() {
  message("auth-error");
  if (!byId("login").open) byId("login").showModal();
}

function selectFile(file) {
  state.file = file || null;
  byId("file-name").textContent = file?.name || "选择离线包";
  byId("file-size").textContent = file ? new Intl.NumberFormat("zh-CN", {
    style: "unit", unit: "megabyte", maximumFractionDigits: 2
  }).format(file.size / 1024 ** 2) : ".tar.gz 或 .tgz";
  message("notice");
}

function uploadFile(file, progress) {
  return new Promise((resolve, reject) => {
    const req = new XMLHttpRequest();
    req.open("POST", "/api/jobs");
    req.responseType = "json";
    req.setRequestHeader("X-Mirror-Action", "console");
    req.setRequestHeader("Content-Type", "application/gzip");
    req.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) progress.value = event.loaded / event.total * 100;
    });
    req.onload = () => {
      if (req.status >= 200 && req.status < 300 && req.response?.id) resolve(req.response);
      else {
        const error = new Error(req.response?.error || "上传失败");
        error.status = req.status;
        reject(error);
      }
    };
    req.onerror = () => reject(new Error("连接中断，离线包未上传完成"));
    req.send(file);
  });
}

byId("filter").oninput = () => { state.page = 0; renderPackages(); };
byId("previous").onclick = () => { state.page--; renderPackages(); };
byId("next").onclick = () => { state.page++; renderPackages(); };
byId("refresh").onclick = () => loadCatalog().catch(() => {});
byId("imports-nav").onclick = () => showView("imports");
document.querySelectorAll("[data-kind], [data-rail-kind]").forEach((button) => {
  button.onclick = () => {
    const kind = button.dataset.kind || button.dataset.railKind;
    if (state.kind !== kind) state.page = 0;
    state.kind = kind;
    document.querySelectorAll("[data-kind]").forEach((tab) => tab.setAttribute("aria-pressed", String(tab.dataset.kind === kind)));
    showView("catalog");
    renderPackages();
  };
});

byId("session").onclick = async () => {
  if (!state.catalog?.admin) return openLogin();
  try {
    await api("/api/logout", "{}");
    state.job = null;
    state.detail = null;
    state.submitting = false;
    byId("report").hidden = true;
    byId("upload").reset();
    selectFile(null);
    await loadCatalog();
  } catch (error) {
    message("overview-status", error.message, "bad");
  }
};
byId("cancel").onclick = () => byId("login").close();
byId("auth").onsubmit = async (event) => {
  event.preventDefault();
  event.submitter.disabled = true;
  try {
    await api("/api/login", JSON.stringify({ password: byId("password").value }));
    byId("auth").reset();
    await loadCatalog();
    byId("login").close();
    if (state.view === "imports") await loadJobs();
  } catch (error) { message("auth-error", error.message, "bad"); }
  finally { event.submitter.disabled = false; }
};
byId("file").onchange = () => selectFile(byId("file").files[0]);
const zone = byId("dropzone");
for (const name of ["dragenter", "dragover", "dragleave", "drop"]) {
  zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.toggle("is-dragging", name === "dragenter" || name === "dragover");
    if (name === "drop" && !byId("file").disabled) {
      byId("file").value = "";
      selectFile(event.dataTransfer.files[0]);
    }
  });
}

byId("upload").onsubmit = async (event) => {
  event.preventDefault();
  if (!state.file || !/\.(tar\.gz|tgz)$/i.test(state.file.name)) {
    message("notice", "请选择 .tar.gz 或 .tgz 离线包。", "bad");
    return;
  }
  const submit = event.submitter;
  const progress = byId("upload-progress");
  submit.disabled = byId("file").disabled = true;
  progress.hidden = false;
  progress.removeAttribute("value");
  message("notice", "上传中…");
  try {
    const job = await uploadFile(state.file, progress);
    state.job = job.id;
    state.detail = null;
    byId("upload").reset();
    selectFile(null);
    await tick(job.id);
  } catch (error) { handleJobError(error); }
  finally { submit.disabled = byId("file").disabled = false; progress.hidden = true; }
};

async function jobAction(action) {
  const id = state.job;
  if (!id || (action === "discard" && !confirm("丢弃待发布文件？已发布的版本会保留。"))) return;
  state.submitting = true;
  byId("publish").disabled = byId("discard").disabled = true;
  message("notice");
  try {
    await api("/api/jobs/" + id + "/" + action, "{}");
    setTimeout(() => tick(id), 400);
  } catch (error) {
    handleJobError(error);
    if (state.detail) renderJob(state.detail);
  } finally {
    byId("discard").disabled = false;
  }
}
byId("publish").onclick = () => jobAction("publish");
byId("discard").onclick = () => jobAction("discard");
loadCatalog().catch(() => {});
