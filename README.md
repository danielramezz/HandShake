# Handshake: Real-Time Client-Server Messaging

**CSEN 503 · Winter 2026 · Team:** _name (ID)_, _name (ID)_, _name (ID)_, _name (ID)_

- One laptop runs the server; every team member's phone is a client (mobile web app) on the same Wi-Fi.
- **Two application-layer protocols:** HSP/1.0, our own protocol over WebSocket, and HTTP REST + Server-Sent Events.
- **Two additional features:** user registration with SQLite persistence, and symmetric AES-256-GCM end-to-end encryption.
- Run: `.\start.bat` (Windows) or `./start.sh`, then scan the QR code shown in the terminal. Details: [docs/RUNNING.md](docs/RUNNING.md).

## 1. Architecture

```
 Phone (browser)                                Laptop: one Python process (asyncio), port 8443, TLS
 ┌─────────────────────────┐   wss://  /ws      ┌───────────────┐
 │ app.js  (UI + state)    │ ─── HSP/1.0 ─────▶ │ protocol_ws   │──┐
 │ transport-ws.js  ───────┤                    └───────────────┘  │   ┌──────────────┐   ┌────────────┐
 │ transport-http.js ──────┤   https:// POST    ┌───────────────┐  ├──▶│ messaging.py │──▶│ database   │
 │ store.js (cache+outbox) │ ─── + SSE stream ▶ │ protocol_http │──┘   │ (shared core)│   │ (SQLite)   │
 │ crypto.js (AES-GCM)     │                    └───────────────┘      └──────┬───────┘   └────────────┘
 └─────────────────────────┘        ▲                                         ▼
                                    └──────── events ◀──── hub.py (one outgoing queue per socket)
```

- **Layered design:** each protocol module only translates its wire format. The rules for storing, ACKing and delivering live once, in `messaging.py`.
- **Hub with a queue per connection:** the hub only enqueues events. Each socket has its own writer task. A slow or dead phone blocks only its own queue, never the others.
- **Both transports have the same interface** (`start / send / ping / stop`), so the phone can switch protocols at runtime from Settings.
- **One port and one process** serve the web client, the REST API, the SSE streams, the WebSocket and the `/dashboard`.

## 2. Network & link layer

- **Discovery:** the Wi-Fi router's DHCP server gives the laptop and the phones private IPs in one subnet (e.g. `192.168.1.0/24`). At start-up the server finds its LAN IP (`network.py:get_lan_ip`) and prints the URL plus a **QR code**, so nobody types an IP.
- **Listening:** `0.0.0.0:8443` (all interfaces). Phones reach the laptop directly through the access point, with no router hop and no NAT between them.
- **TLS with a local CA:** the server creates a *Handshake Local CA* once, and uses it to sign a server certificate whose SAN lists the laptop's current IPs (re-issued automatically when the IP changes). Each iPhone installs and trusts the CA once (`/ca.cer`); iOS refuses WebSockets to untrusted certificates.
- **Why TLS:** it encrypts every phone-to-laptop hop and makes the page a *secure origin*, which the browser requires for Web Crypto (E2EE). If a phone still cannot open a WebSocket, the client falls back to Protocol 2 by itself.
- **No internet needed:** no CDNs or web fonts. Everything is served from the laptop.
- **Optional remote access (`--share`, off by default):** the laptop's private IP is behind NAT, so outsiders can't connect in. `cloudflared` opens an *outgoing* connection to Cloudflare (NAT allows that) and gets a public `https://…trycloudflare.com` address that forwards back down that connection. WebSocket passes through; the free tunnel buffers SSE, so remote phones use Protocol 1.

## 3. Transport layer

- **TCP for everything** (WebSocket and HTTP both run on TCP): reliable, in-order delivery, which a chat needs (Lecture 6 table: text messaging = no loss).
- **Many simultaneous clients:** one asyncio event loop. Each phone gets its own TCP socket, demultiplexed by its **4-tuple**. The dashboard lists every socket's client and server `IP:port`.
- **Bidirectional:** a WebSocket is full duplex. HTTP gets server push through one long-lived SSE response.
- **No message loss (application-level rdt, like rdt3.0):**
  - A TCP ACK only proves bytes reached the server's kernel, not that the app stored them. So we add an **application ACK, sent only after the database commit**.
  - The phone keeps every message in a persistent **outbox** (localStorage) and **retransmits** anything not ACKed within 4 s, even after a reload.
