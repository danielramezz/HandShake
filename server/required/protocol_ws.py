"""
Application-layer protocol #1: HSP/1.0 over WebSocket (endpoint: /ws).

Transport: a WebSocket is ONE long-lived TCP connection (wss:// = TCP + TLS).
It starts as an HTTP request (the "upgrade") and then stays open in both
directions (full duplex), so the server can push messages instantly.

Conversation rules (like SMTP's greeting, then commands):

    client                                 server
    HELLO  (last-id: N)          ---->
                                 <----     WELCOME
                                 <----     MSG ... MSG   (everything after id N)
                                 <----     SYNCED (count, last-id)
    SEND   (msg-id, to, enc)     ---->
                                 <----     ACK   (msg-id, id, duplicate)
                                 <----     MSG   (live messages, any time)
                                 <----     PRESENCE (user, online/offline, last-seen)
    RECEIPT (to, type, up-to)    ---->     "I received/read your messages up to id N"
                                 <----     RECEIPT (from, delivered, read)
    TYPING (to)                  ---->     forwarded as TYPING (from); never stored
    PING   (t, rtt)              ---->
                                 <----     PONG  (t)
    BYE                          ---->     (connection closed)

A broken frame gets an ERROR frame back. The connection stays open, so one
bad message never kills a session.
"""

import asyncio
import time

from fastapi import APIRouter, WebSocket

from .. import config
from ..extras import database, tunnel
from ..extras.auth import user_from_token
from . import hsp, messaging
from .hub import Connection, hub
from .metrics import metrics

router = APIRouter()

HELLO_TIMEOUT_S = 10


# ------------------------------------------------- event -> HSP frame ---

def message_frame(m: dict) -> str:
    return hsp.encode("MSG", {
        "id": m["id"],
        "from": m["from"],
        "to": m["to"],
        "msg-id": m["client_msg_id"],
        "enc": "aes-gcm" if m["encrypted"] else "none",
        "via": m["protocol"],
        "sent-at": m["sent_at"] if m["sent_at"] is not None else "",
        "server-ts": m["server_ts"],
    }, m["body"])


def event_frame(event: dict) -> str:
    """Translate a hub event (a dict) into an HSP frame (text)."""
    kind = event["type"]
    if kind == "message":
        return message_frame(event["message"])
    if kind == "ack":
        return hsp.encode("ACK", {"msg-id": event["client_msg_id"], "id": event["id"],
                                  "server-ts": event["server_ts"],
                                  "duplicate": "yes" if event["duplicate"] else "no"})
    if kind == "presence":
        return hsp.encode("PRESENCE", {
            "user": event["username"],
            "status": "online" if event["online"] else "offline",
            "name": event["display_name"],
            "hue": "" if event["avatar_hue"] is None else event["avatar_hue"],
            "last-seen": "" if event["last_seen"] is None else round(event["last_seen"]),
        })
    if kind == "receipt":
        return hsp.encode("RECEIPT", {"from": event["from"], "delivered": event["delivered_up_to"],
                                      "read": event["read_up_to"]})
    if kind == "typing":
        return hsp.encode("TYPING", {"from": event["from"]})
    if kind == "pong":
        return hsp.encode("PONG", {"t": event["t"]})
    if kind == "error":
        headers = {"code": event["code"], "reason": event["reason"]}
        if event.get("client_msg_id"):
            headers["msg-id"] = event["client_msg_id"]
        return hsp.encode("ERROR", headers)
    raise ValueError(f"unknown event type {kind}")


def error_event(code: int, reason: str, client_msg_id: str | None = None) -> dict:
    return {"type": "error", "code": code, "reason": reason, "client_msg_id": client_msg_id}


# ------------------------------------------------------- writer loop ---

async def writer_loop(ws: WebSocket, conn: Connection, last_id: int) -> None:
    """The ONLY place that writes to this socket (see hub.py for why)."""

    async def send(frame: str) -> None:
        await ws.send_text(frame)
        metrics.record_out(len(frame.encode("utf-8")))

    try:
        await send(hsp.encode("WELCOME", {"user": conn.username, "connection-id": conn.id,
                                          "server-time": round(time.time() * 1000)}))

        # Offline sync: send every message the phone has not seen yet.
        backlog = database.messages_after(conn.user_id, last_id)
        for m in backlog:
            await send(message_frame(m))
        conn.messages_out += len(backlog)
        synced_up_to = backlog[-1]["id"] if backlog else 0
        await send(hsp.encode("SYNCED", {"count": len(backlog), "last-id": synced_up_to or last_id}))

        # Live events from the hub.
        while True:
            event = await conn.queue.get()
            if event["type"] == "message":
                if event["message"]["id"] <= synced_up_to:
                    continue  # already sent as part of the backlog
                conn.messages_out += 1
            await send(event_frame(event))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # the socket broke while writing
        metrics.log(f"write to {conn.username} ({conn.client_ip}) failed: {exc!r}")
        try:
            await ws.close()
        except Exception:
            pass


