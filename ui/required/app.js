/*
 * Handshake web client: the main controller.
 *
 *   api.js             REST calls (login, users)
 *   transport-ws.js    Protocol 1: HSP over WebSocket   } same interface,
 *   transport-http.js  Protocol 2: HTTP POST + SSE       } swappable at runtime
 *   store.js           local cache + outbox (no loss / no duplicates)
 *   crypto.js          AES-GCM end-to-end encryption
 *   sound.js           message sound   ·   password.js   sign-up strength meter
 *   app.js (this)      state, UI rendering, event wiring
 */

import { api } from "./api.js";
import { WsTransport } from "./transport-ws.js";
import { HttpTransport } from "./transport-http.js";
import { Store, settings, messageKey } from "./store.js";
import * as e2ee from "../extras/crypto.js";          // extra: secret chats
import { $, el, icon, avatar, formatTime, dayLabel, shortStamp, uuid, lastSeenText, formatBytes } from "./utils.js";
import { playPop } from "../extras/sound.js";          // extra: message sound
import { passwordStrength } from "../extras/password.js"; // extra: sign-up hint

const RETRY_MS = 4000;      // resend an un-ACKed message after this long
const FLUSH_EVERY_MS = 2000;
const TYPING_SHOW_MS = 4500; // how long "typing…" stays after the last signal
const TYPING_SEND_MS = 2500; // send at most one TYPING signal per 2.5 s
const APP_VERSION = "1.2";

const state = {
  me: null,               // { username, display_name }
  users: new Map(),       // username -> { username, display_name, online }
  store: null,
  transport: null,
  status: "connecting",
  rtt: null,
  peer: null,             // username of the open chat
  plain: new Map(),       // messageKey -> decrypted text (null = failed)
  inFlight: new Map(),    // clientMsgId -> time of last send attempt
  sendStarted: new Map(), // clientMsgId -> performance.now() of last attempt
  shown: new Set(),       // messages already on screen (only NEW ones get the pop-in animation)
  syncing: true,          // true until SYNCED: no sounds/banners for old messages
  typing: new Map(),      // username -> time until which "typing…" is shown
  receiptsSent: { delivered: new Map(), read: new Map() }, // peer -> highest id reported
  unreadMarker: null,     // id after which the "unread messages" divider is drawn
  newBelow: 0,            // new messages that arrived while scrolled up
  lastTypingSent: 0,
};

// Users see the friendly name; the technical name appears only under Settings -> Advanced.
const MODE_NAME = { ws: "Fast", http: "Compatibility" };
const MODE_TECH = { ws: "HSP/1.0 over WebSocket", http: "HTTP + Server-Sent Events" };

// Opened through the remote-access link (python -m server.main --share)?
// The free Cloudflare tunnel buffers Server-Sent Events, so remote phones
// always use Fast mode (WebSocket), which the tunnel passes through untouched.
const REMOTE = location.hostname.endsWith(".trycloudflare.com");

// =================================================================== boot ===

applyAppearance();
boot();

async function boot() {
  try {
    const { user } = await api.me();
    enterApp(user);
  } catch {
    showAuth();
  } finally {
    $("#boot").classList.add("gone");
  }
}

// =================================================================== auth ===

let authMode = "login";

function showAuth() {
  $("#auth").hidden = false;
  $("#app").hidden = true;
  const secure = location.protocol === "https:";
  $("#secure-badge").replaceChildren(icon(secure ? "lock" : "unlock"), secure ? "Secure connection" : "Not secure");
  $("#secure-badge").classList.toggle("warn", !secure);
  $("#server-host").textContent = REMOTE ? "Remote access" : location.host;
  // Trusting our own certificate only matters on the Wi-Fi; the remote link already has a trusted one.
  $("#cert-hint").hidden = !secure || REMOTE;
  $("#auth-form [name=username]").focus();
}

document.querySelectorAll(".auth-tabs button").forEach((tab) =>
  tab.addEventListener("click", () => {
    authMode = tab.dataset.mode;
    document.querySelectorAll(".auth-tabs button").forEach((b) => b.classList.toggle("active", b === tab));
    $("#auth").classList.toggle("register", authMode === "register");
    $("#auth-submit").textContent = authMode === "register" ? "Create account" : "Sign in";
    $("#auth-form [name=password]").autocomplete = authMode === "register" ? "new-password" : "current-password";
    $("#auth-error").textContent = "";
  }));

$("#reveal").addEventListener("click", () => {
  const input = $("#auth-form [name=password]");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  $("#reveal").replaceChildren(icon(show ? "eye-off" : "eye"));
  $("#reveal").setAttribute("aria-label", show ? "Hide password" : "Show password");
});

$("#auth-form [name=password]").addEventListener("input", (e) => {
  const { score, label } = passwordStrength(e.target.value);
  $("#strength").dataset.score = score;
  $("#strength-label").textContent = label;
});

/** Same rules as the server (server/extras/auth.py), checked early for instant feedback. */
function validateAuth(username, displayName, password) {
  if (authMode === "register") {
    if (!/^[a-z0-9_]{3,20}$/.test(username)) return ["username-field", "Username: 3-20 characters, a-z, 0-9 or _"];
    if (!displayName) return [null, "Please enter a display name"];
  }
  if (password.length < 6) return ["password-field", "Password must be at least 6 characters"];
  return null;
}

