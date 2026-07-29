const state = {
  config: null,
  fields: new Map(),
  localStatus: new Map(),
  modelOptions: [],
  modelComboboxes: new Set(),
  activeView: "providers",
  webSearchStatsPeriod: "weekly",
  webSearchAnalyticsLoaded: false,
  customProviders: [],
  editingCustomProviderId: null,
};

const MASKED_SECRET = "********";
const VIEW_GROUPS = [
  {
    id: "providers",
    label: "Providers",
    title: "Providers",
    sections: ["providers", "runtime"],
    containerId: "providersSections",
  },
  {
    id: "model_config",
    label: "Model Config",
    title: "Model Config",
    sections: ["models", "reasoning", "web_tools"],
    containerId: "modelConfigSections",
  },
  {
    id: "messaging",
    label: "Messaging",
    title: "Messaging",
    sections: ["messaging", "voice"],
    containerId: "messagingSections",
  },
  {
    id: "requests",
    label: "Requests",
    title: "Requests",
    sections: [],
    containerId: "requestsSections",
  },
  {
    id: "web_search",
    label: "Web Search",
    title: "Web Search",
    sections: ["websearch"],
    containerId: "webSearchSections",
  },
];

const byId = (id) => document.getElementById(id);

function sourceLabel(source) {
  const labels = {
    default: "default",
    template: "template",
    repo_env: "repo .env",
    managed_env: "",
    explicit_env_file: "FCC_ENV_FILE",
    process: "process env",
  };
  return Object.prototype.hasOwnProperty.call(labels, source) ? labels[source] : source;
}

function sourceText(field) {
  const parts = [];
  const label = sourceLabel(field.source);
  if (label) {
    parts.push(label);
  }
  if (field.locked) {
    parts.push("locked");
  }
  return parts.join(" ");
}

function statusClass(status) {
  if (["configured", "reachable", "running"].includes(status)) return "ok";
  if (["missing_key", "missing_url", "unknown"].includes(status)) return "warn";
  if (["offline", "error"].includes(status)) return "error";
  return "neutral";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = "";
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : "";
    } catch {
      // Non-JSON error body; fall back to the status line.
    }
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function load() {
  showMessage("Loading admin config");
  const config = await api("/admin/api/config");
  state.config = config;
  state.fields = new Map(config.fields.map((field) => [field.key, field]));
  state.credentialEnvs = new Set(
    (config.provider_status || [])
      .map((provider) => provider.credential_env)
      .filter(Boolean),
  );
  renderNav();
  renderProviders(config.provider_status);
  renderSections(config.sections, config.fields);
  renderWebSearchProviders();
  await loadCustomProviders();
  byId("configPath").textContent = config.paths.managed;
  await hydrateModelOptions();
  await validate(false);
  await refreshLocalStatus();
  updateDirtyState();
  showMessage("");
}

function renderNav() {
  const nav = byId("sectionNav");
  nav.innerHTML = "";
  VIEW_GROUPS.forEach((view, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `nav-link${index === 0 ? " active" : ""}`;
    button.dataset.view = view.id;
    button.textContent = view.label;
    if (index === 0) {
      button.setAttribute("aria-current", "page");
    }
    button.addEventListener("click", () => {
      setActiveView(view.id, { scroll: true });
    });
    nav.appendChild(button);
  });
  setActiveView(state.activeView, { scroll: false });
}

