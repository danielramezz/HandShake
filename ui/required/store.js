/*
 * Local state of one logged-in user, saved in localStorage.
 *
 *  messages : every message this phone knows, keyed by "sender:clientMsgId".
 *             That key is unique, so receiving the same message twice
 *             (e.g. live AND in a sync) simply overwrites it: no duplicates on screen.
 *  outbox   : messages written but not yet ACKed by the server. They are
 *             retransmitted until an ACK arrives, even after a page reload.
 *  lastId   : highest server id we have. Sent in HELLO / ?after= on reconnect.
 *
 * The server is the source of truth; this is only a cache plus the outbox.
 */

const MAX_CACHED = 3000;

function load(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage full or blocked: keep working in memory */ }
}

export const messageKey = (m) => `${m.from}:${m.clientMsgId}`;

export class Store {
  constructor(username) {
    this.prefix = `hs:${username}:`;
    this.messages = new Map(load(this.prefix + "messages", []).map((m) => [messageKey(m), m]));
    this.outbox = load(this.prefix + "outbox", []);
    this.lastRead = load(this.prefix + "lastRead", {});
    this.secrets = load(this.prefix + "secrets", {});
  }

  get lastId() {
    let max = 0;
    for (const m of this.messages.values()) if (m.id && m.id > max) max = m.id;
    return max;
  }

  /** Insert or update a message. Returns true if it was not known before. */
  upsert(msg) {
    const key = messageKey(msg);
    const old = this.messages.get(key);
    this.messages.set(key, { ...old, ...msg });
    this.persistSoon();
    return !old;
  }

  get(key) {
    return this.messages.get(key);
  }

  remove(key) {
    this.messages.delete(key);
    this.persistSoon();
  }

  /** All messages between me and peer, oldest first (unsent ones last). */
  conversation(me, peer) {
    const list = [];
    for (const m of this.messages.values()) {
      if ((m.from === me && m.to === peer) || (m.from === peer && m.to === me)) list.push(m);
    }
    return list.sort((a, b) => (a.id ?? Infinity) - (b.id ?? Infinity) || a.sentAt - b.sentAt);
  }

  lastMessageWith(me, peer) {
    const conv = this.conversation(me, peer);
    return conv[conv.length - 1] || null;
  }

  /** Highest server id of the messages peer sent me. */
  latestFrom(me, peer) {
    let max = 0;
    for (const m of this.messages.values()) if (m.from === peer && m.to === me && m.id > max) max = m.id;
    return max;
  }

  /** Approximate size of the local cache (for Settings -> Storage). */
  cacheInfo() {
    let bytes = 0;
    try { bytes = (localStorage.getItem(this.prefix + "messages") || "").length; } catch { /* ignore */ }
    return { count: this.messages.size, bytes };
  }

  /** Forget cached messages (not the outbox); the server re-sends them on the next sync. */
  clearCache() {
    this.messages.clear();
    this.lastRead = {};
    for (const k of ["messages", "lastRead"]) {
      try { localStorage.removeItem(this.prefix + k); } catch { /* ignore */ }
    }
  }

  unreadCount(me, peer) {
    const seen = this.lastRead[peer] || 0;
    let n = 0;
    for (const m of this.messages.values()) if (m.from === peer && m.to === me && m.id > seen) n++;
    return n;
  }

  markRead(peer, id) {
    if (id > (this.lastRead[peer] || 0)) {
      this.lastRead[peer] = id;
      save(this.prefix + "lastRead", this.lastRead);
    }
  }

  // ---- outbox ----
  addToOutbox(msg) {
    this.outbox.push({ clientMsgId: msg.clientMsgId, to: msg.to, body: msg.body, encrypted: msg.encrypted, sentAt: msg.sentAt });
    save(this.prefix + "outbox", this.outbox);
  }

  removeFromOutbox(clientMsgId) {
    const before = this.outbox.length;
    this.outbox = this.outbox.filter((p) => p.clientMsgId !== clientMsgId);
    if (this.outbox.length !== before) save(this.prefix + "outbox", this.outbox);
  }

  // ---- E2EE secrets (stay on this device only) ----
  setSecret(peer, secret) {
    if (secret) this.secrets[peer] = secret;
    else delete this.secrets[peer];
    save(this.prefix + "secrets", this.secrets);
  }

  // ---- persistence ----
  persistSoon() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      const all = [...this.messages.values()].sort((a, b) => (a.id ?? Infinity) - (b.id ?? Infinity));
      save(this.prefix + "messages", all.slice(-MAX_CACHED));
    }, 300);
  }

  clear() {
    for (const k of ["messages", "outbox", "lastRead", "secrets"]) {
      try { localStorage.removeItem(this.prefix + k); } catch { /* ignore */ }
    }
  }
}

/** App-wide preferences (not tied to a user), with their default values. */
const DEFAULTS = {
  protocol: "ws",        // "ws" (HSP) or "http" (HTTP + SSE)
  theme: "auto",         // auto | dark | light
  accent: "teal",        // app colour: teal | blue | purple | coral (style.css [data-accent])
  textSize: "m",         // s | m | l
  enterToSend: true,
  sounds: true,
  banners: true,         // in-app banner for messages in other chats
  readReceipts: true,    // tell others when I have read their messages
};

export const settings = {};
for (const [name, fallback] of Object.entries(DEFAULTS)) {
  Object.defineProperty(settings, name, {
    get: () => load(`hs:${name}`, fallback),
    set: (value) => save(`hs:${name}`, value),
  });
}
