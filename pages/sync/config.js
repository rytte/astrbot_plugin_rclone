"use strict";

let remoteCatalog = [], providerCatalog = [], configMetadata = null, configProgress = null, configEditing = false, configReconnecting = false, guiSession = null;

function renderConfiguration() {
  const active = state.configuration.active;
  if (guiSession && (!active || state.configuration.session_id !== guiSession.session_id)) {
    guiSession = null; elements["gui-login"].value = "";
    elements["gui-settings"].hidden = false; elements["gui-result"].hidden = true;
    elements["start-gui"].hidden = false; elements["stop-gui"].hidden = true;
  }
  if (configProgress && (!active || state.configuration.session_id !== configProgress.session_id)) {
    configProgress = null; elements["config-dialog"].close();
    message("配置会话已结束或到期，未保存的表单需要重新填写。", true);
  }
  for (const id of ["create-remote", "open-gui", "edit-remote"]) elements[id].disabled = requesting || state.busy || active;
  const dirty = ["rclone-binary", "rclone-remote", "remote-folder"].some(id => elements[id].dataset.dirty);
  for (const id of ["install-rclone", "run-setup", "run-reconnect"]) elements[id].disabled = requesting || state.busy || active || dirty;
  elements["open-gui"].disabled = requesting || state.busy || active && !guiSession;
  elements["reload-remotes"].disabled = requesting;
  for (const id of ["save-connection", "connect", "disconnect", "rclone-binary", "rclone-remote", "remote-folder"]) elements[id].disabled ||= active;
  elements["configuration-status"].hidden = !active;
  elements["configuration-description"].textContent = active ? `${state.configuration.mode === "gui" ? "官方 GUI 正在运行" : "正在配置连接"}，同步已暂停。会话到期：${date(state.configuration.expires_at)}。` : "";
  elements["end-configuration"].hidden = !state.configuration.session_id;
  elements["end-configuration"].disabled = requesting;
  renderInstallation();
  updateApplicableFields();
}

function renderInstallation() {
  const install = state.installation, box = elements["install-status"];
  box.hidden = install.status === "idle";
  box.className = "notice" + (["failed", "cancelled"].includes(install.status) ? " error" : "");
  const descriptions = {
    checking: "正在获取官方稳定版信息与校验文件…",
    downloading: `正在下载 ${install.version}：${size(install.received)}${install.total ? " / " + size(install.total) : ""}。`,
    verifying: "正在校验安装包，并检查 rclone 版本和官方 GUI…",
    completed: `rclone ${install.version} 已安装，程序路径已保存。请添加存储或重新检测连接。`,
    failed: install.error, cancelled: install.error
  };
  box.textContent = descriptions[install.status] || "";
  elements["install-rclone"].textContent = ["checking", "downloading", "verifying"].includes(install.status) ? "正在安装…" : "下载并安装 rclone";
}

async function reloadRemotes() {
  remoteCatalog = (await bridge.apiGet("remotes")).remotes;
  elements["remote-options"].replaceChildren();
  for (const remote of remoteCatalog) {
    const option = node("option", "", `${remote.name} · ${remote.type}`);
    option.value = remote.name;
    elements["remote-options"].append(option);
  }
  renderConnection();
}

async function configOperation(operation, errorId) {
  if (requesting) return;
  requesting = true; render();
  elements[errorId].hidden = true;
  for (const id of ["submit-config", "start-gui", "stop-gui", "close-config"]) elements[id].disabled = true;
  elements["config-name"].disabled = true; elements["config-backend"].disabled = true;
  try { await operation(); await loadState(); }
  catch (error) { elements[errorId].textContent = error.message || "配置失败，请重试。"; elements[errorId].hidden = false; }
  finally {
    requesting = false; render();
    for (const id of ["submit-config", "start-gui", "stop-gui", "close-config"]) elements[id].disabled = false;
    elements["config-name"].disabled = configEditing || !!configProgress;
    elements["config-backend"].disabled = configEditing || !!configProgress;
  }
}