- **No duplication:**
  - Every message has a sender-chosen `client_msg_id` (UUID), and the database enforces `UNIQUE(sender, client_msg_id)`. A retransmission returns the original row (`duplicate: yes`) and is never delivered twice.
  - Phones also key messages by `sender:client_msg_id`, so the same message arriving live and in a sync shows once.
- **Instant sync on reconnect:** messages get increasing server ids. On reconnect the phone says "I have everything up to id N" (HSP `HELLO last-id`, or SSE `Last-Event-ID`). The server replays the rest at once, like a **cumulative ACK**.
- **Failure detection:** a 10 s heartbeat (PING/PONG) detects dead Wi-Fi links; reconnect uses exponential back-off (0.5 s up to 8 s) plus the browser's `online` event.
- **Graceful closed sockets:** each connection's `finally` block removes only that connection and broadcasts presence `offline`. A malformed frame gets `ERROR 400` and the session stays open. SSE disconnects are found within 1 s through `request.is_disconnected()`.

## 4. Application layer: two protocols

**Protocol 1: HSP/1.0 (custom, inside WebSocket text frames).** It copies the HTTP message format from Lecture 2:

```
HSP/1.0 SEND\r\n            ← start line: version + command
msg-id: 3f2a9c1e-…\r\n      ← header lines "name: value"
to: bob\r\n
enc: aes-gcm\r\n
content-length: 44\r\n      ← body length in UTF-8 bytes (checked on receipt)
\r\n                        ← blank line ends the headers
<body: plaintext or base64 ciphertext>
```

| Command | Dir | Key headers | Meaning |
|---|---|---|---|
| `HELLO` | C→S | `last-id` | Must be the first frame (like SMTP `EHLO`). Asks for every message after `last-id` |
| `WELCOME` / `SYNCED` | S→C | `user` / `count, last-id` | Handshake accepted / backlog finished |
| `SEND` | C→S | `msg-id, to, enc, sent-at` | A new message |
| `ACK` | S→C | `msg-id, id, duplicate` | Stored in the database (`duplicate: yes` = retransmission ignored) |
| `MSG` | S→C | `id, from, to, msg-id, enc, via` | Message delivery (live or backlog) |
| `PRESENCE` | S→C | `user, status, name, last-seen` | A user came online or went offline, or changed their profile |
| `RECEIPT` | both | C→S `to, type, up-to` · S→C `from, delivered, read` | "I received/read your messages up to id N": cumulative, stored (✓✓ and read ticks) |
| `TYPING` | both | `to` / `from` | Temporary "typing…" signal: forwarded, **never stored** |
| `PING` / `PONG` | both | `t, rtt` | Heartbeat + round-trip time |
| `ERROR` | S→C | `code, reason, msg-id` | HTTP-style codes: 400 bad frame, 404 no such user, 413 too long |
| `BYE` | C→S | none | Clean close |

- **Encapsulation is checked on both sides** (`server/required/hsp.py`, `ui/required/hsp.js`): version, known command, `name: value` headers, no newline inside header values, and `content-length` must equal the body's byte size.
- WebSocket already marks message boundaries; `content-length` is an integrity check, and it is what would let HSP run over a raw TCP byte stream.
- **Two delivery classes:** `SEND` is *reliable* (stored, ACKed, retransmitted). `TYPING` is *best-effort* (no storage, no ACK, a lost one is harmless). It's the TCP-vs-UDP trade-off, made at the application layer.

**Protocol 2: HTTP REST + Server-Sent Events (standard protocols, JSON bodies).**

| Request | Purpose |
|---|---|
| `POST /api/messages` `{client_msg_id, to, body, encrypted}` | Send. The **200 response is the ACK** (`{id, duplicate}`) |
| `GET /api/stream?after=N` (`text/event-stream`) | Receive. One endless response with `id: <msg id>` / `event: message\|presence\|synced` / `data: {json}` |
| `POST /api/receipts` · `POST /api/typing` | Same meaning as HSP `RECEIPT` / `TYPING`; they arrive as SSE `event: receipt` / `event: typing` |
| `GET /api/messages?after=N` · `POST /api/ping` · `PATCH /api/me` | History download · RTT probe · edit profile |

