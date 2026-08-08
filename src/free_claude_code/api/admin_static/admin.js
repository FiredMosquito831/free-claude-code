
// tokens_in is Anthropic's input_tokens: the uncached portion only. Providers
// translate their own accounting to that at the boundary, so total input is
// tokens_in plus whatever the cache served and whatever it wrote.
function uncachedInputTokens(row) {
  return Math.max(0, Number(row?.tokens_in || 0));
}

function totalInputTokens(row) {
  return (
    Number(row?.tokens_in || 0) +
    Number(row?.cache_read_tokens || 0) +
    Number(row?.cache_write_tokens || 0)
  );
}

function formatCacheHitRate(row) {
  const total = totalInputTokens(row);
  const cached = Number(row?.cache_read_tokens || 0);
  if (!total) return "—";
  // Not every upstream reports prompt caching. Showing 0.0% for those reads as
  // "caching is broken" rather than "this provider never told us", so an em
  // dash is reserved for the case where nothing reported a figure at all.
  if (row?.cache_reported === 0) return "—";
  return `${((cached / total) * 100).toFixed(1)}%`;
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
  claudeSettings: null,
  claudeSettingsBusy: false,
  onboarding: null,
  onboardingExpandedStepId: null,
  // Whether the expanded step was opened by a click rather than chosen
  // for the user. Auto-advance is a convenience; it must never overrule
  // someone who deliberately opened a step to re-read it.
  onboardingExpandedByUser: false,
  userNavigated: false,
};

