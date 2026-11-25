/*
 * Protocol 2 client: HTTP REST (send) + Server-Sent Events (receive).
 * Same interface as WsTransport; see transport-ws.js.
 *
 * EventSource is the browser's built-in SSE client. If the stream breaks,
 * it reconnects by itself and sends "Last-Event-ID: <last message id>",
 * so the server knows where to resume.
 */

import { api } from "./api.js";

const HEARTBEAT_MS = 10000;

/** Convert the server's JSON message into the app's message object. */
function messageFromJson(m) {
  return {
    id: m.id,
    clientMsgId: m.client_msg_id,
    from: m.from,
    to: m.to,
    body: m.body,
    encrypted: m.encrypted,
    via: m.protocol,
    sentAt: m.sent_at,
    serverTs: m.server_ts,
  };
}

export class HttpTransport {
  constructor(handlers, getLastId) {
    this.name = "http";
    this.h = handlers;
    this.getLastId = getLastId;
    this.es = null;
    this.ready = false;
    this.stopped = true;
    this.retries = 0;
    this.lastRtt = null;
  }

  start() {
    this.stopped = false;
    this.open();
  }

  open() {
    clearTimeout(this.reconnectTimer);
    if (this.es) this.es.close();
    this.h.onStatus(this.retries ? "reconnecting" : "connecting");

    const es = new EventSource(`/api/stream?after=${this.getLastId()}`);
    this.es = es;

    es.addEventListener("message", (e) => this.h.onMessage(messageFromJson(JSON.parse(e.data))));
    es.addEventListener("presence", (e) => this.h.onPresence(JSON.parse(e.data)));
    es.addEventListener("receipt", (e) => this.h.onReceipt(JSON.parse(e.data)));
    es.addEventListener("typing", (e) => this.h.onTyping(JSON.parse(e.data)));
    es.addEventListener("synced", (e) => {
      this.ready = true;
      this.retries = 0;
      this.h.onStatus("online");
      this.h.onSynced({ count: JSON.parse(e.data).count });
      clearInterval(this.heartbeat);
      this.heartbeat = setInterval(() => this.ping().catch(() => {}), HEARTBEAT_MS);
    });
    es.onerror = () => {
      this.ready = false;
      clearInterval(this.heartbeat);
      if (this.stopped) return;
      this.h.onStatus("reconnecting");
      // CONNECTING = the browser is already retrying by itself.
      // CLOSED     = it gave up (e.g. an HTTP error), so we retry ourselves.
      if (es.readyState === EventSource.CLOSED) {
        const delay = Math.min(8000, 500 * 2 ** this.retries++);
        this.reconnectTimer = setTimeout(() => this.open(), delay);
      }
    };
  }

  send(p) {
    if (!this.ready) return false;
    api.postMessage({
      client_msg_id: p.clientMsgId,
      to: p.to,
      body: p.body,
      encrypted: p.encrypted,
      sent_at: p.sentAt,
    }).then((ack) => {
      // The HTTP 200 response is the acknowledgement.
      this.h.onAck({ clientMsgId: ack.client_msg_id, id: ack.id, serverTs: ack.server_ts, duplicate: ack.duplicate });
    }).catch((err) => {
      // 4xx = the server refused the message (retrying won't help).
      // Network error / 5xx = keep it in the outbox; it is retried later.
      if (err.status >= 400 && err.status < 500) {
        this.h.onReject({ clientMsgId: p.clientMsgId, code: err.status, reason: err.message });
      }
    });
    return true;
  }

  sendReceipt(to, type, upTo) {
    if (!this.ready) return false;
    api.receipt(to, type, upTo).catch(() => {}); // a lost receipt is re-sent with the next one
    return true;
  }

  sendTyping(to) {
    if (this.ready) api.typing(to).catch(() => {});
  }

  async ping() {
    const t = performance.now();
    await api.ping(t, this.lastRtt);
    this.lastRtt = performance.now() - t;
    this.h.onRtt(this.lastRtt);
    return this.lastRtt;
  }

  kick() {
    if (this.stopped || this.ready) return;
    this.retries = 0;
    this.open();
  }

  stop() {
    this.stopped = true;
    this.ready = false;
    clearTimeout(this.reconnectTimer);
    clearInterval(this.heartbeat);
    if (this.es) this.es.close();
    this.es = null;
  }
}
