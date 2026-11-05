"use strict";

const unlockPanel = document.getElementById("unlock-panel");
const feedPanel = document.getElementById("feed-panel");
const form = document.getElementById("unlock-form");
const tokenInput = document.getElementById("access-token");
const unlockButton = document.getElementById("unlock-button");
const unlockError = document.getElementById("unlock-error");
const messageList = document.getElementById("message-list");
const emptyState = document.getElementById("empty-state");
const feedNote = document.getElementById("feed-note");
const connectionLabel = document.getElementById("connection-label");
const connectionDot = document.getElementById("connection-dot");
const refreshButton = document.getElementById("refresh-button");
const askForm = document.getElementById("ask-form");
const questionInput = document.getElementById("question");
const askButton = document.getElementById("ask-button");
const aiNote = document.getElementById("ai-note");
const aiResults = document.getElementById("ai-results");
let pendingQuestion = null;
let accessToken = "";
let reconnectTimer = null;
let socketTimer = null;
let feedSocket = null;
let reconnectDelay = 1000;
let currentMessages = [];
let currentAnswers = [];
let session = 0;

function setConnection(label, state = "") {
  connectionLabel.textContent = label;
  connectionDot.className = state ? `dot ${state}` : "dot";
}

function lockFeed(message = "") {
  session += 1;
  clearTimeout(reconnectTimer);
  clearTimeout(socketTimer);
  const previousSocket = feedSocket;
  feedSocket = null;
  if (previousSocket) previousSocket.close(1000);
  currentMessages = [];
  currentAnswers = [];
  reconnectDelay = 1000;
  if (pendingQuestion) pendingQuestion.abort();
  pendingQuestion = null;
  accessToken = "";
  tokenInput.value = "";
  messageList.replaceChildren();
  aiResults.replaceChildren();
  questionInput.value = "";
  aiNote.textContent = "";
  askButton.disabled = false;
  refreshButton.disabled = false;
  feedPanel.hidden = true;
  unlockPanel.hidden = false;
  unlockButton.disabled = false;
  unlockError.textContent = message;
  unlockError.hidden = !message;
  setConnection("Locked");
}

function renderMessages(messages) {
  const existing = new Map([...messageList.children].map(node => [node.dataset.ref, node]));
  const byReference = new Map(messages.map(message => [message.message_ref, message]));
  const fragment = document.createDocumentFragment();
  for (const message of messages) {
    let article = existing.get(message.message_ref);
    if (!article) {
      article = document.createElement("article");
      article.className = "message";
      article.dataset.ref = message.message_ref;
      const metadata = document.createElement("div");
      metadata.className = "message-meta";
      const source = document.createElement("span");
      source.className = "source";
      source.textContent = message.sender_name || "Unknown sender";
      const kind = document.createElement("span");
      kind.className = message.sender_kind === "ai" ? "message-kind ai-kind" : "message-kind";
      kind.textContent = message.sender_kind === "ai" ? "AI response" : "Message";
      const time = document.createElement("time");
      time.dateTime = message.received_at;
      time.textContent = new Date(message.received_at).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
      metadata.append(source, kind, time);
      const context = document.createElement("p");
      context.className = "thread-context";
      const text = document.createElement("p");
      text.className = "message-text";
      // Chat is untrusted text. Never interpret it as HTML or executable markup.
      text.textContent = message.text || "[Message has no text]";
      article.append(metadata, context, text);
      if (message.sender_kind !== "ai") {
        const ask = document.createElement("button");
        ask.type = "button";
        ask.className = "secondary ask-message";
        ask.textContent = "Ask Codex about this message";
        ask.addEventListener("click", () => submitQuestion({ source_message_ref: message.message_ref }));
        article.append(ask);
      }
    }
    // Resolve only Switch's explicit thread reference, never the nearest message.
    const context = article.querySelector(".thread-context");
    const root = byReference.get(message.thread_ref);
    context.hidden = !message.thread_ref;
    context.textContent = root
      ? `In thread with ${root.sender_name || "Unknown sender"}: ${root.text.slice(0, 160)}`
      : "Thread reply (earlier context unavailable)";
    fragment.append(article);
  }
  messageList.replaceChildren(fragment);
  emptyState.hidden = messages.length > 0;
}