const MASKED_SECRET = "********";
const VIEW_GROUPS = [
  {
    // Static content: no settings sections, nothing to fetch, so it stays
    // readable even when the server cannot reach a provider or the network.
    id: "get_started",
    label: "Get Started",
    title: "Get Started",
    sections: [],
    containerId: null,
  },
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
  {
    // Static content: no settings sections, nothing to fetch, so it stays
    // readable even when the server cannot reach a provider or the network.
    id: "guide",
    label: "Guide",
    title: "Guide",
    sections: [],
    containerId: null,
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
  await loadOnboarding().catch((error) => showMessage(error.message, "error"));
  if (
    state.onboarding &&
    !state.onboarding.dismissed &&
    !state.onboarding.complete &&
    !state.userNavigated
  ) {
    state.activeView = "get_started";
  }
  const config = await api("/admin/api/config");
  state.config = config;
  state.fields = new Map(config.fields.map((field) => [field.key, field]));
  state.credentialEnvs = new Set(
    (config.provider_status || [])
      .map((provider) => provider.credential_env)
      .filter(Boolean),
  );
  renderNav();
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
  await loadClaudeSettings();
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
      state.userNavigated = true;
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

  if (activeView.id === "get_started") {
    loadOnboarding().catch((error) => showMessage(error.message, "error"));
  }

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

// Providers now render inline as searchable, grouped cards inside
// providersSections (see renderProviderGroups) instead of a separate flat
// status strip, so there is one place to read a provider's status rather
// than two. testProvider() / refreshLocalStatus() still update a card's
// pill and meta line in place after a test call, by provider id.
function updateProviderCard(providerId, status, label, metaText) {
  const card = document.querySelector(`.pv-card[data-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    const meta = card.querySelector(".provider-meta");
    if (meta) meta.textContent = metaText;
  }
}

/* ------------------------------------------------------------- get started */

async function loadOnboarding() {
  const data = await api("/admin/api/onboarding");
  state.onboarding = data;
  renderOnboarding();
  return data;
}

async function updateOnboarding(patch) {
  const data = await api("/admin/api/onboarding", {
    method: "POST",
    body: JSON.stringify(patch),
  });
  state.onboarding = data;
  renderOnboarding();
  return data;
}

// The first incomplete required step is "next" — the one worth walking
// through in full. Everything else collapses to a single line so the
// checklist reads as "here is your next action" instead of a wall of text.
function primaryOnboardingStepId(steps) {
  const nextRequired = steps.find((step) => !step.optional && !step.done);
  return nextRequired ? nextRequired.id : null;
}

// Sentinel for "the user explicitly collapsed the expanded step" — distinct
// from `null` ("nothing chosen yet, auto-select"). It never matches a real
// step id, so a step becoming done can't accidentally re-expand a checklist
// the user just closed.
const ONBOARDING_NOTHING_EXPANDED = "__onboarding_nothing_expanded__";

// A label between the two groups of steps, not a step itself -- listed as
// presentation so a screen reader announces it as a divider rather than an
// interactive list item with nothing to activate.
function onboardingGroupHeading(text) {
  const heading = document.createElement("li");
  heading.className = "get-started-group-heading";
  heading.setAttribute("role", "presentation");
  heading.textContent = text;
  return heading;
}

function renderOnboarding() {
  const onboarding = state.onboarding;
  const progress = byId("getStartedProgress");
  const list = byId("getStartedSteps");
  if (!progress || !list || !onboarding) return;

  // A number alone is easy to skim past; a filled bar reads as progress at a
  // glance and is the one place this view spends visual weight.
  progress.innerHTML = "";
  const progressLabel = document.createElement("span");
  progressLabel.className = "get-started-progress-label";
  progressLabel.textContent = `${onboarding.required_done} of ${onboarding.required_total} essential steps done`;
  progress.appendChild(progressLabel);

  const progressBar = document.createElement("div");
  progressBar.className = "get-started-progress-bar";
  progressBar.setAttribute("role", "progressbar");
  progressBar.setAttribute("aria-valuemin", "0");
  progressBar.setAttribute("aria-valuemax", String(onboarding.required_total));
  progressBar.setAttribute("aria-valuenow", String(onboarding.required_done));
  progressBar.setAttribute(
    "aria-label",
    `${onboarding.required_done} of ${onboarding.required_total} essential steps done`,
  );
  const progressFill = document.createElement("div");
  progressFill.className = "get-started-progress-fill";
  const pct =
    onboarding.required_total > 0
      ? (onboarding.required_done / onboarding.required_total) * 100
      : 0;
  progressFill.style.width = `${pct}%`;
  progressBar.appendChild(progressFill);
  progress.appendChild(progressBar);

  // Expanded/collapsed is view state, not persisted. `null` means nothing has
  // been chosen yet, so auto-select the next action. When a step the app chose
  // becomes done, advance to the new next action, or finishing a step would
  // leave it expanded while the real next one sits collapsed out of sight.
  //
  // Auto-advance applies only to steps the app picked. Opening a completed
  // step to re-read what you did is a legitimate thing to want, and advancing
  // out of it would make already-finished steps impossible to view at all.
  // A user who collapsed everything (ONBOARDING_NOTHING_EXPANDED) is likewise
  // left alone: that id never matches a step.
  if (state.onboardingExpandedStepId === null) {
    state.onboardingExpandedStepId = primaryOnboardingStepId(onboarding.steps);
    state.onboardingExpandedByUser = false;
  } else if (!state.onboardingExpandedByUser) {
    const expandedStep = onboarding.steps.find(
      (step) => step.id === state.onboardingExpandedStepId,
    );
    if (expandedStep && expandedStep.done) {
      state.onboardingExpandedStepId = primaryOnboardingStepId(onboarding.steps);
    }
  }

  // The 3 required steps are a real causal chain -- a client can't be pointed
  // anywhere until a model is set, which needs a provider first -- while the
  // rest are independent extras with no order between them. Rendering all 7
  // as one undifferentiated list buries that shape behind a per-card
  // "Optional" pill you have to read every time. Grouping is derived from
  // `step.optional`, which the step already carries, so nothing new is
  // stored and the boundary just falls out of the array's existing order.
  list.innerHTML = "";
  let optionalHeadingShown = false;
  onboarding.steps.forEach((step, index) => {
    if (index === 0 && !step.optional) {
      list.appendChild(onboardingGroupHeading("Essential"));
    }
    if (step.optional && !optionalHeadingShown) {
      list.appendChild(onboardingGroupHeading("Optional"));
      optionalHeadingShown = true;
    }

    const expanded = step.id === state.onboardingExpandedStepId;

    const item = document.createElement("li");
    item.className = `get-started-step${expanded ? " expanded" : " collapsed"}${step.done ? " done" : ""}`;

    const header = document.createElement("div");
    header.className = "get-started-step-header";
    header.setAttribute("role", "button");
    header.setAttribute("aria-expanded", expanded ? "true" : "false");
    header.tabIndex = 0;
    const toggle = () => {
      state.onboardingExpandedByUser = !expanded;
      state.onboardingExpandedStepId = expanded
        ? ONBOARDING_NOTHING_EXPANDED
        : step.id;
      renderOnboarding();
    };
    header.addEventListener("click", toggle);
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });

    const marker = document.createElement("span");
    const state_ = step.done ? "ok" : step.optional ? "neutral" : "warn";
    marker.className = `status-pill ${state_}`;
    marker.textContent = step.done ? "Done" : step.optional ? "Optional" : "To do";
    header.appendChild(marker);

    const label = document.createElement("strong");
    label.textContent = step.label;
    header.appendChild(label);

    // A collapsed step reads two ways: closed because it's done, or closed
    // because it hasn't been opened yet. The pill already says which, but a
    // scanning eye shouldn't have to read text to tell them apart -- a
    // finished step gets a check where an unopened one gets the chevron that
    // invites a click.
    const chevron = document.createElement("span");
    const doneAndCollapsed = step.done && !expanded;
    chevron.className = `get-started-step-chevron${doneAndCollapsed ? " is-done" : ""}`;
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = doneAndCollapsed ? "✓" : "›";
    header.appendChild(chevron);

    item.appendChild(header);

    if (expanded) {
      const body = document.createElement("div");
      body.className = "get-started-step-body";

      const description = document.createElement("p");
      description.textContent = step.description;
      body.appendChild(description);

      if (step.instructions && step.instructions.length) {
        const instructionList = document.createElement("ol");
        instructionList.className = "get-started-step-instructions";
        step.instructions.forEach((instruction) => {
          const instructionItem = document.createElement("li");
          instructionItem.textContent = instruction;
          instructionList.appendChild(instructionItem);
        });
        body.appendChild(instructionList);
      }

      const targetView = VIEW_GROUPS.find((view) => view.id === step.view);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary-button";
      button.textContent = targetView
        ? `Go to ${targetView.label}`
        : `Open ${step.view}`;
      button.addEventListener("click", () => {
        if (step.id === "guide") {
          updateOnboarding({ visited: ["guide"] }).catch((error) =>
            showMessage(error.message, "error"),
          );
        }
        state.userNavigated = true;
        setActiveView(step.view, { scroll: true });
        if (step.target) {
          highlightOnboardingTarget(step.target);
        }
      });
      body.appendChild(button);

      item.appendChild(body);
    }

    list.appendChild(item);
  });

  const dismissButton = byId("getStartedDismissButton");
  dismissButton.textContent = onboarding.dismissed
    ? "Checklist dismissed"
    : "Dismiss checklist";
  dismissButton.disabled = onboarding.dismissed;
}

// The target may be a field that only exists once its (previously hidden)
// view section is in the layout; a rAF lets setActiveView's DOM change settle
// before we measure it for scrollIntoView.
function highlightOnboardingTarget(selector) {
  requestAnimationFrame(() => {
    const target = document.querySelector(selector);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("onboarding-highlight");
    window.setTimeout(() => target.classList.remove("onboarding-highlight"), 2000);
  });
}

/* ------------------------------------------------------- model routing ---
   A tier's primary model and its fallbacks are one thing: the path a request
   takes. The generic field grid flowed them into separate, often
   non-adjacent, cells, so the ordering that governs every request was
   invisible. Each tier is rendered as one card instead, with its models on a
   vertical rail -- the rail's length is the depth of the safety net. */

const ROUTE_TIERS = [
  {
    id: "default",
    label: "Default",
    modelKey: "MODEL",
    chainKey: "MODEL_FALLBACKS",
    note: "Used by any tier without a route of its own.",
  },
  { id: "fable", label: "Fable", modelKey: "MODEL_FABLE", chainKey: "MODEL_FABLE_FALLBACKS" },
  { id: "opus", label: "Opus", modelKey: "MODEL_OPUS", chainKey: "MODEL_OPUS_FALLBACKS" },
  { id: "sonnet", label: "Sonnet", modelKey: "MODEL_SONNET", chainKey: "MODEL_SONNET_FALLBACKS" },
  { id: "haiku", label: "Haiku", modelKey: "MODEL_HAIKU", chainKey: "MODEL_HAIKU_FALLBACKS" },
];

function routeNode(marker, control, modifier) {
  const node = document.createElement("div");
  node.className = `route-node${modifier ? ` ${modifier}` : ""}`;
  const dot = document.createElement("span");
  dot.className = "route-marker";
  dot.setAttribute("aria-hidden", "true");
  dot.textContent = marker;
  node.append(dot, control);
  return node;
}

function renderRouteCard(tier, fieldByKey) {
  const modelField = fieldByKey.get(tier.modelKey);
  const chainField = fieldByKey.get(tier.chainKey);
  if (!modelField) return null;

  const card = document.createElement("article");
  card.className = "route-card";
  card.dataset.tier = tier.id;
  // The onboarding checklist deep-links to [data-key="MODEL"]; keep that
  // selector resolvable now the field lives inside a card.
  card.dataset.key = modelField.key;

  const head = document.createElement("header");
  head.className = "route-card-head";

  const name = document.createElement("h4");
  name.className = "route-tier";
  name.textContent = tier.label;

  head.appendChild(name);
  // The default route has no state to report: it is the thing the others
  // inherit, so calling it "custom" would be noise on every install.
  if (tier.id !== "default") {
    const inherits = !String(modelField.value || "").trim();
    const stateChip = document.createElement("span");
    stateChip.className = `route-state${inherits ? " is-inherited" : ""}`;
    stateChip.textContent = inherits ? "Inherits default" : "Custom route";
    head.appendChild(stateChip);
  }
  card.appendChild(head);

  if (tier.note) {
    const note = document.createElement("p");
    note.className = "route-note";
    note.textContent = tier.note;
    card.appendChild(note);
  }

  const rail = document.createElement("div");
  rail.className = "field route-rail";

  const { control: modelControl } = buildFieldControl(modelField);
  rail.appendChild(routeNode("", modelControl, "is-primary"));

  if (chainField) {
    const { control: chainControl } = buildFieldControl(chainField);
    rail.appendChild(chainControl);
  }

  card.appendChild(rail);
  return card;
}

function renderModelRouting(fields) {
  const fieldByKey = new Map(fields.map((field) => [field.key, field]));
  const wrap = document.createElement("div");
  wrap.className = "route-layout";

  const rule = document.createElement("p");
  rule.className = "route-rule";
  rule.textContent =
    "Each tier tries its models in order. If one cannot serve a request the " +
    "next takes over, up until the response starts streaming.";
  wrap.appendChild(rule);

  const grid = document.createElement("div");
  grid.className = "route-grid";
  ROUTE_TIERS.forEach((tier) => {
    const card = renderRouteCard(tier, fieldByKey);
    if (card) grid.appendChild(card);
  });
  wrap.appendChild(grid);

  // The vision adapter is not a tier. It fires on what a request contains
  // rather than on which model was asked for, so it gets its own shape
  // instead of masquerading as a sixth route.
  const visionField = fieldByKey.get("MODEL_VISION");
  if (visionField) {
    const vision = document.createElement("article");
    vision.className = "route-card route-vision";
    vision.dataset.key = visionField.key;

    const head = document.createElement("header");
    head.className = "route-card-head";
    const name = document.createElement("h4");
    name.className = "route-tier";
    name.textContent = "Vision adapter";
    head.appendChild(name);
    vision.appendChild(head);

    const note = document.createElement("p");
    note.className = "route-note";
    note.textContent =
      "Takes any request carrying an image when the model its tier picked " +
      "is known not to read images. Leave as None to send images wherever " +
      "the tier resolves to.";
    vision.appendChild(note);

    const { control } = buildFieldControl(visionField);
    const visionControl = document.createElement("div");
    visionControl.className = "field route-vision-control";
    visionControl.appendChild(control);
    vision.appendChild(visionControl);
    wrap.appendChild(vision);
  }

  // Anything the manifest adds to this section later still has to appear.
  const claimed = new Set([
    "MODEL_VISION",
    ...ROUTE_TIERS.flatMap((tier) => [tier.modelKey, tier.chainKey]),
  ]);
  const unclaimed = fields.filter((field) => !claimed.has(field.key));
  if (unclaimed.length) {
    const rest = document.createElement("div");
    rest.className = "field-grid";
    unclaimed.forEach((field) => rest.appendChild(renderField(field)));
    wrap.appendChild(rest);
  }

  return wrap;
}

function renderSections(sections, fields) {
  state.modelComboboxes.clear();
  VIEW_GROUPS.forEach((view) => {
    // Static views (the guide) have no settings container to clear.
    const container = view.containerId ? byId(view.containerId) : null;
    if (container) container.innerHTML = "";
  });

  const sectionById = new Map(sections.map((section) => [section.id, section]));
  const bySection = new Map();
  sections.forEach((section) => bySection.set(section.id, []));
  fields.forEach((field) => {
    if (!bySection.has(field.section)) bySection.set(field.section, []);
    bySection.get(field.section).push(field);
  });

  VIEW_GROUPS.forEach((view) => {
    const container = view.containerId ? byId(view.containerId) : null;
    if (!container) return;
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

      if (section.id === "models") {
        sectionEl.appendChild(renderModelRouting(gridFields));
      } else if (section.id === "providers") {
        // Catalog order in one flat grid stopped scaling once there were 30+
        // providers to scan; grouped, searchable cards replace it, and each
        // provider's own advanced fields (proxy, etc.) move into that
        // provider's card instead of floating in the same grid. See
        // renderProviderGroups().
        sectionEl.appendChild(renderProviderGroups(gridFields));
      } else {
        const grid = document.createElement("div");
        grid.className = "field-grid";
        gridFields.forEach((field) => {
          grid.appendChild(renderField(field));
        });
        sectionEl.appendChild(grid);
      }

      // The providers section handles "advanced" per-card (see
      // renderProviderGroups) rather than with this section-wide toggle.
      if (section.id !== "providers" && gridFields.some((field) => field.advanced)) {
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

function prefersReducedMotion() {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

// Configured providers surface first within a group: "what do I have
// working" is the more common question on this page than "what are all my
// options", so a scan down a group should answer it without reading status
// pills one by one.
function providerCardSortRank(provider) {
  if (provider.status === "configured") return 0;
  if (provider.status === "missing_key" || provider.status === "missing_url") return 1;
  if (provider.status === "disabled") return 3;
  return 2;
}

function providerCardSort(a, b) {
  const rank = providerCardSortRank(a) - providerCardSortRank(b);
  if (rank !== 0) return rank;
  return (a.display_name || a.provider_id).localeCompare(b.display_name || b.provider_id);
}

/** Group the providers-section fields by provider and render them as
 * searchable, collapsible card groups instead of one flat grid.
 *
 * Reads `field.provider` and `config.provider_groups` /
 * `provider_status[].group` defensively: those are rollout-in-progress
 * payload keys, and a custom provider may legitimately have no group at all.
 * Anything that cannot be attributed to a provider or a group still renders,
 * just in a fallback bucket, so nothing described in the manifest can ever
 * silently disappear.
 */
function renderProviderGroups(fields) {
  const wrap = document.createElement("div");
  wrap.className = "pv-groups";

  const statusById = new Map(
    (state.config?.provider_status || []).map((provider) => [
      provider.provider_id,
      provider,
    ]),
  );

  const fieldsByProvider = new Map();
  const unclaimed = [];
  fields.forEach((field) => {
    if (!field.provider) {
      unclaimed.push(field);
      return;
    }
    if (!fieldsByProvider.has(field.provider)) fieldsByProvider.set(field.provider, []);
    fieldsByProvider.get(field.provider).push(field);
  });

  const groups = state.config?.provider_groups?.length
    ? state.config.provider_groups
    : [{ id: "_all", label: "All providers", description: "" }];
  const fallbackGroupId = groups[0].id;

  const providerIdsByGroup = new Map(groups.map((group) => [group.id, []]));
  fieldsByProvider.forEach((_fields, providerId) => {
    const status = statusById.get(providerId);
    const groupId =
      status?.group && providerIdsByGroup.has(status.group)
        ? status.group
        : fallbackGroupId;
    providerIdsByGroup.get(groupId).push(providerId);
  });

  wrap.appendChild(renderProviderToolbar(wrap));

  let openedAGroup = false;
  groups.forEach((group) => {
    const providerIds = providerIdsByGroup.get(group.id) || [];
    if (providerIds.length === 0) return;
    const providers = providerIds
      .map(
        (id) =>
          statusById.get(id) || {
            provider_id: id,
            display_name: id,
            status: "unknown",
            label: "Unknown",
          },
      )
      .sort(providerCardSort);
    const configuredCount = providers.filter(
      (provider) => provider.status === "configured",
    ).length;

    const details = document.createElement("details");
    details.className = "pv-group";
    // The first group with something configured opens by default; if
    // nothing anywhere is configured yet, the very first group opens
    // instead, so the page never lands fully collapsed with no visible way
    // in.
    if (!openedAGroup && (configuredCount > 0 || group.id === groups[0].id)) {
      details.open = true;
      openedAGroup = true;
    }
    details.dataset.naturalOpen = details.open ? "true" : "false";

    const summary = document.createElement("summary");
    summary.className = "pv-group-summary";

    const titleRow = document.createElement("div");
    titleRow.className = "pv-group-title-row";
    const title = document.createElement("span");
    title.className = "pv-group-title";
    title.textContent = group.label;
    const count = document.createElement("span");
    count.className = "pv-group-count";
    count.textContent = `${configuredCount}/${providers.length} configured`;
    titleRow.append(title, count);
    summary.appendChild(titleRow);

    if (group.description) {
      const desc = document.createElement("p");
      desc.className = "pv-group-desc";
      desc.textContent = group.description;
      summary.appendChild(desc);
    }

    summary.appendChild(renderProviderDotRail(group, providers, wrap, details));
    details.appendChild(summary);

    const cardGrid = document.createElement("div");
    cardGrid.className = "pv-card-grid";
    providers.forEach((provider) => {
      cardGrid.appendChild(
        renderProviderCard(provider, fieldsByProvider.get(provider.provider_id) || []),
      );
    });
    details.appendChild(cardGrid);

    wrap.appendChild(details);
  });

  if (unclaimed.length) {
    const other = document.createElement("section");
    other.className = "pv-other";
    const heading = document.createElement("p");
    heading.className = "pv-other-heading";
    heading.textContent = "Other configuration";
    other.appendChild(heading);
    const grid = document.createElement("div");
    grid.className = "field-grid";
    unclaimed.forEach((field) => grid.appendChild(renderField(field)));
    other.appendChild(grid);
    wrap.appendChild(other);
  }

  return wrap;
}

// A miniature status map for the group: one dot per provider, colored by
// status, so "what do I have working in this group" reads before opening it.
// Each dot is also a jump link -- clicking it expands the group (if
// collapsed) and scrolls its card into view, which matters most exactly
// when the dot rail is useful: a big group the person has not opened yet.
function renderProviderDotRail(group, providers, wrap, details) {
  const rail = document.createElement("div");
  rail.className = "pv-dot-rail";
  rail.setAttribute("role", "list");
  rail.setAttribute("aria-label", `${group.label} provider status`);
  providers.forEach((provider) => {
    const dot = document.createElement("button");
    dot.type = "button";
    dot.className = `pv-dot ${statusClass(provider.status)}`;
    dot.setAttribute("role", "listitem");
    const dotLabel = `${provider.display_name || provider.provider_id} — ${
      provider.label || provider.status
    }`;
    dot.title = dotLabel;
    dot.setAttribute("aria-label", dotLabel);
    dot.addEventListener("click", (event) => {
      // The dot rail lives inside <summary>; without stopping the event a
      // click here would also toggle the details element itself, right
      // after this handler forces it open, and the group would close again.
      event.preventDefault();
      event.stopPropagation();
      details.open = true;
      const card = wrap.querySelector(`.pv-card[data-provider="${provider.provider_id}"]`);
      if (!card) return;
      card.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "center",
      });
      card.classList.add("pv-card-flash");
      setTimeout(() => card.classList.remove("pv-card-flash"), 900);
      card.querySelector("input, select, button")?.focus();
    });
    rail.appendChild(dot);
  });
  return rail;
}

function renderProviderToolbar(wrap) {
  const toolbar = document.createElement("div");
  toolbar.className = "pv-toolbar";

  const searchWrap = document.createElement("label");
  searchWrap.className = "pv-search";
  const searchLabel = document.createElement("span");
  searchLabel.className = "pv-search-label";
  searchLabel.textContent = "Search providers";
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Search by name, key, or URL…";
  search.autocomplete = "off";
  searchWrap.append(searchLabel, search);

  const configuredOnly = document.createElement("label");
  configuredOnly.className = "toggle-control pv-configured-toggle";
  const configuredCheckbox = document.createElement("input");
  configuredCheckbox.type = "checkbox";
  configuredOnly.append(configuredCheckbox, document.createTextNode("Configured only"));

  const apply = () =>
    applyProviderFilter(wrap, search.value.trim().toLowerCase(), configuredCheckbox.checked);
  search.addEventListener("input", apply);
  configuredCheckbox.addEventListener("change", apply);

  toolbar.append(searchWrap, configuredOnly);
  return toolbar;
}

// Filtering hides with the `hidden` attribute rather than removing anything:
// every input stays in the document (so changedValues() and Apply keep
// working for a provider that is momentarily filtered out) and `hidden`
// takes the element out of the tab order on its own, so a hidden card cannot
// trap keyboard focus.
function applyProviderFilter(wrap, query, configuredOnly) {
  const filtering = Boolean(query) || configuredOnly;
  wrap.querySelectorAll(".pv-group").forEach((group) => {
    let visible = 0;
    group.querySelectorAll(".pv-card").forEach((card) => {
      const matchesQuery = !query || (card.dataset.pvSearch || "").includes(query);
      const matchesConfigured = !configuredOnly || card.dataset.pvConfigured === "true";
      const show = matchesQuery && matchesConfigured;
      card.hidden = !show;
      if (show) visible += 1;
    });
    group.hidden = filtering && visible === 0;
    if (filtering) {
      if (visible > 0) group.open = true;
    } else {
      group.open = group.dataset.naturalOpen === "true";
    }
  });
}

function renderProviderCard(provider, fields) {
  const card = document.createElement("article");
  card.className = "pv-card";
  card.dataset.provider = provider.provider_id;
  card.dataset.pvConfigured = provider.status === "configured" ? "true" : "false";
  card.dataset.pvSearch = [
    provider.display_name,
    provider.provider_id,
    provider.credential_env,
    provider.base_url,
    ...fields.map((field) => field.label),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  const title = document.createElement("div");
  title.className = "provider-title";
  const name = document.createElement("strong");
  name.textContent = provider.display_name || provider.provider_id;
  title.appendChild(name);
  const pill = document.createElement("span");
  pill.className = `status-pill ${statusClass(provider.status)}`;
  pill.textContent = provider.label || provider.status;
  title.appendChild(pill);
  card.appendChild(title);

  const metaText =
    provider.kind === "local"
      ? provider.base_url || "No local URL configured"
      : provider.credential_env || "";
  if (metaText) {
    const meta = document.createElement("div");
    meta.className = "provider-meta";
    meta.textContent = metaText;
    card.appendChild(meta);
  }

  const primaryFields = fields.filter((field) => !field.advanced);
  const advancedFields = fields.filter((field) => field.advanced);

  const body = document.createElement("div");
  body.className = "pv-card-fields";
  primaryFields.forEach((field) => body.appendChild(renderField(field)));
  card.appendChild(body);

  if (advancedFields.length) {
    const details = document.createElement("details");
    details.className = "pv-advanced";
    const summary = document.createElement("summary");
    summary.textContent = "Advanced options";
    details.appendChild(summary);
    advancedFields.forEach((field) => details.appendChild(renderField(field)));
    card.appendChild(details);
  }

  if (provider.custom !== true) {
    const testButton = document.createElement("button");
    testButton.type = "button";
    testButton.className = "test-button";
    testButton.textContent = provider.kind === "local" ? "Test" : "Refresh models";
    testButton.addEventListener("click", () => testProvider(provider.provider_id, testButton));
    card.appendChild(testButton);
  }

  return card;
}

/** Build one field's live control, wired into the dirty/apply machinery.
 *
 * Shared by the generic field grid and the Model Routing view, so a control
 * behaves identically wherever it is placed and there is one place to change
 * when a new field type appears.
 */
function buildFieldControl(field) {
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
      : field.type === "model_chain"
        ? new ModelChainEditor(input, field).element
        : input;
  // A control that wraps its input must still place it in the document.
  // `changedValues()` collects fields by walking [data-key] over the page, so
  // a wrapper that keeps its input detached produces a field that looks
  // edited, never marks the form dirty, and is silently never saved. Enforced
  // here rather than trusted to each wrapper: it is one line, and the failure
  // is invisible until someone tries to save.
  if (!control.contains(input)) control.appendChild(input);
  return { input, control };
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

  const { control } = buildFieldControl(field);
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

  if (field.type === "model_chain") {
    // Wire value is the comma-joined chain string; the chain editor UI
    // (built in renderField) reads/writes this hidden input so the normal
    // dirty-state/apply machinery needs no special-casing for this type.
    const input = document.createElement("input");
    input.type = "hidden";
    input.value = field.value || "";
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

// Renders a "model_chain" field (e.g. MODEL_FALLBACKS) as an ordered list of
// rows, each reusing ModelCombobox for search/autocomplete. Keeps the field's
// hidden <input> (data-key/data-original/data-field-type) as the single
// source of truth for changedValues()/apply; rows themselves carry no
// data-key so they never get picked up as standalone settings.
class ModelChainEditor {
  constructor(input, field) {
    this.input = input;
    this.field = field;
    this.rows = [];
    this.rowSeq = 0;

    this.element = document.createElement("div");
    this.element.className = "model-chain-editor";

    this.rowsEl = document.createElement("div");
    this.rowsEl.className = "model-chain-rows";

    this.addButton = document.createElement("button");
    this.addButton.type = "button";
    this.addButton.className = "secondary-button model-chain-add";
    this.addButton.textContent = "Add fallback";
    this.addButton.setAttribute("aria-label", `Add fallback to ${field.label}`);
    this.addButton.addEventListener("click", () => this.addRow("", true));

    // The hidden input carries this field's value and its data-key, so it
    // has to be in the document: `changedValues()` finds fields by walking
    // [data-key] across the page, and an input left detached is invisible
    // to Apply no matter what gets written to it.
    this.element.append(this.input, this.rowsEl, this.addButton);

    // Seed rows from the current value without touching the hidden input or
    // dirty state - this is initial render, not a user edit.
    this.parseValue(input.value).forEach((value) => this.addRow(value, false));
  }

  parseValue(value) {
    return String(value || "")
      .split(",")
      .map((entry) => entry.trim())
      .filter(Boolean);
  }

  syncValue() {
    this.input.value = this.rows
      .map((row) => row.combobox.input.value.trim())
      .filter(Boolean)
      .join(",");
    updateDirtyState();
  }

  addRow(value, notify) {
    const row = {};
    const rowField = {
      type: "model",
      key: `${this.field.key}__chain_${this.rowSeq++}`,
      label: `${this.field.label} fallback`,
    };

    const rowInput = document.createElement("input");
    rowInput.type = "text";
    rowInput.autocomplete = "off";
    rowInput.value = value;
    // No data-key: this row is not an independent setting, only a fragment
    // of the parent hidden input's comma-joined value.

    const combobox = new ModelCombobox(rowInput, rowField);
    rowInput.addEventListener("input", () => this.syncValue());
    rowInput.addEventListener("change", () => this.syncValue());

    const numberEl = document.createElement("span");
    numberEl.className = "model-chain-index";
    numberEl.setAttribute("aria-hidden", "true");

    const upButton = document.createElement("button");
    upButton.type = "button";
    upButton.className = "ghost-button model-chain-move";
    upButton.textContent = "↑";
    upButton.addEventListener("click", () => this.move(row, -1));

    const downButton = document.createElement("button");
    downButton.type = "button";
    downButton.className = "ghost-button model-chain-move";
    downButton.textContent = "↓";
    downButton.addEventListener("click", () => this.move(row, 1));

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "ghost-button model-chain-remove";
    removeButton.textContent = "×";
    removeButton.addEventListener("click", () => this.removeRow(row));

    const wrapper = document.createElement("div");
    wrapper.className = "model-chain-row";
    wrapper.append(numberEl, combobox.element, upButton, downButton, removeButton);

    Object.assign(row, { wrapper, combobox, numberEl, upButton, downButton, removeButton });
    this.rows.push(row);
    this.rowsEl.appendChild(wrapper);
    this.renumber();
    if (notify) {
      wrapper.classList.add("route-fallback-enter");
      this.syncValue();
      rowInput.focus();
    }
  }

  removeRow(row) {
    const index = this.rows.indexOf(row);
    if (index === -1) return;
    this.rows.splice(index, 1);
    row.wrapper.remove();
    state.modelComboboxes.delete(row.combobox);
    this.renumber();
    this.syncValue();
  }

  move(row, offset) {
    const index = this.rows.indexOf(row);
    const target = index + offset;
    if (index === -1 || target < 0 || target >= this.rows.length) return;
    this.rows.splice(index, 1);
    this.rows.splice(target, 0, row);
    // Re-append in the new order; appendChild moves existing nodes rather
    // than duplicating them, so this is enough to reorder the DOM.
    this.rows.forEach((item) => this.rowsEl.appendChild(item.wrapper));
    this.renumber();
    this.syncValue();
  }

  renumber() {
    this.rows.forEach((row, index) => {
      row.numberEl.textContent = String(index + 1);
      row.upButton.disabled = index === 0;
      row.upButton.setAttribute("aria-label", `Move fallback ${index + 1} up`);
      row.downButton.disabled = index === this.rows.length - 1;
      row.downButton.setAttribute("aria-label", `Move fallback ${index + 1} down`);
      row.removeButton.setAttribute("aria-label", `Remove fallback ${index + 1}`);
      row.combobox.input.setAttribute(
        "aria-label",
        `${this.field.label} fallback ${index + 1}`,
      );
    });
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

// "What should happen" -- the configured route, in try-order -- is needed by
// both the hero headline and the observed-route line below it. Factored out
// so the two can't drift into two slightly different definitions of "the
// route" as the manifest evolves.
function webSearchConfiguredRoute(providers, activeSelection, effectiveProvider) {
  const fallbackPolicy =
    state.fields.get("WEB_SEARCH_FALLBACK_POLICY")?.value || "auto";
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
  return { fallbackPolicy, resolvedPolicy, routeIds };
}

function renderWebSearchRouteSummary(providers, activeSelection, effectiveProvider) {
  const summary = byId("webSearchRouteSummary");
  if (!summary) return;
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
  const { fallbackPolicy, resolvedPolicy, routeIds } = webSearchConfiguredRoute(
    providers,
    activeSelection,
    effectiveProvider,
  );
  const ready =
    effectiveProvider === "legacy" ||
    Boolean(effectiveDescriptor && effectiveDescriptor.configured);
  // The headline is the one thing this bar has to answer at a glance: which
  // provider is actually serving requests right now. Everything else --
  // selection mode, fallback policy, the full chain -- is supporting detail.
  const headline =
    routeIds[0] === "disabled" ? "Web search disabled" : providerLabel(routeIds[0]);

  summary.innerHTML = "";
  const head = document.createElement("div");
  head.className = "ws-hero-head";
  const eyebrow = document.createElement("span");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = "Active web search route";
  const note = document.createElement("span");
  note.className = `status-pill ${
    ready ? "ok" : effectiveProvider ? "warn" : "neutral"
  }`;
  note.textContent = ready
    ? "Ready"
    : effectiveProvider
      ? "Needs configuration"
      : "Search disabled";
  head.append(eyebrow, note);

  const headlineEl = document.createElement("strong");
  headlineEl.className = "ws-hero-provider";
  headlineEl.textContent = headline;

  const route = document.createElement("div");
  route.className = "route-summary-main";
  const path = document.createElement("span");
  path.className = "ws-hero-path";
  const pathLabel = document.createElement("span");
  pathLabel.className = "ws-hero-path-label";
  pathLabel.textContent = "Route: ";
  path.appendChild(pathLabel);
  // The headline already answers "which provider". This answers "and then
  // what": the primary hop carries the visual weight, each fallback hop
  // after it is quieter, so try-order is legible without reading the prose
  // sentence below -- the one thing a picker UI for a single value has no
  // equivalent of.
  if (routeIds[0] === "disabled") {
    path.appendChild(document.createTextNode("Disabled"));
  } else {
    routeIds.forEach((id, index) => {
      if (index > 0) {
        const arrow = document.createElement("span");
        arrow.className = "ws-hero-path-arrow";
        arrow.textContent = " → ";
        path.appendChild(arrow);
      }
      const hop = document.createElement("span");
      hop.className = index === 0 ? "ws-hero-path-primary" : "ws-hero-path-fallback";
      hop.textContent = providerLabel(id);
      path.appendChild(hop);
    });
  }
  const detail = document.createElement("span");
  detail.textContent =
    `Selection: ${selectionLabel} · Fallback: ${fallbackPolicy}` +
    (fallbackPolicy === "auto" ? ` (resolves to ${resolvedPolicy})` : "") +
    " · Configuration errors stop the route";
  route.append(path, detail);

  summary.append(head, headlineEl, route);
  renderWebSearchObservedRoute(state.webSearchLastRoute, routeIds);
}

// configuredRouteIds is optional: renderWebSearchRouteSummary already has it
// on hand and passes it through, but this is also called on its own after an
// analytics refresh (loadWebSearchAnalytics), where it recomputes the same
// route from current field state.
function renderWebSearchObservedRoute(lastRoute, configuredRouteIds = null) {
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

  // The configured route describes intent; this line describes what the
  // last request actually did. When the two disagree on which provider goes
  // first -- almost always because the config changed after that request
  // ran -- that gap is the operationally true thing worth flagging here,
  // not just a timestamped restatement of the same fact as the headline.
  let routeIds = configuredRouteIds;
  if (!routeIds) {
    const allProviders = webSearchProviders();
    const activeSelection = state.fields.get("WEB_SEARCH_PROVIDER")?.value || "auto";
    const effectiveProvider = effectiveWebSearchProvider(allProviders, activeSelection);
    routeIds = webSearchConfiguredRoute(
      allProviders,
      activeSelection,
      effectiveProvider,
    ).routeIds;
  }
  const observedPrimary = lastRoute.primary_provider || providers[0] || null;
  const configuredPrimary = routeIds[0] || null;
  const drifted = Boolean(
    observedPrimary && configuredPrimary && observedPrimary !== configuredPrimary,
  );
  observed.classList.toggle("route-summary-observed-drift", drifted);
  observed.textContent =
    `Last observed: ${path} · ${lastRoute.status || "unknown"} · ${duration}` +
    (drifted ? " — configuration has changed since" : "");
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

// Only the effective provider needs to compete for attention; the rest of
// the strip stays legible but visibly secondary. Shared with
// updateWebSearchCardsFromState() so a live selection change re-applies the
// same badge instead of drifting from the initial render.
function setWebSearchCardEffective(card, isEffective) {
  card.classList.toggle("effective-provider", isEffective);
  const labelWrap = card.querySelector(".provider-title-label");
  let badge = labelWrap?.querySelector(".ws-active-badge");
  if (isEffective && labelWrap && !badge) {
    badge = document.createElement("span");
    badge.className = "ws-active-badge";
    badge.textContent = "Active";
    labelWrap.prepend(badge);
  } else if (!isEffective && badge) {
    badge.remove();
  }
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
    card.className = "provider-card";
    card.dataset.websearchProvider = provider.id;

    const title = document.createElement("div");
    title.className = "provider-title";
    const labelWrap = document.createElement("div");
    labelWrap.className = "provider-title-label";
    const label = document.createElement("strong");
    label.textContent = provider.label;
    labelWrap.appendChild(label);
    title.appendChild(labelWrap);
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
    setWebSearchCardEffective(card, effectiveProvider === provider.id);
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
  applyWebSearchProviderFilter(byId("webSearchProviderSearch")?.value.trim().toLowerCase() || "");
  wireWebSearchProviderSearch();
}

// Filters by hiding (not detaching), same reasoning as the Providers tab's
// applyProviderFilter(): every field input has to stay in the document for
// changedValues()/Apply to see it, and `hidden` already removes an element
// from the tab order, so a hidden card cannot trap keyboard focus.
function applyWebSearchProviderFilter(query) {
  document.querySelectorAll("#webSearchGrid .provider-card").forEach((card) => {
    const haystack = (card.textContent || "").toLowerCase();
    card.hidden = Boolean(query) && !haystack.includes(query);
  });
}

function wireWebSearchProviderSearch() {
  const input = byId("webSearchProviderSearch");
  if (!input || input.dataset.wired === "true") return;
  input.dataset.wired = "true";
  input.addEventListener("input", () => {
    applyWebSearchProviderFilter(input.value.trim().toLowerCase());
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
    setWebSearchCardEffective(card, effectiveProvider === provider.id);
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

/* --------------------------------------------------------------------- */
/* Claude Code settings file                                               */
/* --------------------------------------------------------------------- */

const CLAUDE_SETTINGS_STATUS_CLASS = {
  unset: "",
  configured: "ok",
  mismatch: "warn",
  unreadable: "error",
};

function claudeSettingsPathInputValue() {
  return byId("claudeSettingsPath").value.trim();
}

async function loadClaudeSettings(path) {
  const input = byId("claudeSettingsPath");
  if (!input) return;
  const params = path ? `?path=${encodeURIComponent(path)}` : "";
  try {
    state.claudeSettings = await api(`/admin/api/claude-settings${params}`);
    if (!input.value) {
      input.value = state.claudeSettings.default_path;
    }
  } catch (error) {
    state.claudeSettings = { error: error.message };
  }
  renderClaudeSettings();
}

function renderClaudeSettingsTargets(info) {
  const targetsEl = byId("claudeSettingsTargets");
  if (!targetsEl) return;

  targetsEl.innerHTML = "";
  const targets = info?.targets || [];
  if (targets.length === 0) return;

  if (targets.length === 1) {
    const target = targets[0];
    const line = document.createElement("p");
    line.className = "claude-settings-targets-single";
    line.textContent = target.path;
    const badge = document.createElement("span");
    badge.className = `claude-settings-badge ${target.state === "configured" ? "ok" : ""}`.trim();
    badge.textContent = target.exists
      ? target.state === "configured"
        ? "configured"
        : "exists"
      : "not found";
    line.appendChild(badge);
    targetsEl.appendChild(line);
    return;
  }

  const list = document.createElement("ul");
  list.className = "claude-settings-targets-list";
  targets.forEach((target) => {
    const item = document.createElement("li");

    const select = document.createElement("button");
    select.type = "button";
    select.className = "ghost-button";
    select.textContent = target.is_default ? `${target.path} (default)` : target.path;
    select.addEventListener("click", () => {
      byId("claudeSettingsPath").value = target.path;
      loadClaudeSettings(target.path);
    });
    item.appendChild(select);

    const badge = document.createElement("span");
    badge.className = `claude-settings-badge ${target.state === "configured" ? "ok" : ""}`.trim();
    badge.textContent = target.exists
      ? target.state === "configured"
        ? "configured"
        : "exists"
      : "not found";
    item.appendChild(badge);

    list.appendChild(item);
  });
  targetsEl.appendChild(list);
}

function renderClaudeSettingsOverrides(status) {
  const overridesEl = byId("claudeSettingsOverrides");
  if (!overridesEl) return;

  overridesEl.innerHTML = "";
  const overrides = status?.overrides || [];
  overrides.forEach((override) => {
    const note = document.createElement("p");
    note.className = "claude-settings-override";
    const variables = override.variables.join(" and ");
    note.textContent =
      `${override.scope === "managed" ? "Enterprise managed settings" : "A higher-precedence settings file"} ` +
      `at ${override.path} set ${variables} and override this file.`;
    overridesEl.appendChild(note);
  });
}

function renderClaudeSettings() {
  const statusEl = byId("claudeSettingsStatus");
  const applyButton = byId("claudeSettingsApplyButton");
  const removeButton = byId("claudeSettingsRemoveButton");
  if (!statusEl || !applyButton || !removeButton) return;

  const info = state.claudeSettings;
  applyButton.disabled = state.claudeSettingsBusy;
  removeButton.disabled = state.claudeSettingsBusy;

  renderClaudeSettingsTargets(info);

  statusEl.innerHTML = "";
  if (!info) return;

  if (info.error && !info.status) {
    statusEl.className = "claude-settings-status error";
    statusEl.textContent = `Could not read Claude settings: ${info.error}`;
    renderClaudeSettingsOverrides(null);
    return;
  }

  const status = info.status;
  statusEl.className = `claude-settings-status ${CLAUDE_SETTINGS_STATUS_CLASS[status.state] || ""}`.trim();

  const summary = document.createElement("p");
  summary.className = "claude-settings-summary";
  if (status.state === "unset") {
    summary.textContent = "Not configured";
  } else if (status.state === "configured") {
    summary.textContent = "Configured — pointing at this proxy";
  } else if (status.state === "mismatch") {
    const tokenNote = status.auth_token_present
      ? status.auth_token_matches
        ? "the token matches"
        : "the token differs"
      : "no token is set";
    summary.textContent =
      `Points elsewhere — current base URL is ${status.current_base_url || "(none)"}` +
      `, and ${tokenNote}. Configure will overwrite this.`;
  } else if (status.state === "unreadable") {
    summary.textContent = `Cannot read this file: ${status.error || "unknown error"}. ` +
      "Configure will refuse to overwrite it until this is fixed.";
  }
  statusEl.appendChild(summary);

  renderClaudeSettingsOverrides(status);
}

async function applyClaudeSettings() {
  if (state.claudeSettingsBusy) return;
  state.claudeSettingsBusy = true;
  renderClaudeSettings();
  try {
    state.claudeSettings = await api("/admin/api/claude-settings/apply", {
      method: "POST",
      body: JSON.stringify({ path: claudeSettingsPathInputValue() || null }),
    });
    showMessage("Claude Code settings file configured", "ok");
  } catch (error) {
    showMessage(`Could not configure Claude settings: ${error.message}`, "error");
    await loadClaudeSettings(claudeSettingsPathInputValue());
  } finally {
    state.claudeSettingsBusy = false;
    renderClaudeSettings();
  }
}

async function unsetClaudeSettings() {
  if (state.claudeSettingsBusy) return;
  state.claudeSettingsBusy = true;
  renderClaudeSettings();
  try {
    state.claudeSettings = await api("/admin/api/claude-settings/unset", {
      method: "POST",
      body: JSON.stringify({ path: claudeSettingsPathInputValue() || null }),
    });
    showMessage("Claude Code settings file entries removed", "ok");
  } catch (error) {
    showMessage(`Could not remove Claude settings entries: ${error.message}`, "error");
    await loadClaudeSettings(claudeSettingsPathInputValue());
  } finally {
    state.claudeSettingsBusy = false;
    renderClaudeSettings();
  }
}

byId("claudeSettingsApplyButton").addEventListener("click", () => applyClaudeSettings());
byId("claudeSettingsRemoveButton").addEventListener("click", () => unsetClaudeSettings());
byId("claudeSettingsPath").addEventListener("change", (event) => {
  loadClaudeSettings(event.currentTarget.value.trim());
});

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

byId("getStartedDismissButton").addEventListener("click", () => {
  updateOnboarding({ dismissed: true }).catch((error) =>
    showMessage(error.message, "error"),
  );
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
  // Baseline the pulse poll compares against. Established by the first pulse
  // rather than by a full load: the list query is paged, so its newest visible
  // row is not MAX(ts_epoch) once you are past page 1, and seeding from it
  // would make every later tick look "changed" and reload the whole view.
  lastPulseTotal: null,
  lastPulseTs: null,
  // The filters the baseline above was measured under. A different window or
  // provider has different counts, so comparing across a filter change would
  // report "changed" for something that only moved because the question did.
  lastPulseFilters: null,
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
    byId("reqFallbackRoutes").innerHTML = "";
    clearChart(byId("reqSeriesChart"));
    clearChart(byId("reqModelChart"));
    reqState.total = 0;
    byId("reqBreakdownTruncatedNote").hidden = true;
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
  renderRequestFallbackRoutes(stats.fallback_routes || []);
  renderReqBreakdownTruncatedNote(stats);
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

/** Each breakdown (provider/model/key) is capped server-side; surface it when hit. */
function renderReqBreakdownTruncatedNote(stats) {
  const note = byId("reqBreakdownTruncatedNote");
  const truncated = [];
  if (stats.by_provider_truncated) truncated.push("providers");
  if (stats.by_model_truncated) truncated.push("models");
  if (stats.by_key_truncated) truncated.push("keys");
  if (truncated.length === 0) {
    note.hidden = true;
    note.textContent = "";
    return;
  }
  note.hidden = false;
  note.textContent =
    `Showing the top 50 ${truncated.join(", ")} by request volume; ` +
    "narrow the filters to see the rest.";
}

/** "412 (18.4%)" — the count and its share of the window, in one cell. */
function formatTurnShare(count, total) {
  const value = Number(count || 0);
  const denominator = Number(total || 0);
  if (!denominator) return "—";
  return `${formatAnalyticsNumber(value)} (${((value / denominator) * 100).toFixed(1)}%)`;
}

/** "12 (3.1%)", or an em dash when no row in the window carries route data.
 *
 * Rows written before fallback chains existed have no `route_attempt` at all,
 * and 0% would read as "failover never fires" for traffic we know nothing
 * about. The dash says "not reported" instead, the same distinction the cache
 * columns already make.
 */
function formatFallbackShare(stats) {
  const reported = Number(stats.route_reported || 0);
  if (!reported) return "—";
  const served = Number(stats.served_by_fallback || 0);
  return `${formatAnalyticsNumber(served)} (${((served / reported) * 100).toFixed(1)}%)`;
}

/** Which primary failed, and what covered for it. */
/** Plain wording for the detail panel: which link in the chain answered. */
function formatRouteAttempt(row) {
  const attempt = row.route_attempt;
  if (attempt == null) return null;
  if (Number(attempt) === 0) return "Primary model";
  return row.route_primary_model
    ? `Fallback ${attempt}, after ${row.route_primary_model}`
    : `Fallback ${attempt}`;
}

function renderRequestFallbackRoutes(rows) {
  const container = byId("reqFallbackRoutes");
  if (!container) return;
  container.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "analytics-empty";
    empty.textContent = "No request fell back to another model in this window.";
    container.appendChild(empty);
    return;
  }
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "fallback-route";

    const path = document.createElement("div");
    path.className = "fallback-route-path";
    const from = document.createElement("code");
    from.textContent = row.primary;
    const arrow = document.createElement("span");
    arrow.className = "fallback-route-arrow";
    arrow.setAttribute("aria-label", "fell back to");
    arrow.textContent = "→";
    const to = document.createElement("code");
    to.className = "fallback-route-served";
    to.textContent = row.served_by;
    path.append(from, arrow, to);

    const count = document.createElement("span");
    count.className = "fallback-route-count";
    count.textContent = formatAnalyticsNumber(row.count);

    item.append(path, count);
    container.appendChild(item);
  });
}

function renderRequestStatsCards(stats) {
  const successRate = stats.total
    ? ((Number(stats.success || 0) / Number(stats.total)) * 100).toFixed(1)
    : "0.0";
  const cards = [
    ["Total requests", stats.total],
    ["Success rate", `${successRate}%`],
    ["Error rate", `${((stats.error_rate || 0) * 100).toFixed(1)}%`],
    ["Served by fallback", formatFallbackShare(stats)],
    ["Cancelled", stats.cancelled],
    ["Total input", formatAnalyticsNumber(totalInputTokens(stats))],
    ["Input (uncached)", formatAnalyticsNumber(uncachedInputTokens(stats))],
    ["Cached input", formatAnalyticsNumber(stats.cache_read_tokens || 0)],
    ["Cache hit rate", formatCacheHitRate(stats)],
    ["Cache writes", formatAnalyticsNumber(stats.cache_write_tokens || 0)],
    ["Tokens out", formatAnalyticsNumber(stats.tokens_out || 0)],
    ["Tool calls", formatAnalyticsNumber(stats.tool_calls || 0)],
    ["Turns using tools", formatTurnShare(stats.turns_with_tools, stats.total)],
    ["Turns with reasoning", formatTurnShare(stats.turns_with_reasoning, stats.total)],
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
          formatAnalyticsNumber(uncachedInputTokens(row)),
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
          formatAnalyticsNumber(uncachedInputTokens(row)),
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

/** The model that answered, flagged when it was not the one the route picked.
 *
 * A fallback that quietly works still changes what answered the request, so a
 * row has to say so -- otherwise a chain looks identical to a healthy primary
 * and nobody learns their first choice is failing.
 */
function buildModelCell(row) {
  const td = document.createElement("td");
  const name = document.createElement("span");
  name.textContent = row.resolved_model || row.requested_model || "";
  td.appendChild(name);
  if (Number(row.route_attempt || 0) > 0) {
    const badge = document.createElement("span");
    badge.className = "fallback-badge";
    badge.textContent = `fallback ${row.route_attempt}`;
    badge.title = row.route_primary_model
      ? `Fell back from ${row.route_primary_model}`
      : "Served by a fallback model";
    td.appendChild(badge);
  }
  return td;
}

function renderRequestsTable(rows) {
  const body = byId("reqTableBody");
  body.innerHTML = "";
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 11;
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
    ];
    cells.forEach((text) => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    });
    tr.appendChild(buildModelCell(row));
    const statusCell = document.createElement("td");
    statusCell.textContent = row.status;
    tr.appendChild(statusCell);
    tr.appendChild(buildTurnShapeCell(row));
    [
      `${row.tokens_in ?? "—"}/${row.tokens_out ?? "—"}`,
      row.ttft_ms != null ? `${Math.round(row.ttft_ms)} ms` : "—",
      row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—",
    ].forEach((text) => {
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

/**
 * Show what the assistant turn actually contained. A row with tools and no
 * reply is the normal shape under Claude Code, and it used to look identical
 * to a row that returned nothing at all.
 */
function buildTurnShapeCell(row) {
  const td = document.createElement("td");
  const wrap = document.createElement("div");
  wrap.className = "turn-chips";
  const chips = [];
  if (row.thinking_chars) chips.push(["thinking", "thinking"]);
  if (row.tool_call_count) {
    chips.push(["tools", row.tool_call_count === 1 ? "1 tool" : `${row.tool_call_count} tools`]);
  }
  if (row.output_chars) chips.push(["response", "reply"]);
  if (chips.length === 0) {
    td.className = "turn-chips-empty";
    td.textContent = "—";
    return td;
  }
  chips.forEach(([kind, label]) => {
    const chip = document.createElement("span");
    chip.className = "turn-chip";
    chip.dataset.kind = kind;
    chip.textContent = label;
    wrap.appendChild(chip);
  });
  td.appendChild(wrap);
  return td;
}

function renderReqPager() {
  const start = reqState.total === 0 ? 0 : reqState.offset + 1;
  const end = Math.min(reqState.offset + reqState.limit, reqState.total);
  byId("reqPageInfo").textContent = `${start}–${end} of ${reqState.total}`;
  byId("reqPrevPage").disabled = reqState.offset === 0;
  byId("reqNextPage").disabled = end >= reqState.total;
}

/** Read a design token so the charts stay on the same palette as the UI. */
function token(name, fallback) {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return value || fallback;
}

/**
 * Size a canvas to its rendered box at the display's pixel density.
 *
 * The markup pins width/height attributes, so on any HiDPI screen the bitmap
 * was being stretched and every label came out soft.
 */
function prepareCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  // clientWidth/Height are the content box, so the border is not counted twice.
  const width = canvas.clientWidth || canvas.width;
  const height = canvas.clientHeight || canvas.height;
  if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
    canvas.width = width * ratio;
    canvas.height = height * ratio;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function compactNumber(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value));
}

function drawBarChart(canvas, labels, series) {
  const { ctx, width, height } = prepareCanvas(canvas);
  const padX = 40;
  const padY = 22;
  const max = Math.max(1, ...series.flatMap((s) => s.values));
  const groups = labels.length || 1;
  const groupWidth = (width - padX - 12) / groups;
  const plotHeight = height - padY * 2;
  const colors = [token("--accent", "#10b981"), token("--error", "#ef4444")];
  const muted = token("--muted", "#9ca3af");
  const line = token("--line", "rgba(255,255,255,0.06)");

  // A value scale: the bars were previously unreadable in absolute terms.
  ctx.font = "10px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  [0, 0.5, 1].forEach((fraction) => {
    const y = height - padY - plotHeight * fraction;
    ctx.strokeStyle = line;
    ctx.beginPath();
    ctx.moveTo(padX, y + 0.5);
    ctx.lineTo(width - 8, y + 0.5);
    ctx.stroke();
    ctx.fillStyle = muted;
    ctx.textAlign = "right";
    ctx.fillText(compactNumber(max * fraction), padX - 6, y);
  });

  series.forEach((s, seriesIndex) => {
    ctx.fillStyle = colors[seriesIndex % colors.length];
    s.values.forEach((value, i) => {
      const barWidth = groupWidth / (series.length + 1);
      const x = padX + i * groupWidth + seriesIndex * barWidth;
      const barHeight = (plotHeight * value) / max;
      ctx.fillRect(x, height - padY - barHeight, Math.max(1, barWidth * 0.8), barHeight);
    });
  });

  ctx.fillStyle = muted;
  ctx.textAlign = "left";
  labels.forEach((label, i) => {
    if (labels.length > 12 && i % Math.ceil(labels.length / 12) !== 0) return;
    ctx.fillText(label, padX + i * groupWidth, height - padY / 2);
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
  const { ctx, width, height } = prepareCanvas(canvas);
  if (top.length === 0) return;
  // Total input, not just the uncached slice, or a warm model reads as idle.
  const modelTokens = (m) => totalInputTokens(m) + Number(m.tokens_out || 0);
  const max = Math.max(1, ...top.map(modelTokens));
  const labelWidth = 150;
  const valueWidth = 52;
  const rowHeight = Math.min(20, height / top.length);
  const accent = token("--accent", "#10b981");
  const muted = token("--muted", "#9ca3af");
  ctx.font = "10px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  top.forEach((model, i) => {
    const tokens = modelTokens(model);
    const y = i * rowHeight;
    const mid = y + rowHeight / 2;
    const barWidth = ((width - labelWidth - valueWidth) * tokens) / max;
    ctx.fillStyle = accent;
    ctx.fillRect(labelWidth, y + 2, Math.max(1, barWidth), rowHeight - 5);
    ctx.fillStyle = muted;
    ctx.textAlign = "right";
    ctx.fillText(model.key.slice(0, 26), labelWidth - 8, mid);
    // The bar shows proportion; the number is what people actually quote.
    ctx.textAlign = "left";
    ctx.fillText(compactNumber(tokens), labelWidth + barWidth + 6, mid);
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
    ["Route attempt", formatRouteAttempt(row)],
    ["Status", row.status],
    ["Error", row.error_kind ? `${row.error_kind}: ${row.error_message || ""}` : ""],
    ["Key", row.key_label],
    ["Total input", formatAnalyticsNumber(totalInputTokens(row))],
    ["Input (uncached)", formatOptionalNumber(row.tokens_in)],
    ["Cached input", formatOptionalNumber(row.cache_read_tokens)],
    ["Cache writes", formatOptionalNumber(row.cache_write_tokens)],
    ["Cache hit", formatRowCacheHit(row)],
    ["Tokens out", formatOptionalNumber(row.tokens_out)],
    ["Output rate", formatOutputRate(row)],
    ["TTFT", row.ttft_ms != null ? `${Math.round(row.ttft_ms)} ms` : "—"],
    ["Duration", row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—"],
    ["Turn", formatTurnSummary(row)],
    ["Reasoning policy", row.reasoning],
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
  renderTurnTranscript(row);
  byId("reqDetailModal").hidden = false;
  byId("reqDetailClose").focus();
}

function formatChars(count) {
  if (!count) return "";
  return `${count.toLocaleString()} chars`;
}

function formatOptionalNumber(value) {
  return value == null ? "—" : Number(value).toLocaleString();
}

/** Share of this request's input that the provider served from its cache. */
function formatRowCacheHit(row) {
  if (row.cache_read_tokens == null) return "not reported";
  const total = totalInputTokens(row);
  if (!total) return "—";
  return `${((Number(row.cache_read_tokens) / total) * 100).toFixed(1)}%`;
}

/** Output tokens per second, excluding the wait before the first one. */
function formatOutputRate(row) {
  const tokens = Number(row.tokens_out || 0);
  const duration = Number(row.duration_ms || 0);
  const ttft = Number(row.ttft_ms || 0);
  const generating = duration - ttft;
  if (!tokens || generating <= 0) return "—";
  return `${(tokens / (generating / 1000)).toFixed(1)} tok/s`;
}

function formatTurnSummary(row) {
  const parts = [];
  if (row.thinking_chars) parts.push(`${row.thinking_chars.toLocaleString()} chars reasoning`);
  if (row.tool_call_count) {
    parts.push(row.tool_call_count === 1 ? "1 tool call" : `${row.tool_call_count} tool calls`);
  }
  if (row.output_chars) parts.push(`${row.output_chars.toLocaleString()} chars reply`);
  return parts.join(" · ");
}

/**
 * Fill the prompt / reasoning / tool calls / response panes.
 *
 * Emptiness is not one condition. A pane can be empty because the turn had
 * nothing of that kind, or because body capture is off — those need different
 * words, and the character counts are recorded either way, so we can tell.
 */
function renderTurnTranscript(row) {
  const setBody = (bodyId, metaId, text, chars, emptyText) => {
    const body = byId(bodyId);
    const captured = typeof text === "string" && text !== "";
    body.textContent = captured
      ? text
      : chars
        ? `${chars.toLocaleString()} characters were recorded but not stored. Set REQUEST_LOG_CAPTURE_BODIES=true to keep the text.`
        : emptyText;
    body.classList.toggle("turn-empty-body", !captured);
    byId(metaId).textContent = formatChars(chars);
  };

  setBody(
    "reqDetailInput",
    "reqDetailInputMeta",
    row.input_text,
    row.input_chars,
    "No prompt text recorded.",
  );
  setBody(
    "reqDetailOutput",
    "reqDetailOutputMeta",
    row.output_text,
    row.output_chars,
    row.tool_call_count
      ? "This turn called tools without writing a reply."
      : "No reply text in this turn.",
  );

  const thinkingPane = byId("reqDetailThinkingPane");
  thinkingPane.hidden = !row.thinking_chars;
  if (row.thinking_chars) {
    thinkingPane.open = false;
    setBody(
      "reqDetailThinking",
      "reqDetailThinkingMeta",
      row.thinking_text,
      row.thinking_chars,
      "No reasoning recorded.",
    );
  }

  renderToolCalls(row);
}

function renderToolCalls(row) {
  const pane = byId("reqDetailToolsPane");
  const list = byId("reqDetailTools");
  list.replaceChildren();
  const count = row.tool_call_count || 0;
  pane.hidden = count === 0;
  if (count === 0) return;
  byId("reqDetailToolsMeta").textContent = count === 1 ? "1 call" : `${count} calls`;

  const calls = Array.isArray(row.tool_calls) ? row.tool_calls : null;
  if (!calls) {
    const note = document.createElement("p");
    note.className = "turn-empty";
    note.textContent =
      "Arguments were not stored. Set REQUEST_LOG_CAPTURE_BODIES=true to keep them.";
    list.append(note);
    return;
  }

  calls.forEach((call) => {
    const item = document.createElement("li");
    item.className = "tool-call";

    const head = document.createElement("div");
    head.className = "tool-call-head";
    const ordinal = document.createElement("span");
    ordinal.className = "tool-call-ordinal";
    const name = document.createElement("code");
    name.className = "tool-call-name";
    name.textContent = call.name || "(unnamed tool)";
    head.append(ordinal, name);

    const args = document.createElement("pre");
    args.className = "tool-call-args";
    if (typeof call.input_partial === "string") {
      // The stream ended mid-arguments, so this is a fragment, not JSON.
      args.classList.add("tool-call-partial");
      args.textContent = `${call.input_partial}\n\n— arguments incomplete, the stream ended early —`;
    } else {
      args.textContent = JSON.stringify(call.input ?? {}, null, 2);
    }

    item.append(head, args);
    list.append(item);
  });
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
      // `summary` is tabbable without carrying a tabindex attribute, so it has
      // to be named explicitly or the reasoning pane becomes unreachable.
      'button:not([disabled]), summary, [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter(
    (element) =>
      element instanceof HTMLElement &&
      !element.hidden &&
      // Panes are hidden when the turn had no reasoning or no tool calls.
      element.closest("[hidden]") === null,
  );
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

function requestAutoRefreshEnabled() {
  return byId("reqAutoRefresh").checked;
}

/**
 * Poll the cheap heartbeat endpoint instead of the full stats+list view.
 * Only when the row count or latest timestamp actually moved does this fall
 * through to `loadRequestsView()`, so an idle dashboard stops running the
 * aggregate queries (percentiles, breakdowns, series) on every tick.
 */
async function pollRequestPulse() {
  if (!requestAutoRefreshEnabled()) return;
  if (state.activeView !== "requests") return;
  // A hidden tab must not poll at all, not just skip the expensive call.
  if (document.visibilityState === "hidden") return;
  const params = reqFilters();
  let pulse;
  try {
    pulse = await api(`/admin/api/requests/pulse?${params}`);
  } catch (error) {
    showMessage(error.message, "error");
    return;
  }
  if (pulse.enabled === false) return;
  const signature = params.toString();
  const first =
    reqState.lastPulseTotal === null || signature !== reqState.lastPulseFilters;
  reqState.lastPulseFilters = signature;
  const changed =
    pulse.total !== reqState.lastPulseTotal || pulse.last_ts !== reqState.lastPulseTs;
  reqState.lastPulseTotal = pulse.total;
  reqState.lastPulseTs = pulse.last_ts;
  // The first tick only establishes the baseline; the view was just loaded.
  if (first || !changed) return;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
}

function updateRequestAutoRefresh() {
  if (reqState.autoRefreshTimer != null) {
    window.clearInterval(reqState.autoRefreshTimer);
    reqState.autoRefreshTimer = null;
  }
  if (!requestAutoRefreshEnabled()) return;
  const intervalMs = Number(byId("reqAutoRefreshInterval").value) || 15000;
  reqState.autoRefreshTimer = window.setInterval(() => {
    pollRequestPulse();
  }, intervalMs);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && requestAutoRefreshEnabled()) {
    // Catch up immediately instead of waiting out the rest of the interval.
    pollRequestPulse();
  }
});

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
byId("reqAutoRefresh").addEventListener("change", () => {
  updateRequestAutoRefresh();
  if (requestAutoRefreshEnabled()) pollRequestPulse();
});
byId("reqAutoRefreshInterval").addEventListener("change", updateRequestAutoRefresh);
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

/* ------------------------------------------------------------------ guide ---
   Screenshots in the guide are dashboard captures, so at column width the UI
   inside them is unreadable. They open at full size instead. */

let guideLightboxReturnFocus = null;

function openGuideLightbox(image) {
  const lightbox = byId("guideLightbox");
  const full = byId("guideLightboxImage");
  guideLightboxReturnFocus = document.activeElement;
  full.src = image.src;
  full.alt = image.alt || "";
  lightbox.hidden = false;
  byId("guideLightboxClose").focus();
}

function closeGuideLightbox() {
  const lightbox = byId("guideLightbox");
  if (lightbox.hidden) return;
  lightbox.hidden = true;
  byId("guideLightboxImage").src = "";
  if (guideLightboxReturnFocus instanceof HTMLElement) {
    guideLightboxReturnFocus.focus();
  }
  guideLightboxReturnFocus = null;
}

function setupGuideScreenshots() {
  document.querySelectorAll(".guide-shot").forEach((image) => {
    image.tabIndex = 0;
    image.setAttribute("role", "button");
    image.setAttribute(
      "aria-label",
      `${image.alt || "Screenshot"} — open at full size`,
    );
    image.addEventListener("click", () => openGuideLightbox(image));
    image.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openGuideLightbox(image);
    });
    // The alt text already describes the shot; reuse it as a caption so the
    // click affordance is stated rather than implied.
    if (image.alt && !image.nextElementSibling?.classList.contains("guide-shot-caption")) {
      const caption = document.createElement("p");
      caption.className = "guide-shot-caption";
      caption.textContent = `${image.alt} — click to enlarge`;
      image.insertAdjacentElement("afterend", caption);
    }
  });
  byId("guideLightbox").addEventListener("click", closeGuideLightbox);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeGuideLightbox();
  });
}

/** Mark the section currently being read in the guide's contents list. */
function setupGuideScrollspy() {
  const links = Array.from(document.querySelectorAll(".guide-toc a"));
  if (links.length === 0) return;
  const byHash = new Map(links.map((link) => [link.getAttribute("href"), link]));
  const headings = links
    .map((link) => document.querySelector(link.getAttribute("href")))
    .filter(Boolean);
  if (headings.length === 0) return;

  const mark = (id) => {
    byHash.forEach((link, hash) => {
      if (hash === `#${id}`) {
        link.setAttribute("aria-current", "true");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  };

  const seen = new Set();
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          seen.add(entry.target.id);
        } else {
          seen.delete(entry.target.id);
        }
      });
      // Headings leave the band from the top as you scroll down, so the first
      // one still inside it is the section you are reading.
      const current = headings.find((heading) => seen.has(heading.id));
      if (current) mark(current.id);
    },
    { rootMargin: "-8% 0px -70% 0px", threshold: 0 },
  );
  headings.forEach((heading) => observer.observe(heading));
  mark(headings[0].id);
}