function authError(fieldId, text) {
  $("#auth-error").textContent = text;
  document.querySelectorAll("#auth-form .field").forEach((f) => f.classList.toggle("invalid", f.id === fieldId));
  const card = $("#auth-card");
  card.classList.remove("shake");
  void card.offsetWidth; // restart the animation
  card.classList.add("shake");
}

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const username = String(form.get("username")).trim().toLowerCase();
  const password = String(form.get("password"));
  const displayName = String(form.get("display_name") || "").trim();
  const problem = validateAuth(username, displayName, password);
  if (problem) {
    authError(...problem);
    return;
  }
  document.querySelectorAll("#auth-form .field").forEach((f) => f.classList.remove("invalid"));
  const button = $("#auth-submit");
  button.disabled = true;
  button.classList.add("loading");
  $("#auth-error").textContent = "";
  try {
    const { user } = authMode === "register"
      ? await api.register(username, displayName, password)
      : await api.login(username, password);
    $("#auth").hidden = true;
    enterApp(user);
  } catch (err) {
    authError(err.status === 409 ? "username-field" : null, err.message);
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
  }
});

// ============================================================ app start ===

async function enterApp(user) {
  state.me = user;
  state.store = new Store(user.username);
  $("#auth").hidden = true;
  $("#app").hidden = false;

  renderSkeletonList();
  await refreshUsers();
  startTransport(REMOTE ? "ws" : settings.protocol);
  renderChatList();
  renderMe();

  setInterval(flushOutbox, FLUSH_EVERY_MS);
  setInterval(() => { if (state.peer) renderPeerHeader(); }, 30000); // keep "last seen 5 min ago" fresh
  // The phone tells us when Wi-Fi comes back: reconnect immediately. If the
  // socket survived the blip, a ping proves it still works and we carry on.
  window.addEventListener("online", async () => {
    const t = state.transport;
    if (!t.ready) return t.kick();
    try {
      await t.ping();
      setStatus("online");
      state.inFlight.clear(); // anything sent during the outage may be lost: resend now
      flushOutbox();
    } catch { /* no answer: the heartbeat closes the socket and reconnects */ }
  });
  window.addEventListener("offline", () => setStatus("offline"));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      state.transport.kick();
      markActiveChatRead();
    }
  });
}

/** My own avatar, top-left of the chat list (opens Settings like in most apps). */
function renderMe() {
  $("#me-button").replaceChildren(avatar(state.me));
}

async function refreshUsers() {
  try {
    const { users } = await api.users();
    state.users = new Map(users.map((u) => [u.username, u]));
  } catch (err) {
    if (err.status === 401) location.reload(); // session gone: back to login
  }
}

function startTransport(name) {
  if (state.transport) state.transport.stop();
  const handlers = {
    onStatus: setStatus,
    onMessage: handleMessage,
    onAck: handleAck,
    onReject: handleReject,
    onPresence: handlePresence,
    onReceipt: handleReceipt,
    onTyping: handleTyping,
    onSynced: handleSynced,
    onRtt: (ms) => { state.rtt = ms; renderConn(); },
    onBlocked: fallBackToHttp,
  };
  const Transport = name === "http" ? HttpTransport : WsTransport;
  state.transport = new Transport(handlers, () => state.store.lastId);
  state.inFlight.clear();
  state.receiptsSent = { delivered: new Map(), read: new Map() }; // re-report after reconnecting
  state.rtt = null;
  state.syncing = true;
  state.transport.start();
  renderConn();
}

/**
 * The WebSocket could never be opened. If plain HTTPS still works, the
 * server is fine and only WebSockets are blocked on this device, so switch
 * to Protocol 2. The saved choice stays "ws": a reload tries WebSocket again.
 */
async function fallBackToHttp() {
  try {
    await api.me();
  } catch {
    return; // the server itself is unreachable: keep retrying the WebSocket
  }
  if (state.transport.name !== "ws" || REMOTE) return;
  startTransport("http");
  toast("Live connections are blocked here, switched to Compatibility mode");
}

// ===================================================== protocol events ===

function setStatus(status) {
  state.status = status;
  if (status !== "online") state.syncing = true;
  renderConn();
}

/** A message arrived (live, or as part of an offline sync). */
function handleMessage(msg) {
  const mine = msg.from === state.me.username;
  const isNew = state.store.upsert({ ...msg, status: "sent" });
  if (mine) {
    // Our own message came back from the server: it is confirmed.
    state.store.removeFromOutbox(msg.clientMsgId);
  } else {
    if (isNew) state.store.upsert({ ...state.store.get(messageKey(msg)), receivedAt: Date.now() });
    state.typing.delete(msg.from); // they stopped typing: the message is here
    if (!state.users.has(msg.from)) refreshUsers().then(renderChatList); // someone new registered
    if (isNew && !state.syncing) notifyIncoming(msg);
    scheduleReceipts(); // tell the sender it was delivered (or read)
  }
  scheduleRender();
}

/** The server confirmed it stored one of our messages. */
function handleAck(ack) {
  state.store.removeFromOutbox(ack.clientMsgId);
  state.inFlight.delete(ack.clientMsgId);
  const key = `${state.me.username}:${ack.clientMsgId}`;
  const msg = state.store.get(key);
  if (!msg) return;
  const started = state.sendStarted.get(ack.clientMsgId);
  state.store.upsert({
    ...msg,
    id: ack.id,
    serverTs: ack.serverTs,
    status: "sent",
    ackMs: started ? Math.round(performance.now() - started) : msg.ackMs,
  });
  scheduleRender();
}

/** The server refused one of our messages (invalid); stop retrying it. */
function handleReject({ clientMsgId, reason }) {
  state.store.removeFromOutbox(clientMsgId);
  const msg = state.store.get(`${state.me.username}:${clientMsgId}`);
  if (msg) state.store.upsert({ ...msg, status: "failed", error: reason });
  toast(`Message not sent: ${reason}`);
  scheduleRender();
}

/** Someone came online / went offline / changed their name or colour. */
function handlePresence(presence) {
  const { type, ...card } = presence;
  if (card.username === state.me.username) {
    // My profile changed (maybe from my other device).
    Object.assign(state.me, { display_name: card.display_name, avatar_hue: card.avatar_hue });
    renderMe();
    return;
  }
  const user = state.users.get(card.username);
  if (user) {
    Object.assign(user, card);
    if (!card.online) state.typing.delete(card.username);
    scheduleRender();
  } else {
    refreshUsers().then(renderChatList);
  }
}

