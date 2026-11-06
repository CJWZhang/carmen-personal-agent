"use strict";

const sessionToken = document.querySelector('meta[name="carmen-session"]').content;

const state = {
  overview: null,
  filter: "today",
  selectedId: "",
  chatHistory: [],
  chatOpen: false,
  mobileDetailOpen: false,
  toastTimer: null,
  refreshTimer: null,
  operationTimer: null,
  refreshPollErrors: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if ((options.method || "GET") !== "GET") headers["X-Carmen-Session"] = sessionToken;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(path, { ...options, headers, signal: options.signal || controller.signal });
    const payload = await response.json().catch(() => ({ error: "The local app returned an unreadable response." }));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("The local app did not respond in time.");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 3200);
}

function formatSource(source) {
  return ({ github: "GitHub", slack: "Slack", gmail: "Gmail", calendar: "Calendar", manual: "Personal" })[source] || source;
}

function naturalList(values) {
  if (values.length < 2) return values.join("");
  if (values.length === 2) return values.join(" and ");
  return `${values.slice(0, -1).join(", ")}, and ${values[values.length - 1]}`;
}

function formatStamp(value) {
  if (!value) return "Not refreshed yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Recently refreshed";
  return `Updated ${new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date)}`;
}

function elapsedStamp(value) {
  const started = value ? new Date(value) : null;
  if (!started || Number.isNaN(started.getTime())) return "just started";
  const seconds = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function duePresentation(value) {
  if (!value) return { label: "", invalid: false, overdue: false };
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return { label: "Check due date", invalid: true, overdue: false };
  const [year, month, day] = match.slice(1).map(Number);
  if (year < 2000 || year > 2100) return { label: "Check due date", invalid: true, overdue: false };
  const due = new Date(year, month - 1, day);
  if (due.getFullYear() !== year || due.getMonth() !== month - 1 || due.getDate() !== day) {
    return { label: "Check due date", invalid: true, overdue: false };
  }
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const days = Math.round((due.getTime() - start.getTime()) / 86_400_000);
  if (days < 0) return { label: `${Math.abs(days)}d overdue`, invalid: false, overdue: true };
  if (days === 0) return { label: "Due today", invalid: false, overdue: false };
  if (days === 1) return { label: "Due tomorrow", invalid: false, overdue: false };
  if (days < 7) return { label: `Due ${new Intl.DateTimeFormat(undefined, { weekday: "long" }).format(due)}`, invalid: false, overdue: false };
  return { label: `Due ${new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(due)}`, invalid: false, overdue: false };
}

function recommendedAction(task) {
  if (task.needs_review) return {
    title: "Confirm whether this belongs in your plan",
    copy: "Review the source evidence, then accept or correct Carmen's suggestion.",
    label: "Review suggestion",
    kind: "review",
  };
  if (task.lifecycle_stage === "verifying") return {
    title: "Confirm the action landed in the source",
    copy: "Refresh the connected source before closing this loop locally.",
    label: "Verify source",
    kind: "verify",
  };
  if (task.status === "waiting") return {
    title: "Set up the next follow-up",
    copy: "Decide when to check back and what would unblock the work.",
    label: "Plan follow-up",
    kind: "follow-up",
  };
  if (task.needs_reply) return {
    title: "Close the reply loop",
    copy: "Use the source context to draft a short response in your voice.",
    label: "Draft response",
    kind: "draft",
  };
  if (task.source === "calendar" && task.priority_adjustment < 0) return {
    title: "Treat this meeting as optional",
    copy: task.preference_reason || "Past behavior suggests this meeting rarely needs your attention.",
    label: "Review event",
    kind: "source",
  };
  if (task.source === "calendar") return {
    title: "Prepare the meeting outcome",
    copy: "Pull out the decisions, examples, and questions you need before the meeting.",
    label: "Prepare meeting",
    kind: "meeting",
  };
  if (task.source === "github") return {
    title: "Continue from the current pull request state",
    copy: "Open the PR to address the next failing check, review finding, or draft step.",
    label: "Open PR",
    kind: "source",
  };
  if (task.source === "gmail") return {
    title: "Review the source email",
    copy: "Open the current thread and resolve the decision or deadline it contains.",
    label: "Open email",
    kind: "source",
  };
  return {
    title: "Make one focused block of progress",
    copy: "Turn this into a concrete 30-minute plan with a clear stopping point.",
    label: "Plan 30 minutes",
    kind: "plan",
  };
}

function toDatetimeLocal(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 16);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function greeting(name) {
  const hour = new Date().getHours();
  const opening = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  return name && name !== "there" ? `${opening}, ${name.split(" ")[0]}` : opening;
}

function normalizeOverview() {
  if (!state.overview) return;
  const tasks = state.overview.tasks || [];
  const hasRankedPlan = tasks.some((task) => Object.prototype.hasOwnProperty.call(task, "focus_now"));
  let focusRemaining = 3;
  tasks.forEach((task) => {
    if (!Object.prototype.hasOwnProperty.call(task, "needs_review")) {
      task.needs_review = task.source !== "manual" && (task.snapshot_status === "saved" || Number(task.confidence || 0) < 0.5);
    }
    if (task.needs_review) task.bucket = "review";
    if (!hasRankedPlan) {
      task.focus_now = !task.needs_review && task.status !== "waiting" && focusRemaining > 0;
      if (task.focus_now) focusRemaining -= 1;
    }
  });
  state.overview.counts.review = tasks.filter((task) => task.needs_review).length;
}

function filteredTasks() {
  if (!state.overview) return [];
  if (state.filter === "completed") return state.overview.completed || [];
  const tasks = state.overview.tasks || [];
  if (state.filter === "messages") return tasks.filter((task) => task.bucket === "replies");
  if (state.filter === "meetings") return tasks.filter((task) => task.bucket === "meetings");
  if (state.filter === "waiting") return tasks.filter((task) => task.bucket === "waiting");
  if (state.filter === "review") return tasks.filter((task) => task.bucket === "review");
  return tasks;
}

function selectedTask() {
  if (!state.overview || !state.selectedId) return null;
  return [...(state.overview.tasks || []), ...(state.overview.completed || [])]
    .find((task) => task.id === state.selectedId) || null;
}

function selectFirstVisibleTask() {
  const tasks = filteredTasks();
  state.selectedId = tasks.find((task) => task.focus_now)?.id || tasks[0]?.id || "";
}

function activateFilter(filter) {
  state.filter = filter;
  state.mobileDetailOpen = false;
  selectFirstVisibleTask();
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.filter === filter));
  $("#sidebar").classList.remove("open");
  renderTasks();
  renderDetail();
}

