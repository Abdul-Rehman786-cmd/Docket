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
window.addEventListener("pagehide", () => lockFeed());
window.addEventListener("pageshow", event => { if (event.persisted) lockFeed(); });
