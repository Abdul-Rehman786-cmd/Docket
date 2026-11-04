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
let timer = null;
let pendingRequest = null;
let session = 0;

function setConnection(label, state = "") {
  connectionLabel.textContent = label;
  connectionDot.className = state ? `dot ${state}` : "dot";
}

function lockFeed(message = "") {
  session += 1;
  clearTimeout(timer);
  if (pendingRequest) pendingRequest.abort();
  pendingRequest = null;
  if (pendingQuestion) pendingQuestion.abort();
  pendingQuestion = null;
  accessToken = "";
  tokenInput.value = "";
  messageList.replaceChildren();
  aiResults.replaceChildren();
  questionInput.value = "";
  aiNote.textContent = "";
  askButton.disabled = false;
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

async function refreshFeed() {
  if (!accessToken || pendingRequest) return;
  clearTimeout(timer);
  const currentSession = session;
  const controller = new AbortController();
  pendingRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 10000);
  refreshButton.disabled = true;
  try {
    const response = await fetch("/messages/recent", {
      headers: { "X-Ingest-Token": accessToken }, cache: "no-store",
      credentials: "omit", redirect: "error", signal: controller.signal,
    });
    if (currentSession !== session) return;
    if (response.status === 401 || response.status === 403) {
      lockFeed("Access was denied. Check your local access token.");
      return;
    }
    if (!response.ok) throw new Error("Feed unavailable");
    const messages = await response.json();
    if (currentSession !== session) return;
    if (!Array.isArray(messages)) throw new Error("Invalid feed response");
    renderMessages(messages);
    const answersResponse = await fetch("/ai/requests", {
      headers: { "X-Ingest-Token": accessToken }, cache: "no-store",
      credentials: "omit", redirect: "error", signal: controller.signal,
    });
    if (currentSession !== session) return;
    if (answersResponse.status === 401 || answersResponse.status === 403) {
      lockFeed("Access was denied. Check your local access token.");
      return;
    }
    if (answersResponse.ok) {
      const answers = await answersResponse.json();
      if (currentSession !== session) return;
      if (Array.isArray(answers)) renderAnswers(answers);
    }
    tokenInput.value = "";
    unlockPanel.hidden = true;
    feedPanel.hidden = false;
    unlockError.hidden = true;
    setConnection("Live", "live");
    feedNote.textContent = `Updates every 2 seconds · Last checked ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (currentSession !== session) return;
    if (!unlockPanel.hidden) {
      lockFeed("Could not reach the message feed. Check that FastAPI is running.");
    } else {
      setConnection("Reconnecting", "error-dot");
      feedNote.textContent = "Connection interrupted. Retrying automatically…";
    }
  } finally {
    clearTimeout(timeout);
    if (pendingRequest === controller) pendingRequest = null;
    refreshButton.disabled = false;
    if (currentSession === session && accessToken) timer = setTimeout(refreshFeed, 2000);
  }
}

function renderAnswers(answers) {
  const fragment = document.createDocumentFragment();
  for (const answer of answers) {
    const article = document.createElement("article");
    article.className = "ai-result";
    const question = document.createElement("p");
    question.className = "ai-question";
    question.textContent = answer.db_action ? `[Decision: ${answer.db_action}] ${answer.question}` : answer.question;
    const body = document.createElement("p");
    body.className = "message-text";
    const statuses = { queued: "Queued for Codex…", waiting: "Waiting for Codex…" };
    body.textContent = answer.status === "answered" ? answer.answer
      : answer.status === "failed" ? answer.error : statuses[answer.status] || "Waiting for an update…";
    article.append(question, body);
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
  aiNote.textContent = "Sending your question…";
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
    refreshFeed();
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
