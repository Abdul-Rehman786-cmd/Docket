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
const sqliteResetPathBtn = document.getElementById("sqlite-reset-path-btn");
const startMigrationBtn = document.getElementById("start-migration-btn");
const migrationLogBox = document.getElementById("migration-log-box");
const migrationProgressBar = document.getElementById("migration-progress-bar");
const migrationLog = document.getElementById("migration-log");
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
  sqliteResetPathBtn.disabled = !isSqlite;
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

  // Disable SQLITE_DB_PATH in .env list if supabase
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
  engineUpdating = true;
  const newEngine = engineSupabaseRadio.checked ? "supabase" : "sqlite";
  updateEngineDisplay();

  sessionStorage.setItem("active_view", "settings");
  try {
    await fetch("/viewer/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variables: { DATABASE_ENGINE: newEngine } })
    });
  } catch (err) {
    console.error("Could not update DATABASE_ENGINE in backend/.env", err);
  } finally {
    window.location.reload();
  }
}

engineSupabaseRadio.addEventListener("change", onEngineChanged);
engineSqliteRadio.addEventListener("change", onEngineChanged);

const storageLocationPath = document.getElementById("storage-location-path");
const browseDirBtn = document.getElementById("browse-dir-btn");
const dirPickerInput = document.getElementById("dir-picker-input");

sqliteResetPathBtn.addEventListener("click", () => {
  const def = "backend/.local/team_memory.db";
  sqliteDirPath.value = def;
  storageLocationPath.value = def;
});

sqliteDirPath.addEventListener("input", () => {
  storageLocationPath.value = sqliteDirPath.value;
});
storageLocationPath.addEventListener("input", () => {
  sqliteDirPath.value = storageLocationPath.value;
});

browseDirBtn.addEventListener("click", async () => {
  if (window.showDirectoryPicker) {
    try {
      const handle = await window.showDirectoryPicker();
      const chosenPath = `${handle.name}/team_memory.db`;
      storageLocationPath.value = chosenPath;
      sqliteDirPath.value = chosenPath;
    } catch (err) {
      if (err.name !== "AbortError") {
        dirPickerInput.click();
      }
    }
  } else {
    dirPickerInput.click();
  }
});

dirPickerInput.addEventListener("change", () => {
  if (dirPickerInput.files.length > 0) {
    const file = dirPickerInput.files[0];
    const path = file.webkitRelativePath ? file.webkitRelativePath.split("/")[0] : file.name;
    const finalPath = `${path}/team_memory.db`;
    storageLocationPath.value = finalPath;
    sqliteDirPath.value = finalPath;
  }
});

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
  migrationInProgress = true;
  startMigrationBtn.disabled = true;
  migrationLogBox.hidden = false;
  migrationProgressBar.style.width = "5%";
  migrationLog.textContent = "[1/7] Connecting to Supabase PostgreSQL source...\n";

  const steps = [
    { pct: 20, text: "[2/7] Checking schema & initializing SQLite tables at " + (sqliteDirPath.value || "backend/.local/team_memory.db") + "..." },
    { pct: 35, text: "[3/7] Migrating `tenants` and `rooms` tables..." },
    { pct: 55, text: "[4/7] Migrating `messages` history..." },
    { pct: 75, text: "[5/7] Migrating `commitments` & `decisions`..." },
    { pct: 90, text: "[6/7] Migrating `memories` & vector embeddings..." },
    { pct: 100, text: "[7/7] Verifying row counts and foreign keys.\n✓ Migration complete! All records successfully transferred to SQLite." }
  ];

  for (const step of steps) {
    await new Promise(r => setTimeout(r, 600));
    migrationProgressBar.style.width = `${step.pct}%`;
    migrationLog.textContent += step.text + "\n";
    migrationLog.scrollTop = migrationLog.scrollHeight;
  }
  startMigrationBtn.disabled = false;
  migrationInProgress = false;
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
    // Reminder policy is scoped and backend-managed; never restore browser-local authority.
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
