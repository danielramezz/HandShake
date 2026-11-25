"""
Application-layer protocol #2: HTTP REST + Server-Sent Events (SSE).

Plain HTTP is request/response: the server can only answer, never start
talking. So this protocol uses two HTTP mechanisms:

  * Sending   -> POST /api/messages      (one HTTP request per message,
                                          JSON body; the 200 response IS the ACK)
  * Receiving -> GET  /api/stream        (one long HTTP response that never
                                          ends; the server writes events into it)

SSE wire format (text/event-stream). Each event ends with a blank line:

    id: 42                      <- the message id
    event: message
    data: {"id": 42, "from": "alice", ...}

The browser's EventSource remembers the last "id:" it saw. If the stream
breaks, it reconnects by itself and sends the header `Last-Event-ID: 42`.
The server then replays everything after 42. That is offline sync built
into the protocol.
"""

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config
from ..extras import database, tunnel
from ..extras.auth import require_user
from . import messaging
from .hub import Connection, hub
from .metrics import metrics

router = APIRouter()


class SendBody(BaseModel):
    client_msg_id: str
    to: str
    body: str
    encrypted: bool = False
    sent_at: float | None = None


class ReceiptBody(BaseModel):
    to: str          # whose messages I received / read
    type: str        # "delivered" or "read"
    up_to: int       # every message from them with id <= up_to


class TypingBody(BaseModel):
    to: str


class PingBody(BaseModel):
    t: float
    rtt: float | None = None


def _json_size(data: dict) -> int:
    return len(json.dumps(data).encode("utf-8"))


# ------------------------------------------------------- sending (POST) ---

@router.post("/api/messages")
async def post_message(payload: SendBody, request: Request):
    user = require_user(request)
    metrics.record_in(int(request.headers.get("content-length") or 0))
    try:
        message, duplicate = messaging.send_message(
            user, payload.to, payload.client_msg_id, payload.body,
            encrypted=payload.encrypted, sent_at=payload.sent_at, protocol="http")
    except messaging.MessageError as exc:
        metrics.errors += 1
        raise HTTPException(exc.code, exc.reason)

    for conn in hub.connections_of(user["id"]):
        if conn.protocol == "sse":
            conn.messages_in += 1

    ack = {"ack": True, "client_msg_id": message["client_msg_id"], "id": message["id"],
           "server_ts": message["server_ts"], "duplicate": duplicate}
    metrics.record_out(_json_size(ack))
    return ack


@router.get("/api/messages")
async def get_messages(request: Request, after: int = 0):
    """Plain history download: every message of mine with id > after."""
    user = require_user(request)
    return {"messages": database.messages_after(user["id"], after)}


@router.post("/api/receipts")
async def post_receipt(payload: ReceiptBody, request: Request):
    """Protocol 2 version of the HSP RECEIPT command."""
    user = require_user(request)
    try:
        new_state = messaging.send_receipt(user, payload.to, payload.type, payload.up_to)
    except messaging.MessageError as exc:
        raise HTTPException(exc.code, exc.reason)
    return {"ok": True, "changed": new_state is not None}


@router.post("/api/typing")
async def post_typing(payload: TypingBody, request: Request):
    """Protocol 2 version of the HSP TYPING command (not stored)."""
    user = require_user(request)
    try:
        messaging.send_typing(user, payload.to)
    except messaging.MessageError as exc:
        raise HTTPException(exc.code, exc.reason)
    return {"ok": True}


@router.post("/api/ping")
async def ping(payload: PingBody, request: Request):
    """Echo for round-trip-time measurements over HTTP."""
    user = require_user(request)
    for conn in hub.connections_of(user["id"]):
        if conn.protocol == "sse" and payload.rtt is not None:
            conn.rtt_ms = payload.rtt
    return {"t": payload.t}


# ---------------------------------------------------- receiving (SSE) ---

def sse_event(event: str, data: dict, event_id: int | None = None) -> str:
    """Format one Server-Sent Event."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"


@router.get("/api/stream")
async def stream(request: Request, after: int = 0):
    user = require_user(request)

    # Resume point: the larger of ?after= and the Last-Event-ID header.
    last_event_id = request.headers.get("last-event-id", "")
    if last_event_id.isdigit():
        after = max(after, int(last_event_id))

    client_ip, client_port, via = tunnel.real_client(
        request.client.host if request.client else "?", request.client.port if request.client else 0,
        request.headers)

    async def events():
        server_ip, server_port = request.scope.get("server") or ("?", 0)
        conn = Connection(user, "sse", client_ip, client_port, server_ip, server_port, via)
        hub.add(conn)
        metrics.log(f"{user['username']} connected via HTTP/SSE from {client_ip}:{client_port}")

        def out(chunk: str) -> str:
            metrics.record_out(len(chunk.encode("utf-8")))
            return chunk

        try:
            yield out("retry: 2000\n\n")  # tell EventSource: reconnect after 2 s

            # Offline sync, exactly like HSP's backlog.
            backlog = database.messages_after(user["id"], after)
            for m in backlog:
                yield out(sse_event("message", m, m["id"]))
            conn.messages_out += len(backlog)
            synced_up_to = backlog[-1]["id"] if backlog else 0
            yield out(sse_event("synced", {"count": len(backlog), "last_id": synced_up_to or after}))

            idle = 0.0
            while True:
                try:
                    event = await asyncio.wait_for(conn.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Nothing to send. Did the phone go away?
                    if await request.is_disconnected():
                        break
                    idle += 1.0
                    if idle >= config.SSE_KEEPALIVE_SECONDS:
                        idle = 0.0
                        yield out(": keepalive\n\n")  # a comment line; keeps NAT/Wi-Fi state alive
                    continue

                idle = 0.0
                if event["type"] == "message":
                    m = event["message"]
                    if m["id"] <= synced_up_to:
                        continue
                    conn.messages_out += 1
                    yield out(sse_event("message", m, m["id"]))
                elif event["type"] in ("presence", "receipt", "typing"):
                    data = {k: v for k, v in event.items() if k != "type"}
                    yield out(sse_event(event["type"], data))
                # ack / pong / error events are WebSocket-only; HTTP answers them directly.
        finally:
            hub.remove(conn)
            metrics.log(f"{user['username']} disconnected (HTTP/SSE {client_ip}:{client_port})")

    # Plain "text/event-stream" (the type the SSE standard specifies).
    # Note: the free Cloudflare quick tunnel (--share) buffers this stream, so
    # remote phones use Protocol 1 (WebSocket); SSE is for phones on the Wi-Fi.
    return StreamingResponse(events(), headers={
        "Content-Type": "text/event-stream",
        # no-cache: never cache a live stream. no-transform: proxies must not
        # compress or otherwise alter it.
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "X-Server-Time": str(round(time.time() * 1000)),
    })