function matchesProvider(rule, value) {
  if (!rule) return true;
  const negate = rule.startsWith("!");
  const values = (negate ? rule.slice(1) : rule).split(",");
  return negate ? !values.includes(value) : values.includes(value);
}

function configInput(option, value, configured = false, fullChoices = false) {
  const field = node("label", "field dynamic-field");
  field.dataset.provider = option.Provider || "";
  field.append(node("span", "", option.Name + (option.Required ? " *" : "")));
  let input;
  const examples = option.Examples || [];
  if (option.Type === "bool" || option.Exclusive && examples.length) {
    input = node("select");
    input.append(new Option("使用默认值", ""));
    const choices = option.Type === "bool" ? [{Value: "true", Help: "是"}, {Value: "false", Help: "否"}] : examples;
    for (const example of choices) {
      const item = new Option(`${example.Value}${example.Help ? " — " + example.Help.split("\n")[0] : ""}`, example.Value);
      item.dataset.provider = example.Provider || "";
      input.append(item);
    }
  } else {
    input = node("input");
    input.type = option.IsPassword || option.Sensitive ? "password" : "text";
    input.spellcheck = false;
    input.autocomplete = "off";
    if (examples.length && input.type !== "password" && !fullChoices) {
      const list = node("datalist"); list.id = "option-" + option.Name;
      for (const example of examples) { const item = new Option(example.Help || example.Value, example.Value); item.dataset.provider = example.Provider || ""; list.append(item); }
      input.setAttribute("list", list.id); field.append(list);
    }
  }
  input.dataset.option = option.Name;
  input.dataset.advanced = option.Advanced ? "true" : "false";
  input.value = option.IsPassword || option.Sensitive ? "" : String(value ?? option.DefaultStr ?? option.Default ?? "");
  if (configured) input.placeholder = "已配置，留空保留原值";
  input.required = !!option.Required && !configured && !input.value;
  input.addEventListener("input", () => { input.dataset.changed = "true"; updateApplicableFields(); });
  field.append(input);
  if (fullChoices && examples.length && input.tagName === "INPUT" && input.type !== "password") {
    attachQuestionChoices(field, input, option, configured);
  }
  if (option.Help) {
    const details = node("details", "option-help"), help = node("p", "muted small", option.Help);
    details.append(node("summary", "", "字段说明"), help); field.append(details);
  }
  return field;
}

function attachQuestionChoices(field, input, option, configured) {
  // A datalist filters out other choices when a question has a default value.
  const choices = node("select");
  choices.setAttribute("aria-label", option.Name + " 可选项");
  input.setAttribute("aria-label", option.Name + " 手动输入");
  for (const example of option.Examples) {
    choices.append(new Option(`${example.Value}${example.Help ? " — " + example.Help.split("\n")[0] : ""}`, example.Value));
  }
  const custom = new Option("手动输入…", "");
  choices.append(custom);
  const current = option.Examples.findIndex(example => String(example.Value) === input.value);
  choices.selectedIndex = current < 0 ? option.Examples.length : current;
  input.hidden = current >= 0;
  input.required = !input.hidden && !!option.Required && !configured;
  choices.addEventListener("change", () => {
    input.hidden = choices.selectedOptions[0] !== custom;
    input.required = !input.hidden && !!option.Required && !configured;
    if (input.hidden) input.value = choices.value;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    if (!input.hidden) input.focus();
  });
  field.insertBefore(choices, input);
}

function updateApplicableFields() {
  const provider = elements["config-fields"].querySelector('[data-option="provider"]')?.value || "";
  for (const field of document.querySelectorAll("#config-fields .dynamic-field, #config-advanced .dynamic-field")) {
    const input = field.querySelector("[data-option]");
    const applicable = matchesProvider(field.dataset.provider, provider);
    field.hidden = !applicable; input.disabled = requesting || !!configProgress || !applicable;
    for (const option of field.querySelectorAll("option[data-provider]")) option.hidden = !matchesProvider(option.dataset.provider, provider);
  }
}