# ------------------------------------------------- reading one frame ---

def _number(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def handle_frame(conn: Connection, user: dict, text: str) -> bool:
    """Process one frame from the client. Returns False when the client said BYE."""
    metrics.record_in(len(text.encode("utf-8")))
    try:
        command, headers, body = hsp.decode(text)
    except hsp.HSPError as exc:
        metrics.errors += 1
        conn.push(error_event(400, str(exc)))
        return True

    if command == "SEND":
        conn.messages_in += 1
        msg_id = headers.get("msg-id")
        enc = headers.get("enc", "none")
        if enc not in ("none", "aes-gcm"):
            conn.push(error_event(400, "enc must be 'none' or 'aes-gcm'", msg_id))
            return True
        try:
            message, duplicate = messaging.send_message(
                user, headers.get("to"), msg_id, body,
                encrypted=(enc == "aes-gcm"), sent_at=_number(headers.get("sent-at")),
                protocol="ws")
        except messaging.MessageError as exc:
            metrics.errors += 1
            conn.push(error_event(exc.code, exc.reason, msg_id))
            return True
        conn.push({"type": "ack", "client_msg_id": msg_id, "id": message["id"],
                   "server_ts": message["server_ts"], "duplicate": duplicate})

    elif command == "RECEIPT":
        try:
            messaging.send_receipt(user, headers.get("to"), headers.get("type"), headers.get("up-to"))
        except messaging.MessageError as exc:
            metrics.errors += 1
            conn.push(error_event(exc.code, exc.reason))

    elif command == "TYPING":
        try:
            messaging.send_typing(user, headers.get("to"))
        except messaging.MessageError as exc:
            conn.push(error_event(exc.code, exc.reason))

    elif command == "PING":
        conn.rtt_ms = _number(headers.get("rtt"))  # client's last measured RTT
        conn.push({"type": "pong", "t": headers.get("t", "")})

    elif command == "BYE":
        return False

    else:  # HELLO twice, or a server-only command
        conn.push(error_event(400, f"{command} is not allowed here"))
    return True


# ------------------------------------------------------------ endpoint ---

@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # 1) Authenticate with the session cookie sent in the upgrade request.
    user = user_from_token(ws.cookies.get(config.SESSION_COOKIE))
    if user is None:
        await ws.close(code=1008)  # policy violation; browser sees a failed upgrade
        return
    await ws.accept()
    client_ip, client_port, via = tunnel.real_client(
        ws.client.host if ws.client else "?", ws.client.port if ws.client else 0, ws.headers)

    # 2) Rule: the first frame must be HELLO.
    try:
        first = await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S)
        metrics.record_in(len(first.encode("utf-8")))
        command, headers, _ = hsp.decode(first)
        if command != "HELLO":
            raise hsp.HSPError("first frame must be HELLO")
        last_id = int(headers.get("last-id") or 0)
    except Exception as exc:
        try:
            await ws.send_text(event_frame(error_event(400, f"handshake failed: {exc}")))
            await ws.close()
        except Exception:
            pass
        return

    # 3) Register the connection and start the writer.
    server_ip, server_port = ws.scope.get("server") or ("?", 0)
    conn = Connection(user, "ws", client_ip, client_port, server_ip, server_port, via)
    hub.add(conn)
    metrics.log(f"{user['username']} connected via WebSocket from {client_ip}:{client_port}")
    writer = asyncio.create_task(writer_loop(ws, conn, last_id))

    # 4) Reader loop: runs until the client leaves or the socket dies.
    try:
        while True:
            event = await ws.receive()
            if event["type"] == "websocket.disconnect":
                break
            text = event.get("text")
            if text is None:
                conn.push(error_event(400, "HSP frames must be text"))
                continue
            if not handle_frame(conn, user, text):
                break  # BYE
    except Exception as exc:
        metrics.log(f"socket error for {user['username']}: {exc!r}")
    finally:
        # 5) Clean up ONLY this connection; everyone else is unaffected.
        writer.cancel()
        hub.remove(conn)
        metrics.log(f"{user['username']} disconnected (WebSocket {client_ip}:{client_port})")
        try:
            await ws.close()
        except Exception:
            pass