function setChatOpen(open, { focus = true } = {}) {
  state.chatOpen = Boolean(open);
  $(".app-shell").classList.toggle("chat-open", state.chatOpen);
  $("#assistant-drawer").setAttribute("aria-hidden", String(!state.chatOpen));
  $("#chat-launcher").setAttribute("aria-expanded", String(state.chatOpen));
  if (state.chatOpen && focus) window.setTimeout(() => $("#assistant-input").focus(), 0);
}

function renderMetrics() {
  const { counts, user, date, last_updated: lastUpdated, source_health: sourceHealth, refresh, maintenance } = state.overview;
  const focusNow = (state.overview.tasks || []).filter((task) => task.focus_now).length;
  $("#metric-focus").textContent = focusNow;
  $("#metric-replies").textContent = counts.replies;
  $("#metric-meetings").textContent = counts.meetings;
  $("#count-today").textContent = counts.open;
  $("#count-messages").textContent = counts.replies;
  $("#count-meetings").textContent = counts.meetings;
  $("#count-waiting").textContent = counts.waiting;
  $("#count-review").textContent = counts.review || 0;
  $("#count-completed").textContent = counts.completed;
  $("#current-date").textContent = date;
  $("#provider-label").textContent = user.provider === "mlx" ? "Local MLX" : user.provider === "ollama" ? "Local Ollama" : "Configured model";
  $("#chat-model-label").textContent = `${user.provider} · ${user.model} · ${user.adapter}`;
  $("#greeting").textContent = greeting(user.name);
  const attentionParts = [];
  if (focusNow) attentionParts.push(`${focusNow} ${focusNow === 1 ? "priority" : "priorities"}`);
  if (counts.replies) attentionParts.push(`${counts.replies} ${counts.replies === 1 ? "response" : "responses"}`);
  if (counts.meetings) attentionParts.push(`${counts.meetings} ${counts.meetings === 1 ? "meeting" : "meetings"} to prepare`);
  $("#daily-summary").textContent = attentionParts.length
    ? `Start with ${naturalList(attentionParts)}.`
    : counts.open
    ? `${counts.open} open ${counts.open === 1 ? "item" : "items"}, with nothing urgent right now.`
    : "Your plan is clear.";
  const sourceRecords = sourceHealth?.sources || [];
  const refreshing = refresh?.state === "running";
  const refreshElapsed = refreshing ? elapsedStamp(refresh.started_at) : "";
  const refreshFailed = refresh?.state === "failed";
  const savedAvailable = Number(sourceHealth?.saved_available_count || 0);
  const lastSuccessfulSource = sourceRecords
    .map((record) => record.last_success_at || "")
    .filter(Boolean)
    .sort()
    .at(-1);
  $("#freshness").textContent = lastSuccessfulSource
    ? `Last successful sync · ${formatStamp(lastSuccessfulSource).replace("Updated ", "")}`
    : `Plan updated · ${formatStamp(lastUpdated).replace("Updated ", "")}`;
  const sourceFreshness = $("#source-freshness");
  sourceFreshness.classList.toggle("needs-attention", sourceHealth?.level !== "ok");
  sourceFreshness.textContent = sourceHealth?.level === "ok"
    ? "Sources current"
    : "Using the last successful sync";
  const healthStrip = $("#source-health-strip");
  healthStrip.classList.toggle("needs-attention", sourceHealth?.level !== "ok");
  $("#source-health-summary").textContent = refreshing
    ? "Updating your work…"
    : sourceHealth?.level === "ok"
    ? "Work sources are up to date"
    : "Using the last successful sync";
  const warning = $("#source-warning");
  const showWarning = savedAvailable > 0 && !refreshing;
  warning.classList.toggle("hidden", !showWarning);
  warning.classList.toggle("warning", sourceHealth?.level === "warning");
  if (showWarning) {
    $("#source-warning-title").textContent = `${savedAvailable} saved ${savedAvailable === 1 ? "task is" : "tasks are"} ready to import`;
    $("#source-warning-message").textContent = "These are older open loops that are not in the current plan yet.";
  }
  const refreshButton = $("#source-refresh-button");
  refreshButton.disabled = refreshing;
  refreshButton.textContent = refreshing ? `Syncing · ${refreshElapsed}` : "Import and sync";
  const stripRefresh = $("#source-strip-refresh");
  stripRefresh.disabled = refreshing;
  stripRefresh.textContent = refreshing ? `Syncing · ${refreshElapsed}` : refreshFailed ? "Try sync again" : "Sync now";
  const headerRefresh = $("#reload-button");
  headerRefresh.disabled = refreshing;
  headerRefresh.textContent = refreshing ? `↻ Syncing · ${refreshElapsed}` : "↻ Sync";
  const seedButton = $("#source-seed-button");
  seedButton.classList.toggle("hidden", !savedAvailable || refreshing);
  seedButton.textContent = savedAvailable
    ? `Import ${savedAvailable} saved ${savedAvailable === 1 ? "task" : "tasks"}`
    : "Import saved tasks";
  const operationStatus = $("#operation-status");
  const operationRunning = maintenance?.state === "running";
  operationStatus.classList.toggle("hidden", !maintenance || maintenance.state === "idle");
  operationStatus.classList.toggle("failed", maintenance?.state === "failed");
  if (maintenance && maintenance.state !== "idle") {
    operationStatus.textContent = `${maintenance.message}${maintenance.detail ? `\n\n${maintenance.detail}` : ""}`;
  }
  ["#save-automation-button", "#install-schedule-button", "#notify-button", "#personalize-button", "#refresh-voice-button", "#fine-tune-button", "#evaluate-button"]
    .forEach((selector) => {
      const button = $(selector);
      if (button) button.disabled = operationRunning || refreshing;
    });
  $("#settings-refresh-button").disabled = operationRunning || refreshing;
  $("#settings-refresh-button").textContent = refreshing
    ? "Syncing…"
    : refreshFailed
    ? "Try sync again"
    : "Sync now";
  renderConnections(sourceHealth?.sources || []);
  renderPersonalization(state.overview.personalization || {});
}