/** A friend received / read my messages up to some id: update the ticks. */
function handleReceipt({ from, delivered_up_to, read_up_to }) {
  const user = state.users.get(from);
  if (!user) return;
  const now = Date.now();
  for (const m of state.store.conversation(state.me.username, from)) {
    if (m.from !== state.me.username || !m.id) continue;
    const changes = {};
    if (m.id <= delivered_up_to && !m.deliveredAt) changes.deliveredAt = now;
    if (m.id <= read_up_to && !m.readAt) changes.readAt = now;
    if (Object.keys(changes).length) state.store.upsert({ ...m, ...changes });
  }
  user.delivered_up_to = Math.max(user.delivered_up_to || 0, delivered_up_to);
  user.read_up_to = Math.max(user.read_up_to || 0, read_up_to);
  scheduleRender();
}

function handleTyping({ from }) {
  state.typing.set(from, Date.now() + TYPING_SHOW_MS);
  scheduleRender();
  setTimeout(scheduleRender, TYPING_SHOW_MS + 50); // hide it again
}

const isTyping = (username) => (state.typing.get(username) || 0) > Date.now();

function handleSynced({ count }) {
  state.syncing = false;
  if (count > 0) toast(`Caught up on ${count} message${count === 1 ? "" : "s"}`);
  refreshUsers().then(scheduleRender); // fresh online statuses and receipts after a reconnect
  // New connection: anything sent on the old one may be lost, so resend
  // every un-ACKed message now instead of waiting for its retry timer.
  state.inFlight.clear();
  flushOutbox();
  scheduleReceipts();
}

// ============================================================ receipts out ===

/**
 * Tell each friend how far I got through their messages: "delivered" when the
 * messages are on this phone, "read" when their chat is open on screen (and
 * read receipts are on). Receipts are cumulative (one number per chat), so
 * sending the latest id covers every earlier message, like a cumulative ACK.
 */
let receiptTimer;
function scheduleReceipts() {
  clearTimeout(receiptTimer);
  receiptTimer = setTimeout(sendReceipts, 300);
}

function sendReceipts() {
  const t = state.transport;
  if (!t || !t.ready || !state.store) return;
  const me = state.me.username;
  for (const peer of state.users.keys()) {
    const latest = state.store.latestFrom(me, peer);
    if (!latest) continue;
    const reading = peer === state.peer && document.visibilityState === "visible" && settings.readReceipts;
    const type = reading ? "read" : "delivered";
    if ((state.receiptsSent[type].get(peer) || 0) >= latest) continue;
    if (t.sendReceipt(peer, type, latest)) {
      state.receiptsSent[type].set(peer, latest);
      state.receiptsSent.delivered.set(peer, Math.max(latest, state.receiptsSent.delivered.get(peer) || 0));
    }
  }
}

/** Where is my message? pending -> sent (server ACK) -> delivered -> read */
function deliveryState(m) {
  if (m.status === "failed") return "failed";
  if (m.status === "pending" || !m.id) return "pending";
  const peer = state.users.get(m.to);
  if (peer && m.id <= (peer.read_up_to || 0)) return "read";
  if (peer && m.id <= (peer.delivered_up_to || 0)) return "delivered";
  return "sent";
}

const STATUS_ICON = {
  pending: ["clock", "Sending…"],
  sent: ["check", "Sent"],
  delivered: ["checks", "Delivered"],
  read: ["checks", "Read"],
  failed: ["alert", "Not sent"],
};

function statusIcon(m) {
  const s = deliveryState(m);
  return el("span", { class: `status ${s}`, title: STATUS_ICON[s][1] }, icon(STATUS_ICON[s][0], "icon tiny"));
}

// ======================================================== notifications ===

let notifTimer;
function notifyIncoming(msg) {
  const visible = document.visibilityState === "visible";
  if (settings.sounds) playPop();
  if (!settings.banners || (msg.from === state.peer && visible)) return;
  const user = state.users.get(msg.from) || { username: msg.from, display_name: msg.from };
  $("#notif-avatar").replaceChildren(avatar(user));
  $("#notif-name").textContent = user.display_name;
  $("#notif-text").textContent = previewText(msg, false);
  const banner = $("#notif");
  banner.dataset.user = msg.from;
  banner.hidden = false;
  requestAnimationFrame(() => banner.classList.add("show"));
  clearTimeout(notifTimer);
  notifTimer = setTimeout(hideBanner, 4000);
}

function hideBanner() {
  const banner = $("#notif");
  banner.classList.remove("show");
  setTimeout(() => { if (!banner.classList.contains("show")) banner.hidden = true; }, 300);
}

$("#notif").addEventListener("click", () => {
  hideBanner();
  openChat($("#notif").dataset.user);
});

// ================================================ sending + retransmit ===

/**
 * Send every message in the outbox that has no ACK yet.
 * Runs every 2 s and right after reconnecting. A message that was already
 * sent but not ACKed within RETRY_MS is sent AGAIN (retransmission). The
 * server recognises the repeated msg-id and does not store it twice.
 */
function flushOutbox() {
  const t = state.transport;
  if (!t || !t.ready) return;
  const now = Date.now();
  for (const pending of state.store.outbox) {
    const last = state.inFlight.get(pending.clientMsgId);
    if (last && now - last < RETRY_MS) continue;
    if (t.send(pending)) {
      state.inFlight.set(pending.clientMsgId, now);
      state.sendStarted.set(pending.clientMsgId, performance.now());
    }
  }
}