- On a broken stream, the browser's EventSource reconnects by itself and sends `Last-Event-ID`, so resuming is built into the protocol.

**Comparison:** HSP uses one persistent full-duplex connection and small text headers per message. HTTP opens a request/response per send, carrying full HTTP headers, but it is standard, stateless per request, and works through any proxy. Measured results are in §6.

**Other application-layer protocols used:** HTTP for login and the static files (cookies keep the session; conditional GET returns `304 Not Modified`), and SSE for the dashboard's live metrics.

## 5. Additional features

**A. Registration + persistent storage (SQLite)**
- Tables: `users`, `sessions`, `messages` (`server/extras/database.py`). Message history follows the user to any device: a fresh phone logs in, says `last-id: 0` and receives the full history.
- Passwords are stored as **PBKDF2-SHA256 (200k rounds, random salt)**, never in plain text, and compared with `hmac.compare_digest`. Hashing runs in a worker thread so the event loop never stalls.
- Sessions work like the **cookies from Lecture 2**: a random 256-bit token in an `HttpOnly; Secure; SameSite=Strict` cookie maps to a database row. The browser attaches it to REST calls, the SSE stream and the WebSocket upgrade.

**B. Symmetric end-to-end encryption (AES-256-GCM, `ui/extras/crypto.js`)**
- Both people type the same shared secret. **PBKDF2 (150k rounds)** turns it into a 256-bit AES key inside each phone. The key is not extractable and is never sent anywhere.
- Each message gets a fresh 12-byte IV, and the body on the wire is `base64(IV ‖ ciphertext ‖ 16-byte tag)`. The **server and database only ever see ciphertext**.
- The GCM tag provides **integrity**: a wrong key or a tampered message fails to decrypt (much stronger than the Internet checksum).
- A **safety code** (SHA-256 of salt + secret) lets two people confirm out loud that they use the same key.

## 6. Performance evaluation

Method: `python -m benchmarks.bench latency|scale|crypto`. Sender and receiver run in one process, so a single clock (no skew) measures **one-way delivery time** (sender → server → receiver), sending one message at a time unless stated. Results are in `benchmarks/results/`.

| Metric | Localhost baseline (laptop only, 10 Nov 2026) | Demo Wi-Fi (fill in) |
|---|---|---|
| 1. Latency, HSP/WebSocket, 100-char message | mean 2.9 ms · p95 3.6 ms | _to measure_ |
| 2. Protocol comparison, HTTP POST + SSE | mean 5.7 ms · p95 7.2 ms (≈2× HSP) | _to measure_ |
| 3. Scalability, all clients sending at once (10 msgs each) | 2 clients: 45 ms · 10: 202 ms · 25: 463 ms (mean); throughput ≈ 420 msg/s throughout | _to measure_ |
| E2EE comparison: plaintext vs AES-GCM | 3.1 ms vs 3.0 ms (no measurable cost) · frame 207 B → 282 B (+36 %) | _to measure_ |

- **Why HTTP is slower:** each send is a full HTTP request/response with its headers. HSP reuses one open connection.
- **Why throughput levels off:** every message is committed to SQLite **before** the ACK (the durability that gives "no loss"). Bursts queue behind those disk writes, so latency grows with the number of clients while throughput stays flat.
- **Network conditions:** Settings → *Network test* sends 20 pings (avg / min / max / jitter) over the active protocol. Run it near the router and far from it.

## 7. Repository structure

```
server/   main.py (start-up) · config.py (settings)
  required/  network.py (network/link) · hub.py (many clients, closed sockets) · hsp.py + protocol_ws.py
             (protocol 1) · protocol_http.py (protocol 2) · messaging.py (no loss/dup) · metrics.py
  extras/    auth.py (registration) · database.py (persistent storage) · tunnel.py (remote access)
ui/       index.html · dashboard.html · manifest.webmanifest · css/ · img/
  required/  app.js · api.js · store.js · utils.js · hsp.js · transport-ws.js · transport-http.js · dashboard.js
  extras/    crypto.js (end-to-end encryption) · password.js (sign-up hint) · sound.js (notifications)
local/    data · certs · tools (created at run time, this laptop only, not on GitHub)
tests/    test_hsp.py · test_server.py        benchmarks/bench.py        docs/RUNNING.md
```
