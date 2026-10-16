"use strict";

const elements = Object.fromEntries([...document.querySelectorAll("[id]")].map(element => [element.id, element]));
const labels = {queued: "排队中", checking: "检查来源", syncing: "同步中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断"};
const modes = {
  upload: {label: "上传更新", upload: true, description: "可能覆盖云端同名文件，保留多余文件。"},
  upload_mirror: {label: "镜像到云端", upload: true, description: "可能覆盖云端同名文件，并删除云端多余文件。"},
  download: {label: "下载更新", upload: false, description: "可能覆盖本机同名文件，保留多余文件。"},
  download_mirror: {label: "镜像到本地", upload: false, description: "可能覆盖本机同名文件，并删除本机多余文件。"}
};
const activeStatuses = new Set(["queued", "checking", "syncing"]);
const guideSteps = ["install", "storage", "connection", "tasks", "authorization"];
let bridge, state, editingId = null, requesting = false;
let polling = false, pollTimer;
let guideStep = "install";

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function size(value) {
  if (!Number.isFinite(value)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}
function date(value) { return value ? new Date(value * 1000).toLocaleString("zh-CN", {hour12: false}) : "—"; }
function message(text, error = false) {
  elements.message.hidden = !text;
  elements.message.textContent = text;
  elements.message.className = "notice" + (error ? " error" : "");
}
function lines(value) { return value.split(/\r?\n/).map(line => line.trim()).filter(Boolean); }
function activeRun(id) { return state.runs.find(run => run.task_id === id && activeStatuses.has(run.status)); }
function destination(target) { return `${state.connection.remote}:${state.connection.folder}/${target}`; }
function progressText(progress) {
  if (!Object.keys(progress).length) return "正在比较文件和同步目录…";
  return `传输 ${progress.transfers ?? 0}/${progress.totalTransfers ?? 0} 个文件 · ${size(progress.bytes ?? 0)}/${size(progress.totalBytes ?? 0)} · ${size(progress.speed ?? 0)}/s · 已删除 ${progress.deletes ?? 0} 个文件`;
}
async function post(action, body = {}) { return bridge.apiPost(action, {csrf: state.csrf, ...body}); }
async function perform(operation, success) {
  if (requesting) return;
  requesting = true; render();
  try { await operation(); await loadState(); if (success) message(success); }
  catch (error) { message(error.message || "操作失败，请刷新后重试。", true); }
  finally { requesting = false; render(); }
}
async function loadState(passive = false) {
  const next = await bridge.apiGet("state");
  if (passive && requesting) return;
  state = next;
  for (const [id, key] of [["rclone-binary", "binary"], ["rclone-remote", "remote"], ["remote-folder", "folder"]]) {
    if (document.activeElement !== elements[id] && !elements[id].dataset.dirty) elements[id].value = state.connection[key];
  }
  render();
}
function renderConnection() {
  const cloud = state.cloud;
  elements["connection-label"].textContent = cloud.connected ? "已连接" : cloud.error ? "连接待处理" : "未连接";
  elements["connection-dot"].classList.toggle("connected", cloud.connected);
  elements.connect.textContent = requesting ? "处理中…" : cloud.connected ? "重新检测" : "检测并连接";
  elements.connect.disabled = requesting || state.busy || ["rclone-binary", "rclone-remote", "remote-folder"].some(id => elements[id].dataset.dirty);
  elements["save-connection"].disabled = requesting || state.busy;
  for (const id of ["rclone-binary", "rclone-remote", "remote-folder"]) elements[id].disabled = requesting || state.busy;
  elements.disconnect.hidden = !cloud.connected;
  elements.disconnect.disabled = requesting || state.busy;
  elements["connection-error"].hidden = !cloud.error;
  elements["connection-error"].textContent = cloud.error || "";
  elements["setup-command"].value = state.commands.setup;
  elements["reconnect-command"].value = state.commands.reconnect;
  renderConfiguration();
}
function renderTasks() {
  const container = elements["task-list"];
  container.replaceChildren();
  if (!state.tasks.length) {
    const empty = node("div", "empty");
    empty.append(node("strong", "", "选择方向，创建同步任务"));
    container.append(empty); return;
  }
  for (const task of state.tasks) {
    const active = activeRun(task.id);
    const item = node("article", "task-item"), description = node("div"), title = node("div", "task-title");
    title.append(node("h3", "", task.name), node("span", "pill", active ? labels[active.status] : task.enabled && task.interval_minutes ? "定时" : "手动"));
    const mode = modes[task.mode], cloud = destination(task.remote_path);
    description.append(title, node("div", "path-summary", mode.upload ? task.local_path : cloud), node("div", "path-summary", "→ " + (mode.upload ? cloud : task.local_path)));
    if (active?.status === "syncing") description.append(node("p", "run-detail", progressText(active.progress)));
    const schedule = node("div", "task-meta", task.enabled && task.interval_minutes ? `每 ${task.interval_minutes} 分钟` : "仅手动执行");
    if (task.next_run) schedule.append(node("span", "", `下次 ${date(task.next_run)}`));
    const rules = node("div", "task-meta", mode.label);
    if (task.exclude.length) rules.append(node("span", "", `${task.exclude.length} 条排除规则`));
    const actions = node("div", "actions");
    const run = node("button", "button secondary", active ? "取消同步" : "立即同步");
    run.disabled = requesting || !active && (!state.cloud.connected || state.configuration.active || state.busy && !state.runs.some(item => activeStatuses.has(item.status)));
    run.addEventListener("click", () => perform(() => post(active ? "cancel" : "run", {task_id: task.id}), active ? "已取消此次同步，已完成的文件变更会保留。" : "任务已加入队列，可在执行记录中查看进度。"));
    const edit = node("button", "text-button", "编辑");
    edit.disabled = requesting || !!active; edit.addEventListener("click", () => openTask(task));
    const remove = node("button", "text-button danger", "移除");
    remove.disabled = requesting || !!active;
    remove.addEventListener("click", async () => {
      if (await confirmAction("移除同步任务", `移除“${task.name}”的配置和定时执行？本地和云端文件都会保留。`, "移除任务")) await perform(() => post("delete-task", {task_id: task.id}), "任务已移除。");
    });
    actions.append(run, edit, remove); item.append(description, schedule, rules, actions); container.append(item);
  }
}
function renderRuns() {
  const container = elements["run-list"];
  container.replaceChildren();
  if (!state.runs.length) { container.append(node("p", "empty", "暂无执行记录")); return; }
  for (const run of state.runs) {
    const item = node("article", "run-item"), top = node("div", "run-top"), title = node("div", "task-title");
    title.append(node("h3", "", run.task_name), node("span", "pill " + run.status, labels[run.status]));
    top.append(title, node("span", "muted", date(run.created_at))); item.append(top);
    const mode = modes[run.mode];
    item.append(node("p", "run-detail", `${run.scheduled ? "定时执行" : "手动执行"} · ${mode.label}${run.finished_at ? " · 结束 " + date(run.finished_at) : ""}`));
    item.append(node("p", "run-detail", mode.upload ? `${run.local_path} → ${run.remote_path}` : `${run.remote_path} → ${run.local_path}`));
    if (Object.keys(run.progress).length || run.status === "syncing") item.append(node("p", "run-detail", progressText(run.progress)));
    if (run.error) item.append(node("p", "run-detail run-error", run.error));
    if (run.warning) item.append(node("p", "run-detail run-error", run.warning));
    container.append(item);
  }
}
function render() {
  if (!state) return;
  renderConnection(); renderTasks(); renderRuns();
  elements["task-count"].textContent = state.tasks.length;
  const active = state.runs.find(run => ["checking", "syncing"].includes(run.status)) || state.runs.find(run => run.status === "queued");
  elements["activity-label"].textContent = active ? labels[active.status] : "空闲";
  elements["new-task"].disabled = requesting; elements.refresh.disabled = requesting;
  elements["save-task"].disabled = requesting;
  elements.preview.disabled = requesting || state.busy || !state.cloud.connected || state.configuration.active;
  for (const id of ["task-mode", "task-name", "task-local", "task-remote", "task-exclude", "task-interval", "task-enabled"]) elements[id].disabled = requesting;
  renderDestination();
  renderGuide();
}
function showGuideStep(step) {
  guideStep = step;
  const index = guideSteps.indexOf(step);
  for (const name of guideSteps) elements["guide-" + name].hidden = name !== step;
  for (const button of document.querySelectorAll("[data-guide-step]")) {
    const active = button.dataset.guideStep === step;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  elements["guide-previous"].disabled = index === 0;
  elements["guide-next"].textContent = index === guideSteps.length - 1 ? "完成，返回页面" : "下一步";
  elements["guide-body"].scrollTop = 0;
}
function renderGuide() {
  const dirty = ["rclone-binary", "rclone-remote", "remote-folder"].some(id => elements[id].dataset.dirty);
  elements["guide-settings-notice"].hidden = !dirty;
  elements["guide-open-gui"].disabled = elements["open-gui"].disabled || dirty;
  elements["guide-new-task"].disabled = elements["new-task"].disabled;
  for (const button of document.querySelectorAll("[data-guide-focus]")) button.disabled = elements[button.dataset.guideFocus].disabled;
}
function bindGuide() {
  elements["open-guide"].addEventListener("click", () => {
    elements["guide-dialog"].showModal();
    showGuideStep(guideStep);
  });
  elements["close-guide"].addEventListener("click", () => elements["guide-dialog"].close());
  for (const button of document.querySelectorAll("[data-guide-step]")) button.addEventListener("click", () => showGuideStep(button.dataset.guideStep));
  elements["guide-previous"].addEventListener("click", () => showGuideStep(guideSteps[guideSteps.indexOf(guideStep) - 1]));
  elements["guide-next"].addEventListener("click", () => {
    const index = guideSteps.indexOf(guideStep);
    if (index === guideSteps.length - 1) elements["guide-dialog"].close();
    else showGuideStep(guideSteps[index + 1]);
  });
  for (const button of document.querySelectorAll("[data-guide-focus]")) button.addEventListener("click", () => {
    elements["guide-dialog"].close();
    elements[button.dataset.guideFocus].focus();
  });
  for (const [id, target] of [["guide-open-gui", "open-gui"], ["guide-new-task", "new-task"]]) elements[id].addEventListener("click", () => {
    elements["guide-dialog"].close();
    elements[target].click();
  });
}
function showTab(tab) {
  document.querySelectorAll("[data-tab]").forEach(button => { const active = button.dataset.tab === tab; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
  for (const name of ["tasks", "runs"]) elements[name + "-panel"].hidden = name !== tab;
}
function renderDestination() {
  const mode = modes[elements["task-mode"].value];
  const cloud = destination(elements["task-remote"].value.trim() || "<云端子目录>");
  const local = elements["task-local"].value.trim() || (mode.upload ? "<本机来源>" : "<本机接收目录>");
  elements["task-destination"].textContent = mode.upload ? `${local} → ${cloud}` : `${cloud} → ${local}`;
  elements["mode-description"].textContent = mode.description;
  elements["local-path-label"].textContent = mode.upload ? "本机来源（文件／目录）" : "本机接收目录";
  elements["remote-path-label"].textContent = mode.upload ? "云端目标子目录" : "云端来源子目录";
  elements["task-local"].placeholder = mode.upload ? "填写 AstrBot 所在机器上的一个路径" : "填写 AstrBot 所在机器上的专用接收目录";
  elements["local-examples"].hidden = !mode.upload;
}
function openTask(task = null) {
  editingId = task?.id || null;
  elements["task-dialog-title"].textContent = task ? "编辑同步任务" : "新建同步任务";
  elements["task-name"].value = task?.name || "";
  elements["task-mode"].value = task?.mode || "upload";
  elements["task-local"].value = task?.local_path || "";
  elements["task-remote"].value = task?.remote_path || "";
  elements["task-exclude"].value = task?.exclude.join("\n") || "";
  elements["task-interval"].value = task?.interval_minutes ?? 0;
  elements["task-enabled"].checked = task?.enabled ?? true;
  elements["preview-result"].hidden = true; elements["task-error"].hidden = true;
  renderDestination(); elements["task-dialog"].showModal();
}
function confirmAction(title, text, label = "确认") {
  return new Promise(resolve => {
    elements["confirmation-title"].textContent = title; elements["confirmation-text"].textContent = text;
    elements["confirm-action"].textContent = label; elements.confirmation.returnValue = "cancel";
    elements.confirmation.addEventListener("close", () => resolve(elements.confirmation.returnValue === "confirm"), {once: true});
    elements.confirmation.showModal();
  });
}
function taskPaths() {
  return {...(editingId ? {id: editingId} : {}), mode: elements["task-mode"].value, local_path: elements["task-local"].value.trim(), remote_path: elements["task-remote"].value.trim(), exclude: lines(elements["task-exclude"].value)};
}
function renderPreview(preview) {
  const box = elements["preview-result"], counts = preview.counts;
  box.replaceChildren(node("p", "", `${modes[elements["task-mode"].value].label} · 将修改${modes[elements["task-mode"].value].upload ? "云端" : "本机"}：新增 ${counts.add} · 更新 ${counts.update} · 删除 ${counts.delete} · 未变 ${counts.unchanged}`));
  const actionNames = {add: "新增", update: "更新", delete: "删除"};
  if (preview.changes.length) {
    const list = node("ul", "change-list");
    for (const change of preview.changes) list.append(node("li", "", `${actionNames[change.action]} · ${change.path}`));
    box.append(list);
  }
  if (preview.truncated) box.append(node("p", "muted small", "仅展示前 200 项变更。"));
  box.hidden = false;
}
function bindEvents() {
  bindConfiguration();
  elements.refresh.addEventListener("click", () => perform(loadState, "状态已刷新。"));
  const connectionInputs = ["rclone-binary", "rclone-remote", "remote-folder"];
  for (const id of connectionInputs) elements[id].addEventListener("input", () => { elements[id].dataset.dirty = "true"; renderConnection(); renderGuide(); });
  elements["save-connection"].addEventListener("click", async () => {
    if (state.cloud.connected && !await confirmAction("保存连接设置", "保存后暂停连接，请重新检测。更换连接或同步根目录会改变所有任务的云端目标。", "保存设置")) return;
    await perform(async () => {
      await post("connection", {binary: elements["rclone-binary"].value.trim(), remote: elements["rclone-remote"].value.trim(), folder: elements["remote-folder"].value.trim()});
      for (const id of connectionInputs) delete elements[id].dataset.dirty;
    }, "连接设置已保存，请检测并连接。");
  });
  elements.connect.addEventListener("click", () => perform(() => post("connect"), "rclone 已连接目标存储。"));
  elements.disconnect.addEventListener("click", async () => {
    if (await confirmAction("暂停云端连接", "暂停本插件的云端操作和定时同步？", "暂停连接")) await perform(() => post("disconnect"), "已暂停连接。");
  });
  document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => showTab(button.dataset.tab)));
  elements["new-task"].addEventListener("click", () => openTask());
  elements["close-task"].addEventListener("click", () => elements["task-dialog"].close());
  for (const [id, key] of [["example-plugins", "plugins"], ["example-backups", "backups"]]) {
    elements[id].addEventListener("click", () => { elements["task-local"].value = state.examples[key]; elements["preview-result"].hidden = true; renderDestination(); });
  }
  for (const id of ["task-mode", "task-local", "task-remote", "task-exclude"]) elements[id].addEventListener("input", () => { elements["preview-result"].hidden = true; renderDestination(); });
  elements.preview.addEventListener("click", async () => {
    if (requesting || !elements["task-local"].reportValidity() || !elements["task-remote"].reportValidity()) return;
    requesting = true; render(); elements["task-error"].hidden = true;
    try { renderPreview(await post("preview", taskPaths())); }
    catch (error) { elements["task-error"].hidden = false; elements["task-error"].textContent = error.message || "预览失败。"; }
    finally { requesting = false; render(); }
  });
  elements["task-form"].addEventListener("submit", async event => {
    event.preventDefault(); if (requesting) return;
    requesting = true; render(); elements["task-error"].hidden = true;
    try {
      await post("save-task", {...taskPaths(), name: elements["task-name"].value.trim(), enabled: elements["task-enabled"].checked, interval_minutes: Number(elements["task-interval"].value)});
      elements["task-dialog"].close(); await loadState(); message("任务已保存。修改或手动执行任务会重新计算下次执行时间。");
    } catch (error) { elements["task-error"].hidden = false; elements["task-error"].textContent = error.message || "保存失败。"; }
    finally { requesting = false; render(); }
  });
}
async function initialize() {
  bindGuide();
  try {
    bridge = window.AstrBotPluginPage;
    if (!bridge || typeof bridge.ready !== "function") throw new Error("请从 AstrBot WebUI 的插件详情 → 云端同步页面打开。当前环境缺少官方插件页面 bridge。");
    await bridge.ready();
    bridge.onContext(context => { document.documentElement.dataset.theme = context.isDark ? "dark" : "light"; });
    const logo = await bridge.apiGet("logo");
    elements["page-logo"].src = logo.src;
    try { await elements["page-logo"].decode(); }
    catch { throw new Error(`无法显示 Logo ${logo.name}，请检查 logos 目录中的图片。`); }
    elements["page-logo"].hidden = false;
    await loadState(); bindEvents();
    pollTimer = window.setInterval(async () => {
      if (polling || requesting || document.hidden) return;
      polling = true;
      try { await loadState(true); }
      catch (error) { message(error.message || "状态读取失败，请刷新页面。", true); window.clearInterval(pollTimer); }
      finally { polling = false; }
    }, 3000);
  } catch (error) { elements["boot-error"].hidden = false; elements["boot-error"].textContent = error.message || "插件页面初始化失败。"; }
}
initialize();