function connectFeed() {
  if (!accessToken || feedSocket) return;
  clearTimeout(reconnectTimer);
  const currentSession = session;
  const url = new URL("/viewer/ws", window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  let socket;
  try {
    socket = new WebSocket(url);
  } catch {
    lockFeed("Could not open the live feed. Check that FastAPI is running.");
    return;
  }
  feedSocket = socket;
  const isCurrent = () => currentSession === session && feedSocket === socket;
  const armWatchdog = () => {
    clearTimeout(socketTimer);
    socketTimer = setTimeout(() => {
      if (isCurrent()) socket.close(4000);
    }, 45000);
  };
  // Opening and authenticating must complete promptly, even on a broken network.
  socketTimer = setTimeout(() => { if (isCurrent()) socket.close(4000); }, 10000);
  socket.addEventListener("open", () => {
    if (!isCurrent()) { socket.close(1000); return; }
    socket.send(JSON.stringify({ type: "authenticate", token: accessToken }));
    tokenInput.value = "";
  });
  socket.addEventListener("message", event => {
    if (!isCurrent()) return;
    let update;
    try { update = JSON.parse(event.data); } catch { socket.close(4000); return; }
    if (update.type === "ping") {
      socket.send(JSON.stringify({ type: "pong" }));
      armWatchdog();
      return;
    }
    if (update.type === "snapshot") {
      if (!Array.isArray(update.messages) || !Array.isArray(update.answers)) {
        socket.close(4000);
        return;
      }
      currentMessages = update.messages;
      currentAnswers = update.answers;
      renderMessages(currentMessages);
      renderAnswers(currentAnswers);
      unlockPanel.hidden = true;
      feedPanel.hidden = false;
      unlockError.hidden = true;
      refreshButton.disabled = false;
      reconnectDelay = 1000;
      setConnection("Live", "live");
    } else if (update.type === "message_added") {
      currentMessages = [update.message, ...currentMessages.filter(item => item.message_ref !== update.message.message_ref)].slice(0, 100);
      renderMessages(currentMessages);
    } else if (update.type === "message_removed") {
      currentMessages = currentMessages.filter(item => item.message_ref !== update.message_ref);
      renderMessages(currentMessages);
    } else if (["ai_queued", "ai_waiting", "ai_answered", "ai_failed"].includes(update.type)) {
      const index = currentAnswers.findIndex(item => item.request_id === update.answer.request_id);
      if (index >= 0) currentAnswers[index] = update.answer;
      else currentAnswers.unshift(update.answer);
      currentAnswers = currentAnswers.slice(0, 100);
      renderAnswers(currentAnswers);
    } else if (update.type === "ai_removed") {
      currentAnswers = currentAnswers.filter(item => item.request_id !== update.request_id);
      renderAnswers(currentAnswers);
    } else {
      socket.close(4000);
      return;
    }
    armWatchdog();
    feedNote.textContent = `Live updates connected ? Last update ${new Date().toLocaleTimeString()}`;
  });
  socket.addEventListener("close", event => {
    if (!isCurrent()) return;
    feedSocket = null;
    clearTimeout(socketTimer);
    refreshButton.disabled = false;
    if ([4401, 4403, 1008].includes(event.code)) {
      lockFeed("Access was denied or changed. Check your local access token.");
      return;
    }
    if (!unlockPanel.hidden) {
      lockFeed("Could not open the live feed. Check that FastAPI is running.");
      return;
    }
    // Remove stale private data while reconnecting; the next snapshot replaces it.
    currentMessages = [];
    currentAnswers = [];
    renderMessages(currentMessages);
    renderAnswers(currentAnswers);
    setConnection("Reconnecting", "error-dot");
    feedNote.textContent = "Connection interrupted. Reconnecting automatically...";
    reconnectTimer = setTimeout(connectFeed, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  });
}

function refreshFeed() {
  if (!accessToken) return;
  if (feedSocket && feedSocket.readyState === WebSocket.OPEN) {
    feedSocket.send(JSON.stringify({ type: "resync" }));
  } else if (!feedSocket) {
    connectFeed();
  }
}

function renderAnswers(answers) {
  const existing = new Map([...aiResults.children].map(node => [node.dataset.ref, node]));
  const fragment = document.createDocumentFragment();
  for (const answer of answers) {
    let article = existing.get(answer.request_id);
    if (!article) {
      article = document.createElement("article");
      article.className = "ai-result";
      article.dataset.ref = answer.request_id;
      const question = document.createElement("p");
      question.className = "ai-question";
      const body = document.createElement("p");
      body.className = "message-text";
      article.append(question, body);
    }
    const question = article.querySelector(".ai-question");
    const body = article.querySelector(".message-text");
    const questionText = answer.db_action ? `[Decision: ${answer.db_action}] ${answer.question}` : answer.question;
    const statuses = { queued: "Queued for Codex...", waiting: "Waiting for Codex..." };
    const bodyText = answer.status === "answered" ? answer.answer
      : answer.status === "failed" ? answer.error : statuses[answer.status] || "Waiting for an update...";
    if (question.textContent !== questionText) question.textContent = questionText;
    if (body.textContent !== bodyText) body.textContent = bodyText;
    fragment.append(article);
  }
  aiResults.replaceChildren(fragment);
}

async function submitQuestion(source) {
  if (!accessToken || pendingQuestion) return;
  const currentSession = session;
  const controller = new AbortController();
  pendingQuestion = controller;
  askButton.disabled = true;
  aiNote.textContent = "Sending your question...";
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch("/ai/requests", {
      method: "POST", headers: { "X-Ingest-Token": accessToken, "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: crypto.randomUUID(), ...source }),
      cache: "no-store", credentials: "omit", redirect: "error", signal: controller.signal,
    });
    if (currentSession !== session) return;
    if (response.status === 401 || response.status === 403) {
      lockFeed("Access was denied. Check your local access token.");
      return;
    }
    if (!response.ok) {
      const errors = { 404: "That message is no longer in your feed. Refresh and select another.",
        409: "That request ID was already used.", 422: "Enter a question or select a user message.",
        429: "Several questions are already pending. Try again shortly.",
        503: "Codex is not configured yet. Complete the Switch server-client setup." };
      aiNote.textContent = errors[response.status] || "Could not submit your question. Check the server.";
      return;
    }
    questionInput.value = "";
    aiNote.textContent = "Question accepted. The answer will appear here automatically.";
  } catch (error) {
    if (currentSession === session) aiNote.textContent = "Submission could not be confirmed. Check the answers below before trying again.";
  } finally {
    clearTimeout(timeout);
    if (pendingQuestion === controller) pendingQuestion = null;
    if (currentSession === session) askButton.disabled = false;
  }
}

askForm.addEventListener("submit", event => {
  event.preventDefault();
  if (questionInput.value.trim()) submitQuestion({ question: questionInput.value });
});

form.addEventListener("submit", event => {
  event.preventDefault();
  const enteredToken = tokenInput.value;
  if (!enteredToken.trim()) return;
  lockFeed();
  accessToken = enteredToken;
  unlockButton.disabled = true;
  setConnection("Connecting");
  refreshFeed();
});

refreshButton.addEventListener("click", refreshFeed);
document.getElementById("lock-button").addEventListener("click", () => {
  lockFeed();
  tokenInput.focus();
});
// --- Settings Page Logic ---
const settingsPanel = document.getElementById("settings-panel");
const navFeedBtn = document.getElementById("nav-feed-btn");
const navSettingsBtn = document.getElementById("nav-settings-btn");
const engineSupabaseRadio = document.getElementById("engine-supabase");
const engineSqliteRadio = document.getElementById("engine-sqlite");
const sqliteConfigBox = document.getElementById("sqlite-config-box");
const sqliteDirPath = document.getElementById("sqlite-dir-path");
const browseSqliteDirBtn = document.getElementById("browse-sqlite-dir-btn");
const sqliteQuickLocalBtn = document.getElementById("sqlite-quick-local-btn");
const startMigrationBtn = document.getElementById("start-migration-btn");
const migrationLogBox = document.getElementById("migration-log-box");
const migrationProgressBar = document.getElementById("migration-progress-bar");
const migrationLog = document.getElementById("migration-log");
const migrationModal = document.getElementById("migration-modal");
const modalDirInput = document.getElementById("modal-dir-input");
const modalBrowseBtn = document.getElementById("modal-browse-btn");
const modalQuickLocalBtn = document.getElementById("modal-quick-local-btn");
const modalErrorMsg = document.getElementById("modal-error-msg");
const modalProgressWrap = document.getElementById("modal-progress-wrap");
const modalProgressBar = document.getElementById("modal-progress-bar");
const modalProgressText = document.getElementById("modal-progress-text");
const modalCancelBtn = document.getElementById("modal-cancel-btn");
const modalSkipBtn = document.getElementById("modal-skip-btn");
const modalMigrateBtn = document.getElementById("modal-migrate-btn");
const remindersToggle = document.getElementById("reminders-toggle");
const remindersLeadBox = document.getElementById("reminders-lead-time-box");
const reminderLeadTime = document.getElementById("reminder-lead-time");
const reminderLeadUnit = document.getElementById("reminder-lead-unit");
const storageRetentionTime = document.getElementById("storage-retention-time");
const storageRetentionUnit = document.getElementById("storage-retention-unit");
const storageMaxSpace = document.getElementById("storage-max-space");
const storageMaxUnit = document.getElementById("storage-max-unit");
const storageUsageText = document.getElementById("storage-usage-text");
const storageBarFill = document.getElementById("storage-bar-fill");
const envVarList = document.getElementById("env-var-list");
const reloadEnvBtn = document.getElementById("reload-env-btn");
const saveEnvBtn = document.getElementById("save-env-btn");
const copyEnvBtn = document.getElementById("copy-env-btn");
const downloadEnvBtn = document.getElementById("download-env-btn");
const saveSettingsBtn = document.getElementById("save-settings-btn");
const envStatusMsg = document.getElementById("env-status-msg");

const DEFAULT_ENV_KEYS = [
  "SUPABASE_URL",
  "SUPABASE_SECRET_KEY",
  "SUPABASE_SERVICE_ROLE_KEY",
  "SWITCH_INGEST_TOKEN",
  "SWITCH_BRIDGE_URL",
  "SWITCH_TENANT_ID",
  "SWITCH_ROOM_IDS",
  "SWITCH_AGENT_ID",
  "SWITCH_AGENT_API_KEY",
  "SWITCH_REQUESTER_AGENT_ID",
  "SWITCH_REQUESTER_API_KEY",
  "SWITCH_AI_ROOM_ID",
  "SWITCH_AI_AGENT_NAME",
  "SWITCH_AI_SENDER_ID",
  "SWITCH_AI_TIMEOUT_SECONDS",
  "SWITCH_REPLY_TO_DISCORD",
  "SWITCH_BACKEND_URL",
  "ADMIN_API_TOKEN",
  "SQLITE_DB_PATH",
  "LOG_DISCORD_MESSAGES"
];

function switchView(view) {
  sessionStorage.setItem("active_view", view);
  if (view === "settings") {
    navSettingsBtn.classList.add("active");
    navFeedBtn.classList.remove("active");
    settingsPanel.hidden = false;
    unlockPanel.hidden = true;
    feedPanel.hidden = true;
  } else {
    navFeedBtn.classList.add("active");
    navSettingsBtn.classList.remove("active");
    settingsPanel.hidden = true;
    if (accessToken) {
      feedPanel.hidden = false;
      unlockPanel.hidden = true;
    } else {
      unlockPanel.hidden = false;
      feedPanel.hidden = true;
    }
  }
}

navFeedBtn.addEventListener("click", () => switchView("feed"));
navSettingsBtn.addEventListener("click", () => switchView("settings"));

function updateEngineDisplay() {
  const isSqlite = engineSqliteRadio.checked;
  sqliteConfigBox.hidden = false;
  sqliteDirPath.disabled = !isSqlite;
  if (browseSqliteDirBtn) browseSqliteDirBtn.disabled = !isSqlite;
  storageLocationPath.disabled = !isSqlite;
  browseDirBtn.disabled = !isSqlite;

  if (isSqlite) {
    sqliteConfigBox.classList.remove("disabled-box");
  } else {
    sqliteConfigBox.classList.add("disabled-box");
  }

  const dbEngineInput = envVarList.querySelector('.env-val-input[data-key="DATABASE_ENGINE"]');
  if (dbEngineInput) {
    dbEngineInput.value = isSqlite ? "sqlite" : "supabase";
  }

  const sqliteDbPathInput = envVarList.querySelector('.env-val-input[data-key="SQLITE_DB_PATH"]');
  if (sqliteDbPathInput) {
    sqliteDbPathInput.disabled = !isSqlite;
    const parentRow = sqliteDbPathInput.closest(".env-row");
    if (parentRow) {
      if (!isSqlite) {
        parentRow.classList.add("disabled-box");
      } else {
        parentRow.classList.remove("disabled-box");
      }
    }
  }
}

let engineUpdating = false;
async function onEngineChanged() {
  if (engineUpdating) return;

  if (engineSqliteRadio.checked) {
    // Open modal directly to ask user for directory path & migration choice
    const currentPath = (sqliteDirPath.value || storageLocationPath.value || "").trim();
    if (modalDirInput) {
      modalDirInput.value = currentPath;
    }
    modalErrorMsg.hidden = true;
    modalProgressWrap.hidden = true;
    modalMigrateBtn.disabled = false;
    modalSkipBtn.disabled = false;
    modalCancelBtn.disabled = false;
    migrationModal.hidden = false;
    if (modalDirInput) {
      setTimeout(() => modalDirInput.focus(), 50);
    }
    return;
  }

  // Switched back to Supabase
  engineUpdating = true;
  updateEngineDisplay();
  sessionStorage.setItem("active_view", "settings");
  try {
    await fetch("/viewer/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variables: { DATABASE_ENGINE: "supabase" } }),
    });
  } catch (err) {
    console.error("Could not update DATABASE_ENGINE in backend/.env", err);
  } finally {
    window.location.reload();
  }
}

engineSupabaseRadio.addEventListener("change", onEngineChanged);
engineSqliteRadio.addEventListener("change", onEngineChanged);

modalCancelBtn.addEventListener("click", () => {
  migrationModal.hidden = true;
  engineSupabaseRadio.checked = true;
  updateEngineDisplay();
});

modalSkipBtn.addEventListener("click", async () => {
  if (engineUpdating) return;
  const chosenPath = (modalDirInput ? modalDirInput.value : sqliteDirPath.value).trim();
  if (!chosenPath) {
    modalErrorMsg.textContent = "Please enter or browse a folder path for SQLite storage.";
    modalErrorMsg.hidden = false;
    if (modalDirInput) modalDirInput.focus();
    return;
  }

  engineUpdating = true;
  modalSkipBtn.disabled = true;
  modalMigrateBtn.disabled = true;
  modalCancelBtn.disabled = true;

  sessionStorage.setItem("active_view", "settings");
  try {
    await fetch("/viewer/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        variables: {
          DATABASE_ENGINE: "sqlite",
          SQLITE_DB_PATH: chosenPath,
        },
      }),
    });
    window.location.reload();
  } catch (err) {
    modalErrorMsg.textContent = "Failed to update configuration: " + err.message;
    modalErrorMsg.hidden = false;
    modalSkipBtn.disabled = false;
    modalMigrateBtn.disabled = false;
    modalCancelBtn.disabled = false;
    engineUpdating = false;
  }
});

