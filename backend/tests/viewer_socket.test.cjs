// Offline viewer lifecycle tests using Node's built-in runner and a small DOM/socket harness.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(fragment = false) {
    this.children = [];
    this.dataset = {};
    this.listeners = new Map();
    this.fragment = fragment;
    this.hidden = false;
    this.value = "";
    this.textContent = "";
    const classes = new Set();
    this.classList = {
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
    };
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node.fragment) { this.append(...[...node.children]); continue; }
      if (node.parent) node.parent.children = node.parent.children.filter(child => child !== node);
      node.parent = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this.append(...nodes);
  }
  querySelector(selector) {
    const className = selector.slice(1);
    for (const child of this.children) {
      if ((child.className || "").split(" ").includes(className)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  emit(name, event = {}) {
    for (const callback of this.listeners.get(name) || []) callback({ preventDefault() {}, ...event });
  }
  focus() {}
}

function harness(responses = []) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const sockets = [], timers = new Map(), requests = [], clearedCaches = [];
  let nextTimer = 0;
  class Socket extends Element {
    static OPEN = 1;
    constructor(url) {
      super();
      this.url = String(url);
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
    }
    open() { this.readyState = Socket.OPEN; this.emit("open"); }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    server(payload) { this.emit("message", { data: JSON.stringify(payload) }); }
    close(code = 1000) { this.readyState = 3; this.emit("close", { code }); }
  }
  const window = new Element();
  window.location = { href: "http://localhost:8000/", protocol: "http:" };
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => new Element(),
                createDocumentFragment: () => new Element(true) },
    window, WebSocket: Socket, URL, AbortController,
    localStorage: { removeItem(key) { clearedCaches.push(key); } },
    crypto: { randomUUID: () => "fixture-request-id" },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) { requests.push({ url, options }); return responses.shift() || { ok: true, status: 202 }; },
  });
  element("feed-panel").hidden = true;
  vm.runInContext(readFileSync(join(__dirname, "../app/views/messages.js"), "utf8"), context);
  return {
    element, sockets, timers, requests, context, window, clearedCaches,
    unlock() { element("access-token").value = "fictional-viewer-token"; element("unlock-form").emit("submit"); },
    snapshot(socket = sockets.at(-1), messages = [], answers = []) {
      socket.server({ type: "snapshot", connection_id: "fixture-connection", messages, answers });
    },
    timer(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `Expected timer with delay ${delay}`);
      timers.delete(entry[0]);
      entry[1].callback();
    },
  };
}

const message = { message_ref: "a".repeat(24), sender_name: "Person", sender_kind: "message",
                  received_at: "2026-11-05T12:00:00Z", text: "<img src=x onerror=alert(1)>", thread_ref: null };

test("unlock authenticates over a credential-free socket URL; updates require no HTTP polling", () => {
  const view = harness();
  assert.equal(view.sockets.length, 0);
  view.unlock();
  const socket = view.sockets[0];
  assert.equal(socket.url, "ws://localhost:8000/viewer/ws");
  socket.open();
  assert.deepEqual(socket.sent[0], { type: "authenticate", token: "fictional-viewer-token" });
  assert.equal(view.element("access-token").value, "");
  view.snapshot();
  socket.server({ type: "message_added", message });
  socket.server({ type: "message_added", message });
  assert.equal(view.element("message-list").children.length, 1);
  assert.equal(view.element("message-list").children[0].querySelector(".message-text").textContent, message.text);
  socket.server({ type: "ping" });
  assert.deepEqual(socket.sent.at(-1), { type: "pong" });
  view.element("refresh-button").emit("click");
  assert.deepEqual(socket.sent.at(-1), { type: "resync" });
  assert.equal(view.requests.length, 0);
  assert.equal(view.element("connection-label").textContent, "Live");
});

test("lock closes the socket, clears data and timers, and ignores stale events", () => {
  const view = harness();
  view.unlock();
  const socket = view.sockets[0];
  socket.open();
  view.snapshot(socket, [message]);
  view.element("lock-button").emit("click");
  socket.server({ type: "message_added", message });
  assert.equal(socket.readyState, 3);
  assert.equal(view.element("message-list").children.length, 0);
  assert.equal(view.element("ai-results").children.length, 0);
  assert.equal(view.element("connection-label").textContent, "Locked");
  assert.equal(vm.runInContext("accessToken", view.context), "");
  assert.equal(view.timers.size, 0);
});

