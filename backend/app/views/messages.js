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
  accessToken = "";
  tokenInput.value = "";
  messageList.replaceChildren();
  feedPanel.hidden = true;
  unlockPanel.hidden = false;
  unlockButton.disabled = false;
  unlockError.textContent = message;
  unlockError.hidden = !message;
  setConnection("Locked");
}

function renderMessages(messages) {
  const existing = new Map([...messageList.children].map(node => [node.dataset.ref, node]));
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
      const time = document.createElement("time");
      time.dateTime = message.received_at;
      time.textContent = new Date(message.received_at).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
      metadata.append(source, time);
      const text = document.createElement("p");
      text.className = "message-text";
      // Chat is untrusted text. Never interpret it as HTML or executable markup.
      text.textContent = message.text || "[Message has no text]";
      article.append(metadata, text);
    }
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