async function sendCurrentText() {
  const input = $("#input");
  const text = input.value.trim();
  const peer = state.peer;
  if (!text || !peer) return;
  input.value = "";
  autosize();
  input.focus();

  let body = text;
  let encrypted = false;
  const secret = state.store.secrets[peer];
  if (secret && e2ee.available) {
    const key = await e2ee.deriveKey(secret, state.me.username, peer);
    body = await e2ee.encryptText(key, text);
    encrypted = true;
  }

  const msg = {
    id: null,
    clientMsgId: uuid(),
    from: state.me.username,
    to: peer,
    body,
    encrypted,
    via: state.transport.name,
    sentAt: Date.now(),
    serverTs: null,
    status: "pending",
  };
  if (encrypted) state.plain.set(messageKey(msg), text);
  state.unreadMarker = null; // replying = I've seen the unread ones
  state.store.upsert(msg);
  state.store.addToOutbox(msg);  // saved BEFORE sending: survives a crash/reload
  render();
  scrollToBottom(true);
  flushOutbox();
}

// ================================================================ render ===

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    render();
  });
}

function render() {
  renderChatList();
  renderConn();
  if (state.peer) renderConversation();
}

function renderConn() {
  const conn = $("#conn");
  const queued = state.store ? state.store.outbox.length : 0;
  const texts = {
    online: "Connected",
    connecting: "Connecting…",
    reconnecting: "Reconnecting…",
    offline: "Waiting for network",
  };
  let text = texts[state.status] || state.status;
  if (queued && state.status !== "online") text += ` · ${queued} queued`;
  conn.dataset.state = state.status;
  // Like WhatsApp: nothing to read when all is well (a small dot by the title),
  // a status bar only while something is wrong.
  const online = state.status === "online";
  $(".side-title").classList.toggle("live", online);
  $(".side-title").title = online ? "Connected" : text;
  conn.classList.toggle("quiet", online && !REMOTE && state.transport?.name !== "http");
  $(".conn-text", conn).textContent = text;
  // Only mention the mode when it is not the normal one.
  const notes = [];
  if (REMOTE) notes.push("remote");
  if (state.transport && state.transport.name === "http") notes.push("compatibility mode");
  $(".conn-proto", conn).textContent = notes.length ? `· ${notes.join(" · ")}` : "";

  // Banner inside the open chat: makes the outbox (queued messages) visible.
  const banner = $("#net-banner");
  banner.hidden = state.status === "online";
  banner.dataset.state = state.status;
  const queuedText = queued ? ` ${queued} message${queued === 1 ? "" : "s"} will send automatically.` : "";
  $("#net-banner-text").textContent = {
    offline: `No connection.${queuedText || " Messages you write will wait here."}`,
    reconnecting: `Reconnecting…${queuedText}`,
    connecting: "Connecting…",
  }[state.status] || "";
}

function previewText(msg, withYou = true) {
  if (!msg) return "No messages yet";
  const mine = withYou && msg.from === state.me.username ? "You: " : "";
  if (msg.encrypted && !isDecrypted(msg)) return mine + "Secret message";
  return mine + (msg.encrypted ? state.plain.get(messageKey(msg)) : msg.body);
}

const isDecrypted = (msg) => typeof state.plain.get(messageKey(msg)) === "string";

function renderChatList() {
  if (!state.store) return;
  const me = state.me.username;
  const query = $("#search").value.trim().toLowerCase();
  const items = [...state.users.values()]
    .filter((u) => !query || u.username.includes(query) || u.display_name.toLowerCase().includes(query))
    .map((u) => ({ user: u, last: state.store.lastMessageWith(me, u.username), unread: state.store.unreadCount(me, u.username) }))
    .sort((a, b) => {
      const ta = a.last ? a.last.serverTs || a.last.sentAt : 0;
      const tb = b.last ? b.last.serverTs || b.last.sentAt : 0;
      return tb - ta || Number(b.user.online) - Number(a.user.online) || a.user.display_name.localeCompare(b.user.display_name);
    });

  // Unread total: on the back button (phones) and in the browser tab title.
  const totalUnread = items.reduce((sum, i) => sum + (i.user.username === state.peer ? 0 : i.unread), 0);
  $("#back-badge").hidden = !totalUnread;
  $("#back-badge").textContent = totalUnread > 99 ? "99+" : totalUnread;
  document.title = totalUnread ? `(${totalUnread}) Handshake` : "Handshake";

  const list = $("#chat-list");
  if (!items.length) {
    list.replaceChildren(el("li", { class: "list-empty" },
      el("strong", { text: query ? "No match" : "No one here yet" }),
      el("span", { text: query ? "Try another name." : `Ask your teammates to open ${location.host} and create an account.` })));
    return;
  }

  list.replaceChildren(...items.map(({ user, last, unread }) => {
    const av = avatar(user);
    av.append(el("span", { class: `presence ${user.online ? "on" : ""}` }));
    return el("li", {},
      el("button", {
        class: `chat-item ${user.username === state.peer ? "active" : ""}`,
        onclick: () => openChat(user.username),
      },
      av,
      el("div", { class: "chat-item-body" },
        el("div", { class: "row" },
          el("span", { class: "name", text: user.display_name }),
          last ? el("span", { class: "time", text: shortStamp(last.serverTs || last.sentAt) }) : null),
        el("div", { class: "row" },
          isTyping(user.username)
            ? el("span", { class: "preview typing", text: "typing…" })
            : el("span", { class: "preview" },
              last && last.from === me ? statusIcon(last) : null,
              last && last.encrypted && !isDecrypted(last) ? icon("lock", "icon tiny") : null,
              el("span", { class: "preview-text", text: previewText(last, false) })),
          unread ? el("span", { class: "badge-count", text: unread > 99 ? "99+" : String(unread) }) : null))));
  }));
}

// ---------------------------------------------------------- conversation ---