modalMigrateBtn.addEventListener("click", async () => {
  if (engineUpdating) return;
  const chosenPath = (modalDirInput ? modalDirInput.value : sqliteDirPath.value).trim();
  if (!chosenPath) {
    modalErrorMsg.textContent = "Please enter or browse a folder path before migrating data.";
    modalErrorMsg.hidden = false;
    if (modalDirInput) modalDirInput.focus();
    return;
  }

  engineUpdating = true;
  modalMigrateBtn.disabled = true;
  modalSkipBtn.disabled = true;
  modalCancelBtn.disabled = true;
  modalErrorMsg.hidden = true;
  modalProgressWrap.hidden = false;
  modalProgressBar.style.width = "20%";
  modalProgressText.textContent = "Saving directory path and connecting to Supabase...";

  try {
    // 1. Save SQLITE_DB_PATH first so backend migration knows the user-selected path
    await fetch("/viewer/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variables: { SQLITE_DB_PATH: chosenPath } }),
    });

    modalProgressBar.style.width = "50%";
    modalProgressText.textContent = "Migrating database records & embeddings...";

    // 2. Run backend migration
    const resp = await fetch("/viewer/migrate-db", { method: "POST" });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      throw new Error(errData.detail || "Database migration failed on server");
    }
    const res = await resp.json();

    modalProgressBar.style.width = "90%";
    modalProgressText.textContent = "Switching active engine to SQLite...";

    // 3. Switch engine to sqlite
    await fetch("/viewer/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variables: { DATABASE_ENGINE: "sqlite" } }),
    });

    modalProgressBar.style.width = "100%";
    modalProgressText.textContent = "Migration complete! Reloading...";
    sessionStorage.setItem("active_view", "settings");
    setTimeout(() => { window.location.reload(); }, 600);
  } catch (err) {
    modalProgressWrap.hidden = true;
    modalErrorMsg.textContent = err.message || "Failed to migrate database";
    modalErrorMsg.hidden = false;
    modalMigrateBtn.disabled = false;
    modalSkipBtn.disabled = false;
    modalCancelBtn.disabled = false;
    engineUpdating = false;
  }
});

