/*
 * Protocol 1 client: HSP/1.0 over a WebSocket.
 *
 * Both transports (this one and transport-http.js) have the SAME interface,
 * so app.js does not care which protocol is active:
 *
 *   start() / stop() / kick()          open, close, reconnect now
 *   send(pending) -> true|false         false = not connected, keep in outbox
 *   sendReceipt(to, type, upTo)         "I received/read your messages up to id N"
 *   sendTyping(to)                      temporary "I am typing" signal
 *   ping() -> Promise<rtt ms>
 *   handlers: onStatus, onMessage, onAck, onReject, onPresence, onReceipt,
 *             onTyping, onSynced, onRtt, onBlocked
 */

import { encode, decode } from "./hsp.js";

const HEARTBEAT_MS = 10000;
const PING_TIMEOUT_MS = 6000;

/** Convert an HSP MSG frame into the app's message object. */
function messageFromFrame({ headers, body }) {
  return {
    id: Number(headers.id),
    clientMsgId: headers["msg-id"],
    from: headers.from,
    to: headers.to,
    body,
    encrypted: headers.enc === "aes-gcm",
    via: headers.via,
    sentAt: headers["sent-at"] ? Number(headers["sent-at"]) : null,
    serverTs: Number(headers["server-ts"]),
  };
}

export class WsTransport {
  constructor(handlers, getLastId) {
    this.name = "ws";
    this.h = handlers;
    this.getLastId = getLastId;
    this.ws = null;
    this.ready = false;     // true after SYNCED
    this.stopped = true;
    this.retries = 0;
    this.lastRtt = null;
    this.pendingPings = new Map(); // t -> resolve
    this.everOpened = false;       // did a WebSocket ever open in this session?
    this.failedOpens = 0;
  }

  start() {
    this.stopped = false;
    this.connect();
  }

  connect() {
    clearTimeout(this.reconnectTimer);
    this.h.onStatus(this.retries ? "reconnecting" : "connecting");
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    this.ws = ws;

    // Rule: the first frame is HELLO, telling the server what we already have.
    ws.onopen = () => {
      this.everOpened = true;
      ws.send(encode("HELLO", { "last-id": this.getLastId() }));
    };
    ws.onmessage = (event) => this.onFrame(event.data);
    ws.onclose = () => {
      if (this.ws !== ws) return; // an old socket we already replaced
      this.ready = false;
      clearInterval(this.heartbeat);
      // Never managed to open even once, 3 times in a row: the device may be
      // blocking WebSockets (e.g. an iPhone that does not trust the certificate).
      if (!this.everOpened && ++this.failedOpens === 3 && this.h.onBlocked) this.h.onBlocked();
      if (!this.stopped) {
        this.h.onStatus("reconnecting");
        // Exponential back-off: 0.5 s, 1 s, 2 s, 4 s, then every 8 s.
        const delay = Math.min(8000, 500 * 2 ** this.retries++);
        this.reconnectTimer = setTimeout(() => this.connect(), delay);
      }
    };
  }

  onFrame(text) {
    let frame;
    try {
      frame = decode(text);
    } catch (err) {
      console.warn("Bad HSP frame from server:", err.message);
      return;
    }
    const { command, headers } = frame;
    switch (command) {
      case "WELCOME":
        break;
      case "MSG":
        this.h.onMessage(messageFromFrame(frame));
        break;
      case "SYNCED":
        this.ready = true;
        this.retries = 0;
        this.h.onStatus("online");
        this.h.onSynced({ count: Number(headers.count) });
        clearInterval(this.heartbeat);
        this.heartbeat = setInterval(() => this.heartbeatTick(), HEARTBEAT_MS);
        break;
      case "ACK":
        this.h.onAck({
          clientMsgId: headers["msg-id"],
          id: Number(headers.id),
          serverTs: Number(headers["server-ts"]),
          duplicate: headers.duplicate === "yes",
        });
        break;
      case "ERROR":
        if (headers["msg-id"]) {
          this.h.onReject({ clientMsgId: headers["msg-id"], code: Number(headers.code), reason: headers.reason });
        } else {
          console.warn("HSP error:", headers.code, headers.reason);
        }
        break;
      case "PRESENCE":
        this.h.onPresence({
          username: headers.user,
          online: headers.status === "online",
          display_name: headers.name,
          avatar_hue: headers.hue === "" ? null : Number(headers.hue),
          last_seen: headers["last-seen"] === "" ? null : Number(headers["last-seen"]),
        });
        break;
      case "RECEIPT":
        this.h.onReceipt({ from: headers.from, delivered_up_to: Number(headers.delivered), read_up_to: Number(headers.read) });
        break;
      case "TYPING":
        this.h.onTyping({ from: headers.from });
        break;
      case "PONG": {
        const resolve = this.pendingPings.get(headers.t);
        if (resolve) resolve();
        break;
      }
    }
  }

  send(p) {
    if (!this.ready) return false;
    this.ws.send(encode("SEND", {
      "msg-id": p.clientMsgId,
      to: p.to,
      enc: p.encrypted ? "aes-gcm" : "none",
      "sent-at": p.sentAt,
    }, p.body));
    return true;
  }

  sendReceipt(to, type, upTo) {
    if (!this.ready) return false;
    this.ws.send(encode("RECEIPT", { to, type, "up-to": upTo }));
    return true;
  }

  sendTyping(to) {
    if (this.ready) this.ws.send(encode("TYPING", { to }));
  }

  /** Round-trip time of one PING -> PONG exchange. */
  ping() {
    if (!this.ready) return Promise.reject(new Error("not connected"));
    const t = String(performance.now());
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingPings.delete(t);
        reject(new Error("ping timeout"));
      }, PING_TIMEOUT_MS);
      this.pendingPings.set(t, () => {
        clearTimeout(timer);
        this.pendingPings.delete(t);
        this.lastRtt = performance.now() - Number(t);
        this.h.onRtt(this.lastRtt);
        resolve(this.lastRtt);
      });
      const headers = { t };
      if (this.lastRtt !== null) headers.rtt = this.lastRtt.toFixed(1);
      this.ws.send(encode("PING", headers));
    });
  }

  /** Every 10 s: a PING. No PONG means the link is dead (e.g. Wi-Fi lost). */
  async heartbeatTick() {
    try {
      await this.ping();
    } catch {
      if (this.ws) this.ws.close(); // triggers onclose -> reconnect
    }
  }

  /** Reconnect right now (used when the phone says it is back online). */
  kick() {
    if (this.stopped || this.ready) return;
    this.retries = 0;
    this.connect();
  }

  stop() {
    this.stopped = true;
    this.ready = false;
    clearTimeout(this.reconnectTimer);
    clearInterval(this.heartbeat);
    if (this.ws) {
      try {
        if (this.ws.readyState === WebSocket.OPEN) this.ws.send(encode("BYE"));
        this.ws.close();
      } catch { /* already closed */ }
      this.ws = null;
    }
  }
}