function openChat(username) {
  const wasOpen = Boolean(state.peer);
  if (username !== state.peer) {
    state.shown = new Set(); // no animation when a chat first opens
    // Remember where the unread messages start, to draw the divider there.
    const unread = state.store.unreadCount(state.me.username, username);
    state.unreadMarker = unread ? state.store.lastRead[username] || 0 : null;
    state.newBelow = 0;
  }
  state.peer = username;
  state.firstPaint = true;
  $("#empty-state").hidden = true;
  $("#convo").hidden = false;
  $("#app").classList.add("show-chat");
  // Let the phone's back button return to the list.
  if (!wasOpen) history.pushState({ chat: username }, "");
  render();
  scheduleReceipts();
  if (matchMedia("(min-width: 860px)").matches) $("#input").focus();
}

function closeChat() {
  state.peer = null;
  $("#app").classList.remove("show-chat");
  $("#convo").hidden = true;
  $("#empty-state").hidden = false;
  renderChatList();
}

window.addEventListener("popstate", () => { if (state.peer) closeChat(); });
$("#back").addEventListener("click", () => history.back());

function renderPeerHeader() {
  const user = state.users.get(state.peer) || { username: state.peer, display_name: state.peer };
  const av = avatar(user);
  av.append(el("span", { class: `presence ${user.online ? "on" : ""}` }));
  $("#peer-avatar").replaceChildren(av);
  $("#peer-name").textContent = user.display_name;
  const typing = isTyping(state.peer);
  $("#peer-status").textContent = typing ? "typing…" : lastSeenText(user);
  $("#peer-status").classList.toggle("on", Boolean(user.online) || typing);
  $("#peer-status").classList.toggle("typing", typing);

  const lock = $("#lock-btn");
  const hasSecret = Boolean(state.store.secrets[state.peer]);
  lock.classList.toggle("on", hasSecret && e2ee.available);
  lock.disabled = !e2ee.available;
  lock.title = !e2ee.available ? "Secret chats need a secure connection" : hasSecret ? "Secret chat is on" : "Start a secret chat";
  lock.replaceChildren(icon(hasSecret ? "lock" : "unlock"),
    el("span", { text: hasSecret && e2ee.available ? "Secret chat" : "" }));
}