const storageLocationPath = document.getElementById("storage-location-path");
const browseDirBtn = document.getElementById("browse-dir-btn");
const dirPickerInput = document.getElementById("dir-picker-input");

function applyChosenPath(chosenPath) {
  if (!chosenPath) return;
  const pathStr = String(chosenPath).trim();
  if (modalDirInput) {
    modalDirInput.value = pathStr;
  }
  if (sqliteDirPath) {
    sqliteDirPath.value = pathStr;
  }
  if (storageLocationPath) {
    storageLocationPath.value = pathStr;
  }
  const envInput = envVarList.querySelector('.env-val-input[data-key="SQLITE_DB_PATH"]');
  if (envInput) envInput.value = pathStr;
}

sqliteDirPath.addEventListener("input", () => {
  storageLocationPath.value = sqliteDirPath.value;
  if (modalDirInput) modalDirInput.value = sqliteDirPath.value;
  const envInput = envVarList.querySelector('.env-val-input[data-key="SQLITE_DB_PATH"]');
  if (envInput) envInput.value = sqliteDirPath.value;
});
storageLocationPath.addEventListener("input", () => {
  sqliteDirPath.value = storageLocationPath.value;
  if (modalDirInput) modalDirInput.value = storageLocationPath.value;
  const envInput = envVarList.querySelector('.env-val-input[data-key="SQLITE_DB_PATH"]');
  if (envInput) envInput.value = storageLocationPath.value;
});
if (modalDirInput) {
  modalDirInput.addEventListener("input", () => {
    sqliteDirPath.value = modalDirInput.value;
    storageLocationPath.value = modalDirInput.value;
    const envInput = envVarList.querySelector('.env-val-input[data-key="SQLITE_DB_PATH"]');
    if (envInput) envInput.value = modalDirInput.value;
  });
}

