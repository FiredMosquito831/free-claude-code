
// Anthropic reports tokens_in as the *uncached* portion, so total prompt input
// is uncached + cache reads + cache writes. Anything else understates the rate.
function formatCacheHitRate(row) {
  const uncached = Number(row?.tokens_in || 0);
  const read = Number(row?.cache_read_tokens || 0);
  const written = Number(row?.cache_write_tokens || 0);
  const total = uncached + read + written;
  if (!total) return "—";
  return `${((read / total) * 100).toFixed(1)}%`;
}

const state = {
  config: null,
  fields: new Map(),
  localStatus: new Map(),
  modelOptions: [],
  modelComboboxes: new Set(),
  activeView: "providers",
  webSearchStatsPeriod: "daily",
  webSearchAnalyticsStats: null,
  webSearchAnalyticsStatsKey: "",
  webSearchAnalyticsPage: null,
  webSearchAnalyticsPageKey: "",
  webSearchAnalyticsLoadId: 0,
  webSearchLastRoute: null,
  webSearchDetailReturnFocus: null,
  customProviders: [],
  editingCustomProviderId: null,
  versionInfo: null,
  versionUpgrading: false,
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
    label: "Analytics",
    title: "Observability",
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
  await loadVersionInfo();
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

  if (activeView.id === "web_search") {
    loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error"));
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
    if (field.key === "CHATGPT_OAUTH_IMPORT_CODEX") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary-button";
      button.textContent = "Import existing Codex login";
      button.addEventListener("click", () => {
        importChatGPTOAuthCodexTokens(button);
      });
      wrapper.appendChild(button);
      return wrapper;
    }

    const deviceButton = document.createElement("button");
    deviceButton.type = "button";
    deviceButton.className = "primary-button";
    deviceButton.textContent = "Log in with device code";

    const browserButton = document.createElement("button");
    browserButton.type = "button";
    browserButton.className = "secondary-button";
    browserButton.textContent = "Browser login (same device)";

    const loginButtons = [deviceButton, browserButton];
    deviceButton.addEventListener("click", () => {
      startChatGPTOAuthDeviceLogin(deviceButton, loginButtons);
    });
    browserButton.addEventListener("click", () => {
      startChatGPTOAuthBrowserLogin(browserButton, loginButtons);
    });
    wrapper.append(deviceButton, browserButton);
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

async function runChatGPTOAuthLogin(button, buttons, progressLabel, login) {
  const labels = buttons.map((candidate) => candidate.textContent);
  buttons.forEach((candidate) => {
    candidate.disabled = true;
  });
  button.textContent = progressLabel;
  try {
    await login();
  } catch (error) {
    showMessage(`ChatGPT OAuth login failed: ${error.message}`, "error");
  } finally {
    buttons.forEach((candidate, index) => {
      candidate.disabled = false;
      candidate.textContent = labels[index];
    });
  }
}

async function startChatGPTOAuthDeviceLogin(button, buttons) {
  await runChatGPTOAuthLogin(
    button,
    buttons,
    "Starting device login...",
    startDeviceOAuthLogin,
  );
}

async function startChatGPTOAuthBrowserLogin(button, buttons) {
  await runChatGPTOAuthLogin(
    button,
    buttons,
    "Starting browser login...",
    async () => {
      // This explicit option is only safe when the browser and FCC share the
      // same localhost. Device-code login is the cross-WSL/remote default.
      const initiate = await api(
        "/admin/api/chatgpt-oauth/browser/initiate?same_host_confirmed=true",
        {
          method: "POST",
          body: "{}",
        },
      );
      window.open(initiate.authorize_url, "_blank", "noopener");
      showMessage(
        "ChatGPT OAuth: complete the login in the same-device browser tab.",
        "warn",
      );
      await pollBrowserOAuthLogin();
    },
  );
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
  if (accountField) {
    accountField.value = accountId || "";
    accountField.dispatchEvent(new Event("input"));
  }
}

async function startDeviceOAuthLogin() {
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
    .filter((item) => !["auto", "off", "disabled"].includes(item.value))
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

function effectiveWebSearchProvider(providers, activeSelection) {
  if (activeSelection === "disabled") return null;
  if (activeSelection === "off") return "legacy";
  if (activeSelection !== "auto") return activeSelection;
  return (
    providers.find((provider) => provider.id !== "ddgs" && provider.configured)?.id ||
    "ddgs"
  );
}

function webSearchProviderMeta(provider, activeSelection, effectiveProvider) {
  const parts = [];
  if (effectiveProvider === provider.id) {
    parts.push(activeSelection === "auto" ? "Effective via auto" : "Selected");
  } else if (activeSelection === "auto" && provider.configured) {
    parts.push("Available");
  }
  parts.push(
    provider.envKey ||
      (provider.id === "searxng" ? "SEARXNG_BASE_URL" : "No key required"),
  );
  return parts.join(" · ");
}

function renderWebSearchRouteSummary(providers, activeSelection, effectiveProvider) {
  const summary = byId("webSearchRouteSummary");
  if (!summary) return;
  const fallbackPolicy =
    state.fields.get("WEB_SEARCH_FALLBACK_POLICY")?.value || "auto";
  const effectiveDescriptor = providers.find(
    (provider) => provider.id === effectiveProvider,
  );
  const providerLabel = (providerId) =>
    providerId === "legacy"
      ? "Legacy DuckDuckGo scraper"
      : providers.find((provider) => provider.id === providerId)?.label || providerId;
  const selectionLabel =
    activeSelection === "auto"
      ? "Auto"
      : activeSelection === "off"
        ? "Legacy compatibility"
        : activeSelection === "disabled"
          ? "Disabled"
          : providers.find((provider) => provider.id === activeSelection)?.label ||
            activeSelection;
  const resolvedPolicy =
    fallbackPolicy === "auto"
      ? activeSelection === "auto"
        ? "legacy"
        : "none"
      : fallbackPolicy;
  const routeIds = [];
  if (activeSelection === "disabled") {
    routeIds.push("disabled");
  } else if (activeSelection === "off") {
    routeIds.push("legacy");
  } else if (effectiveProvider) {
    routeIds.push(effectiveProvider);
    if (
      (resolvedPolicy === "ddgs" || resolvedPolicy === "legacy") &&
      effectiveProvider !== "ddgs"
    ) {
      routeIds.push("ddgs");
    }
    if (resolvedPolicy === "legacy") routeIds.push("legacy");
  }
  const routeLabel =
    routeIds[0] === "disabled"
      ? "Disabled"
      : routeIds.map(providerLabel).join(" → ");
  summary.innerHTML = "";
  const route = document.createElement("div");
  route.className = "route-summary-main";
  const title = document.createElement("strong");
  title.textContent = `Configured route: ${routeLabel}`;
  const detail = document.createElement("span");
  detail.textContent =
    `Selection: ${selectionLabel} · Fallback: ${fallbackPolicy}` +
    (fallbackPolicy === "auto" ? ` (resolves to ${resolvedPolicy})` : "") +
    " · Configuration errors stop the route";
  route.append(title, detail);
  const note = document.createElement("span");
  const ready =
    effectiveProvider === "legacy" ||
    Boolean(effectiveDescriptor && effectiveDescriptor.configured);
  note.className = `status-pill ${
    ready ? "ok" : effectiveProvider ? "warn" : "neutral"
  }`;
  note.textContent = ready
    ? "Ready"
    : effectiveProvider
      ? "Needs configuration"
      : "Search disabled";
  summary.append(route, note);
  renderWebSearchObservedRoute(state.webSearchLastRoute);
}

function renderWebSearchObservedRoute(lastRoute) {
  const route = byId("webSearchRouteSummary")?.querySelector(".route-summary-main");
  if (!route) return;
  route.querySelector(".route-summary-observed")?.remove();
  if (!lastRoute) return;
  const observed = document.createElement("span");
  observed.className = "route-summary-observed";
  const providers = Array.isArray(lastRoute.providers)
    ? lastRoute.providers
    : [];
  const path =
    providers.length > 0
      ? providers.join(" → ")
      : lastRoute.terminal_provider || lastRoute.primary_provider || "unknown";
  const duration =
    lastRoute.duration_ms == null ? "unknown latency" : `${lastRoute.duration_ms} ms`;
  observed.textContent =
    `Last observed: ${path} · ${lastRoute.status || "unknown"} · ${duration}`;
  route.appendChild(observed);
}

function populateWebSearchAnalyticsProviders(providers) {
  const select = byId("webSearchFilterProvider");
  if (!select) return;
  const selected = select.value;
  select.replaceChildren(new Option("all providers", ""));
  providers.forEach((provider) => {
    select.add(new Option(provider.label, provider.id));
  });
  if (providers.some((provider) => provider.id === selected)) {
    select.value = selected;
  }
}

function selectWebSearchProvider(providerId) {
  const input = document.querySelector(
    'select[data-key="WEB_SEARCH_PROVIDER"]',
  );
  const field = state.fields.get("WEB_SEARCH_PROVIDER");
  if (!input || !field) return;
  input.value = providerId;
  field.value = providerId;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  updateWebSearchCardsFromState();
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
  const providers = webSearchProviders();
  const effectiveProvider = effectiveWebSearchProvider(providers, active);
  populateWebSearchAnalyticsProviders(providers);
  renderWebSearchRouteSummary(providers, active, effectiveProvider);
  providers.forEach((provider) => {
    const card = document.createElement("article");
    card.className = `provider-card${
      effectiveProvider === provider.id ? " effective-provider" : ""
    }`;
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
    meta.textContent = webSearchProviderMeta(provider, active, effectiveProvider);

    const actions = document.createElement("div");
    actions.className = "card-actions";

    const selectButton = document.createElement("button");
    selectButton.type = "button";
    selectButton.className = "ghost-button";
    selectButton.textContent =
      active === provider.id ? "Selected" : "Use provider";
    selectButton.disabled = active === provider.id || !provider.configured;
    selectButton.addEventListener("click", () =>
      selectWebSearchProvider(provider.id),
    );
    actions.appendChild(selectButton);

    const testButton = document.createElement("button");
    testButton.type = "button";
    testButton.className = "test-button";
    testButton.textContent = "Test provider";
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
  ["WEB_SEARCH_PROVIDER", "WEB_SEARCH_FALLBACK_POLICY"].forEach((key) => {
    const input = document.querySelector(`select[data-key="${key}"]`);
    if (!input || input.dataset.routeSummaryWired === "true") return;
    input.dataset.routeSummaryWired = "true";
    input.addEventListener("change", () => {
      const field = state.fields.get(key);
      if (field) field.value = input.value;
      updateWebSearchCardsFromState();
    });
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
  const providers = webSearchProviders();
  const effectiveProvider = effectiveWebSearchProvider(providers, active);
  renderWebSearchRouteSummary(providers, active, effectiveProvider);
  providers.forEach((provider) => {
    const card = document.querySelector(
      `[data-websearch-provider="${provider.id}"]`,
    );
    if (!card) return;
    card.classList.toggle("effective-provider", effectiveProvider === provider.id);
    const pill = card.querySelector(".status-pill");
    pill.className = `status-pill ${provider.configured ? "ok" : "warn"}`;
    pill.textContent = provider.configured ? "Configured" : "Missing key";
    card.querySelector(".provider-meta").textContent = webSearchProviderMeta(
      provider,
      active,
      effectiveProvider,
    );
    const selectButton = Array.from(card.querySelectorAll("button")).find(
      (button) =>
        button.textContent === "Selected" || button.textContent === "Use provider",
    );
    if (selectButton) {
      selectButton.textContent = active === provider.id ? "Selected" : "Use provider";
      selectButton.disabled = active === provider.id || !provider.configured;
    }
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
      if (cell instanceof Node) {
        td.appendChild(cell);
      } else {
        td.textContent = cell;
      }
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
  const scroll = document.createElement("div");
  scroll.className = "table-scroll";
  scroll.appendChild(table);
  block.append(heading, scroll);
  return block;
}

function formatRequestTime(entry) {
  const iso = entry.ts_iso || entry.ts || "";
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso || "—";
  return new Date(parsed).toLocaleString();
}

function formatAnalyticsNumber(value, maximumFractionDigits = 0) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits });
}

function formatAnalyticsCost(value) {
  if (value == null || Number.isNaN(Number(value))) return "Unknown";
  return `$${Number(value).toFixed(Number(value) < 0.01 ? 4 : 2)}`;
}

function analyticsMetricCards(metrics) {
  const container = document.createElement("div");
  container.className = "requests-cards";
  metrics.forEach(([label, value, detail = ""]) => {
    const card = document.createElement("div");
    card.className = "requests-card";
    const valueElement = document.createElement("strong");
    valueElement.textContent = value;
    const labelElement = document.createElement("span");
    labelElement.textContent = label;
    card.append(valueElement, labelElement);
    if (detail) {
      const detailElement = document.createElement("small");
      detailElement.textContent = detail;
      card.appendChild(detailElement);
    }
    container.appendChild(card);
  });
  return container;
}

function aggregateWebSearchSeries(series) {
  const buckets = new Map();
  (series || []).forEach((entry) => {
    const bucket = entry.bucket || "unknown";
    const aggregate = buckets.get(bucket) || {
      bucket,
      requests: 0,
      errors: 0,
      results: 0,
    };
    aggregate.requests += Number(entry.searches ?? entry.requests ?? 0);
    aggregate.errors += Number(entry.errors || 0);
    aggregate.results += Number(entry.results || 0);
    buckets.set(bucket, aggregate);
  });
  return Array.from(buckets.values()).sort((left, right) =>
    left.bucket.localeCompare(right.bucket),
  );
}

function webSearchSeriesChart(series) {
  const wrapper = document.createElement("section");
  wrapper.className = "requests-chart analytics-panel";
  const heading = document.createElement("div");
  heading.className = "chart-heading";
  const title = document.createElement("h4");
  title.textContent = "Search volume and errors";
  const legend = document.createElement("div");
  legend.className = "chart-legend";
  legend.innerHTML =
    '<span><i class="legend-swatch requests"></i>Logical searches</span>' +
    '<span><i class="legend-swatch errors"></i>Errors</span>';
  heading.append(title, legend);
  const canvas = document.createElement("canvas");
  canvas.width = 960;
  canvas.height = 220;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "Logical web searches and errors over time");
  wrapper.append(heading, canvas);
  const aggregate = aggregateWebSearchSeries(series);
  requestAnimationFrame(() => {
    drawBarChart(
      canvas,
      aggregate.map((entry) => entry.bucket),
      [
        { values: aggregate.map((entry) => entry.requests) },
        { values: aggregate.map((entry) => entry.errors) },
      ],
    );
  });
  return wrapper;
}

function renderWebSearchAnalytics(
  container,
  stats,
  requests,
  period,
  partialErrors = [],
  stale = {},
) {
  container.innerHTML = "";
  const routeTotals = stats?.routes?.totals || stats?.route_totals || null;
  const attemptStats = stats?.attempts || stats || {};
  const totals = routeTotals || attemptStats.totals || {};
  const totalRequests = Number(totals.searches ?? totals.requests ?? 0);
  const totalErrors = Number(totals.errors || 0);
  const successRate =
    totalRequests > 0 ? ((totalRequests - totalErrors) / totalRequests) * 100 : 0;
  const resultsPerSearch =
    totalRequests > 0 ? Number(totals.results || 0) / totalRequests : 0;

  if (partialErrors.length) {
    const warning = document.createElement("div");
    warning.className = "analytics-warning";
    const staleParts = [];
    if (stale.stats) staleParts.push("summary");
    if (stale.requests) staleParts.push("recent requests");
    warning.textContent =
      `Some analytics could not be loaded: ${partialErrors.join("; ")}` +
      (staleParts.length
        ? `. Showing the last successful ${staleParts.join(" and ")} data.`
        : ".");
    container.appendChild(warning);
  }
  if (
    stats &&
    routeTotals &&
    totalRequests === 0 &&
    Number(attemptStats.totals?.requests || 0) > 0
  ) {
    const migrationNote = document.createElement("div");
    migrationNote.className = "analytics-warning";
    migrationNote.textContent =
      "Logical-route telemetry starts with FCC 4.12.0. Historical provider-attempt rows remain available below.";
    container.appendChild(migrationNote);
  }

  const metricValue = (value, formatter = formatAnalyticsNumber) =>
    stats ? formatter(value) : "Unavailable";
  container.appendChild(
    analyticsMetricCards([
      [
        "Logical searches",
        metricValue(totals.searches ?? totals.requests ?? 0),
      ],
      ["Route success rate", stats ? `${successRate.toFixed(1)}%` : "Unavailable"],
      [
        "Fallback rate",
        stats
          ? `${(Number(totals.fallback_rate || 0) * 100).toFixed(1)}%`
          : "Unavailable",
      ],
      [
        "Average attempts",
        stats ? formatAnalyticsNumber(totals.avg_attempts, 2) : "Unavailable",
      ],
      ["Failed searches", metricValue(totals.errors ?? 0)],
      [
        "End-to-end latency",
        !stats
          ? "Unavailable"
          : totals.avg_duration_ms == null
          ? "—"
          : `${formatAnalyticsNumber(totals.avg_duration_ms)} ms`,
      ],
      ["Results", metricValue(totals.results ?? 0)],
      [
        "Results / search",
        stats ? formatAnalyticsNumber(resultsPerSearch, 2) : "Unavailable",
      ],
      [
        "Known spend",
        stats ? formatAnalyticsCost(totals.cost_usd) : "Unavailable",
        "Best-effort provider-reported cost; unavailable costs are excluded",
      ],
      [
        "Dropped records",
        metricValue(stats?.dropped_records ?? 0),
        "Writer queue overflow",
      ],
    ]),
  );

  const routeSeries = stats?.routes?.series || stats?.route_series || stats?.series;
  if (stats && Array.isArray(routeSeries)) {
    container.appendChild(webSearchSeriesChart(routeSeries));
  }

  const terminalRows = asAnalyticsRows(
    stats?.routes?.by_terminal_provider,
    "provider",
  ).map((row) => {
    const searches = Number(row.searches ?? row.requests ?? 0);
    const errors = Number(row.errors || 0);
    return [
      row.provider || row.terminal_provider || "—",
      formatAnalyticsNumber(searches),
      searches ? `${(((searches - errors) / searches) * 100).toFixed(1)}%` : "0%",
      formatAnalyticsNumber(row.fallbacks ?? 0),
      row.avg_duration_ms != null
        ? `${formatAnalyticsNumber(row.avg_duration_ms)} ms`
        : "—",
      formatAnalyticsNumber(row.results ?? 0),
      formatAnalyticsCost(row.cost_usd),
    ];
  });
  container.appendChild(
    analyticsBlock(
      "Terminal route outcomes",
      analyticsTable(
        [
          "Terminal provider",
          "Searches",
          "Success rate",
          "Fallbacks",
          "End-to-end latency",
          "Results",
          "Cost",
        ],
        terminalRows,
        stats ? "No completed search routes yet." : "Route metrics unavailable.",
      ),
    ),
  );

  const providerRows = asAnalyticsRows(
    attemptStats.by_provider,
    "provider",
  ).map(
    (row) => {
      const requestsCount = Number(row.requests || 0);
      const errorsCount = Number(row.errors || 0);
      return [
        row.provider || "—",
        formatAnalyticsNumber(requestsCount),
        requestsCount ? `${((errorsCount / requestsCount) * 100).toFixed(1)}%` : "0%",
        row.avg_duration_ms != null
          ? `${formatAnalyticsNumber(row.avg_duration_ms)} ms`
          : "—",
        formatAnalyticsNumber(row.results ?? 0),
        formatAnalyticsCost(row.cost_usd),
      ];
    },
  );
  container.appendChild(
    analyticsBlock(
      "Provider attempt performance",
      analyticsTable(
        ["Provider", "Attempts", "Error rate", "Avg latency", "Results", "Cost"],
        providerRows,
        stats ? "No provider attempts recorded yet." : "Provider metrics unavailable.",
      ),
    ),
  );

  const keyRows = asAnalyticsRows(attemptStats.by_key, "key_label").map((row) => [
    row.provider || "—",
    row.key_label || row.key || "—",
    formatAnalyticsNumber(row.requests ?? 0),
    formatAnalyticsNumber(row.errors ?? 0),
    row.avg_duration_ms != null
      ? `${formatAnalyticsNumber(row.avg_duration_ms)} ms`
      : "—",
    formatAnalyticsNumber(row.results ?? 0),
  ]);
  container.appendChild(
    analyticsBlock(
      "Credential health",
      analyticsTable(
        ["Provider", "Key", "Requests", "Errors", "Avg latency", "Results"],
        keyRows,
        stats ? "No key usage recorded yet." : "Credential metrics unavailable.",
      ),
    ),
  );

  const routeErrorRows = asAnalyticsRows(
    stats?.routes?.top_errors,
    "error_kind",
  ).map((row) => [
    row.error_kind || "unknown",
    row.error_message || "No message",
    formatAnalyticsNumber(row.count ?? 0),
  ]);
  container.appendChild(
    analyticsBlock(
      "Top terminal route errors",
      analyticsTable(
        ["Kind", "Message", "Count"],
        routeErrorRows,
        stats ? "No terminal route errors in this range." : "Error metrics unavailable.",
      ),
    ),
  );

  const errorRows = asAnalyticsRows(attemptStats.top_errors, "error_kind").map(
    (row) => [
      row.error_kind || "unknown",
      row.error_message || "No message",
      formatAnalyticsNumber(row.count ?? 0),
    ],
  );
  container.appendChild(
    analyticsBlock(
      "Top provider-attempt errors",
      analyticsTable(
        ["Kind", "Message", "Count"],
        errorRows,
        stats
          ? "No provider-attempt errors in this range."
          : "Error metrics unavailable.",
      ),
    ),
  );

  const requestItems = requests
    ? requests.requests || requests.items || (Array.isArray(requests) ? requests : [])
    : [];
  const requestRows = requestItems.map((entry) => [
    formatRequestTime(entry),
    entry.route_id ? String(entry.route_id).slice(0, 8) : "—",
    entry.attempt_number ?? "—",
    entry.provider || "—",
    entry.key_label || "—",
    entry.query || "—",
    entry.results_count ?? 0,
    entry.duration_ms != null ? `${Math.round(entry.duration_ms)} ms` : "—",
    entry.status || "—",
    entry.error_kind || "—",
    formatAnalyticsCost(entry.cost_usd),
    webSearchDetailButton(entry),
  ]);
  container.appendChild(
    analyticsBlock(
      "Recent requests",
      analyticsTable(
        [
          "Time",
          "Route",
          "Attempt",
          "Provider",
          "Key",
          "Query",
          "Results",
          "Latency",
          "Status",
          "Error",
          "Cost",
          "Details",
        ],
        requestRows,
        requests ? "No recent provider attempts." : "Recent attempts unavailable.",
      ),
    ),
  );

  const periodLabel = {
    hourly: "hour",
    daily: "day",
    weekly: "ISO week",
    monthly: "month",
  }[period];
  const footer = document.createElement("p");
  footer.className = "analytics-footnote";
  footer.textContent =
    `Series bucket: ${periodLabel || period}; bucket boundaries use UTC. ` +
    "Route metrics count one user search; provider tables and recent rows count attempts. " +
    "Queries are stored locally and truncated to 256 characters. " +
    (stats?.capture_content
      ? `Full normalized I/O is captured up to ${formatAnalyticsNumber(
          stats.max_content_chars,
        )} characters per payload.`
      : "Search I/O capture is disabled; only lengths and SHA-256 hashes are retained.");
  container.appendChild(footer);
}

function webSearchDetailButton(entry) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary-button req-detail-button";
  button.textContent = "View";
  button.setAttribute(
    "aria-label",
    `View web search attempt ${entry.id || entry.attempt_number || ""}`.trim(),
  );
  button.addEventListener("click", () =>
    openWebSearchDetail(entry.id).catch((error) =>
      showMessage(`Could not load web search detail: ${error.message}`, "error"),
    ),
  );
  return button;
}

function prettyJson(value) {
  return value == null ? "" : JSON.stringify(value, null, 2);
}

function capturedPayloadText(row, field) {
  const payload = row[field];
  if (payload != null) return prettyJson(payload);
  const chars = row[`${field}_chars`];
  const hash = row[`${field}_sha256`];
  if (chars == null && !hash) return "(not available for this historical record)";
  return [
    "(content not captured)",
    chars != null ? `Characters: ${chars}` : "",
    hash ? `SHA-256: ${hash}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

function appendWebSearchDetailMeta(meta, fields) {
  meta.innerHTML = "";
  fields.forEach(([label, value]) => {
    if (value == null || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    meta.append(dt, dd);
  });
}

function renderWebSearchResponseSummary(output) {
  const container = byId("webSearchDetailSummary");
  container.innerHTML = "";
  if (!output || output._truncated) {
    container.textContent = output?._truncated
      ? "The stored output is truncated; inspect the preview and SHA-256 below."
      : "No captured provider response is available.";
    return;
  }
  if (output.error) {
    const error = document.createElement("div");
    error.className = "analytics-warning";
    error.textContent = `${output.error.kind || "error"}: ${
      output.error.message || output.error.type || "Provider attempt failed"
    }`;
    container.appendChild(error);
    return;
  }
  if (output.answer) {
    const answer = document.createElement("div");
    answer.className = "websearch-result-answer";
    const title = document.createElement("strong");
    title.textContent = "Provider answer / rich summary";
    const text = document.createElement("p");
    text.textContent = output.answer;
    answer.append(title, text);
    container.appendChild(answer);
  }
  const results = Array.isArray(output.results) ? output.results : [];
  results.forEach((result, index) => {
    const item = document.createElement("article");
    item.className = "websearch-result-item";
    const title = document.createElement("strong");
    title.textContent = `${index + 1}. ${result.title || "Untitled result"}`;
    item.appendChild(title);
    if (result.url && /^https?:\/\//i.test(result.url)) {
      const link = document.createElement("a");
      link.href = result.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = result.url;
      item.appendChild(link);
    } else if (result.url) {
      const url = document.createElement("small");
      url.textContent = result.url;
      item.appendChild(url);
    }
    if (result.published) {
      const published = document.createElement("small");
      published.textContent = `Published: ${result.published}`;
      item.appendChild(published);
    }
    if (result.snippet) {
      const snippet = document.createElement("p");
      snippet.textContent = result.snippet;
      item.appendChild(snippet);
    }
    if (result.content && result.content !== result.snippet) {
      const content = document.createElement("p");
      content.textContent = result.content;
      item.appendChild(content);
    }
    container.appendChild(item);
  });
  if (!output.answer && results.length === 0) {
    container.textContent = "The provider returned no results or answer.";
  }
}

async function openWebSearchDetail(requestId) {
  state.webSearchDetailReturnFocus = document.activeElement;
  const row = await api(`/admin/api/websearch/requests/${requestId}`);
  byId("webSearchDetailTitle").textContent =
    `Web search ${String(row.route_id || "route").slice(0, 8)} · attempt ${
      row.attempt_number
    }`;
  appendWebSearchDetailMeta(byId("webSearchDetailMeta"), [
    ["Time", formatRequestTime(row)],
    ["Route ID", row.route_id],
    ["Attempt", row.attempt_number],
    ["Provider", row.provider],
    ["Credential", row.key_label || "keyless"],
    ["Status", row.status],
    ["Results", row.results_count],
    ["Latency", row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—"],
    ["Cost", formatAnalyticsCost(row.cost_usd)],
    ["Error", row.error_kind ? `${row.error_kind}: ${row.error_message || ""}` : ""],
    ["Input characters", row.input_chars],
    ["Output characters", row.output_chars],
    ["Input SHA-256", row.input_sha256],
    ["Output SHA-256", row.output_sha256],
  ]);
  byId("webSearchDetailConfig").textContent =
    prettyJson(row.provider_config) || "(configuration unavailable)";
  byId("webSearchDetailInput").textContent = capturedPayloadText(row, "input");
  byId("webSearchDetailOutput").textContent = capturedPayloadText(row, "output");
  renderWebSearchResponseSummary(row.output);
  byId("webSearchDetailModal").hidden = false;
  byId("webSearchDetailClose").focus();
}

function closeWebSearchDetail() {
  byId("webSearchDetailModal").hidden = true;
  if (state.webSearchDetailReturnFocus instanceof HTMLElement) {
    state.webSearchDetailReturnFocus.focus();
  }
  state.webSearchDetailReturnFocus = null;
}

function trapWebSearchDetailFocus(event) {
  const modal = byId("webSearchDetailModal");
  if (event.key !== "Tab" || modal.hidden) return;
  const focusable = Array.from(
    modal.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element instanceof HTMLElement && !element.hidden);
  if (focusable.length === 0) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function webSearchAnalyticsParams({ includePeriod = false, limit = null } = {}) {
  const params = new URLSearchParams();
  const provider = byId("webSearchFilterProvider")?.value || "";
  const status = byId("webSearchFilterStatus")?.value || "";
  const query = byId("webSearchFilterQuery")?.value.trim() || "";
  const windowSeconds = byId("webSearchFilterWindow")?.value || "";
  if (includePeriod) {
    params.set("period", state.webSearchStatsPeriod);
  }
  if (provider) params.set("provider", provider);
  if (status) params.set("status", status);
  if (query) params.set("q", query);
  if (windowSeconds) {
    params.set(
      "since",
      new Date(Date.now() - Number(windowSeconds) * 1000).toISOString(),
    );
  }
  if (limit != null) params.set("limit", String(limit));
  return params;
}

async function loadWebSearchAnalytics() {
  const loadId = ++state.webSearchAnalyticsLoadId;
  const period = byId("webSearchStatsPeriod")?.value || state.webSearchStatsPeriod;
  state.webSearchStatsPeriod = period;
  const container = byId("webSearchAnalytics");
  container.textContent = "Loading analytics…";
  const statsParams = webSearchAnalyticsParams({ includePeriod: true });
  const statsKey = statsParams.toString();
  const requestParams = webSearchAnalyticsParams({ limit: 50 });
  const requestKey = requestParams.toString();
  const [statsResult, requestsResult] = await Promise.allSettled([
    api(`/admin/api/websearch/stats?${statsParams}`),
    api(`/admin/api/websearch/requests?${requestParams}`),
  ]);
  if (loadId !== state.webSearchAnalyticsLoadId) return;

  let stats = null;
  let requests = null;
  const partialErrors = [];
  const stale = { stats: false, requests: false };
  if (statsResult.status === "fulfilled") {
    stats = statsResult.value;
    state.webSearchAnalyticsStats = stats;
    state.webSearchAnalyticsStatsKey = statsKey;
    state.webSearchLastRoute =
      stats?.last_route || stats?.routes?.last_route || null;
    renderWebSearchObservedRoute(state.webSearchLastRoute);
  } else {
    partialErrors.push(
      `summary: ${statsResult.reason?.message || String(statsResult.reason)}`,
    );
    stats =
      state.webSearchAnalyticsStatsKey === statsKey
        ? state.webSearchAnalyticsStats
        : null;
    stale.stats = Boolean(stats);
    if (!stats) {
      state.webSearchLastRoute = null;
      renderWebSearchObservedRoute(null);
    }
  }
  if (requestsResult.status === "fulfilled") {
    requests = requestsResult.value;
    state.webSearchAnalyticsPage = requests;
    state.webSearchAnalyticsPageKey = requestKey;
  } else {
    partialErrors.push(
      `requests: ${requestsResult.reason?.message || String(requestsResult.reason)}`,
    );
    requests =
      state.webSearchAnalyticsPageKey === requestKey
        ? state.webSearchAnalyticsPage
        : null;
    stale.requests = Boolean(requests);
  }
  renderWebSearchAnalytics(container, stats, requests, period, partialErrors, stale);
  byId("webSearchLastUpdated").textContent =
    `${partialErrors.length ? "Refresh incomplete" : "Updated"} ${new Date().toLocaleTimeString()}`;
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

/* --------------------------------------------------------------------- */
/* Version / self-update                                                 */
/* --------------------------------------------------------------------- */

function versionDismissKey(version) {
  return `fcc-version-dismissed-${version}`;
}

function formatCheckedAt(epochSeconds) {
  if (epochSeconds == null) return "Never checked";
  return new Date(epochSeconds * 1000).toLocaleString();
}

async function loadVersionInfo() {
  try {
    state.versionInfo = await api("/admin/api/version");
  } catch (error) {
    state.versionInfo = { error: error.message };
  }
  renderVersionIndicator();
  renderVersionBanners();
  renderVersionPanel();
}

function renderVersionIndicator() {
  const indicator = byId("versionIndicator");
  if (!indicator) return;
  const info = state.versionInfo;
  indicator.innerHTML = "";
  if (!info) return;
  const label = document.createElement("span");
  label.textContent = info.current ? `v${info.current}` : "version unknown";
  indicator.appendChild(label);
  if (info.update_available) {
    const dot = document.createElement("span");
    dot.className = "version-update-dot";
    dot.title = info.latest ? `Update available: v${info.latest}` : "Update available";
    indicator.appendChild(dot);
  }
}

function renderVersionBanners() {
  const container = byId("versionBanners");
  if (!container) return;
  container.innerHTML = "";
  const info = state.versionInfo;
  if (!info || info.error) return;

  // A deferred install (Windows) reports its outcome only after the server
  // that staged it has exited, so surface it on the next start.
  if (info.pending_upgrade && info.pending_upgrade.ok === false) {
    const banner = document.createElement("div");
    banner.className = "version-banner restart-required";
    const body = document.createElement("div");
    body.className = "version-banner-body";
    const title = document.createElement("div");
    title.className = "version-banner-title";
    title.textContent = "The staged update did not install";
    const detail = document.createElement("div");
    detail.className = "version-banner-detail";
    detail.textContent = `${
      info.pending_upgrade.message || "The update helper reported a failure."
    } Your current version is still intact — re-run the install command to update.`;
    body.append(title, detail);
    banner.appendChild(body);
    container.appendChild(banner);
    return;
  }

  if (info.restart_required) {
    const banner = document.createElement("div");
    banner.className = "version-banner restart-required";
    const body = document.createElement("div");
    body.className = "version-banner-body";
    const title = document.createElement("div");
    title.className = "version-banner-title";
    title.textContent = info.staged_install
      ? "Update staged — stop the server to finish installing"
      : "Update installed — restart the server to apply";
    const detail = document.createElement("div");
    detail.className = "version-banner-detail";
    detail.textContent = info.staged_install
      ? "Windows cannot replace the environment while the server is running. Stop fcc-server; the update installs automatically, then start it again."
      : info.installed_version
        ? `Installed v${info.installed_version}.`
        : "The new version is installed.";
    body.append(title, detail);
    banner.appendChild(body);
    container.appendChild(banner);
    return;
  }

  if (!info.update_available || !info.latest) return;
  if (localStorage.getItem(versionDismissKey(info.latest)) === "1") return;

  const banner = document.createElement("div");
  banner.className = "version-banner";
  const body = document.createElement("div");
  body.className = "version-banner-body";
  const title = document.createElement("div");
  title.className = "version-banner-title";
  title.textContent = `Update available: v${info.latest}`;
  const detail = document.createElement("div");
  detail.className = "version-banner-detail";
  if (info.release_url) {
    const link = document.createElement("a");
    link.href = info.release_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = info.release_name || `v${info.latest}`;
    detail.appendChild(link);
  } else {
    detail.textContent = info.release_name || `v${info.latest}`;
  }
  body.append(title, detail);

  // Without the notes the banner only says a number changed, which tells you
  // nothing about whether the update matters to you.
  if (info.release_notes) {
    const notes = document.createElement("details");
    notes.className = "version-banner-notes";
    const summary = document.createElement("summary");
    summary.textContent = "What changed";
    const text = document.createElement("pre");
    text.textContent = info.release_notes;
    notes.append(summary, text);
    body.appendChild(notes);
  }

  const actions = document.createElement("div");
  actions.className = "version-banner-actions";
  const updateButton = document.createElement("button");
  updateButton.type = "button";
  updateButton.className = "primary-button";
  updateButton.textContent = "Update now";
  updateButton.addEventListener("click", () => runVersionUpgrade(updateButton));
  const dismissButton = document.createElement("button");
  dismissButton.type = "button";
  dismissButton.className = "ghost-button";
  dismissButton.textContent = "Dismiss";
  dismissButton.addEventListener("click", () => {
    localStorage.setItem(versionDismissKey(info.latest), "1");
    renderVersionBanners();
  });
  actions.append(updateButton, dismissButton);

  banner.append(body, actions);
  container.appendChild(banner);
}

function renderVersionPanel() {
  const details = byId("versionDetails");
  const checkButton = byId("versionCheckButton");
  const updateButton = byId("versionUpdateButton");
  if (!details || !checkButton || !updateButton) return;
  const info = state.versionInfo;

  details.innerHTML = "";
  const entries = [
    ["Current", info?.current ? `v${info.current}` : "—"],
    ["Latest", info?.latest ? `v${info.latest}` : "—"],
    ["Last checked", formatCheckedAt(info?.checked_at)],
  ];
  entries.forEach(([label, value]) => {
    const dl = document.createElement("dl");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
    details.appendChild(dl);
  });
  if (info?.error) {
    const note = document.createElement("p");
    note.className = "version-error field-description";
    note.textContent = `Could not check for updates: ${info.error}`;
    details.appendChild(note);
  }

  if (!state.versionUpgrading) {
    updateButton.disabled = !info?.update_available;
    updateButton.textContent = "Update now";
  }
}

async function checkForUpdates(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Checking...";
  try {
    state.versionInfo = await api("/admin/api/version/check", {
      method: "POST",
      body: "{}",
    });
    renderVersionIndicator();
    renderVersionBanners();
    renderVersionPanel();
    showMessage(
      state.versionInfo.update_available
        ? `Update available: v${state.versionInfo.latest}`
        : "Already up to date",
      "ok",
    );
  } catch (error) {
    showMessage(`Could not check for updates: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function runVersionUpgrade(button) {
  if (state.versionUpgrading) return;
  state.versionUpgrading = true;
  const logEl = byId("versionUpgradeLog");
  const updateButton = byId("versionUpdateButton");
  [button, updateButton].forEach((candidate) => {
    if (candidate) {
      candidate.disabled = true;
      candidate.textContent = "Updating... (this can take a few minutes)";
    }
  });
  if (logEl) {
    logEl.hidden = true;
    logEl.textContent = "";
  }
  try {
    const result = await api("/admin/api/version/upgrade", {
      method: "POST",
      body: "{}",
    });
    if (logEl && Array.isArray(result.log) && result.log.length) {
      logEl.textContent = result.log.join("\n");
      logEl.hidden = false;
    }
    if (result.ok) {
      showMessage(result.message || "Update installed", "ok");
    } else {
      showMessage(result.message || "Update failed", "error");
    }
    await loadVersionInfo();
  } catch (error) {
    showMessage(`Update failed: ${error.message}`, "error");
  } finally {
    state.versionUpgrading = false;
    if (button) button.textContent = "Update now";
    renderVersionPanel();
  }
}

byId("versionCheckButton").addEventListener("click", (event) =>
  checkForUpdates(event.currentTarget),
);
byId("versionUpdateButton").addEventListener("click", (event) =>
  runVersionUpgrade(event.currentTarget),
);

function downloadJson(filename, value) {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function exportWebSearchAnalytics() {
  const params = webSearchAnalyticsParams({ limit: 500 });
  params.set("include_content", "true");
  const page = await api(`/admin/api/websearch/requests?${params}`);
  downloadJson(
    `fcc-websearch-${new Date().toISOString().replace(/[:.]/g, "-")}.json`,
    {
      exported_at: new Date().toISOString(),
      filters: Object.fromEntries(params),
      ...page,
    },
  );
}

async function clearWebSearchAnalytics() {
  const total = Number(state.webSearchAnalyticsPage?.total || 0);
  if (
    !window.confirm(
      `Delete the entire web-search log${total ? ` (${total} matching rows shown)` : ""}?`,
    )
  ) {
    return;
  }
  await api("/admin/api/websearch/requests", { method: "DELETE" });
  await loadWebSearchAnalytics();
}

byId("validateButton").addEventListener("click", () => validate(true));
byId("applyButton").addEventListener("click", apply);
byId("webSearchStatsApply").addEventListener("click", () =>
  loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error")),
);
byId("webSearchStatsRefresh").addEventListener("click", () =>
  loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error")),
);
byId("webSearchFilterQuery").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error"));
});
byId("webSearchExportButton").addEventListener("click", () =>
  exportWebSearchAnalytics().catch((error) => showMessage(error.message, "error")),
);
byId("webSearchClearButton").addEventListener("click", () =>
  clearWebSearchAnalytics().catch((error) => showMessage(error.message, "error")),
);
byId("webSearchDetailClose").addEventListener("click", closeWebSearchDetail);
byId("webSearchDetailModal").addEventListener("click", (event) => {
  if (event.target === byId("webSearchDetailModal")) closeWebSearchDetail();
});
document.addEventListener("keydown", (event) => {
  trapWebSearchDetailFocus(event);
  if (event.key === "Escape" && !byId("webSearchDetailModal").hidden) {
    closeWebSearchDetail();
  }
});
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
  loadId: 0,
  autoRefreshTimer: null,
  detailReturnFocus: null,
  providerOptions: new Set(),
  modelOptions: new Set(),
  keyOptions: new Set(),
};

function reqFilters() {
  const params = new URLSearchParams();
  const provider = byId("reqFilterProvider").value.trim();
  const model = byId("reqFilterModel").value.trim();
  const key = byId("reqFilterKey").value.trim();
  const status = byId("reqFilterStatus").value;
  const search = byId("reqFilterSearch").value.trim();
  const endpoint = byId("reqFilterEndpoint").value.trim();
  const windowSeconds = byId("reqFilterWindow").value;
  if (provider) params.set("provider", provider);
  if (model) params.set("model", model);
  if (key) params.set("key", key);
  if (status) params.set("status", status);
  if (search) params.set("q", search);
  if (endpoint) params.set("endpoint", endpoint);
  if (windowSeconds) {
    params.set("since", (Date.now() / 1000 - Number(windowSeconds)).toFixed(0));
  }
  return params;
}

async function loadRequestsView() {
  const loadId = ++reqState.loadId;
  const params = reqFilters();
  let stats;
  let list;
  try {
    [stats, list] = await Promise.all([
      api(`/admin/api/requests/stats?${params}`),
      api(
        `/admin/api/requests?limit=${reqState.limit}&offset=${reqState.offset}&${params}`,
      ),
    ]);
  } catch (error) {
    if (loadId !== reqState.loadId) return;
    throw error;
  }
  if (loadId !== reqState.loadId) return;
  if (stats.enabled === false) {
    byId("reqStatsCards").innerHTML = "";
    byId("reqTableBody").innerHTML = "";
    byId("reqProviderBreakdown").innerHTML = "";
    byId("reqKeyBreakdown").innerHTML = "";
    byId("reqTopErrors").innerHTML = "";
    clearChart(byId("reqSeriesChart"));
    clearChart(byId("reqModelChart"));
    reqState.total = 0;
    byId("reqBodiesIndicator").textContent = "Request log disabled (REQUEST_LOG_ENABLED=false)";
    renderReqPager();
    byId("reqLastUpdated").textContent = "Logging disabled";
    return;
  }
  byId("reqBodiesIndicator").textContent = stats.capture_bodies
    ? "Bodies: captured"
    : "Bodies: hashes only (REQUEST_LOG_CAPTURE_BODIES=false)";
  renderRequestStatsCards(stats);
  renderReqSeriesChart(stats.series || []);
  renderReqModelChart(stats.by_model || []);
  populateRequestFilterOptions(stats);
  renderRequestProviderBreakdown(stats.by_provider || []);
  renderRequestKeyBreakdown(stats.by_key || []);
  renderRequestTopErrors(stats.top_errors || []);
  reqState.total = list.total || 0;
  renderRequestsTable(list.rows || []);
  renderReqPager();
  byId("reqLastUpdated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

function populateRequestFilterOptions(stats) {
  const populate = (id, rows, known) => {
    rows.forEach((row) => known.add(row.key));
    const datalist = byId(id);
    datalist.replaceChildren(
      ...Array.from(known)
        .sort((left, right) => left.localeCompare(right))
        .map((value) => {
          const option = document.createElement("option");
          option.value = value;
          return option;
        }),
    );
  };
  populate("reqProviderOptions", stats.by_provider || [], reqState.providerOptions);
  populate("reqModelOptions", stats.by_model || [], reqState.modelOptions);
  populate("reqKeyOptions", stats.by_key || [], reqState.keyOptions);
}

function renderRequestStatsCards(stats) {
  const successRate = stats.total
    ? ((Number(stats.success || 0) / Number(stats.total)) * 100).toFixed(1)
    : "0.0";
  const cards = [
    ["Total requests", stats.total],
    ["Success rate", `${successRate}%`],
    ["Error rate", `${((stats.error_rate || 0) * 100).toFixed(1)}%`],
    ["Cancelled", stats.cancelled],
    ["Input (uncached)", formatAnalyticsNumber(stats.tokens_in || 0)],
    ["Cached input", formatAnalyticsNumber(stats.cache_read_tokens || 0)],
    ["Cache hit rate", formatCacheHitRate(stats)],
    ["Cache writes", formatAnalyticsNumber(stats.cache_write_tokens || 0)],
    ["Tokens out", formatAnalyticsNumber(stats.tokens_out || 0)],
    ["Avg duration", stats.avg_duration_ms != null ? `${stats.avg_duration_ms} ms` : "—"],
    ["p50 duration", stats.p50_duration_ms != null ? `${stats.p50_duration_ms} ms` : "—"],
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

function renderRequestProviderBreakdown(rows) {
  const container = byId("reqProviderBreakdown");
  container.innerHTML = "";
  container.appendChild(
    analyticsTable(
      [
        "Provider",
        "Requests",
        "Error rate",
        "Input (uncached)",
        "Cached input",
        "Cache hit",
        "Tokens out",
        "Avg latency",
      ],
      rows.map((row) => {
        const requests = Number(row.requests || 0);
        const errors = Number(row.errors || 0);
        return [
          row.key || "unknown",
          formatAnalyticsNumber(requests),
          requests ? `${((errors / requests) * 100).toFixed(1)}%` : "0%",
          formatAnalyticsNumber(Number(row.tokens_in || 0)),
          formatAnalyticsNumber(Number(row.cache_read_tokens || 0)),
          formatCacheHitRate(row),
          formatAnalyticsNumber(Number(row.tokens_out || 0)),
          row.avg_duration_ms != null ? `${row.avg_duration_ms} ms` : "—",
        ];
      }),
      "No provider activity in this range.",
    ),
  );
}

function renderRequestKeyBreakdown(rows) {
  const container = byId("reqKeyBreakdown");
  container.innerHTML = "";
  container.appendChild(
    analyticsTable(
      [
        "Key",
        "Requests",
        "Error rate",
        "Input (uncached)",
        "Cached input",
        "Cache hit",
        "Tokens out",
        "Avg latency",
      ],
      rows.map((row) => {
        const requests = Number(row.requests || 0);
        const errors = Number(row.errors || 0);
        return [
          row.key || "unknown",
          formatAnalyticsNumber(requests),
          requests ? `${((errors / requests) * 100).toFixed(1)}%` : "0%",
          formatAnalyticsNumber(Number(row.tokens_in || 0)),
          formatAnalyticsNumber(Number(row.cache_read_tokens || 0)),
          formatCacheHitRate(row),
          formatAnalyticsNumber(Number(row.tokens_out || 0)),
          row.avg_duration_ms != null ? `${row.avg_duration_ms} ms` : "—",
        ];
      }),
      "No per-key data yet.",
    ),
  );
}

function renderRequestTopErrors(rows) {
  const container = byId("reqTopErrors");
  container.innerHTML = "";
  container.appendChild(
    analyticsTable(
      ["Message", "Count"],
      rows.map((row) => [
        row.message || "Unknown error",
        formatAnalyticsNumber(row.count || 0),
      ]),
      "No errors in this range.",
    ),
  );
}

function renderRequestsTable(rows) {
  const body = byId("reqTableBody");
  body.innerHTML = "";
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 10;
    td.className = "analytics-empty";
    td.textContent = "No requests match the current filters.";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = `req-row req-status-${row.status}`;
    const cells = [
      formatRequestTime(row),
      row.endpoint || "",
      row.provider || "",
      row.key_label || "",
      row.resolved_model || row.requested_model || "",
      row.status,
      `${row.tokens_in ?? "—"}/${row.tokens_out ?? "—"}`,
      row.ttft_ms != null ? `${Math.round(row.ttft_ms)} ms` : "—",
      row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—",
    ];
    cells.forEach((text) => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    });
    const actionCell = document.createElement("td");
    const detailButton = document.createElement("button");
    detailButton.type = "button";
    detailButton.className = "secondary-button req-detail-button";
    detailButton.textContent = "View";
    detailButton.setAttribute("aria-label", `View request ${row.id}`);
    detailButton.addEventListener("click", () => openRequestDetail(row.id));
    actionCell.appendChild(detailButton);
    tr.appendChild(actionCell);
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
  reqState.detailReturnFocus = document.activeElement;
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
  byId("reqDetailClose").focus();
}

function clearChart(canvas) {
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
}

function closeRequestDetail() {
  byId("reqDetailModal").hidden = true;
  if (reqState.detailReturnFocus instanceof HTMLElement) {
    reqState.detailReturnFocus.focus();
  }
  reqState.detailReturnFocus = null;
}

function trapRequestDetailFocus(event) {
  const modal = byId("reqDetailModal");
  if (event.key !== "Tab" || modal.hidden) return;
  const focusable = Array.from(
    modal.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element instanceof HTMLElement && !element.hidden);
  if (focusable.length === 0) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

async function exportRequestAnalytics() {
  const params = reqFilters();
  const exportParams = new URLSearchParams(params);
  exportParams.set("limit", "500");
  exportParams.set("offset", "0");
  const [stats, page] = await Promise.all([
    api(`/admin/api/requests/stats?${params}`),
    api(`/admin/api/requests?${exportParams}`),
  ]);
  downloadJson(
    `fcc-requests-${new Date().toISOString().replace(/[:.]/g, "-")}.json`,
    {
      exported_at: new Date().toISOString(),
      filters: Object.fromEntries(params),
      stats,
      ...page,
    },
  );
}

function updateRequestAutoRefresh() {
  if (reqState.autoRefreshTimer != null) {
    window.clearInterval(reqState.autoRefreshTimer);
    reqState.autoRefreshTimer = null;
  }
  if (!byId("reqAutoRefresh").checked) return;
  reqState.autoRefreshTimer = window.setInterval(() => {
    if (state.activeView !== "requests") return;
    loadRequestsView().catch((error) => showMessage(error.message, "error"));
  }, 15000);
}

byId("reqDetailClose").addEventListener("click", closeRequestDetail);
byId("reqDetailModal").addEventListener("click", (event) => {
  if (event.target === byId("reqDetailModal")) closeRequestDetail();
});
document.addEventListener("keydown", (event) => {
  trapRequestDetailFocus(event);
  if (event.key === "Escape" && !byId("reqDetailModal").hidden) {
    closeRequestDetail();
  }
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
byId("reqPageSize").addEventListener("change", () => {
  reqState.limit = Number(byId("reqPageSize").value);
  reqState.offset = 0;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
});
byId("reqRefreshButton").addEventListener("click", () =>
  loadRequestsView().catch((error) => showMessage(error.message, "error")),
);
byId("reqAutoRefresh").addEventListener("change", updateRequestAutoRefresh);
byId("reqExportButton").addEventListener("click", () =>
  exportRequestAnalytics().catch((error) => showMessage(error.message, "error")),
);
byId("reqClearButton").addEventListener("click", () => {
  if (
    !window.confirm(
      `Delete the entire request log? The current filters match ${reqState.total} rows; all stored rows will be deleted.`,
    )
  ) {
    return;
  }
  api("/admin/api/requests", { method: "DELETE" })
    .then(() => {
      reqState.offset = 0;
      return loadRequestsView();
    })
    .catch((error) => showMessage(error.message, "error"));
});