function renderConversation() {
  renderPeerHeader();
  const box = $("#messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const me = state.me.username;
  const messages = state.store.conversation(me, state.peer).slice(-400);

  // Count messages that arrive while the user is scrolled up (for the jump button).
  if (!state.firstPaint && !nearBottom) {
    state.newBelow += messages.filter((m) => m.from !== me && !state.shown.has(messageKey(m))).length;
  }

  const nodes = [];
  let divider = null;
  let lastDay = null;
  let prev = null;
  if (!messages.length) {
    if (state.syncing && state.status !== "offline") {
      nodes.push(...skeletonBubbles());   // history may still be on its way
    } else {
      const name = (state.users.get(state.peer)?.display_name || state.peer).split(" ")[0];
      nodes.push(el("div", { class: "convo-empty" },
        icon("chat", "icon big"),
        el("p", { text: `No messages yet. Say hi to ${name}!` })));
    }
  }
  for (const m of messages) {
    const stamp = m.serverTs || m.sentAt;
    const day = dayLabel(stamp);
    if (day !== lastDay) {
      nodes.push(el("div", { class: "day", text: day }));
      lastDay = day;
      prev = null;
    }
    if (!divider && state.unreadMarker !== null && m.from !== me && m.id > state.unreadMarker) {
      const count = messages.filter((x) => x.from !== me && x.id > state.unreadMarker).length;
      divider = el("div", { class: "unread-divider" }, el("span", { text: `${count} unread message${count === 1 ? "" : "s"}` }));
      nodes.push(divider);
      prev = null;
    }
    const grouped = prev && prev.from === m.from && stamp - (prev.serverTs || prev.sentAt) < 180000;
    nodes.push(messageNode(m, m.from === me, grouped));
    prev = m;
  }
  if (isTyping(state.peer)) {
    nodes.push(el("div", { class: "msg theirs typing-bubble" }, el("div", { class: "bubble" }, el("span"), el("span"), el("span"))));
  }
  box.replaceChildren(...nodes);
  if (state.firstPaint) {
    // Opening a chat: jump to the first unread message, otherwise to the end.
    requestAnimationFrame(() => (divider ? divider.scrollIntoView({ block: "center" }) : box.scrollTo({ top: box.scrollHeight })));
  } else if (nearBottom) {
    scrollToBottom();
  }
  state.firstPaint = false;
  updateJumpButton();
  decryptVisible(messages);
  markActiveChatRead();
}

function messageNode(m, mine, grouped) {
  const key = messageKey(m);
  let text = m.body;
  let textClass = "text";
  if (m.encrypted) {
    const plain = state.plain.get(key);
    if (typeof plain === "string") text = plain;
    else if (plain === null) { text = "🔒 Can't open this message: your chat passwords don't match"; textClass += " locked"; }
    else { text = state.store.secrets[state.peer] ? "Opening…" : "🔒 Secret message. Tap the lock above and enter your chat password."; textClass += " locked"; }
  }

  const status = mine ? statusIcon(m) : null;

  // Tap a message: menu with Copy / Message info / Cancel sending.
  const locked = textClass.includes("locked");
  const bubble = el("div", { class: "bubble", onclick: () => openMessageMenu(m, text) },
    el("div", { class: textClass }, locked ? icon("lock", "icon tiny inline") : null, text),
    el("div", { class: "meta" },
      m.encrypted ? icon("lock", "icon tiny") : null,
      el("span", { text: formatTime(m.serverTs || m.sentAt) }),
      status));

  const isNew = !state.shown.has(key) && !state.firstPaint;
  state.shown.add(key);

  return el("div", { class: `msg ${mine ? "mine" : "theirs"} ${grouped ? "grouped" : ""} ${m.encrypted ? "enc" : ""} ${m.status === "pending" ? "is-pending" : ""} ${isNew ? "enter" : ""}` },
    bubble,
    m.status === "failed" ? el("div", { class: "msg-error" }, icon("alert", "icon tiny"), `Not sent: ${m.error || "rejected by the server"}`) : null);
}

// ------------------------------------------------------------ skeletons ---

function skeletonBubbles() {
  return [62, 40, 74, 52].map((w, i) => el("div", { class: `msg ${i % 2 ? "mine" : "theirs"} skeleton-msg` },
    el("div", { class: "bubble skeleton", style: `width:${w}vw;max-width:${w * 5}px` })));
}

function renderSkeletonList() {
  $("#chat-list").replaceChildren(...[0, 1, 2, 3].map(() => el("li", { class: "skeleton-item" },
    el("span", { class: "skeleton circle" }),
    el("span", { class: "skeleton-lines" }, el("span", { class: "skeleton line" }), el("span", { class: "skeleton line short" })))));
}

// ------------------------------------------------------ iPhone keyboard ---
// iOS Safari does not shrink the page when the keyboard opens, so the message
// box would hide behind it. visualViewport tells us the visible height; the app
// is sized to it, which keeps the composer just above the keyboard.
if (window.visualViewport) {
  const fitToVisibleArea = () => {
    document.documentElement.style.setProperty("--app-h", `${visualViewport.height}px`);
    if (visualViewport.offsetTop) window.scrollTo(0, 0);
  };
  visualViewport.addEventListener("resize", fitToVisibleArea);
  visualViewport.addEventListener("scroll", fitToVisibleArea);
  fitToVisibleArea();
}

/** Decrypt encrypted messages we have not decrypted yet, then re-render once. */
let decrypting = false;
async function decryptVisible(messages) {
  if (decrypting || !e2ee.available) return;
  const peer = state.peer;
  const secret = state.store.secrets[peer];
  if (!secret) return;
  const todo = messages.filter((m) => m.encrypted && !state.plain.has(messageKey(m)));
  if (!todo.length) return;
  decrypting = true;
  try {
    const key = await e2ee.deriveKey(secret, state.me.username, peer);
    for (const m of todo) {
      try {
        state.plain.set(messageKey(m), await e2ee.decryptText(key, m.body));
      } catch {
        state.plain.set(messageKey(m), null); // wrong key or tampered
      }
    }
  } finally {
    decrypting = false;
  }
  scheduleRender();
}

function markActiveChatRead() {
  if (!state.peer || document.visibilityState !== "visible") return;
  const conv = state.store.conversation(state.me.username, state.peer);
  const maxId = conv.reduce((max, m) => (m.id > max ? m.id : max), 0);
  const before = state.store.unreadCount(state.me.username, state.peer);
  state.store.markRead(state.peer, maxId);
  if (before) renderChatList();
  scheduleReceipts();
}

// ------------------------------------------------------ jump to latest ---

function updateJumpButton() {
  const box = $("#messages");
  const away = box.scrollHeight - box.scrollTop - box.clientHeight > 300;
  if (!away) state.newBelow = 0;
  $("#jump").hidden = !away;
  $("#jump-count").hidden = !state.newBelow;
  $("#jump-count").textContent = state.newBelow;
}

$("#messages").addEventListener("scroll", updateJumpButton, { passive: true });
$("#jump").addEventListener("click", () => {
  state.newBelow = 0;
  scrollToBottom();
});

// --------------------------------------------------------- message menu ---

let menuMessage = null;

function openMessageMenu(m, shownText) {
  menuMessage = { m, text: shownText };
  const mine = m.from === state.me.username;
  $("#menu-preview").replaceChildren(el("div", { class: `bubble ${mine ? "mine" : ""}`, text: shownText }));
  $('#msg-menu [data-action="cancel"]').hidden = !(mine && m.status === "pending");
  $("#msg-menu").showModal();
}

$("#msg-menu").addEventListener("click", async (e) => {
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (!action || !menuMessage) return;
  const { m, text } = menuMessage;
  $("#msg-menu").close();
  if (action === "copy") {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
    } catch {
      toast("Copy not allowed here");
    }
  } else if (action === "info") {
    showMessageInfo(m);
  } else if (action === "cancel") {
    state.store.removeFromOutbox(m.clientMsgId);
    state.store.remove(messageKey(m));
    toast("Message won't be sent");
    render();
  }
});

/** The "Message info" sheet: the life of one message, step by step. */
function showMessageInfo(m) {
  const me = state.me.username;
  const mine = m.from === me;
  const peerName = state.users.get(mine ? m.to : m.from)?.display_name || (mine ? m.to : m.from);
  const s = mine ? deliveryState(m) : null;
  const at = (t) => (t ? `${dayLabel(t)}, ${formatTime(t)}` : null);
  const step = (done, iconName, title, detail) => el("li", { class: done ? "done" : "" },
    icon(iconName, "icon"), el("div", {}, el("strong", { text: title }), el("small", { text: detail || "-" })));

  const steps = mine ? [
    step(true, "send", "Written", at(m.sentAt)),
    step(Boolean(m.id), "check", "Sent", m.id ? at(m.serverTs) : "Waiting for a connection"),
    step(["delivered", "read"].includes(s), "checks", `Delivered to ${peerName}`,
      at(m.deliveredAt) || (["delivered", "read"].includes(s) ? "Yes" : "Not yet")),
    step(s === "read", "eye", `Read by ${peerName}`, at(m.readAt) || (s === "read" ? "Yes" : "Not yet")),
  ] : [
    step(true, "send", `Sent by ${peerName}`, at(m.sentAt)),
    step(true, "checks", "Received", at(m.receivedAt) || at(m.serverTs)),
  ];
  $("#info-timeline").replaceChildren(...steps);

  const wireBytes = new TextEncoder().encode(m.body).length;
  const rows = [
    ["Server message id", m.id ? `#${m.id}` : "not assigned yet"],
    ["Client message id", m.clientMsgId.slice(0, 8) + "…"],
    ["Protocol", m.via === "http" ? "HTTP POST + SSE" : "HSP/1.0 over WebSocket"],
    ["Server ACK", m.ackMs != null ? `after ${m.ackMs} ms` : mine ? "-" : "n/a"],
    ["Encryption", m.encrypted ? "AES-256-GCM (end-to-end)" : "TLS only (hop by hop)"],
    ["Body on the wire", formatBytes(wireBytes)],
  ];
  $("#info-grid").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  $("#msg-info").showModal();
}