function renderPersonalization(personalization = {}) {
  const profileReady = Boolean(personalization.profile_ready);
  const adapterActive = Boolean(personalization.adapter_active);
  const messageCount = Number(personalization.message_count || 0);
  const usableCount = Number(personalization.usable_message_count || 0);
  const banner = $("#personalization-banner");
  if (banner) banner.classList.toggle("hidden", profileReady);

  const status = $("#personalization-status");
  if (!status) return;
  status.classList.toggle("ready", profileReady);
  status.textContent = adapterActive
    ? "Fine-tune active"
    : profileReady
    ? "Voice profile active"
    : messageCount >= 50
    ? "Writing ready"
    : "Not personalized";

  $("#personalization-writing-copy").textContent = messageCount
    ? `${messageCount} authored writing samples imported${usableCount ? ` · ${usableCount} usable` : ""}`
    : `Waiting to import authored emails or messages via ${personalization.collection_transport || "a connection"}`;
  $("#personalization-training-copy").textContent = personalization.supports_lora
    ? adapterActive
      ? "Active for message drafting"
      : personalization.adapter_ready
      ? "A trained local adapter is ready to activate"
      : "Optional local MLX LoRA"
    : "Voice profile works on this device; LoRA requires Apple Silicon";
  $("#personalization-next").textContent = personalization.next_action || "Connect Gmail or Slack to begin.";
  $("#personalization-step-writing").classList.toggle("complete", messageCount >= 50);
  $("#personalization-step-profile").classList.toggle("complete", profileReady);
  $("#personalization-step-training").classList.toggle("complete", adapterActive);

  const personalize = $("#personalize-button");
  personalize.classList.toggle("hidden", profileReady);
  personalize.textContent = messageCount >= 50 ? "Build my voice profile" : "Personalize my agent";
  const refresh = $("#refresh-voice-button");
  refresh.classList.toggle("hidden", !profileReady);
  const fineTune = $("#fine-tune-button");
  fineTune.classList.toggle(
    "hidden",
    !profileReady || !personalization.supports_lora || adapterActive,
  );
  fineTune.textContent = personalization.adapter_ready
    ? "Activate existing fine-tune"
    : "Fine-tune locally";
  $("#personalization-privacy").textContent = personalization.collection_transport === "direct Slack API"
    ? "Your agent reads only messages authored by you directly from Slack. Secrets and identifiers are filtered, and the resulting corpus, profile, and adapter stay on this device."
    : "Your signed-in Gmail or Slack connection is used only to find writing authored by you. Quoted history, signatures, secrets, and identifiers are filtered before the private corpus, profile, or adapter is stored on this device.";
}

function renderConnections(sources) {
  const list = $("#connector-list");
  if (!list) return;
  list.replaceChildren();
  const timedOut = sources.filter((source) => /timed out/i.test(source.detail || ""));
  const unavailable = sources.filter((source) => source.status === "unavailable");
  const running = sources.filter((source) => source.status === "running");
  const failed = sources.filter((source) => ["error", "unavailable"].includes(source.status));
  sources.forEach((source) => {
    const card = node("div", "connector-card");
    const copy = node("div", "connector-copy");
    copy.append(node("strong", "", formatSource(source.source)));
    const current = source.fresh && ["ok", "empty"].includes(source.status);
    const stateLabel = current
      ? `${source.item_count || 0} current ${Number(source.item_count || 0) === 1 ? "item" : "items"}`
      : source.status === "unavailable"
      ? "Not connected"
      : source.status === "running"
      ? "Syncing"
      : /timed out/i.test(source.detail || "")
      ? "Timed out"
      : source.status === "error"
      ? "Refresh error"
      : source.snapshot_exists
      ? "Snapshot is stale"
      : "Not refreshed yet";
    copy.append(node("span", "", source.detail || stateLabel));
    const transport = source.transport === "direct_api"
      ? "Direct API"
      : source.transport === "compatibility_bridge"
      ? "Connector bridge"
      : "Legacy snapshot";
    const duration = Number(source.duration_ms || 0);
    const healthMeta = [transport];
    if (duration > 0) healthMeta.push(duration >= 1000 ? `${(duration / 1000).toFixed(1)}s` : `${Math.round(duration)}ms`);
    if (source.last_success_at) healthMeta.push(formatStamp(source.last_success_at).replace("Updated ", "Last success "));
    copy.append(node("span", "connector-meta", healthMeta.join(" · ")));
    const badge = node(
      "span",
      `connector-badge ${current ? "connected" : "attention"}`,
      current ? "Current" : stateLabel,
    );
    card.append(copy, badge);
    list.append(card);
  });
  const guidance = $("#connector-guidance");
  if (running.length) {
    const names = running.map((source) => formatSource(source.source)).join(", ");
    guidance.textContent = `Sync in progress: ${names}. Last successful snapshots remain available until each source finishes.`;
  } else if (timedOut.length === sources.length && sources.length) {
    guidance.textContent = "The last shared sync reached its three-minute safety limit. Your previous snapshots are preserved; try syncing again.";
  } else if (unavailable.length) {
    guidance.textContent = "Connect the source with Carmen OAuth/direct credentials, or enable its compatibility connector in the ChatGPT desktop app.";
  } else if (failed.length || sources.some((source) => !source.fresh)) {
    guidance.textContent = "Some sources could not refresh. Carmen is still using their last successful snapshots and will not present them as newly verified.";
  } else if (sources.length && sources.every((source) => source.fresh && ["ok", "empty"].includes(source.status))) {
    guidance.textContent = "All connected sources are current.";
  } else {
    guidance.textContent = "Connection status is not available yet.";
  }
}

const viewCopy = {
  today: ["Today", "What to do next"],
  messages: ["Messages", "Responses to send"],
  meetings: ["Meetings", "Preparation and decisions"],
  waiting: ["Waiting", "Follow-ups to watch"],
  review: ["Review", "Suggestions to confirm"],
  completed: ["Completed", "Recently closed"],
};