async function pickDirectory() {
  if (window.showDirectoryPicker) {
    try {
      const handle = await window.showDirectoryPicker();
      if (handle && handle.name) {
        applyChosenPath(`${handle.name}/team_memory.db`);
        return;
      }
    } catch (err) {
      if (err.name === "AbortError") return;
    }
  }

  // Fallback: prompt user for folder or database file path
  const current = (modalDirInput ? modalDirInput.value : "") || (sqliteDirPath ? sqliteDirPath.value : "") || "backend/.local/team_memory.db";
  const userEntered = prompt("Enter local SQLite storage folder or file path:", current);
  if (userEntered && userEntered.trim()) {
    applyChosenPath(userEntered.trim());
  }
}

browseDirBtn.addEventListener("click", pickDirectory);
if (browseSqliteDirBtn) browseSqliteDirBtn.addEventListener("click", pickDirectory);
if (modalBrowseBtn) modalBrowseBtn.addEventListener("click", pickDirectory);
if (sqliteQuickLocalBtn) {
  sqliteQuickLocalBtn.addEventListener("click", () => applyChosenPath("backend/.local/team_memory.db"));
}
if (modalQuickLocalBtn) {
  modalQuickLocalBtn.addEventListener("click", () => applyChosenPath("backend/.local/team_memory.db"));
}