function scrollToBottom(instant = false) {
  const box = $("#messages");
  requestAnimationFrame(() => box.scrollTo({ top: box.scrollHeight, behavior: instant ? "auto" : "smooth" }));
}

// ============================================================== composer ===

function autosize() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 140) + "px";
}

$("#input").addEventListener("input", () => {
  autosize();
  // Tell the other person I'm typing (throttled; never stored on the server).
  const now = Date.now();
  if (state.peer && $("#input").value.trim() && now - state.lastTypingSent > TYPING_SEND_MS) {
    state.lastTypingSent = now;
    state.transport.sendTyping(state.peer);
  }
});
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && settings.enterToSend) {
    e.preventDefault();
    sendCurrentText();
  }
});
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  sendCurrentText();
});
$("#search").addEventListener("input", renderChatList);

// ============================================================== settings ===

const HUES = [null, 0, 30, 140, 190, 220, 260, 300, 330]; // null = automatic colour
let draftHue;

function renderSettings() {
  // profile
  if (draftHue === undefined) draftHue = state.me.avatar_hue ?? null;
  $("#profile-avatar").replaceChildren(avatar({ ...state.me, avatar_hue: draftHue, display_name: $("#profile-name").value || state.me.display_name }, "large"));
  $("#profile-username").textContent = `@${state.me.username}`;
  $("#hue-swatches").replaceChildren(...HUES.map((hue) => el("button", {
    type: "button",
    class: `swatch ${hue === null ? "auto" : ""} ${hue === draftHue ? "active" : ""}`,
    style: hue === null ? "" : `--hue:${hue}`,
    "aria-label": hue === null ? "Automatic colour" : `Colour ${hue}`,
    onclick: () => { draftHue = hue; renderSettings(); updateSaveButton(); },
  })));

  // connection
  document.querySelectorAll(".proto").forEach((b) => b.classList.toggle("active", b.dataset.proto === state.transport.name));
  $("#conn-detail").textContent = state.status === "online"
    ? `Connected to ${location.host} using ${MODE_TECH[state.transport.name]}${state.rtt !== null ? ` · round trip ${state.rtt.toFixed(0)} ms` : ""}`
    : `Not connected (${state.status})`;
  // Remote link: Compatibility (SSE) is buffered by the tunnel, so only Fast is offered.
  $('.proto[data-proto="http"]').disabled = REMOTE;
  if (REMOTE) $("#conn-detail").textContent += ". Compatibility mode only works on the same Wi-Fi.";
  $("#trust-row").hidden = REMOTE || location.protocol !== "https:";

  // preferences
  document.querySelectorAll("[data-pref]").forEach((input) => { input.checked = Boolean(settings[input.dataset.pref]); });
  document.querySelectorAll("[data-size]").forEach((b) => b.classList.toggle("active", b.dataset.size === settings.textSize));
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.classList.toggle("active", b.dataset.themeChoice === settings.theme));
  document.querySelectorAll("[data-accent-choice]").forEach((b) => b.classList.toggle("active", b.dataset.accentChoice === settings.accent));

  // install hint: iPhone/iPad in Safari, not already installed
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = navigator.standalone || matchMedia("(display-mode: standalone)").matches;
  $("#install-section").hidden = !ios || standalone;

  // storage + about
  const cache = state.store.cacheInfo();
  $("#cache-info").textContent = `${cache.count} messages stored on this phone (${formatBytes(cache.bytes)}). They download again from the server.`;
  const about = [
    ["Version", `Handshake ${APP_VERSION}`],
    ["Server", REMOTE ? `${location.host} (remote, Cloudflare tunnel)` : location.host],
    ["Transport security", location.protocol === "https:" ? "TLS (HTTPS / WSS)" : "None (HTTP)"],
    ["Secret chats", e2ee.available ? "AES-256-GCM via Web Crypto" : "Unavailable (needs HTTPS)"],
    ["Running as", standalone ? "Installed app" : "Browser tab"],
  ];
  $("#about-grid").replaceChildren(...about.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
}

function openSettings() {
  draftHue = undefined;
  $("#profile-name").value = state.me.display_name;
  renderSettings();
  updateSaveButton();
  $("#settings").showModal();
}

function updateSaveButton() {
  const name = $("#profile-name").value.trim();
  const changed = name !== state.me.display_name || draftHue !== (state.me.avatar_hue ?? null);
  $("#profile-save").disabled = !changed || !name;
}

$("#open-settings").addEventListener("click", openSettings);
$("#me-button").addEventListener("click", openSettings);
$("#conn").addEventListener("click", openSettings);
$("#profile-name").addEventListener("input", () => { updateSaveButton(); renderSettings(); });

$("#profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const { user } = await api.updateProfile($("#profile-name").value.trim(), draftHue);
    Object.assign(state.me, user);
    renderMe();
    updateSaveButton();
    $("#profile-error").textContent = "";
    toast("Profile updated");
  } catch (err) {
    $("#profile-error").textContent = err.message;
  }
});