function taskCard(task) {
  const card = node("button", "task-card");
  card.type = "button";
  card.dataset.taskId = task.id;
  card.classList.toggle("selected", task.id === state.selectedId);

  const priority = node("span", `priority-mark ${task.urgency}`);
  const copy = node("div", "task-card-copy");
  copy.append(node("h3", "task-title", task.title));
  copy.append(node("p", "task-why", task.why || "No additional context yet."));
  const meta = node("div", "task-meta");
  meta.append(node("span", "task-meta-item source-meta", formatSource(task.source)));
  const due = duePresentation(task.due);
  if (due.label) meta.append(node("span", `task-meta-item ${due.invalid ? "invalid-date" : due.overdue ? "overdue-date" : ""}`, due.label));
  if (task.needs_reply) meta.append(node("span", "task-meta-item", "Needs reply"));
  if (task.lifecycle_stage === "in_progress") meta.append(node("span", "task-meta-item", "In progress"));
  if (task.lifecycle_stage === "verifying") meta.append(node("span", "task-meta-item", "Verifying"));
  else if (task.status === "waiting") meta.append(node("span", "task-meta-item", "Waiting"));
  if (task.status === "done") meta.append(node("span", "task-meta-item", "Done"));
  if (task.needs_review) meta.append(node("span", "mini-pill review-pill", "Review first"));
  if (task.preference_reason) meta.append(node("span", "mini-pill", "Personalized"));
  if (task.snapshot_status === "saved") {
    const stamp = task.snapshot_at ? new Date(task.snapshot_at) : null;
    const label = stamp && !Number.isNaN(stamp.getTime())
      ? `Saved ${new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(stamp)}`
      : "Saved snapshot";
    meta.append(node("span", "mini-pill saved-pill", label));
  }
  copy.append(meta);
  const action = recommendedAction(task);
  card.append(priority, copy, node("span", "task-next-action", `${action.label} →`));
  card.addEventListener("click", () => {
    state.selectedId = task.id;
    state.mobileDetailOpen = true;
    $("#more-actions").open = false;
    renderTasks();
    renderDetail();
  });
  return card;
}

function renderTasks() {
  const tasks = filteredTasks();
  const [eyebrow, title] = viewCopy[state.filter];
  $("#view-eyebrow").textContent = eyebrow;
  $("#view-title").textContent = title;
  $("#item-total").textContent = `${tasks.length} ${tasks.length === 1 ? "item" : "items"}`;
  const list = $("#task-list");
  list.replaceChildren();
  if (!tasks.length) {
    const empty = node("div", "empty-list");
    empty.append(node("strong", "", state.filter === "completed" ? "Nothing completed yet" : "This view is clear"));
    empty.append(node("span", "", state.filter === "today" ? "Add a task or refresh your connected sources." : "There are no matching open loops."));
    list.append(empty);
    return;
  }
  if (state.filter !== "today") {
    tasks.forEach((task) => list.append(taskCard(task)));
    return;
  }
  const groups = [
    ["Do these first", "Your best next moves", tasks.filter((task) => task.focus_now)],
    ["Later", "Keep these visible, but not first", tasks.filter((task) => !task.focus_now && !task.needs_review)],
    ["Review suggestions", "Confirm these before they enter your plan", tasks.filter((task) => task.needs_review)],
  ];
  groups.forEach(([label, description, items]) => {
    if (!items.length) return;
    const heading = node("div", "task-group-heading");
    const headingCopy = node("div");
    headingCopy.append(node("strong", "", label), node("span", "", description));
    heading.append(headingCopy, node("span", "", String(items.length)));
    list.append(heading);
    items.forEach((task) => list.append(taskCard(task)));
  });
}

function renderDetail() {
  const task = selectedTask();
  const detailPanel = $("#detail-panel");
  detailPanel.classList.toggle("sheet-open", Boolean(task) && state.mobileDetailOpen);
  document.body.classList.toggle("detail-sheet-open", Boolean(task) && state.mobileDetailOpen);
  $("#detail-backdrop").classList.toggle("hidden", !task || !state.mobileDetailOpen);
  $("#empty-detail").classList.toggle("hidden", Boolean(task));
  $("#task-detail").classList.toggle("hidden", !task);
  if (!task) {
    $("#assistant-context").textContent = "Ask about your day, or select a task for contextual help.";
    $("#chat-provenance").textContent = "Context: verified workspace task list";
    return;
  }

  $("#detail-source").textContent = task.snapshot_status === "saved"
    ? `${formatSource(task.source)} · saved snapshot`
    : formatSource(task.source);
  $("#detail-title").textContent = task.title;
  $("#detail-why").textContent = task.why || "No explanation was recorded for this task.";
  const action = recommendedAction(task);
  $("#primary-action-title").textContent = action.title;
  $("#primary-action-copy").textContent = action.copy;
  $("#primary-action-button").textContent = action.label;
  $("#primary-action-button").dataset.actionKind = action.kind;
  $("#detail-recommendation").textContent = task.recommendation_reason || "This is an open commitment in your workspace.";
  const verified = task.verified_at ? formatStamp(task.verified_at).replace("Updated", "Verified") : "No source verification timestamp";
  $("#detail-evidence-status").textContent = `${task.confidence_label || "Source-derived"} · ${verified} · ${formatSource(task.source)}`;
  const meta = $("#detail-meta");
  meta.replaceChildren();
  meta.append(node("span", "task-meta-item", task.urgency.replace("_", " ")));
  if (task.people?.length) meta.append(node("span", "task-meta-item", task.people.slice(0, 3).join(", ")));
  const due = duePresentation(task.due);
  if (due.label) meta.append(node("span", `task-meta-item ${due.invalid ? "invalid-date" : due.overdue ? "overdue-date" : ""}`, due.label));
  if (task.remind_at) meta.append(node("span", "task-meta-item", "Reminder set"));
  if (task.edited_fields?.length) meta.append(node("span", "task-meta-item", "Edited by you"));
  if (task.lifecycle_stage === "in_progress") meta.append(node("span", "task-meta-item", "In progress"));
  if (task.lifecycle_stage === "verifying") meta.append(node("span", "task-meta-item", "Awaiting source verification"));
  else if (task.status === "waiting") meta.append(node("span", "task-meta-item", "Waiting"));
  if (task.snapshot_status === "saved") meta.append(node("span", "mini-pill saved-pill", "Needs refresh"));
  if (task.snapshot_status === "cached") meta.append(node("span", "task-meta-item", "Using last sync"));
  if (task.snapshot_status === "stale") meta.append(node("span", "task-meta-item", "Needs source check"));
  $("#fix-date-button").classList.toggle("hidden", !due.invalid || task.status === "done");

  $("#review-actions").classList.toggle("hidden", !task.needs_review || task.status === "done");

  const active = task.status !== "done";
  $("#complete-button").classList.toggle("hidden", !active);
  $("#start-button").classList.toggle("hidden", !active || task.lifecycle_stage === "in_progress");
  $("#waiting-button").classList.toggle("hidden", !active || task.lifecycle_stage === "waiting");
  $("#edit-button").classList.toggle("hidden", !active);
  $("#snooze-button").classList.toggle("hidden", !active);
  $("#usually-skip-button").classList.toggle("hidden", !active || task.source !== "calendar");
  $("#usually-attend-button").classList.toggle("hidden", !active || task.source !== "calendar");
  const hasDraftContext = Boolean(task.context) && task.source !== "manual";
  $("#draft-button").classList.toggle("hidden", !active || !hasDraftContext);
  const connectorActions = task.connector_actions || [];
  const connectorPanel = $("#connector-action-panel");
  const connectorSelect = $("#connector-action-select");
  connectorSelect.replaceChildren();
  connectorActions.forEach((connectorAction) => {
    const option = document.createElement("option");
    option.value = connectorAction.id;
    option.textContent = connectorAction.label;
    connectorSelect.append(option);
  });
  connectorPanel.classList.toggle(
    "hidden",
    !active || !state.overview.capabilities?.task_execution || !connectorActions.length,
  );

  const sourceUrl = task.context?.url || task.links?.[0] || "";
  $("#source-button").classList.toggle("hidden", !sourceUrl);
  $("#source-button").dataset.url = sourceUrl;

  const context = task.context;
  $("#context-block").classList.toggle("hidden", !context);
  if (context) {
    const location = context.channel ? ` in ${context.channel}` : "";
    $("#context-sender").textContent = `${context.sender || "Source"}${location}`;
    $("#context-body").textContent = context.body || context.subject || "No source excerpt available.";
  }

  $("#draft-block").classList.toggle("hidden", !hasDraftContext && !task.draft);
  $("#draft-copy").textContent = task.draft || "No draft yet. Carmen can create one from this task's context.";
  $("#copy-draft-button").classList.toggle("hidden", !task.draft);
  $("#assistant-context").textContent = active
    ? `Using context from “${task.title}”.`
    : "Completed items are shown for reference; select an open task for contextual help.";
  $("#chat-provenance").textContent = active
    ? `Context: ${formatSource(task.source)} · ${task.snapshot_status === "saved" ? "saved snapshot" : task.snapshot_status === "cached" ? "cached source after refresh failure" : task.snapshot_status === "stale" ? "stale source awaiting verification" : task.context ? "source message loaded" : "task summary only"}`
    : "Context: completed task summary";
}