remindersToggle.addEventListener("change", () => {
  remindersLeadBox.style.opacity = remindersToggle.checked ? "1" : "0.45";
  remindersLeadBox.style.pointerEvents = remindersToggle.checked ? "auto" : "none";
});

function updateStorageBar() {
  const maxVal = parseFloat(storageMaxSpace.value) || 500;
  const unit = storageMaxUnit.value;
  const maxMB = unit === "GB" ? maxVal * 1024 : maxVal;
  const simulatedUsedMB = 14.2;
  const pct = Math.min(100, Math.max(0.5, (simulatedUsedMB / maxMB) * 100));
  storageUsageText.textContent = `${simulatedUsedMB} MB / ${maxVal} ${unit} (${pct.toFixed(1)}%)`;
  storageBarFill.style.width = `${pct}%`;
}
storageMaxSpace.addEventListener("input", updateStorageBar);
storageMaxUnit.addEventListener("change", updateStorageBar);

let migrationInProgress = false;
startMigrationBtn.addEventListener("click", async () => {
  if (migrationInProgress) return;
  const chosenPath = (sqliteDirPath.value || storageLocationPath.value || "").trim();
  if (!chosenPath) {
    alert("Please select or enter a SQLite storage directory path first.");
    sqliteDirPath.focus();
    return;
  }

  migrationInProgress = true;
  startMigrationBtn.disabled = true;
  migrationLogBox.hidden = false;
  migrationProgressBar.style.width = "10%";
  migrationLog.textContent = `[1/4] Preparing SQLite database at ${chosenPath}...\n`;

  try {
    await fetch("/viewer/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variables: { SQLITE_DB_PATH: chosenPath } }),
    });

    migrationProgressBar.style.width = "40%";
    migrationLog.textContent += "[2/4] Connecting to Supabase and reading tables...\n";

    const resp = await fetch("/viewer/migrate-db", { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "Server migration error");
    }
    const res = await resp.json();

    migrationProgressBar.style.width = "80%";
    migrationLog.textContent += "[3/4] Migrating records into SQLite schema:\n";
    if (res.counts) {
      Object.entries(res.counts).forEach(([tbl, cnt]) => {
        migrationLog.textContent += `  • ${tbl}: ${cnt} rows\n`;
      });
    }

    migrationProgressBar.style.width = "100%";
    migrationLog.textContent += `[4/4] ✓ Migration complete! Data saved at ${res.target_path || chosenPath}\n`;
  } catch (err) {
    migrationLog.textContent += `\n❌ Migration failed: ${err.message}\n`;
  } finally {
    startMigrationBtn.disabled = false;
    migrationInProgress = false;
  }
});