function setActiveView(viewId, { scroll = false } = {}) {
  const activeView =
    VIEW_GROUPS.find((view) => view.id === viewId) || VIEW_GROUPS[0];
  state.activeView = activeView.id;
  byId("pageTitle").textContent = activeView.title;

  document.querySelectorAll(".nav-link").forEach((link) => {
    const selected = link.dataset.view === activeView.id;
    link.classList.toggle("active", selected);
    if (selected) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });

  document.querySelectorAll(".admin-view").forEach((view) => {
    const selected = view.dataset.view === activeView.id;
    view.classList.toggle("active", selected);
    view.hidden = !selected;
  });

  if (activeView.id === "web_search" && !state.webSearchAnalyticsLoaded) {
    state.webSearchAnalyticsLoaded = true;
    loadWebSearchAnalytics(state.webSearchStatsPeriod);
  }

  if (scroll) {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  if (activeView.id === "requests") {
    loadRequestsView().catch((error) => showMessage(error.message, "error"));
  }
}

function renderProviders(providerStatus) {
  const grid = byId("providerGrid");
  grid.innerHTML = "";
  providerStatus.forEach((provider) => {
    const card = document.createElement("article");
    card.className = "provider-card";
    card.dataset.provider = provider.provider_id;

    const title = document.createElement("div");
    title.className = "provider-title";
    title.innerHTML = `<strong>${provider.display_name || provider.provider_id}</strong>`;

    const pill = document.createElement("span");
    pill.className = `status-pill ${statusClass(provider.status)}`;
    pill.textContent = provider.label;
    title.appendChild(pill);

    const meta = document.createElement("div");
    meta.className = "provider-meta";
    meta.textContent =
      provider.kind === "local"
        ? provider.base_url || "No local URL configured"
        : provider.credential_env;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "test-button";
    button.textContent = provider.kind === "local" ? "Test" : "Refresh models";
    button.addEventListener("click", () => testProvider(provider.provider_id, button));

    card.append(title, meta, button);
    grid.appendChild(card);
  });
}

function updateProviderCard(providerId, status, label, metaText) {
  const card = document.querySelector(`[data-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    card.querySelector(".provider-meta").textContent = metaText;
  }
}

function renderSections(sections, fields) {
  state.modelComboboxes.clear();
  VIEW_GROUPS.forEach((view) => {
    byId(view.containerId).innerHTML = "";
  });

  const sectionById = new Map(sections.map((section) => [section.id, section]));
  const bySection = new Map();
  sections.forEach((section) => bySection.set(section.id, []));
  fields.forEach((field) => {
    if (!bySection.has(field.section)) bySection.set(field.section, []);
    bySection.get(field.section).push(field);
  });

  VIEW_GROUPS.forEach((view) => {
    const container = byId(view.containerId);
    view.sections.forEach((sectionId) => {
      const section = sectionById.get(sectionId);
      const sectionFields = bySection.get(sectionId) || [];
      if (!section || sectionFields.length === 0) return;

      const sectionEl = document.createElement("section");
      sectionEl.className = "settings-section";
      sectionEl.id = `section-${section.id}`;

      // Rotation selects are rendered inside the credential key manager
      // instead of the generic grid. Websearch advanced option fields are
      // rendered inside the provider cards' collapsed groups.
      const gridFields = sectionFields.filter(
        (field) =>
          !field.key.endsWith("_ROTATION") &&
          !(field.section === "websearch" && field.advanced),
      );

      const heading = document.createElement("div");
      heading.className = "section-heading";
      heading.innerHTML = `<div><h3>${section.label}</h3><p>${section.description}</p></div>`;
      if (section.id === "models") {
        const refreshButton = document.createElement("button");
        refreshButton.type = "button";
        refreshButton.className = "secondary-button";
        refreshButton.textContent = "Refresh models";
        refreshButton.addEventListener("click", () => refreshModelOptions(refreshButton));
        heading.appendChild(refreshButton);
      }
      sectionEl.appendChild(heading);

      const grid = document.createElement("div");
      grid.className = "field-grid";
      gridFields.forEach((field) => {
        grid.appendChild(renderField(field));
      });
      sectionEl.appendChild(grid);

      if (gridFields.some((field) => field.advanced)) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "ghost-button advanced-toggle";
        toggle.textContent = "Show advanced";
        toggle.addEventListener("click", () => {
          const showing = sectionEl.classList.toggle("show-advanced");
          toggle.textContent = showing ? "Hide advanced" : "Show advanced";
        });
        sectionEl.appendChild(toggle);
      }

      container.appendChild(sectionEl);
    });
  });
}

function renderField(field) {
  const wrapper = document.createElement("div");
  wrapper.className = `field${field.advanced ? " advanced-field" : ""}`;
  wrapper.dataset.key = field.key;

  const label = document.createElement("label");
  label.htmlFor = `field-${field.key}`;
  const labelText = document.createElement("span");
  labelText.textContent = field.label;
  label.appendChild(labelText);

  const source = sourceText(field);
  if (source) {
    const sourceEl = document.createElement("span");
    sourceEl.className = "field-source";
    sourceEl.textContent = source;
    label.appendChild(sourceEl);
  }

  const input = inputForField(field);
  input.id = `field-${field.key}`;
  input.dataset.key = field.key;
  input.dataset.original = field.value || "";
  input.dataset.secret = field.secret ? "true" : "false";
  input.dataset.configured = field.configured ? "true" : "false";
  input.dataset.fieldType = field.type;
  input.disabled = field.locked;
  if (field.type !== "oauth_login") {
    input.addEventListener("input", updateDirtyState);
    input.addEventListener("change", updateDirtyState);
    if (field.type === "optional_model") {
      input.addEventListener("blur", () => {
        if (!input.value.trim() || input.value.trim().toLowerCase() === "none") {
          input.value = "None";
          updateDirtyState();
        }
      });
    }
  }

  const control =
    field.type === "model" || field.type === "optional_model"
      ? new ModelCombobox(input, field).element
      : input;
  wrapper.append(label, control);
  if (field.description) {
    const description = document.createElement("div");
    description.className = "field-description";
    description.textContent = field.description;
    wrapper.appendChild(description);
  }
  if (
    field.secret &&
    state.credentialEnvs &&
    state.credentialEnvs.has(field.key)
  ) {
    wrapper.appendChild(keyManagerForField(field));
  }
  return wrapper;
}

function keyManagerForField(field) {
  const container = document.createElement("div");
  container.className = "key-manager";

  const header = document.createElement("div");
  header.className = "key-manager-header";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "ghost-button key-manager-toggle";
  toggle.textContent = "Manage keys";
  header.appendChild(toggle);

  // Rotation policy select for this credential (participates in the normal
  // dirty/apply flow via the shared input machinery).
  const rotationField = state.fields.get(`${field.key}_ROTATION`);
  if (rotationField) {
    const rotationWrap = document.createElement("label");
    rotationWrap.className = "key-manager-rotation";
    const rotationLabel = document.createElement("span");
    rotationLabel.textContent = "Rotation";
    const rotationInput = inputForField(rotationField);
    rotationInput.id = `field-${rotationField.key}`;
    rotationInput.dataset.key = rotationField.key;
    rotationInput.dataset.original = rotationField.value || "";
    rotationInput.dataset.secret = "false";
    rotationInput.dataset.configured = rotationField.configured ? "true" : "false";
    rotationInput.dataset.fieldType = rotationField.type;
    rotationInput.disabled = rotationField.locked;
    rotationInput.addEventListener("input", updateDirtyState);
    rotationInput.addEventListener("change", updateDirtyState);
    rotationInput.title = rotationField.description || "Key rotation policy";
    rotationWrap.append(rotationLabel, rotationInput);
    header.appendChild(rotationWrap);
  }

  const panel = document.createElement("div");
  panel.className = "key-manager-panel";
  panel.hidden = true;

  const open = async () => {
    panel.hidden = false;
    toggle.textContent = "Hide keys";
    await renderKeyManager(panel, field);
  };
  const close = () => {
    panel.hidden = true;
    toggle.textContent = "Manage keys";
  };
  toggle.addEventListener("click", () => {
    if (panel.hidden) {
      open();
    } else {
      close();
    }
  });

  container.append(header, panel);

  if (state.reopenKeyManager === field.key) {
    state.reopenKeyManager = null;
    open();
  }
  return container;
}

async function renderKeyManager(panel, field) {
  panel.textContent = "Loading keys...";
  let info;
  try {
    info = await api(`/admin/api/credentials/${field.key}/keys`);
  } catch (error) {
    panel.textContent = `Could not load keys: ${error.message}`;
    return;
  }

  panel.innerHTML = "";

  const list = document.createElement("div");
  list.className = "key-manager-list";
  if (info.count === 0) {
    const empty = document.createElement("div");
    empty.className = "key-manager-empty";
    empty.textContent = "No keys configured.";
    list.appendChild(empty);
  }
  info.keys.forEach((masked, index) => {
    const row = document.createElement("div");
    row.className = "key-manager-row";

    const label = document.createElement("code");
    label.className = "key-manager-key";
    label.textContent = masked;

    row.appendChild(label);

    const health = Array.isArray(info.health) ? info.health[index] : null;
    if (health && health.state) {
      row.appendChild(keyHealthBadge(health));
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button key-manager-remove";
    remove.textContent = "Remove";
    remove.disabled = info.locked;
    remove.addEventListener("click", () =>
      removeCredentialKey(field, index, remove),
    );

    row.appendChild(remove);
    list.appendChild(row);
  });
  panel.appendChild(list);

  const addRow = document.createElement("div");
  addRow.className = "key-manager-add";
  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "off";
  input.placeholder = info.locked
    ? "Locked by process environment"
    : "Paste a new key";
  input.disabled = info.locked;

  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary-button";
  add.textContent = "Add key";
  add.disabled = info.locked;

  const submit = () => addCredentialKey(field, input, add);
  add.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit();
  });

  addRow.append(input, add);
  panel.appendChild(addRow);

  if (info.locked) {
    const note = document.createElement("div");
    note.className = "key-manager-note";
    note.textContent =
      "This credential comes from the process environment and is read-only here.";
    panel.appendChild(note);
  }
}

function formatSeconds(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mrem = m % 60;
  return mrem ? `${h}h ${mrem}m` : `${h}h`;
}

function keyHealthBadge(health) {
  const state = String(health.state || "HEALTHY");
  const badge = document.createElement("span");
  badge.className = `key-health-badge key-health-${state.toLowerCase().replace(/_/g, "-")}`;

  let backIn = "";
  const remaining =
    state === "LOCKED_OUT"
      ? health.lockout_remaining || 0
      : health.cooldown_remaining || 0;
  if (remaining > 0 && state !== "HEALTHY") {
    backIn = ` — back in ${formatSeconds(remaining)}`;
  }
  badge.textContent = state;

  const requests = health.request_count || 0;
  const failures = health.failure_count || 0;
  badge.title = `${state}${backIn} — ${requests} requests, ${failures} failures`;
  return badge;
}

async function reloadAndReopenKeyManager(field, message) {
  state.reopenKeyManager = field.key;
  await load();
  showMessage(message, "ok");
}

async function addCredentialKey(field, input, button) {
  const value = input.value.trim();
  if (!value) return;
  button.disabled = true;
  try {
    const result = await api(`/admin/api/credentials/${field.key}/keys`, {
      method: "POST",
      body: JSON.stringify({ key: value }),
    });
    await reloadAndReopenKeyManager(
      field,
      `Added key ${result.added} (${result.count} configured). Applied.`,
    );
  } catch (error) {
    button.disabled = false;
    showMessage(`Could not add key: ${error.message}`, "error");
  }
}

async function removeCredentialKey(field, index, button) {
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/credentials/${field.key}/keys/${index}`,
      { method: "DELETE" },
    );
    await reloadAndReopenKeyManager(
      field,
      `Removed key ${result.removed} (${result.count} remaining). Applied.`,
    );
  } catch (error) {
    button.disabled = false;
    showMessage(`Could not remove key: ${error.message}`, "error");
  }
}