document.querySelectorAll("[data-pref]").forEach((input) =>
  input.addEventListener("change", () => {
    settings[input.dataset.pref] = input.checked;
    if (input.dataset.pref === "readReceipts" && input.checked) scheduleReceipts();
  }));

document.querySelectorAll("[data-size]").forEach((button) =>
  button.addEventListener("click", () => {
    settings.textSize = button.dataset.size;
    applyAppearance();
    renderSettings();
  }));

document.querySelectorAll("[data-accent-choice]").forEach((button) =>
  button.addEventListener("click", () => {
    settings.accent = button.dataset.accentChoice;
    applyAppearance();
    renderSettings();
  }));

$("#clear-cache").addEventListener("click", () => {
  // Proof that the server is the source of truth: wipe the local copy and
  // re-sync everything from message id 0.
  state.store.clearCache();
  state.plain.clear();
  state.shown = new Set();
  startTransport(state.transport.name);
  renderSettings();
  toast("Space freed, your chats are downloading again");
});

// Close a sheet with its X button, or by tapping the dimmed area OUTSIDE it.
// (A tap on empty space INSIDE the sheet also has the dialog as its target,
// so we check the tap position against the sheet's rectangle.)
document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("click", (e) => {
    if (e.target.closest("[data-close]")) return dialog.close();
    if (e.target !== dialog) return;
    const r = dialog.getBoundingClientRect();
    const outside = e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    if (outside) dialog.close();
  });
});

document.querySelectorAll(".proto").forEach((button) =>
  button.addEventListener("click", () => {
    const name = button.dataset.proto;
    if (name === state.transport.name) return;
    settings.protocol = name;
    startTransport(name);
    renderSettings();
    toast(`Switched to ${MODE_NAME[name]} mode`);
  }));

document.querySelectorAll("[data-theme-choice]").forEach((button) =>
  button.addEventListener("click", () => {
    settings.theme = button.dataset.themeChoice;
    applyAppearance();
    renderSettings();
  }));

/** Theme, accent colour and text size are attributes on <html>; style.css does the rest. */
function applyAppearance() {
  const root = document.documentElement;
  if (settings.theme === "auto") delete root.dataset.theme;
  else root.dataset.theme = settings.theme;
  if (!["teal", "blue", "purple", "coral"].includes(settings.accent)) settings.accent = "teal"; // old v1.1 values
  root.dataset.accent = settings.accent;
  root.dataset.textSize = settings.textSize;
}

$("#ping-run").addEventListener("click", async () => {
  const button = $("#ping-run");
  const bars = $("#ping-bars");
  button.disabled = true;
  bars.replaceChildren();
  const results = [];
  for (let i = 0; i < 20; i++) {
    try {
      results.push(await state.transport.ping());
    } catch {
      results.push(null); // lost / timed out
    }
    const ok = results.filter((r) => r !== null);
    const max = Math.max(...ok, 1);
    bars.replaceChildren(...results.map((r) =>
      el("span", { class: r === null ? "lost" : "", style: `height:${r === null ? 100 : Math.max(8, (r / max) * 100)}%`, title: r === null ? "lost" : `${r.toFixed(1)} ms` })));
    if (ok.length) {
      const avg = ok.reduce((a, b) => a + b, 0) / ok.length;
      let jitter = 0;
      for (let j = 1; j < ok.length; j++) jitter += Math.abs(ok[j] - ok[j - 1]);
      jitter = ok.length > 1 ? jitter / (ok.length - 1) : 0;
      $("#ps-avg").textContent = `${avg.toFixed(1)} ms`;
      $("#ps-min").textContent = `${Math.min(...ok).toFixed(1)} ms`;
      $("#ps-max").textContent = `${Math.max(...ok).toFixed(1)} ms`;
      $("#ps-jit").textContent = `${jitter.toFixed(1)} ms`;
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  const lost = results.filter((r) => r === null).length;
  if (lost) toast(`${lost} of 20 checks got no answer`);
  button.disabled = false;
});

$("#logout").addEventListener("click", async () => {
  state.transport.stop();
  try { await api.logout(); } catch { /* ignore */ }
  state.store.clear();
  location.reload();
});

// ======================================================= encryption UI ===

$("#lock-btn").addEventListener("click", () => {
  const peer = state.peer;
  const user = state.users.get(peer);
  $("#secret-peer").textContent = user ? user.display_name : peer;
  $("#secret-input").value = state.store.secrets[peer] || "";
  $("#secret-remove").hidden = !state.store.secrets[peer];
  $("#secret-error").textContent = "";
  updateSafetyCode();
  $("#secret-dialog").showModal();
});

let safetyTimer;
async function updateSafetyCode() {
  const secret = $("#secret-input").value;
  $("#safety-code").textContent = secret.length >= 4
    ? await e2ee.safetyCode(secret, state.me.username, state.peer)
    : "---- ---- ----";
}
$("#secret-input").addEventListener("input", () => {
  clearTimeout(safetyTimer);
  safetyTimer = setTimeout(updateSafetyCode, 150);
});

function forgetFailedDecryptions() {
  for (const [key, value] of state.plain) if (value === null) state.plain.delete(key);
}

$("#secret-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const secret = $("#secret-input").value;
  if (secret.length < 4) {
    $("#secret-error").textContent = "Use at least 4 characters";
    return;
  }
  $("#secret-error").textContent = "";
  state.store.setSecret(state.peer, secret);
  forgetFailedDecryptions();
  $("#secret-dialog").close();
  toast("Secret chat is on");
  render();
});

$("#secret-remove").addEventListener("click", () => {
  state.store.setSecret(state.peer, null);
  $("#secret-dialog").close();
  toast("Secret chat is off for new messages");
  render();
});

// ================================================================= toast ===

let toastTimer;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}