async function loadConfigFields(name = null) {
  configMetadata = await post("config-fields", {backend: elements["config-backend"].value, name});
  elements["config-fields"].replaceChildren(); elements["config-advanced"].replaceChildren();
  for (const option of configMetadata.provider.Options || []) {
    if ((option.Hide || 0) & 1) continue;
    const field = configInput(option, configMetadata.values[option.Name], configMetadata.configured.includes(option.Name));
    elements[option.Advanced ? "config-advanced" : "config-fields"].append(field);
  }
  updateApplicableFields();
}

async function openConfig(edit = false) {
  let prepared = false;
  await perform(async () => {
    configProgress = null; configEditing = edit; configReconnecting = false;
    await reloadRemotes();
    providerCatalog = (await bridge.apiGet("providers")).providers;
    elements["config-backend"].replaceChildren();
    for (const provider of providerCatalog) elements["config-backend"].append(new Option(`${provider.Name} — ${provider.Description}`, provider.Name));
    const current = remoteCatalog.find(item => item.name === elements["rclone-remote"].value);
    if (edit && !current) throw new Error("请先选择一个已配置的连接，再点击编辑。");
    elements["config-name"].value = edit ? current.name : "";
    elements["config-name"].disabled = edit;
    elements["config-name"].required = true;
    elements["config-backend"].required = true;
    elements["config-backend"].disabled = edit;
    if (edit) elements["config-backend"].value = current.type;
    elements["config-title"].textContent = edit ? "编辑存储连接" : "添加存储连接";
    elements["config-basics"].hidden = false; elements["config-question"].hidden = true; elements["config-question"].replaceChildren();
    elements["config-error"].hidden = true; elements["submit-config"].textContent = "下一步";
    elements["advanced-options"].open = false;
    await loadConfigFields(edit ? current.name : null);
    prepared = true;
  });
  if (prepared) elements["config-dialog"].showModal();
}

async function reconnectConfig() {
  let prepared = false;
  await perform(async () => {
    configProgress = await post("config-reconnect");
    configEditing = true; configReconnecting = true;
    elements["config-name"].value = state.connection.remote;
    elements["config-title"].textContent = `重新授权 · ${state.connection.remote}`;
    elements["config-error"].hidden = true;
    renderConfigQuestion();
    prepared = true;
  });
  if (prepared) elements["config-dialog"].showModal();
}

function renderConfigQuestion() {
  elements["config-basics"].hidden = true;
  elements["config-question"].hidden = false;
  elements["config-question"].replaceChildren();
  elements["config-name"].required = false; elements["config-backend"].required = false;
  // Hidden initial fields must not participate in validation for later questions.
  for (const input of document.querySelectorAll("#config-basics input, #config-basics select")) input.disabled = true;
  if (configProgress.complete) {
    elements["config-question"].append(node("div", "notice", configReconnecting ? "授权完成，请保存。" : "配置完成，请保存。"));
    elements["submit-config"].textContent = "保存连接";
  } else {
    elements["config-question"].append(configInput(configProgress.option, undefined, false, true));
    const help = elements["config-question"].querySelector("details"); if (help) help.open = true;
    elements["submit-config"].textContent = "继续";
    if (configProgress.error) { elements["config-error"].textContent = configProgress.error; elements["config-error"].hidden = false; }
  }
}

async function closeConfig() {
  await configOperation(async () => {
    if (configProgress && state.configuration.active) await post("config-finish", {session_id: configProgress.session_id, save: false});
    configProgress = null; elements["config-dialog"].close();
  }, "config-error");
}