function inputForField(field) {
  if (field.type === "boolean") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = String(field.value).toLowerCase() === "true";
    input.dataset.original = input.checked ? "true" : "false";
    return input;
  }

  if (field.type === "oauth_login") {
    const wrapper = document.createElement("div");
    wrapper.className = "oauth-login-control";
    const button = document.createElement("button");
    button.type = "button";
    button.className =
      field.key === "CHATGPT_OAUTH_IMPORT_CODEX"
        ? "secondary-button"
        : "primary-button";
    button.textContent =
      field.key === "CHATGPT_OAUTH_IMPORT_CODEX"
        ? "Import existing Codex login"
        : "Log in with ChatGPT";
    button.addEventListener("click", () => {
      if (field.key === "CHATGPT_OAUTH_IMPORT_CODEX") {
        importChatGPTOAuthCodexTokens(button);
      } else {
        startChatGPTOAuthLogin(button);
      }
    });
    wrapper.appendChild(button);
    return wrapper;
  }

  if (field.type === "select") {
    const select = document.createElement("select");
    field.options.forEach((item) =>
      select.appendChild(option(item.value, item.label)),
    );
    select.value = field.value || field.options[0]?.value || "";
    return select;
  }

  if (field.type === "textarea") {
    const textarea = document.createElement("textarea");
    textarea.value = field.value || "";
    return textarea;
  }

  if (field.type === "model" || field.type === "optional_model") {
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value || (field.type === "optional_model" ? "None" : "");
    input.autocomplete = "off";
    return input;
  }

  const input = document.createElement("input");
  input.type = field.type === "number" ? "number" : "text";
  if (field.type === "secret") {
    input.type = "password";
    input.placeholder = field.configured
      ? "Configured - enter a new value to replace"
      : "Not configured";
    input.value = "";
    input.autocomplete = "off";
  } else {
    input.value = field.value || "";
  }
  return input;
}

class ModelCombobox {
  constructor(input, field) {
    this.input = input;
    this.fieldType = field.type;
    this.activeIndex = -1;
    this.query = "";

    this.element = document.createElement("div");
    this.element.className = "model-combobox";
    this.listbox = document.createElement("div");
    this.listbox.className = "model-combobox-list";
    this.listbox.id = `model-options-${field.key}`;
    this.listbox.setAttribute("role", "listbox");
    this.listbox.hidden = true;
    this.toggle = document.createElement("button");
    this.toggle.type = "button";
    this.toggle.className = "model-combobox-toggle";
    this.toggle.disabled = input.disabled;
    this.toggle.setAttribute("aria-label", `Show ${field.label} options`);

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    for (const control of [input, this.toggle]) {
      control.setAttribute("aria-controls", this.listbox.id);
      control.setAttribute("aria-expanded", "false");
    }

    input.addEventListener("click", () => this.open());
    input.addEventListener("input", () => this.open(input.value));
    input.addEventListener("keydown", (event) => this.handleKeydown(event));
    this.toggle.addEventListener("mousedown", (event) => event.preventDefault());
    this.toggle.addEventListener("click", () => {
      if (this.isOpen) this.close();
      else this.open();
      input.focus();
    });
    this.listbox.addEventListener("mousedown", (event) => event.preventDefault());
    this.listbox.addEventListener("mousemove", (event) => {
      const optionEl = event.target.closest('[role="option"]');
      if (optionEl) this.setActive(this.visibleOptions.indexOf(optionEl));
    });
    this.listbox.addEventListener("click", (event) => {
      const optionEl = event.target.closest('[role="option"]');
      if (optionEl) this.select(optionEl.dataset.value);
    });

    this.element.append(input, this.toggle, this.listbox);
    state.modelComboboxes.add(this);
  }

  get isOpen() {
    return this.element.classList.contains("open");
  }

  get values() {
    return this.fieldType === "optional_model"
      ? ["None", ...state.modelOptions]
      : state.modelOptions;
  }

  get visibleOptions() {
    return Array.from(this.listbox.querySelectorAll('[role="option"]'));
  }

  open(query = "") {
    if (this.input.disabled) return;
    state.modelComboboxes.forEach((combobox) => {
      if (combobox !== this) combobox.close();
    });
    this.render(query);
    this.element.classList.add("open");
    this.listbox.hidden = false;
    this.setExpanded(true);
  }

  close() {
    this.element.classList.remove("open");
    this.listbox.hidden = true;
    this.activeIndex = -1;
    this.input.removeAttribute("aria-activedescendant");
    this.setExpanded(false);
  }

  setExpanded(expanded) {
    for (const control of [this.input, this.toggle]) {
      control.setAttribute("aria-expanded", String(expanded));
    }
  }

  render(query) {
    this.query = query;
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const values = normalizedQuery
      ? this.values.filter((value) =>
          value.toLocaleLowerCase().includes(normalizedQuery),
        )
      : this.values;
    this.listbox.innerHTML = "";

    if (values.length === 0) {
      const empty = document.createElement("div");
      empty.className = "model-combobox-empty";
      empty.textContent = state.modelOptions.length
        ? "No matching models. You can still enter a custom slug."
        : "No discovered models. Refresh models or enter a custom slug.";
      this.listbox.appendChild(empty);
      this.activeIndex = -1;
      this.input.removeAttribute("aria-activedescendant");
      return;
    }

    values.forEach((value, index) => {
      const optionEl = document.createElement("div");
      optionEl.className = "model-combobox-option";
      optionEl.id = `${this.listbox.id}-option-${index}`;
      optionEl.dataset.value = value;
      optionEl.setAttribute("role", "option");
      optionEl.textContent = value;
      this.listbox.appendChild(optionEl);
    });
    const selectedIndex = values.indexOf(this.input.value);
    this.setActive(selectedIndex >= 0 ? selectedIndex : 0, false);
  }

  setActive(index, scroll = true) {
    const options = this.visibleOptions;
    if (options.length === 0) return;
    this.activeIndex = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((optionEl, optionIndex) => {
      const active = optionIndex === this.activeIndex;
      optionEl.classList.toggle("active", active);
      optionEl.setAttribute("aria-selected", String(active));
    });
    const activeOption = options[this.activeIndex];
    this.input.setAttribute("aria-activedescendant", activeOption.id);
    if (scroll) activeOption.scrollIntoView({ block: "nearest" });
  }

  move(offset) {
    const count = this.visibleOptions.length;
    if (count) this.setActive((this.activeIndex + offset + count) % count);
  }

  select(value) {
    this.input.value = value;
    this.input.dispatchEvent(new Event("change", { bubbles: true }));
    this.close();
    this.input.focus();
  }

  handleKeydown(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (this.isOpen) {
        this.move(event.key === "ArrowDown" ? 1 : -1);
      } else {
        this.open();
        if (event.key === "ArrowUp") {
          this.setActive(this.visibleOptions.length - 1);
        }
      }
    } else if (this.isOpen && (event.key === "Home" || event.key === "End")) {
      event.preventDefault();
      this.setActive(event.key === "Home" ? 0 : this.visibleOptions.length - 1);
    } else if (this.isOpen && event.key === "Enter") {
      const active = this.visibleOptions[this.activeIndex];
      if (active) {
        event.preventDefault();
        this.select(active.dataset.value);
      }
    } else if (this.isOpen && event.key === "Escape") {
      event.preventDefault();
      this.close();
    } else if (this.isOpen && event.key === "Tab") {
      this.close();
    }
  }
}

function option(value, label) {
  const optionEl = document.createElement("option");
  optionEl.value = value;
  optionEl.textContent = label;
  return optionEl;
}

function readFieldValue(input) {
  if (input.type === "checkbox") return input.checked ? "true" : "false";
  if (
    input.dataset.fieldType === "optional_model" &&
    input.value.trim().toLowerCase() === "none"
  ) {
    return "";
  }
  if (input.dataset.secret === "true" && input.dataset.configured === "true") {
    return input.value ? input.value : MASKED_SECRET;
  }
  return input.value;
}

function changedValues() {
  const values = {};
  document.querySelectorAll("[data-key]").forEach((input) => {
    if (input.disabled || !input.matches("input, select, textarea")) return;
    const value = readFieldValue(input);
    if (value !== input.dataset.original) {
      values[input.dataset.key] = value;
    }
  });
  return values;
}