test("disconnect reconnects once with a fresh snapshot; revoked access locks without retry", () => {
  const view = harness();
  view.unlock();
  const first = view.sockets[0];
  first.open();
  view.snapshot(first, [message]);
  first.close(1006);
  assert.equal(view.element("message-list").children.length, 0);
  assert.equal(view.element("connection-label").textContent, "Reconnecting");
  view.timer(1000);
  const second = view.sockets[1];
  assert.notEqual(first, second);
  second.open();
  view.snapshot(second, [message]);
  first.server({ type: "message_removed", message_ref: message.message_ref });
  assert.equal(view.element("message-list").children.length, 1);
  second.close(4401);
  assert.equal(view.element("connection-label").textContent, "Locked");
  assert.equal(view.timers.size, 0);
});

test("AI updates reuse result elements and submissions stay on authenticated HTTP", async () => {
  const view = harness();
  view.unlock();
  const socket = view.sockets[0];
  socket.open();
  view.snapshot();
  const answer = { request_id: "fixture-request-id", question: "Question", status: "queued" };
  socket.server({ type: "ai_queued", answer });
  const article = view.element("ai-results").children[0];
  socket.server({ type: "ai_answered", answer: { ...answer, status: "answered", answer: "Done" } });
  assert.equal(view.element("ai-results").children[0], article);
  assert.equal(article.querySelector(".message-text").textContent, "Done");
  await vm.runInContext("submitQuestion({ question: 'Question' })", view.context);
  assert.equal(view.requests.length, 1);
  assert.equal(view.requests[0].url, "/ai/requests");
  assert.equal(view.requests[0].options.headers["X-Ingest-Token"], "fictional-viewer-token");
});

test("each tab owns a socket; page exit locks only that tab", () => {
  const first = harness(), second = harness();
  first.unlock(); second.unlock();
  first.sockets[0].open(); second.sockets[0].open();
  first.snapshot(); second.snapshot();
  first.window.emit("pagehide");
  assert.equal(first.sockets[0].readyState, 3);
  assert.equal(second.sockets[0].readyState, 1);
});


test("settings navigation does not fetch credentials before admin unlock", () => {
  const view = harness();
  assert.deepEqual(view.clearedCaches, ["team_memory_env_values"]);
  view.element("nav-settings-btn").emit("click");
  assert.equal(view.element("settings-panel").hidden, false);
  assert.equal(view.element("unlock-panel").hidden, true);
  assert.equal(view.requests.length, 0);
  view.element("nav-feed-btn").emit("click");
  assert.equal(view.element("settings-panel").hidden, true);
  assert.equal(view.element("unlock-panel").hidden, false);
});


test("credential settings use admin authentication, keep saved secrets blank and clear after save", async () => {
  const view = harness([
    { ok: true, json: async () => ({ variables: { SWITCH_BACKEND_URL: "http://localhost:8000" },
      credentials: { SUPABASE_SECRET_KEY: true } }) },
    { ok: true },
  ]);
  view.element("settings-admin-token").value = "fictional-admin-token";
  await vm.runInContext("unlockSettings()", view.context);
  assert.equal(view.requests[0].options.headers["X-Admin-Token"], "fictional-admin-token");
  assert.equal(view.element("settings-admin-token").value, "");
  const input = view.element("credential-fields").children[1].children[1];
  assert.equal(input.type, "password");
  assert.equal(input.value, "");
  assert.match(input.placeholder, /Configured/);
  input.value = "fictional-replacement";
  await vm.runInContext("saveCredentialSettings()", view.context);
  assert.equal(JSON.parse(view.requests[1].options.body).variables.SUPABASE_SECRET_KEY, "fictional-replacement");
  assert.equal(input.value, "");
  assert.equal(view.element("credential-editor").hidden, true);
  assert.equal(vm.runInContext("settingsToken", view.context), "");
});