function openEditDialog() {
  const task = selectedTask();
  if (!task || task.status === "done") return;
  const form = $("#edit-task-form");
  form.elements.title.value = task.title || "";
  form.elements.why.value = task.why || "";
  form.elements.urgency.value = task.urgency || "this_week";
  form.elements.due.value = task.due || "";
  form.elements.remind_at.value = toDatetimeLocal(task.remind_at);
  $("#edit-task-dialog").showModal();
  form.elements.title.focus();
}

function renderAll() {
  normalizeOverview();
  renderMetrics();
  renderTasks();
  renderDetail();
}

async function loadOverview({ preserveSelection = true } = {}) {
  const reload = $("#reload-button");
  reload.disabled = true;
  try {
    state.overview = await api("/api/overview");
    state.refreshPollErrors = 0;
    const allIds = new Set([...(state.overview.tasks || []), ...(state.overview.completed || [])].map((task) => task.id));
    if (!preserveSelection || !allIds.has(state.selectedId)) selectFirstVisibleTask();
    renderAll();
    if (state.overview.refresh?.state === "running") scheduleRefreshPoll();
    if (state.overview.maintenance?.state === "running") scheduleOperationPoll();
  } catch (error) {
    showToast(error.message, true);
    if (state.overview?.refresh?.state === "running") {
      state.refreshPollErrors += 1;
      if (state.refreshPollErrors < 3) scheduleRefreshPoll();
      else {
        state.overview.refresh = {
          ...state.overview.refresh,
          state: "failed",
          message: "The refresh status could not be reached. You can try again.",
        };
        renderAll();
      }
    }
  } finally {
    reload.disabled = state.overview?.refresh?.state === "running";
  }
}

function scheduleRefreshPoll() {
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = window.setTimeout(pollRefresh, 2500);
}

async function pollRefresh() {
  const priorState = state.overview?.refresh?.state;
  try {
    const status = await api("/api/status");
    state.refreshPollErrors = 0;
    state.overview.refresh = status.refresh;
    state.overview.maintenance = status.maintenance;
    if (status.source_health) state.overview.source_health = status.source_health;
    renderMetrics();
    if (status.refresh?.state === "running") {
      scheduleRefreshPoll();
      return;
    }
    await loadOverview();
    if (priorState === "running" && status.refresh?.message) {
      showToast(status.refresh.message, status.refresh.state === "failed");
    }
  } catch (error) {
    state.refreshPollErrors += 1;
    if (state.refreshPollErrors < 3) {
      scheduleRefreshPoll();
      return;
    }
    state.overview.refresh = {
      ...state.overview.refresh,
      state: "failed",
      message: "The refresh status could not be reached. You can try again.",
    };
    renderMetrics();
    showToast(error.message, true);
  }
}