function updateDirtyState() {
  const count = Object.keys(changedValues()).length;
  byId("dirtyState").textContent =
    count === 0 ? "No changes" : `${count} unsaved change${count === 1 ? "" : "s"}`;
  byId("applyButton").disabled = count === 0;
}

async function validate(showResult = true) {
  const result = await api("/admin/api/config/validate", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  if (showResult) {
    showValidationResult(result);
  }
  return result;
}

function showValidationResult(result) {
  if (result.valid) {
    showMessage("Config shape is valid", "ok");
  } else {
    showMessage(result.errors.join("; "), "error");
  }
}

async function apply() {
  const result = await api("/admin/api/config/apply", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  if (!result.applied) {
    showValidationResult(result);
    return;
  }
  const restart = result.restart || {};
  if (restart.required && restart.automatic) {
    showMessage("Applied. Restarting server...", "ok");
    byId("applyButton").disabled = true;
    setTimeout(() => {
      window.location.href = restart.admin_url || "/admin";
    }, 1600);
    return;
  }
  const pending = restart.required ? restart.fields || [] : result.pending_fields || [];
  await load();
  showMessage(
    pending.length
      ? `Applied. Restart fcc-server to use: ${pending.join(", ")}`
      : "Applied",
    "ok",
  );
}

async function refreshLocalStatus() {
  const result = await api("/admin/api/providers/local-status");
  result.providers.forEach((provider) => {
    state.localStatus.set(provider.provider_id, provider);
    const meta = provider.status_code
      ? `${provider.base_url} returned HTTP ${provider.status_code}`
      : provider.base_url;
    updateProviderCard(provider.provider_id, provider.status, provider.label, meta);
  });
}

async function testProvider(providerId, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Testing";
  try {
    const result = await api(`/admin/api/providers/${providerId}/test`, {
      method: "POST",
      body: "{}",
    });
    if (result.ok) {
      updateProviderCard(
        providerId,
        "reachable",
        `${result.models.length} models`,
        result.models.slice(0, 3).join(", ") || "No models returned",
      );
      setModelOptions([
        ...state.modelOptions,
        ...result.models.map((model) => `${providerId}/${model}`),
      ]);
    } else {
      updateProviderCard(providerId, "offline", result.error_type, result.error_type);
    }
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function hydrateModelOptions() {
  try {
    await loadModelOptions();
  } catch {
    // Model fields remain editable when optional catalog hydration is unavailable.
  }
}

async function loadModelOptions(refresh = false) {
  const result = await api("/admin/api/models" + (refresh ? "/refresh" : ""), {
    method: refresh ? "POST" : "GET",
  });
  setModelOptions(result.models);
  return result;
}

async function refreshModelOptions(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Refreshing";
  try {
    const result = await loadModelOptions(true);
    const failedProviders = result.failed_providers || [];
    if (failedProviders.length) {
      const labels = failedProviders.map(providerDisplayName).join(", ");
      showMessage(
        `${state.modelOptions.length} models available; could not refresh ${labels}`,
        "warn",
      );
    } else {
      showMessage(`${state.modelOptions.length} models available`, "ok");
    }
  } catch (error) {
    showMessage(`Could not refresh models: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function importChatGPTOAuthCodexTokens(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Importing...";
  try {
    const result = await api("/admin/api/chatgpt-oauth/import-codex", {
      method: "POST",
      body: "{}",
    });
    if (result.status === "complete") {
      fillChatGPTOAuthFields(
        result.credential_reference,
        result.account_id,
      );
      showMessage(
        "Copied renewable Codex credentials. Apply settings to activate the provider.",
        "ok",
      );
    }
  } catch (error) {
    showMessage(`Could not import Codex tokens: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function startChatGPTOAuthLogin(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Starting login...";

  try {
    // Primary: browser PKCE flow — the login page opens automatically and the
    // local callback completes the flow; no code to copy or paste.
    try {
      const initiate = await api("/admin/api/chatgpt-oauth/browser/initiate", {
        method: "POST",
        body: "{}",
      });
      window.open(initiate.authorize_url, "_blank", "noopener");
      showMessage(
        "ChatGPT OAuth: complete the login in the browser tab that just opened.",
        "warn",
      );
      await pollBrowserOAuthLogin();
      button.disabled = false;
      button.textContent = original;
      return;
    } catch (browserError) {
      showMessage(
        `Browser login unavailable (${browserError.message}); trying device-code login...`,
        "warn",
      );
    }

    await startDeviceOAuthLogin(button);
    button.disabled = false;
    button.textContent = original;
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showMessage(`ChatGPT OAuth login failed: ${error.message}`, "error");
  }
}

async function pollBrowserOAuthLogin() {
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const result = await api("/admin/api/chatgpt-oauth/browser/status", {
      method: "POST",
      body: "{}",
    });
    if (result.status === "complete") {
      fillChatGPTOAuthFields(
        result.credential_reference,
        result.account_id,
      );
      showMessage(
        "ChatGPT OAuth login complete. Apply settings to activate the provider.",
        "ok",
      );
      return;
    }
    if (result.status === "error") {
      throw new Error(result.message || "Browser login failed");
    }
  }
  throw new Error("Timed out waiting for the browser login to complete");
}

function fillChatGPTOAuthFields(credentialReference, accountId) {
  const tokenField = document.querySelector(
    '[data-key="CHATGPT_OAUTH_ACCESS_TOKEN"] input',
  );
  const accountField = document.querySelector(
    '[data-key="CHATGPT_OAUTH_ACCOUNT_ID"] input',
  );
  if (tokenField) {
    tokenField.value = credentialReference;
    tokenField.dispatchEvent(new Event("input"));
  }
  if (accountField && accountId) {
    accountField.value = accountId;
    accountField.dispatchEvent(new Event("input"));
  }
}

async function startDeviceOAuthLogin(button) {
  const initiate = await api("/admin/api/chatgpt-oauth/initiate", {
    method: "POST",
    body: "{}",
  });
  const verificationUrl = initiate.verification_url;
  const userCode = initiate.user_code;

  // Open the verification page automatically; the user only enters the code.
  window.open(verificationUrl, "_blank", "noopener");
  showMessage(
    `ChatGPT OAuth: a browser tab was opened for ${verificationUrl} - enter code ${userCode}`,
    "warn",
  );

  const deadline = Date.now() + 10 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 8000));
    const result = await api("/admin/api/chatgpt-oauth/exchange", {
      method: "POST",
      body: JSON.stringify({
        device_auth_id: initiate.device_auth_id,
        user_code: userCode,
      }),
    });
    if (result.status === "complete") {
      fillChatGPTOAuthFields(
        result.credential_reference,
        result.account_id,
      );
      showMessage(
        "ChatGPT OAuth login complete. Apply settings to activate the provider.",
        "ok",
      );
      return;
    }
  }
  throw new Error("Timed out waiting for device authorization");
}

function providerDisplayName(providerId) {
  const provider = state.config?.provider_status?.find(
    (candidate) => candidate.provider_id === providerId,
  );
  return provider?.display_name || providerId;
}

function setModelOptions(models) {
  state.modelOptions = Array.from(
    new Set(models.filter((model) => typeof model === "string" && model.trim())),
  ).sort((left, right) => left.localeCompare(right));
  state.modelComboboxes.forEach((combobox) => {
    if (combobox.isOpen) combobox.render(combobox.query);
  });
}

function webSearchProviders() {
  const providerField = state.fields.get("WEB_SEARCH_PROVIDER");
  if (!providerField) return [];
  const credentialFields = Array.from(state.fields.values()).filter(
    (field) => field.section === "websearch" && field.secret,
  );
  return providerField.options
    .filter((item) => !["auto", "off"].includes(item.value))
    .map((item) => {
      const credential = credentialFields.find(
        (field) => field.label === `${item.label} API Key`,
      );
      const baseUrlField =
        item.value === "searxng" ? state.fields.get("SEARXNG_BASE_URL") : null;
      const configured = credential
        ? credential.configured
        : baseUrlField
          ? baseUrlField.configured
          : true;
      return {
        id: item.value,
        label: item.label,
        envKey: credential ? credential.key : null,
        rotationKey: credential ? `${credential.key}_ROTATION` : null,
        configured,
      };
    });
}

function webSearchProviderMeta(provider, activeSelection) {
  const parts = [];
  if (activeSelection === provider.id) {
    parts.push("Active provider");
  } else if (activeSelection === "auto" && provider.configured) {
    parts.push("Auto-eligible");
  }
  parts.push(
    provider.envKey ||
      (provider.id === "searxng" ? "SEARXNG_BASE_URL" : "No key required"),
  );
  return parts.join(" · ");
}

// Advanced option fields are dotenv-only catalog entries whose env names are
// prefixed with the provider id (e.g. EXA_*, DDGS_*); the manifest marks them
// advanced so they group under each provider card instead of the grid.
function webSearchAdvancedFields(provider) {
  const prefix = `${provider.id.toUpperCase()}_`;
  return Array.from(state.fields.values()).filter(
    (field) =>
      field.section === "websearch" &&
      field.advanced &&
      field.key.startsWith(prefix),
  );
}

function renderWebSearchAdvanced(provider) {
  const fields = webSearchAdvancedFields(provider);
  if (fields.length === 0) return null;
  const details = document.createElement("details");
  details.className = "ws-advanced";
  const summary = document.createElement("summary");
  summary.textContent = "Advanced options";
  details.appendChild(summary);
  fields.forEach((field) => details.appendChild(renderField(field)));
  return details;
}

function renderWebSearchProviders() {
  const grid = byId("webSearchGrid");
  if (!grid) return;
  grid.innerHTML = "";
  const active = state.fields.get("WEB_SEARCH_PROVIDER")?.value || "auto";
  webSearchProviders().forEach((provider) => {
    const card = document.createElement("article");
    card.className = "provider-card";
    card.dataset.websearchProvider = provider.id;

    const title = document.createElement("div");
    title.className = "provider-title";
    title.innerHTML = `<strong>${provider.label}</strong>`;
    const pill = document.createElement("span");
    pill.className = `status-pill ${provider.configured ? "ok" : "warn"}`;
    pill.textContent = provider.configured ? "Configured" : "Missing key";
    title.appendChild(pill);

    const meta = document.createElement("div");
    meta.className = "provider-meta";
    meta.textContent = webSearchProviderMeta(provider, active);

    const actions = document.createElement("div");
    actions.className = "card-actions";

    const testButton = document.createElement("button");
    testButton.type = "button";
    testButton.className = "test-button";
    testButton.textContent = "Test";
    testButton.addEventListener("click", () =>
      testWebSearchProvider(provider, testButton),
    );
    actions.appendChild(testButton);

    card.append(title, meta, actions);
    const advanced = renderWebSearchAdvanced(provider);
    if (advanced) {
      card.appendChild(advanced);
    }
    if (provider.envKey) {
      const manageButton = document.createElement("button");
      manageButton.type = "button";
      manageButton.className = "ghost-button";
      manageButton.textContent = "Manage keys";
      const panel = document.createElement("div");
      panel.className = "ws-key-manager";
      panel.hidden = true;
      manageButton.addEventListener("click", () =>
        toggleKeyManager(provider, panel, manageButton),
      );
      actions.appendChild(manageButton);
      card.appendChild(panel);
    }
    grid.appendChild(card);
  });
}

function updateWebSearchCard(providerId, status, label, metaText) {
  const card = document.querySelector(`[data-websearch-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    card.querySelector(".provider-meta").textContent = metaText;
  }
}

function updateWebSearchCardsFromState() {
  const active = state.fields.get("WEB_SEARCH_PROVIDER")?.value || "auto";
  webSearchProviders().forEach((provider) => {
    const card = document.querySelector(
      `[data-websearch-provider="${provider.id}"]`,
    );
    if (!card) return;
    const pill = card.querySelector(".status-pill");
    pill.className = `status-pill ${provider.configured ? "ok" : "warn"}`;
    pill.textContent = provider.configured ? "Configured" : "Missing key";
    card.querySelector(".provider-meta").textContent = webSearchProviderMeta(
      provider,
      active,
    );
  });
}

async function refreshConfigState() {
  const config = await api("/admin/api/config");
  state.config = config;
  state.fields = new Map(config.fields.map((field) => [field.key, field]));
  config.fields.forEach((field) => {
    const input = document.querySelector(`[data-key="${field.key}"]`);
    if (input && input.dataset) {
      input.dataset.configured = field.configured ? "true" : "false";
    }
  });
  updateWebSearchCardsFromState();
}

async function toggleKeyManager(provider, panel, button) {
  if (panel.hidden) {
    panel.hidden = false;
    button.textContent = "Hide keys";
    await loadKeyManager(provider, panel);
  } else {
    panel.hidden = true;
    button.textContent = "Manage keys";
  }
}

function keyHealthClass(health) {
  if (!health) return "neutral";
  if (health.state === "healthy") return "ok";
  if (health.state === "cooldown") return "warn";
  return "error";
}

function keyHealthText(health) {
  if (!health) return "Unused";
  const stateName = String(health.state || "unknown").replace(/_/g, " ");
  return `${stateName} · ${health.requests} req · ${health.failures} err`;
}

async function loadKeyManager(provider, panel) {
  panel.innerHTML = "";
  const list = document.createElement("div");
  list.className = "ws-key-list";
  panel.appendChild(list);
  let result;
  try {
    result = await api(`/admin/api/websearch/credentials/${provider.envKey}/keys`);
  } catch (error) {
    list.textContent = `Could not load keys: ${error.message}`;
    return;
  }
  const healthByIndex = new Map(
    ((result.health && result.health.keys) || []).map((entry) => [
      entry.index,
      entry,
    ]),
  );
  if (result.keys.length === 0) {
    const empty = document.createElement("div");
    empty.className = "ws-key-empty";
    empty.textContent = "No keys configured.";
    list.appendChild(empty);
  }
  result.keys.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "ws-key-row";
    const label = document.createElement("span");
    label.className = "ws-key-label";
    label.textContent = entry.key_label || "(empty)";
    const health = healthByIndex.get(entry.index);
    const healthEl = document.createElement("span");
    healthEl.className = `status-pill ${keyHealthClass(health)}`;
    healthEl.textContent = keyHealthText(health);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button";
    remove.textContent = "Delete";
    remove.disabled = result.locked;
    remove.addEventListener("click", () =>
      deleteWebSearchKey(provider, entry.index, panel, remove),
    );
    row.append(label, healthEl, remove);
    list.appendChild(row);
  });
  const form = document.createElement("div");
  form.className = "ws-key-add";
  const input = document.createElement("input");
  input.type = "password";
  input.placeholder = "Paste a new API key";
  input.autocomplete = "off";
  input.disabled = result.locked;
  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary-button";
  add.textContent = "Add key";
  add.disabled = result.locked;
  add.addEventListener("click", () => addWebSearchKey(provider, input, panel, add));
  form.append(input, add);
  panel.appendChild(form);
  if (result.locked) {
    const note = document.createElement("div");
    note.className = "field-description";
    note.textContent = "This credential is locked by an external source; edit it there.";
    panel.appendChild(note);
  }
}

async function addWebSearchKey(provider, input, panel, button) {
  const key = input.value.trim();
  if (!key) {
    showMessage("Enter a key first", "warn");
    return;
  }
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/websearch/credentials/${provider.envKey}/keys`,
      { method: "POST", body: JSON.stringify({ key }) },
    );
    if (!result.applied) {
      showMessage((result.errors || []).join("; ") || "Key was not applied", "error");
      return;
    }
    showMessage(`Added key to ${provider.envKey}`, "ok");
    await refreshConfigState();
    await loadKeyManager(provider, panel);
  } catch (error) {
    showMessage(`Could not add key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function deleteWebSearchKey(provider, index, panel, button) {
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/websearch/credentials/${provider.envKey}/keys/${index}`,
      { method: "DELETE" },
    );
    if (!result.applied) {
      showMessage((result.errors || []).join("; ") || "Key was not applied", "error");
      return;
    }
    showMessage(`Removed key ${index} from ${provider.envKey}`, "ok");
    await refreshConfigState();
    await loadKeyManager(provider, panel);
  } catch (error) {
    showMessage(`Could not delete key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function testWebSearchProvider(provider, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Testing";
  try {
    const result = await api(`/admin/api/websearch/providers/${provider.id}/test`, {
      method: "POST",
      body: "{}",
    });
    if (result.ok) {
      const titles = (result.titles || []).filter(Boolean).slice(0, 2).join("; ");
      updateWebSearchCard(
        provider.id,
        "ok",
        `${result.result_count} results`,
        `OK in ${Math.round(result.latency_ms)} ms${titles ? ` — ${titles}` : ""}`,
      );
    } else {
      const error = result.error || {};
      updateWebSearchCard(
        provider.id,
        "error",
        error.kind || "error",
        error.message || "Web search test failed",
      );
    }
  } catch (error) {
    updateWebSearchCard(provider.id, "error", "error", error.message);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function asAnalyticsRows(value, keyName) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") {
    return Object.entries(value).map(([name, row]) => ({ [keyName]: name, ...row }));
  }
  return [];
}

function analyticsTable(headers, rows, emptyText) {
  const table = document.createElement("table");
  table.className = "analytics-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((header) => {
    const th = document.createElement("th");
    th.textContent = header;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  const tbody = document.createElement("tbody");
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = headers.length;
    td.className = "analytics-empty";
    td.textContent = emptyText;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  rows.forEach((cells) => {
    const tr = document.createElement("tr");
    cells.forEach((cell) => {
      const td = document.createElement("td");
      td.textContent = cell;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
  return table;
}

function analyticsBlock(title, table) {
  const block = document.createElement("div");
  block.className = "analytics-block";
  const heading = document.createElement("h4");
  heading.textContent = title;
  block.append(heading, table);
  return block;
}

function formatRequestTime(entry) {
  const iso = entry.ts_iso || entry.ts || "";
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso || "—";
  return new Date(parsed).toLocaleString();
}

function renderWebSearchAnalytics(container, stats, requests, period) {
  container.innerHTML = "";
  const totals = (stats && stats.totals) || {};
  const summary = document.createElement("div");
  summary.className = "analytics-summary";
  summary.textContent = stats
    ? `${period === "monthly" ? "Monthly" : "Weekly"}: ` +
      `${totals.requests ?? 0} requests · ${totals.errors ?? 0} errors · ` +
      `${totals.results ?? 0} results`
    : "Summary unavailable.";
  container.appendChild(summary);

  const providerRows = asAnalyticsRows(stats && stats.by_provider, "provider").map(
    (row) => [
      row.provider || "—",
      row.requests ?? 0,
      row.errors ?? 0,
      row.avg_duration_ms != null ? Math.round(row.avg_duration_ms) : "—",
      row.results ?? 0,
    ],
  );
  container.appendChild(
    analyticsBlock(
      "Per provider",
      analyticsTable(
        ["Provider", "Requests", "Errors", "Avg ms", "Results"],
        providerRows,
        "No web searches recorded yet.",
      ),
    ),
  );

  const keyRows = asAnalyticsRows(stats && stats.by_key, "key_label").map((row) => [
    row.provider || "—",
    row.key_label || row.key || "—",
    row.requests ?? 0,
    row.errors ?? 0,
  ]);
  container.appendChild(
    analyticsBlock(
      "Per key",
      analyticsTable(
        ["Provider", "Key", "Requests", "Errors"],
        keyRows,
        "No key usage recorded yet.",
      ),
    ),
  );

  const requestItems = requests
    ? requests.requests || requests.items || (Array.isArray(requests) ? requests : [])
    : [];
  const requestRows = requestItems.map((entry) => [
    formatRequestTime(entry),
    entry.provider || "—",
    entry.key_label || "—",
    entry.query || "—",
    entry.results_count ?? 0,
    entry.duration_ms != null ? Math.round(entry.duration_ms) : "—",
    entry.status || "—",
  ]);
  container.appendChild(
    analyticsBlock(
      "Recent requests",
      analyticsTable(
        ["Time", "Provider", "Key", "Query", "Results", "ms", "Status"],
        requestRows,
        "No recent web search requests.",
      ),
    ),
  );
}

async function loadWebSearchAnalytics(period) {
  state.webSearchStatsPeriod = period;
  document.querySelectorAll(".period-button").forEach((button) => {
    const buttonPeriod = button.id === "webSearchStatsMonthly" ? "monthly" : "weekly";
    button.classList.toggle("active", buttonPeriod === period);
  });
  const container = byId("webSearchAnalytics");
  container.textContent = "Loading analytics…";
  let stats = null;
  let requests = null;
  try {
    stats = await api(`/admin/api/websearch/stats?period=${period}`);
  } catch {
    stats = null;
  }
  try {
    requests = await api("/admin/api/websearch/requests?limit=25");
  } catch {
    requests = null;
  }
  if (!stats && !requests) {
    container.textContent =
      "Web search analytics are unavailable (log API not reachable).";
    return;
  }
  renderWebSearchAnalytics(container, stats, requests, period);
}

function showMessage(message, kind = "") {
  const area = byId("messageArea");
  area.textContent = message;
  area.className = `message-area ${kind}`.trim();
}

/* --------------------------------------------------------------------- */
/* Custom providers                                                        */
/* --------------------------------------------------------------------- */

const CUSTOM_PROVIDER_STATUS_LABELS = {
  configured: "Configured",
  missing_key: "Missing key",
  disabled: "Disabled",
};

async function loadCustomProviders() {
  const grid = byId("customProviderGrid");
  if (!grid) return;
  let result;
  try {
    result = await api("/admin/api/custom-providers");
  } catch (error) {
    grid.innerHTML = "";
    const note = document.createElement("div");
    note.className = "cp-note";
    note.textContent = `Custom providers unavailable: ${error.message}`;
    grid.appendChild(note);
    return;
  }
  state.customProviders = result.providers || [];
  renderCustomProviders();
}

function renderCustomProviders() {
  const grid = byId("customProviderGrid");
  grid.innerHTML = "";
  if (state.customProviders.length === 0) {
    const empty = document.createElement("div");
    empty.className = "cp-note";
    empty.textContent = "No custom providers yet.";
    grid.appendChild(empty);
    return;
  }
  state.customProviders.forEach((provider) => {
    grid.appendChild(customProviderCard(provider));
  });
}

function customProviderCard(provider) {
  const card = document.createElement("article");
  card.className = "provider-card";
  card.dataset.customProvider = provider.provider_id;

  const title = document.createElement("div");
  title.className = "provider-title";
  title.innerHTML = `<strong>${provider.display_name || provider.provider_id}</strong>`;
  const pill = document.createElement("span");
  pill.className = `status-pill ${statusClass(provider.status)}`;
  pill.textContent =
    CUSTOM_PROVIDER_STATUS_LABELS[provider.status] || provider.status;
  title.appendChild(pill);

  const meta = document.createElement("div");
  meta.className = "provider-meta";
  meta.textContent = provider.base_url;

  const details = document.createElement("div");
  details.className = "cp-details";
  details.textContent =
    `${provider.key_count} key${provider.key_count === 1 ? "" : "s"} · ` +
    `${provider.credential_rotation} · ${provider.model_count} models` +
    (provider.proxy ? ` · proxy ${provider.proxy}` : "");

  const keyList = document.createElement("div");
  keyList.className = "cp-key-list";
  provider.masked_keys.forEach((masked, index) => {
    const row = document.createElement("div");
    row.className = "cp-key-row";
    const label = document.createElement("code");
    label.className = "cp-key-label";
    label.textContent = masked;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button";
    remove.textContent = "Remove";
    remove.addEventListener("click", () =>
      removeCustomProviderKey(provider, index, remove),
    );
    row.append(label, remove);
    keyList.appendChild(row);
  });

  const addRow = document.createElement("div");
  addRow.className = "cp-key-add";
  const keyInput = document.createElement("input");
  keyInput.type = "password";
  keyInput.autocomplete = "off";
  keyInput.placeholder = "Paste a new key";
  const addButton = document.createElement("button");
  addButton.type = "button";
  addButton.className = "secondary-button";
  addButton.textContent = "Add key";
  const submitKey = () => addCustomProviderKey(provider, keyInput, addButton);
  addButton.addEventListener("click", submitKey);
  keyInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submitKey();
  });
  addRow.append(keyInput, addButton);
  keyList.appendChild(addRow);

  const actions = document.createElement("div");
  actions.className = "card-actions";

  const testButton = document.createElement("button");
  testButton.type = "button";
  testButton.className = "test-button";
  testButton.textContent = "Test";
  testButton.addEventListener("click", () =>
    testCustomProvider(provider, testButton),
  );

  const editButton = document.createElement("button");
  editButton.type = "button";
  editButton.className = "secondary-button";
  editButton.textContent = "Edit";
  editButton.addEventListener("click", () => openCustomProviderForm(provider));

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "secondary-button danger";
  deleteButton.textContent = "Delete";
  deleteButton.addEventListener("click", () =>
    deleteCustomProvider(provider, deleteButton),
  );

  actions.append(testButton, editButton, deleteButton);
  card.append(title, meta, details, keyList, actions);
  return card;
}

function updateCustomProviderCard(providerId, status, label, metaText) {
  const card = document.querySelector(`[data-custom-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    card.querySelector(".provider-meta").textContent = metaText;
  }
}

async function testCustomProvider(provider, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Testing";
  try {
    const result = await api(
      `/admin/api/providers/${provider.provider_id}/test`,
      { method: "POST", body: "{}" },
    );
    if (result.ok) {
      updateCustomProviderCard(
        provider.provider_id,
        "reachable",
        `${result.models.length} models`,
        result.models.slice(0, 3).join(", ") || "No models returned",
      );
      setModelOptions([
        ...state.modelOptions,
        ...result.models.map((model) => `${provider.provider_id}/${model}`),
      ]);
    } else {
      updateCustomProviderCard(
        provider.provider_id,
        "offline",
        result.error_type,
        result.error_type,
      );
    }
  } catch (error) {
    updateCustomProviderCard(
      provider.provider_id,
      "offline",
      "error",
      error.message,
    );
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function addCustomProviderKey(provider, input, button) {
  const key = input.value.trim();
  if (!key) {
    showMessage("Enter a key first", "warn");
    return;
  }
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}/keys`,
      { method: "POST", body: JSON.stringify({ api_key: key }) },
    );
    showMessage(`Added key ${result.added} (${result.key_count} configured).`, "ok");
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not add key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function removeCustomProviderKey(provider, index, button) {
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}/keys/${index}`,
      { method: "DELETE" },
    );
    showMessage(`Removed key ${result.removed} (${result.key_count} remaining).`, "ok");
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not remove key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function deleteCustomProvider(provider, button) {
  const confirmed = window.confirm(
    `Delete custom provider "${provider.display_name}" (${provider.provider_id})?`,
  );
  if (!confirmed) return;
  button.disabled = true;
  try {
    await api(`/admin/api/custom-providers/${provider.provider_id}`, {
      method: "DELETE",
    });
    showMessage(`Deleted ${provider.display_name}.`, "ok");
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not delete provider: ${error.message}`, "error");
    button.disabled = false;
  }
}

function openCustomProviderForm(provider) {
  state.editingCustomProviderId = provider ? provider.provider_id : null;
  byId("cpDisplayName").value = provider ? provider.display_name : "";
  byId("cpBaseUrl").value = provider ? provider.base_url : "";
  byId("cpApiKey").value = "";
  byId("cpApiKeyField").hidden = Boolean(provider);
  byId("cpRotation").value = provider ? provider.credential_rotation : "failover";
  byId("cpProxy").value = provider && provider.proxy ? provider.proxy : "";
  byId("cpSubmitButton").textContent = provider ? "Save changes" : "Add provider";
  byId("customProviderForm").hidden = false;
  byId("cpDisplayName").focus();
}

function closeCustomProviderForm() {
  byId("customProviderForm").hidden = true;
  state.editingCustomProviderId = null;
}

async function submitCustomProviderForm(event) {
  event.preventDefault();
  const editingId = state.editingCustomProviderId;
  const button = byId("cpSubmitButton");
  button.disabled = true;
  try {
    if (editingId) {
      await api(`/admin/api/custom-providers/${editingId}`, {
        method: "PATCH",
        body: JSON.stringify({
          display_name: byId("cpDisplayName").value,
          base_url: byId("cpBaseUrl").value,
          credential_rotation: byId("cpRotation").value,
          proxy: byId("cpProxy").value,
        }),
      });
      showMessage(`Updated ${editingId}.`, "ok");
    } else {
      const result = await api("/admin/api/custom-providers", {
        method: "POST",
        body: JSON.stringify({
          display_name: byId("cpDisplayName").value,
          base_url: byId("cpBaseUrl").value,
          api_key: byId("cpApiKey").value,
          credential_rotation: byId("cpRotation").value,
          proxy: byId("cpProxy").value,
        }),
      });
      if (result.test_error) {
        showMessage(
          `Added ${result.display_name}, but the live test failed: ${result.test_error}`,
          "warn",
        );
      } else {
        const preview = result.models.slice(0, 3).join(", ");
        showMessage(
          `Added ${result.display_name} — ${result.model_count} models detected` +
            (preview ? `: ${preview}` : ""),
          "ok",
        );
      }
      setModelOptions([
        ...state.modelOptions,
        ...result.models.map((model) => `${result.provider_id}/${model}`),
      ]);
    }
    closeCustomProviderForm();
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not save custom provider: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

byId("addCustomProviderButton").addEventListener("click", () =>
  openCustomProviderForm(null),
);
byId("cpCancelButton").addEventListener("click", closeCustomProviderForm);
byId("customProviderForm").addEventListener("submit", submitCustomProviderForm);

byId("validateButton").addEventListener("click", () => validate(true));
byId("applyButton").addEventListener("click", apply);
byId("webSearchStatsWeekly").addEventListener("click", () =>
  loadWebSearchAnalytics("weekly"),
);
byId("webSearchStatsMonthly").addEventListener("click", () =>
  loadWebSearchAnalytics("monthly"),
);
document.addEventListener("pointerdown", (event) => {
  state.modelComboboxes.forEach((combobox) => {
    if (combobox.isOpen && !combobox.element.contains(event.target)) combobox.close();
  });
});

load().catch((error) => {
  showMessage(error.message, "error");
});


/* --------------------------------------------------------------------- */
/* Requests / analytics view                                             */
/* --------------------------------------------------------------------- */

const reqState = {
  offset: 0,
  limit: 25,
  total: 0,
};

function reqFilters() {
  const params = new URLSearchParams();
  const provider = byId("reqFilterProvider").value.trim();
  const model = byId("reqFilterModel").value.trim();
  const status = byId("reqFilterStatus").value;
  const search = byId("reqFilterSearch").value.trim();
  const windowSeconds = byId("reqFilterWindow").value;
  if (provider) params.set("provider", provider);
  if (model) params.set("model", model);
  if (status) params.set("status", status);
  if (search) params.set("q", search);
  if (windowSeconds) {
    params.set("since", (Date.now() / 1000 - Number(windowSeconds)).toFixed(0));
  }
  return params;
}

async function loadRequestsView() {
  const params = reqFilters();
  const [stats, list] = await Promise.all([
    api(`/admin/api/requests/stats?${params}`),
    api(`/admin/api/requests?limit=${reqState.limit}&offset=${reqState.offset}&${params}`),
  ]);
  if (stats.enabled === false) {
    byId("reqStatsCards").innerHTML = "";
    byId("reqTableBody").innerHTML = "";
    byId("reqBodiesIndicator").textContent = "Request log disabled (REQUEST_LOG_ENABLED=false)";
    byId("reqPageInfo").textContent = "";
    return;
  }
  byId("reqBodiesIndicator").textContent = stats.capture_bodies
    ? "Bodies: captured"
    : "Bodies: hashes only (REQUEST_LOG_CAPTURE_BODIES=false)";
  renderRequestStatsCards(stats);
  renderReqSeriesChart(stats.series || []);
  renderReqModelChart(stats.by_model || []);
  reqState.total = list.total || 0;
  renderRequestsTable(list.rows || []);
  renderReqPager();
}

function renderRequestStatsCards(stats) {
  const cards = [
    ["Total requests", stats.total],
    ["Error rate", `${((stats.error_rate || 0) * 100).toFixed(1)}%`],
    ["Cancelled", stats.cancelled],
    ["Tokens in", stats.tokens_in],
    ["Tokens out", stats.tokens_out],
    ["Avg duration", stats.avg_duration_ms != null ? `${stats.avg_duration_ms} ms` : "—"],
    ["p95 duration", stats.p95_duration_ms != null ? `${stats.p95_duration_ms} ms` : "—"],
    ["Avg TTFT", stats.avg_ttft_ms != null ? `${stats.avg_ttft_ms} ms` : "—"],
  ];
  const container = byId("reqStatsCards");
  container.innerHTML = "";
  cards.forEach(([label, value]) => {
    const card = document.createElement("div");
    card.className = "requests-card";
    const valueEl = document.createElement("strong");
    valueEl.textContent = value;
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    card.append(valueEl, labelEl);
    container.appendChild(card);
  });
}

function renderRequestsTable(rows) {
  const body = byId("reqTableBody");
  body.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = `req-row req-status-${row.status}`;
    const cells = [
      (row.ts_iso || "").replace("T", " ").slice(0, 19),
      row.endpoint || "",
      row.provider || "",
      row.resolved_model || row.requested_model || "",
      row.status,
      `${row.tokens_in ?? "—"}/${row.tokens_out ?? "—"}`,
      row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—",
    ];
    cells.forEach((text) => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    });
    tr.addEventListener("click", () => openRequestDetail(row.id));
    body.appendChild(tr);
  });
}

function renderReqPager() {
  const start = reqState.total === 0 ? 0 : reqState.offset + 1;
  const end = Math.min(reqState.offset + reqState.limit, reqState.total);
  byId("reqPageInfo").textContent = `${start}–${end} of ${reqState.total}`;
  byId("reqPrevPage").disabled = reqState.offset === 0;
  byId("reqNextPage").disabled = end >= reqState.total;
}

function drawBarChart(canvas, labels, series) {
  const ctx = canvas.getContext("2d");
  const { width, height } = canvas;
  ctx.clearRect(0, 0, width, height);
  const pad = 24;
  const max = Math.max(1, ...series.flatMap((s) => s.values));
  const groups = labels.length || 1;
  const groupWidth = (width - pad * 2) / groups;
  const colors = ["#4f8ef7", "#e05d5d"];
  series.forEach((s, seriesIndex) => {
    ctx.fillStyle = colors[seriesIndex % colors.length];
    s.values.forEach((value, i) => {
      const barWidth = groupWidth / (series.length + 1);
      const x = pad + i * groupWidth + seriesIndex * barWidth;
      const barHeight = ((height - pad * 2) * value) / max;
      ctx.fillRect(x, height - pad - barHeight, barWidth * 0.8, barHeight);
    });
  });
  ctx.fillStyle = "#888";
  ctx.font = "10px sans-serif";
  labels.forEach((label, i) => {
    if (labels.length > 12 && i % Math.ceil(labels.length / 12) !== 0) return;
    ctx.fillText(label, pad + i * groupWidth, height - 8);
  });
}

function renderReqSeriesChart(series) {
  const labels = series.map((point) => (point.bucket || "").slice(5));
  drawBarChart(document.getElementById("reqSeriesChart"), labels, [
    { values: series.map((point) => point.requests) },
    { values: series.map((point) => point.errors) },
  ]);
}

function renderReqModelChart(byModel) {
  const top = byModel.slice(0, 10);
  const canvas = document.getElementById("reqModelChart");
  const ctx = canvas.getContext("2d");
  const { width, height } = canvas;
  ctx.clearRect(0, 0, width, height);
  const max = Math.max(1, ...top.map((m) => m.tokens_in + m.tokens_out));
  const rowHeight = Math.min(20, (height - 10) / Math.max(1, top.length));
  top.forEach((model, i) => {
    const tokens = model.tokens_in + model.tokens_out;
    const barWidth = ((width - 180) * tokens) / max;
    ctx.fillStyle = "#4f8ef7";
    ctx.fillRect(160, 5 + i * rowHeight, barWidth, rowHeight - 4);
    ctx.fillStyle = "#888";
    ctx.font = "10px sans-serif";
    ctx.fillText(model.key.slice(0, 24), 4, 14 + i * rowHeight);
  });
}

async function openRequestDetail(requestId) {
  const row = await api(`/admin/api/requests/${requestId}`);
  byId("reqDetailTitle").textContent = `Request ${row.id}`;
  const meta = byId("reqDetailMeta");
  meta.innerHTML = "";
  const fields = [
    ["Time", row.ts_iso],
    ["Endpoint", row.endpoint],
    ["Protocol", row.protocol],
    ["Requested model", row.requested_model],
    ["Provider", row.provider],
    ["Resolved model", row.resolved_model],
    ["Status", row.status],
    ["Error", row.error_kind ? `${row.error_kind}: ${row.error_message || ""}` : ""],
    ["Tokens", `${row.tokens_in ?? "—"} in / ${row.tokens_out ?? "—"} out`],
    ["TTFT", row.ttft_ms != null ? `${Math.round(row.ttft_ms)} ms` : "—"],
    ["Duration", row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—"],
    ["Reasoning", row.reasoning],
    ["Params", row.params ? JSON.stringify(row.params) : ""],
    ["Input SHA-256", row.input_sha256],
    ["Output SHA-256", row.output_sha256],
  ];
  fields.forEach(([label, value]) => {
    if (value == null || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    meta.append(dt, dd);
  });
  byId("reqDetailInput").textContent = row.input_text || "(not captured)";
  byId("reqDetailOutput").textContent = row.output_text || "(not captured)";
  byId("reqDetailModal").hidden = false;
}

byId("reqDetailClose").addEventListener("click", () => {
  byId("reqDetailModal").hidden = true;
});
byId("reqApplyFilters").addEventListener("click", () => {
  reqState.offset = 0;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
});
byId("reqFilterSearch").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  reqState.offset = 0;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
});
byId("reqPrevPage").addEventListener("click", () => {
  reqState.offset = Math.max(0, reqState.offset - reqState.limit);
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
});
byId("reqNextPage").addEventListener("click", () => {
  reqState.offset += reqState.limit;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
});
byId("reqClearButton").addEventListener("click", () => {
  if (!window.confirm("Delete the entire request log?")) return;
  api("/admin/api/requests", { method: "DELETE" })
    .then(() => {
      reqState.offset = 0;
      return loadRequestsView();
    })
    .catch((error) => showMessage(error.message, "error"));
});