// Code blocks hold literal env vars, JSON and commands the reader is about to
// paste elsewhere -- retyping them by hand is exactly the friction this page
// exists to remove. 127.0.0.1 over plain http is a secure context under the
// browser's localhost exception, so navigator.clipboard is expected to work
// here despite the dashboard not being served over https; feature-detected
// anyway, and a rejected write fails quietly rather than breaking the page --
// the code stays selectable and readable either way.
function setupGuideCodeCopy() {
  if (!navigator.clipboard || !navigator.clipboard.writeText) return;
  document.querySelectorAll(".guide-body pre").forEach((block) => {
    const code = block.querySelector("code");
    if (!code) return;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "guide-copy-button";
    button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy code to clipboard");
    button.addEventListener("click", () => {
      navigator.clipboard
        .writeText(code.textContent)
        .then(() => {
          button.textContent = "Copied";
          button.classList.add("is-copied");
          window.setTimeout(() => {
            button.textContent = "Copy";
            button.classList.remove("is-copied");
          }, 1500);
        })
        .catch(() => {
          // Clipboard writes can fail on permissions or browser policy; the
          // reader can still select and copy the text by hand.
        });
    });
    block.appendChild(button);
  });
}

setupGuideScreenshots();
setupGuideScrollspy();
setupGuideCodeCopy();