function createEnvRow(key = "", val = "", optionalText = "") {
  const row = document.createElement("div");
  row.className = "env-row";

  const keyWrap = document.createElement("div");
  keyWrap.className = "env-key-wrap";

  const keyLabel = document.createElement("span");
  keyLabel.className = "env-key-label";
  keyLabel.textContent = key;
  keyLabel.title = key;
  keyWrap.appendChild(keyLabel);

  if (optionalText) {
    const optBadge = document.createElement("span");
    optBadge.className = "optional-badge";
    optBadge.textContent = "Optional";
    optBadge.title = optionalText;
    keyWrap.appendChild(optBadge);
  }

  const valInput = document.createElement("input");
  valInput.type = "password"; // All values hidden by default
  valInput.className = "env-val-input";
  valInput.dataset.key = key;
  valInput.placeholder = optionalText ? "Optional" : "Required";
  valInput.value = val || "";

  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "secondary btn-icon";
  toggleBtn.title = "Toggle visibility";
  toggleBtn.textContent = "Show";
  toggleBtn.addEventListener("click", () => {
    const isPass = valInput.type === "password";
    valInput.type = isPass ? "text" : "password";
    toggleBtn.textContent = isPass ? "Hide" : "Show";
  });

  row.append(keyWrap, valInput, toggleBtn);
  return row;
}

function parseEnvText(text) {
  const map = {};
  for (let line of (text || "").split(/\r?\n/)) {
    line = line.trim();
    if (!line || line.startsWith("#")) continue;
    const idx = line.indexOf("=");
    if (idx !== -1) {
      const k = line.slice(0, idx).trim();
      let v = line.slice(idx + 1).trim();
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1);
      }
      map[k] = v;
    }
  }
  return map;
}

function applyEnvValues(valuesMap) {
  const inputs = envVarList.querySelectorAll(".env-val-input");
  inputs.forEach(input => {
    const k = input.dataset.key;
    if (k in valuesMap) {
      input.value = valuesMap[k];
    }
  });
  saveEnvValues();
}

function saveEnvValues() {
  const inputs = envVarList.querySelectorAll(".env-val-input");
  const valuesMap = {};
  inputs.forEach(input => {
    valuesMap[input.dataset.key] = input.value;
  });
  try {
    localStorage.setItem("team_memory_env_values", JSON.stringify(valuesMap));
  } catch {}
}

async function loadEnvVars() {
  envVarList.replaceChildren();
  let optionalMap = {};
  let loadedVars = {};

  try {
    const res = await fetch("/viewer/env");
    if (res.ok) {
      const data = await res.json();
      if (data && data.variables) {
        loadedVars = data.variables;
        optionalMap = data.optional_keys || {};
      }
    }
  } catch {}

  // Determine current db engine from .env: defaults to sqlite
  const engineVal = (loadedVars["DATABASE_ENGINE"] || "").toLowerCase().trim();
  if (engineVal === "supabase") {
    engineSupabaseRadio.checked = true;
  } else {
    engineSqliteRadio.checked = true;
    if (!loadedVars["DATABASE_ENGINE"]) {
      loadedVars["DATABASE_ENGINE"] = "sqlite";
    }
  }

  const pathVal = loadedVars["SQLITE_DB_PATH"] || "";
  sqliteDirPath.value = pathVal;
  storageLocationPath.value = pathVal;

  // Populate rows
  Object.entries(loadedVars).forEach(([key, val]) => {
    const optText = optionalMap[key] || "";
    envVarList.appendChild(createEnvRow(key, val, optText));
  });

  updateEngineDisplay();
  envStatusMsg.textContent = "✓ Environment variables loaded from backend/.env";
  setTimeout(() => { envStatusMsg.textContent = ""; }, 3000);
}