function bindConfiguration() {
  elements["create-remote"].addEventListener("click", () => openConfig());
  elements["run-setup"].addEventListener("click", () => openConfig());
  elements["run-reconnect"].addEventListener("click", reconnectConfig);
  elements["install-rclone"].addEventListener("click", () => perform(() => post("install-rclone")));
  elements["edit-remote"].addEventListener("click", () => openConfig(true));
  elements["reload-remotes"].addEventListener("click", () => perform(reloadRemotes, "已读取存储连接。"));
  elements["config-backend"].addEventListener("change", () => configOperation(() => loadConfigFields(), "config-error"));
  elements["close-config"].addEventListener("click", closeConfig);
  elements["config-dialog"].addEventListener("cancel", event => { event.preventDefault(); closeConfig(); });
  elements["config-form"].addEventListener("submit", event => {
    event.preventDefault();
    configOperation(async () => {
      if (configProgress?.complete) {
        const name = elements["config-name"].value;
        await post("config-finish", {session_id: configProgress.session_id, save: true});
        configProgress = null; elements["config-dialog"].close();
        if (!configReconnecting) {
          elements["rclone-remote"].value = name; elements["rclone-remote"].dataset.dirty = "true";
        }
        await reloadRemotes(); message(configReconnecting ? "授权配置已保存，请重新检测连接。" : "存储连接已保存。请保存连接设置并检测连接。"); return;
      }
      if (configProgress) {
        const input = elements["config-question"].querySelector("[data-option]");
        const answer = input.value || String(configProgress.option.Default ?? "");
        input.value = "";
        configProgress = await post("config-answer", {session_id: configProgress.session_id, answer});
      } else {
        const parameters = {};
        for (const input of document.querySelectorAll("#config-fields [data-option], #config-advanced [data-option]")) {
          // Requests disable all fields; only provider-inapplicable fields are omitted.
          if (input.closest(".dynamic-field").hidden || input.dataset.advanced === "true" && !input.dataset.changed) continue;
          const option = configMetadata.provider.Options.find(item => item.Name === input.dataset.option);
          if ((option.IsPassword || option.Sensitive) && !input.value) continue;
          if (configEditing && !input.dataset.changed) continue;
          if (input.value || input.dataset.changed) parameters[input.dataset.option] = input.value;
        }
        configProgress = await post("config-begin", {name: elements["config-name"].value, backend: elements["config-backend"].value, parameters, edit: configEditing});
        for (const input of elements["config-basics"].querySelectorAll('input[type="password"]')) input.value = "";
      }
      renderConfigQuestion();
    }, "config-error");
  });
  elements["open-gui"].addEventListener("click", () => { elements["gui-error"].hidden = true; elements["gui-dialog"].showModal(); });
  elements["close-gui"].addEventListener("click", () => elements["gui-dialog"].close());
  elements["gui-listen"].addEventListener("change", () => {
    const hostname = elements["gui-listen"].value === "127.0.0.1" ? "127.0.0.1" : new URL(window.location.href).hostname;
    elements["gui-url"].value = `http://${hostname}:${elements["gui-port"].value}`;
    elements["api-url"].value = `http://${hostname}:${elements["api-port"].value}`;
  });
  elements["gui-form"].addEventListener("submit", event => {
    event.preventDefault();
    configOperation(async () => {
      guiSession = await post("gui-start", {listen: elements["gui-listen"].value, gui_port: Number(elements["gui-port"].value), api_port: Number(elements["api-port"].value), gui_url: elements["gui-url"].value.trim(), api_url: elements["api-url"].value.trim()});
      elements["gui-login"].value = guiSession.login_url;
      elements["gui-settings"].hidden = true; elements["gui-result"].hidden = false;
      elements["start-gui"].hidden = true; elements["stop-gui"].hidden = false;
    }, "gui-error");
  });
  elements["copy-gui"].addEventListener("click", () => {
    elements["gui-login"].select();
    const copied = document.execCommand("copy");
    elements["copy-gui"].textContent = copied ? "已复制，请在浏览器打开" : "地址已选中，请按 Ctrl+C 复制";
  });
  async function finishConfiguration() {
    const id = guiSession?.session_id || state.configuration.session_id;
    await post("config-finish", {session_id: id, save: state.configuration.mode === "gui"});
    guiSession = null; configProgress = null;
    elements["gui-login"].value = ""; elements["gui-dialog"].close(); elements["config-dialog"].close();
    elements["gui-settings"].hidden = false; elements["gui-result"].hidden = true;
    elements["start-gui"].hidden = false; elements["stop-gui"].hidden = true;
    await reloadRemotes(); message("配置会话已结束，请保存连接设置并重新检测连接。");
  }
  elements["stop-gui"].addEventListener("click", () => configOperation(finishConfiguration, "gui-error"));
  elements["end-configuration"].addEventListener("click", () => perform(finishConfiguration));
}