async function refreshSources() {
  const button = $("#source-refresh-button");
  button.disabled = true;
  button.textContent = "Starting…";
  try {
    state.refreshPollErrors = 0;
    const result = await api("/api/refresh", {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.overview = result.overview;
    renderAll();
    showToast(result.message);
    scheduleRefreshPoll();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
    button.textContent = "Refresh now";
  }
}

async function seedSavedSnapshot() {
  const button = $("#source-seed-button");
  button.disabled = true;
  try {
    const result = await api("/api/seed-snapshot", {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.overview = result.overview;
    renderAll();
    showToast(result.message);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function scheduleOperationPoll() {
  window.clearTimeout(state.operationTimer);
  state.operationTimer = window.setTimeout(pollOperation, 2500);
}

async function pollOperation() {
  const priorState = state.overview?.maintenance?.state;
  await loadOverview();
  const operation = state.overview?.maintenance;
  if (operation?.state === "running") return;
  if (priorState === "running" && operation?.message) {
    showToast(operation.message, operation.state === "failed");
  }
}

function renderDiagnostics(items) {
  const list = $("#diagnostics-list");
  list.replaceChildren();
  items.forEach((item) => {
    const row = node("div", "diagnostic-row");
    row.append(
      node("span", `diagnostic-dot ${item.level}`),
      node("span", "diagnostic-name", item.name),
      node("span", "diagnostic-detail", item.detail),
    );
    list.append(row);
  });
}

function populateSettings(settings) {
  const form = $("#settings-form");
  form.elements.user_name.value = settings.user_name || "";
  form.elements.provider.value = settings.provider || "ollama";
  form.elements.model.value = settings.model || "qwen3:4b";
  form.elements.base_url.value = settings.base_url || "";
  form.elements.action_command.value = settings.action_command || "";
  const automation = settings.automation || {};
  const dailySelect = $("#daily-refresh-select");
  const reminderSelect = $("#reminder-interval-select");
  const dailyOptions = automation.daily_refresh_options || [];
  const reminderOptions = automation.reminder_options || [];
  if (dailyOptions.length) {
    dailySelect.replaceChildren(...dailyOptions.map((item) => {
      const option = node("option", "", item.label);
      option.value = item.value;
      return option;
    }));
  }
  if (reminderOptions.length) {
    reminderSelect.replaceChildren(...reminderOptions.map((item) => {
      const option = node("option", "", item.label);
      option.value = String(item.value);
      return option;
    }));
  }
  dailySelect.value = automation.daily_refresh || "05:00";
  reminderSelect.value = String(automation.reminder_minutes || 30);
  const selectedDailyLabel = dailySelect.selectedOptions[0]?.textContent || "5:00 AM";
  $("#automation-description").textContent = `Carmen refreshes automatically every day at ${selectedDailyLabel} in your local timezone.`;
  renderPersonalization(settings.personalization || {});
}

function selectedAutomation() {
  return {
    daily_refresh: $("#daily-refresh-select").value,
    reminder_minutes: Number($("#reminder-interval-select").value),
  };
}

async function openSettings() {
  $("#settings-dialog").showModal();
  try {
    const result = await api("/api/diagnostics", {
      method: "POST",
      body: JSON.stringify({}),
    });
    populateSettings(result.settings);
    renderDiagnostics(result.diagnostics);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function openPersonalization() {
  await openSettings();
  window.setTimeout(() => {
    $("#personalization-section")?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, 0);
}

async function startOperation(action, values = {}) {
  try {
    const result = await api("/api/operations", {
      method: "POST",
      body: JSON.stringify({ action, ...values }),
    });
    state.overview = result.overview;
    renderAll();
    showToast(result.message);
    scheduleOperationPoll();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function taskAction(action, payload = {}) {
  const task = selectedTask();
  if (!task) return;
  const complete = $("#complete-button");
  const snooze = $("#snooze-button");
  const edit = $("#edit-button");
  const execute = $("#execute-button");
  const controls = [complete, snooze, edit, execute, $("#start-button"), $("#waiting-button"), $("#verify-context-button"), $("#usually-skip-button"), $("#usually-attend-button"), $("#accept-button"), $("#not-task-button"), $("#not-mine-button"), $("#handled-button")];
  controls.forEach((control) => { control.disabled = true; });
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(task.id)}/${action}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.overview = result.overview;
    if (["complete", "not-a-task", "not-mine", "already-handled"].includes(action)) {
      state.mobileDetailOpen = false;
      selectFirstVisibleTask();
    }
    if (action === "execute" && !(state.overview.tasks || []).some((item) => item.id === task.id)) {
      state.selectedId = "";
    }
    $("#more-actions").open = false;
    showToast(result.message);
    renderAll();
    if (action === "verify-context") scheduleRefreshPoll();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    controls.forEach((control) => { control.disabled = false; });
  }
}

function addChatMessage(role, content, extraClass = "") {
  const thread = $("#chat-thread");
  thread.classList.add("has-messages");
  const message = node("div", `chat-message ${role} ${extraClass}`.trim(), content);
  thread.append(message);
  thread.scrollTop = thread.scrollHeight;
  return message;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      parent.append(strong);
    } else if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else if (token.startsWith("*")) {
      const emphasis = document.createElement("em");
      emphasis.textContent = token.slice(1, -1);
      parent.append(emphasis);
    } else {
      const parts = /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(token);
      if (parts) {
        const link = document.createElement("a");
        link.textContent = parts[1];
        link.href = parts[2];
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.append(link);
      }
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function renderAssistantMarkdown(message, content) {
  message.replaceChildren();
  message.classList.add("markdown");
  const lines = String(content || "").replace(/\r\n?/g, "\n").split("\n");
  let paragraph = null;
  let list = null;
  let listKind = "";

  const resetBlocks = () => { paragraph = null; list = null; listKind = ""; };
  lines.forEach((rawLine) => {
    const line = rawLine.trim();
    if (!line) {
      resetBlocks();
      return;
    }
    if (/^(-{3,}|\*{3,})$/.test(line)) {
      message.append(document.createElement("hr"));
      resetBlocks();
      return;
    }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      const level = Math.min(4, heading[1].length + 1);
      const element = document.createElement(`h${level}`);
      appendInlineMarkdown(element, heading[2]);
      message.append(element);
      resetBlocks();
      return;
    }
    const item = /^(?:[-*+]\s+|(\d+)\.\s+)(.+)$/.exec(line);
    if (item) {
      const kind = item[1] ? "ol" : "ul";
      if (!list || listKind !== kind) {
        list = document.createElement(kind);
        listKind = kind;
        message.append(list);
      }
      const listItem = document.createElement("li");
      appendInlineMarkdown(listItem, item[2]);
      list.append(listItem);
      paragraph = null;
      return;
    }
    if (!paragraph) {
      paragraph = document.createElement("p");
      message.append(paragraph);
    } else {
      paragraph.append(document.createElement("br"));
    }
    appendInlineMarkdown(paragraph, line);
    list = null;
    listKind = "";
  });
}

async function sendChat(message, { asDraft = false } = {}) {
  const cleaned = message.trim();
  if (!cleaned) return;
  const task = selectedTask();
  const activeTaskId = task?.status !== "done" ? task?.id || "" : "";
  addChatMessage("user", cleaned);
  const thinking = addChatMessage("assistant", "Thinking locally…", "thinking");
  const input = $("#assistant-input");
  const send = $(".send-button");
  input.value = "";
  input.style.height = "auto";
  input.disabled = true;
  send.disabled = true;
  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: cleaned, task_id: activeTaskId, history: state.chatHistory }),
    });
    thinking.classList.remove("thinking");
    renderAssistantMarkdown(thinking, result.reply);
    if (result.meta) {
      $("#chat-model-label").textContent = `${result.meta.provider} · ${result.meta.model} · ${result.meta.adapter}`;
      const context = result.meta.context || {};
      $("#chat-provenance").textContent = context.task
        ? `Context: ${formatSource(context.source)} · ${context.source_message_loaded ? "source message loaded" : "task summary only"}`
        : "Context: verified workspace task list";
      if (result.meta.quality_fallback) showToast("The model output failed validation, so Carmen used a grounded fallback.");
    }
    state.chatHistory.push({ role: "user", content: cleaned }, { role: "assistant", content: result.reply });
    state.chatHistory = state.chatHistory.slice(-12);
    if (asDraft) {
      $("#draft-copy").textContent = result.reply;
      $("#copy-draft-button").classList.remove("hidden");
      $("#copy-draft-button").dataset.generatedDraft = result.reply;
    }
  } catch (error) {
    thinking.classList.remove("thinking");
    thinking.textContent = `I couldn't answer: ${error.message}`;
    showToast(error.message, true);
  } finally {
    input.disabled = false;
    send.disabled = false;
    input.focus();
  }
}

function closeTaskDetail() {
  state.mobileDetailOpen = false;
  if (window.matchMedia("(max-width: 760px)").matches) state.selectedId = "";
  renderTasks();
  renderDetail();
}

function runPrimaryAction() {
  const task = selectedTask();
  if (!task) return;
  const kind = $("#primary-action-button").dataset.actionKind;
  if (kind === "review") {
    $("#accept-button").focus();
    $("#review-actions").scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (kind === "source") {
    const url = task.context?.url || task.links?.[0] || "";
    if (url && /^https?:\/\//i.test(url)) {
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
  }
  if (kind === "verify") {
    taskAction("verify-context");
    return;
  }
  const prompts = {
    draft: "Draft a concise response for this task. Return only the message I should send.",
    meeting: "Prepare me for this meeting. Give me the decisions to make, questions to ask, and context to have ready.",
    "follow-up": "Help me plan the next follow-up for this task, including when to check back and what to ask.",
    plan: "Give me a realistic 30-minute plan to move this task forward.",
  };
  setChatOpen(true, { focus: false });
  sendChat(prompts[kind] || prompts.plan, { asDraft: kind === "draft" });
}

function renderQuickTaskResults(query) {
  const results = $("#quick-task-results");
  results.replaceChildren();
  const cleaned = query.trim().toLocaleLowerCase();
  $("#quick-action-list").classList.toggle("hidden", Boolean(cleaned));
  if (!cleaned) return;
  const matches = (state.overview?.tasks || [])
    .filter((task) => `${task.title} ${task.why} ${formatSource(task.source)}`.toLocaleLowerCase().includes(cleaned))
    .slice(0, 8);
  if (!matches.length) {
    results.append(node("p", "quick-empty", "No matching open tasks."));
    return;
  }
  matches.forEach((task) => {
    const button = node("button", "quick-task-result");
    button.type = "button";
    button.dataset.taskId = task.id;
    const copy = node("span");
    copy.append(node("strong", "", task.title), node("small", "", `${formatSource(task.source)} · ${recommendedAction(task).label}`));
    button.append(copy, node("span", "", "↗"));
    button.addEventListener("click", () => {
      state.selectedId = task.id;
      state.mobileDetailOpen = true;
      $("#quick-actions-dialog").close();
      renderTasks();
      renderDetail();
    });
    results.append(button);
  });
}

function openQuickActions() {
  const dialog = $("#quick-actions-dialog");
  if (!dialog.open) dialog.showModal();
  $("#quick-action-input").value = "";
  renderQuickTaskResults("");
  $("#quick-action-input").focus();
}

function runQuickAction(action) {
  $("#quick-actions-dialog").close();
  if (action === "today") activateFilter("today");
  if (action === "new") {
    $("#new-task-form").reset();
    $("#new-task-dialog").showModal();
  }
  if (action === "refresh") refreshSources();
  if (action === "review") activateFilter("review");
  if (action === "chat") setChatOpen(true);
  if (action === "settings") openSettings();
}

function wireEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => {
    activateFilter(button.dataset.filter);
  }));
  $$(".metric-card").forEach((button) => button.addEventListener("click", () => activateFilter(button.dataset.filter)));

  $("#reload-button").addEventListener("click", refreshSources);
  $("#quick-actions-button").addEventListener("click", openQuickActions);
  $("#quick-actions-close").addEventListener("click", () => $("#quick-actions-dialog").close());
  $("#quick-action-input").addEventListener("input", (event) => renderQuickTaskResults(event.currentTarget.value));
  $$("[data-quick-action]").forEach((button) => button.addEventListener("click", () => runQuickAction(button.dataset.quickAction)));
  $("#settings-button").addEventListener("click", openSettings);
  $("#open-personalization-button").addEventListener("click", openPersonalization);
  $("#source-health-details").addEventListener("click", openSettings);
  $("#source-strip-refresh").addEventListener("click", refreshSources);
  $("#source-refresh-button").addEventListener("click", refreshSources);
  $("#settings-refresh-button").addEventListener("click", () => {
    $("#settings-dialog").close();
    refreshSources();
  });
  $("#source-seed-button").addEventListener("click", seedSavedSnapshot);
  $("#new-task-button").addEventListener("click", () => {
    $("#new-task-form").reset();
    $("#new-task-dialog").showModal();
  });
  $$(".dialog-close").forEach((button) => button.addEventListener("click", (event) => {
    event.currentTarget.closest("dialog")?.close();
  }));
  $("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#close-detail").addEventListener("click", closeTaskDetail);
  $("#detail-backdrop").addEventListener("click", closeTaskDetail);
  $("#primary-action-button").addEventListener("click", runPrimaryAction);
  $("#complete-button").addEventListener("click", () => taskAction("complete"));
  $("#start-button").addEventListener("click", () => taskAction("start"));
  $("#waiting-button").addEventListener("click", () => taskAction("wait"));
  $("#verify-context-button").addEventListener("click", () => taskAction("verify-context"));
  $("#usually-skip-button").addEventListener("click", () => taskAction("usually-skip"));
  $("#usually-attend-button").addEventListener("click", () => taskAction("usually-attend"));
  $("#accept-button").addEventListener("click", () => taskAction("accept"));
  $("#not-task-button").addEventListener("click", () => taskAction("not-a-task"));
  $("#not-mine-button").addEventListener("click", () => taskAction("not-mine"));
  $("#handled-button").addEventListener("click", () => taskAction("already-handled"));
  $("#edit-button").addEventListener("click", openEditDialog);
  $("#fix-date-button").addEventListener("click", openEditDialog);
  $("#execute-button").addEventListener("click", () => {
    const task = selectedTask();
    const actionId = $("#connector-action-select").value;
    const action = (task?.connector_actions || []).find((item) => item.id === actionId);
    if (task && action && window.confirm(`${action.label} for “${task.title}”?\n\n${action.description}`)) {
      taskAction("execute", { action_id: actionId });
    }
  });
  $("#snooze-button").addEventListener("click", () => taskAction("snooze", { minutes: 60 }));
  $("#source-button").addEventListener("click", (event) => {
    const url = event.currentTarget.dataset.url;
    if (url && /^https?:\/\//i.test(url)) window.open(url, "_blank", "noopener,noreferrer");
  });
  $("#draft-button").addEventListener("click", () => {
    const task = selectedTask();
    if (task) {
      setChatOpen(true, { focus: false });
      sendChat("Draft a concise response for this task. Return only the message I should send.", { asDraft: true });
    }
  });
  $$("[data-task-prompt]").forEach((button) => button.addEventListener("click", () => {
    if (!selectedTask()) return;
    setChatOpen(true, { focus: false });
    sendChat(button.dataset.taskPrompt, { asDraft: button.dataset.draft === "true" });
  }));
  $("#copy-draft-button").addEventListener("click", async () => {
    const text = $("#copy-draft-button").dataset.generatedDraft || selectedTask()?.draft || "";
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      showToast("Response copied to your clipboard.");
    } catch {
      showToast("The browser could not access your clipboard.", true);
    }
  });

  $("#new-task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const result = await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
      state.overview = result.overview;
      form.reset();
      $("#new-task-dialog").close();
      showToast(result.message);
      renderAll();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("#edit-task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const task = selectedTask();
    if (!task) return;
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form).entries());
    const original = {
      title: task.title || "",
      why: task.why || "",
      urgency: task.urgency || "this_week",
      due: task.due || "",
      remind_at: toDatetimeLocal(task.remind_at),
    };
    const payload = Object.fromEntries(
      Object.entries(values).filter(([field, value]) => value !== original[field]),
    );
    if (!Object.keys(payload).length) {
      $("#edit-task-dialog").close();
      showToast("No task changes to save.");
      return;
    }
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const result = await api(`/api/tasks/${encodeURIComponent(task.id)}/edit`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.overview = result.overview;
      $("#edit-task-dialog").close();
      showToast(result.message);
      renderAll();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    const payload = {
      user_name: form.elements.user_name.value,
      provider: form.elements.provider.value,
      model: form.elements.model.value,
      base_url: form.elements.base_url.value,
      action_command: form.elements.action_command.value,
    };
    submit.disabled = true;
    try {
      const result = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.overview = result.overview;
      showToast(result.message);
      renderAll();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("#diagnostics-button").addEventListener("click", async () => {
    try {
      const result = await api("/api/diagnostics", {
        method: "POST",
        body: JSON.stringify({}),
      });
      renderDiagnostics(result.diagnostics);
      showToast("System checks finished.");
    } catch (error) {
      showToast(error.message, true);
    }
  });
  $("#daily-refresh-select").addEventListener("change", (event) => {
    const label = event.currentTarget.selectedOptions[0]?.textContent || "the selected time";
    $("#automation-description").textContent = `Carmen will refresh automatically every day at ${label} in your local timezone after you save.`;
  });
  $("#save-automation-button").addEventListener("click", async () => {
    const button = $("#save-automation-button");
    button.disabled = true;
    try {
      const result = await api("/api/automation", {
        method: "POST",
        body: JSON.stringify(selectedAutomation()),
      });
      state.overview = result.overview;
      showToast(result.message);
      renderAll();
      scheduleOperationPoll();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#install-schedule-button").addEventListener("click", () => startOperation("schedule"));
  $("#notify-button").addEventListener("click", () => startOperation("notify"));
  $("#personalize-button").addEventListener("click", () => {
    startOperation("personalize_voice", { mode: "profile", refresh: false });
  });
  $("#refresh-voice-button").addEventListener("click", () => {
    startOperation("personalize_voice", { mode: "profile", refresh: true });
  });
  $("#fine-tune-button").addEventListener("click", () => {
    const personalization = state.overview?.personalization || {};
    if (personalization.adapter_ready) {
      startOperation("personalize_voice", { mode: "activate", refresh: false });
      return;
    }
    if (window.confirm("Start private local LoRA training? This can take a while, and the app should stay open until it finishes.")) {
      startOperation("personalize_voice", { mode: "full", refresh: false });
    }
  });
  $("#evaluate-button").addEventListener("click", () => startOperation("evaluate"));

  $("#assistant-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendChat($("#assistant-input").value);
  });
  $("#chat-launcher").addEventListener("click", () => setChatOpen(true));
  $("#chat-close").addEventListener("click", () => setChatOpen(false));
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
      event.preventDefault();
      openQuickActions();
      return;
    }
    if (event.key === "Escape" && state.chatOpen && !document.querySelector("dialog[open]")) {
      setChatOpen(false, { focus: false });
      $("#chat-launcher").focus();
    } else if (event.key === "Escape" && state.mobileDetailOpen && !document.querySelector("dialog[open]")) {
      closeTaskDetail();
    }
  });
  $("#assistant-input").addEventListener("input", (event) => {
    event.currentTarget.style.height = "auto";
    event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 130)}px`;
  });
  $("#assistant-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#assistant-form").requestSubmit();
    }
  });
  $$(".prompt-chips button").forEach((button) => button.addEventListener("click", () => sendChat(button.dataset.prompt)));
}

wireEvents();
loadOverview({ preserveSelection: false });