async function saveEnvToServer() {
  const inputs = envVarList.querySelectorAll(".env-val-input");
  const variables = {};
  inputs.forEach(input => {
    variables[input.dataset.key] = input.value;
  });

  try {
    const res = await fetch("/viewer/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variables })
    });
    if (res.ok) {
      envStatusMsg.textContent = "✓ Saved directly into backend/.env file!";
      return true;
    } else {
      envStatusMsg.textContent = "Failed to save into backend/.env";
      return false;
    }
  } catch {
    envStatusMsg.textContent = "Error connecting to server to save .env";
    return false;
  } finally {
    setTimeout(() => { envStatusMsg.textContent = ""; }, 3500);
  }
}

reloadEnvBtn.addEventListener("click", () => {
  loadEnvVars();
});

saveEnvBtn.addEventListener("click", () => {
  saveEnvToServer();
});

function getEnvContent() {
  const inputs = envVarList.querySelectorAll(".env-val-input");
  const lines = [
    "# Generated by Team Memory Settings UI",
    "# Stored in backend/.env (ignored by Git)"
  ];
  inputs.forEach(input => {
    const k = input.dataset.key;
    const v = input.value;
    lines.push(`${k}=${v}`);
  });
  return lines.join("\n") + "\n";
}

copyEnvBtn.addEventListener("click", async () => {
  const content = getEnvContent();
  try {
    await navigator.clipboard.writeText(content);
    envStatusMsg.textContent = "✓ .env content copied to clipboard!";
  } catch {
    envStatusMsg.textContent = "Could not copy automatically. Select and copy from output.";
  }
  setTimeout(() => { envStatusMsg.textContent = ""; }, 3500);
});

downloadEnvBtn.addEventListener("click", () => {
  const content = getEnvContent();
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = ".env";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  envStatusMsg.textContent = "✓ Downloaded .env file!";
  setTimeout(() => { envStatusMsg.textContent = ""; }, 3500);
});

saveSettingsBtn.addEventListener("click", async () => {
  await saveEnvToServer();

  const settingsData = {
    dbEngine: engineSqliteRadio.checked ? "sqlite" : "supabase",
    sqlitePath: sqliteDirPath.value.trim(),
    remindersEnabled: remindersToggle.checked,
    reminderLeadTime: reminderLeadTime.value,
    reminderLeadUnit: reminderLeadUnit.value,
    retentionTime: storageRetentionTime.value,
    retentionUnit: storageRetentionUnit.value,
    maxSpace: storageMaxSpace.value,
    maxSpaceUnit: storageMaxUnit.value
  };

  try {
    localStorage.setItem("team_memory_settings", JSON.stringify(settingsData));
    saveSettingsBtn.textContent = "✓ Saved All!";
    setTimeout(() => { saveSettingsBtn.textContent = "Save Settings"; }, 2000);
  } catch {}
});

function loadSavedSettings() {
  try {
    const saved = localStorage.getItem("team_memory_settings");
    if (!saved) return;
    const data = JSON.parse(saved);
    if (data.dbEngine === "sqlite") {
      engineSqliteRadio.checked = true;
    } else {
      engineSupabaseRadio.checked = true;
    }
    updateEngineDisplay();
    if (data.sqlitePath) {
      sqliteDirPath.value = data.sqlitePath;
      storageLocationPath.value = data.sqlitePath;
    }
    if (typeof data.remindersEnabled === "boolean") {
      remindersToggle.checked = data.remindersEnabled;
      remindersLeadBox.style.opacity = data.remindersEnabled ? "1" : "0.45";
      remindersLeadBox.style.pointerEvents = data.remindersEnabled ? "auto" : "none";
    }
    if (data.reminderLeadTime) reminderLeadTime.value = data.reminderLeadTime;
    if (data.reminderLeadUnit) reminderLeadUnit.value = data.reminderLeadUnit;
    if (data.retentionTime) storageRetentionTime.value = data.retentionTime;
    if (data.retentionUnit) storageRetentionUnit.value = data.retentionUnit;
    if (data.maxSpace) storageMaxSpace.value = data.maxSpace;
    if (data.maxSpaceUnit) storageMaxUnit.value = data.maxSpaceUnit;
    updateStorageBar();
  } catch {}
}

loadEnvVars();
loadSavedSettings();
updateEngineDisplay();

if (sessionStorage.getItem("active_view") === "settings" || window.location.hash === "#settings") {
  switchView("settings");
}

window.addEventListener("pagehide", () => lockFeed());
window.addEventListener("pageshow", event => { if (event.persisted) lockFeed(); });
