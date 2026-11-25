"""
The messaging core, shared by BOTH application-layer protocols.

protocol_ws.py (HSP over WebSocket) and protocol_http.py (REST + SSE) only
translate their own wire format into calls to send_message(). That keeps
the rules for "no loss" and "no duplication" in ONE place:

  1. The sending phone gives every message a unique client_msg_id and keeps
     it in an outbox until it gets an ACK (like rdt3.0 from Lecture 6:
     sequence numbers + ACK + retransmission on timeout).
  2. The server stores the message first, THEN acknowledges it. An ACK
     means "it is safely in the database".
  3. If the ACK is lost, the phone retransmits. The database sees the same
     (sender, client_msg_id) again and returns the stored original, so
     nothing is stored twice and nothing is delivered twice.
  4. Each stored message gets an increasing id. A reconnecting phone says
     "I have everything up to id N" and receives the rest. This works like
     a cumulative ACK.
"""

import re
import time

from .. import config
from ..extras import database
from .hub import hub
from .metrics import metrics

CLIENT_MSG_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class MessageError(Exception):
    """A SEND that breaks the rules. code follows HTTP status code meanings."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def send_message(sender: dict, to_username: str, client_msg_id: str, body: str,
                 encrypted: bool, sent_at: float | None, protocol: str) -> tuple[dict, bool]:
    """
    Validate, store and deliver one message.
    Returns (stored_message, is_duplicate). Raises MessageError if invalid.
    """
    started = time.perf_counter()

    # --- validation (correct encapsulation: every field must make sense) ---
    if not CLIENT_MSG_ID_RE.match(client_msg_id or ""):
        raise MessageError(400, "msg-id must be 8-64 characters: letters, digits or '-'")
    if not body:
        raise MessageError(400, "empty message")
    if len(body) > config.MAX_BODY_CHARS:
        raise MessageError(413, "message too long")
    recipient = database.get_user_by_username((to_username or "").lower())
    if recipient is None:
        raise MessageError(404, f"no such user {to_username!r}")
    if recipient["id"] == sender["id"]:
        raise MessageError(400, "cannot message yourself")

    # --- store first, so an ACK always means "saved" ---
    message, duplicate = database.save_message(
        sender["id"], recipient["id"], client_msg_id, body, encrypted, protocol, sent_at)

    # --- deliver to every open connection of BOTH people in the chat ---
    # (the sender's other devices also need to see what was sent)
    if not duplicate:
        event = {"type": "message", "message": message}
        hub.push_to_user(recipient["id"], event)
        hub.push_to_user(sender["id"], event)

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.record_message(protocol, elapsed_ms, duplicate)
    if duplicate:
        metrics.log(f"duplicate #{message['id']} from {sender['username']} ignored (retransmission)")
    return message, duplicate


# ------------------------------------------------------- receipts + typing ---

RECEIPT_KINDS = ("delivered", "read")


def send_receipt(reader: dict, to_username: str, kind: str, up_to) -> dict | None:
    """
    The reader says: "I have received/read every message you sent me up to id N".
    Stored (so the sender sees it even after reconnecting) and forwarded to the
    sender's open connections. Like a cumulative ACK, but end to end.
    Returns the new receipt state, or None if it did not move forward.
    """
    if kind not in RECEIPT_KINDS:
        raise MessageError(400, "type must be 'delivered' or 'read'")
    try:
        up_to = int(up_to)
    except (TypeError, ValueError):
        raise MessageError(400, "up-to must be a message id")
    sender = database.get_user_by_username((to_username or "").lower())
    if sender is None or sender["id"] == reader["id"]:
        raise MessageError(404, f"no such user {to_username!r}")

    # You cannot claim to have read messages that do not exist yet.
    up_to = min(max(up_to, 0), database.latest_message_id(sender["id"], reader["id"]))
    new_state = database.update_receipt(reader["id"], sender["id"], kind, up_to)
    if new_state:
        hub.push_to_user(sender["id"], {"type": "receipt", "from": reader["username"], **new_state})
    return new_state


def send_typing(sender: dict, to_username: str) -> None:
    """
    "I am typing" is a temporary signal: forwarded to the recipient, never stored,
    no ACK. If one is lost nothing breaks; the next keystroke sends another.
    """
    recipient = database.get_user_by_username((to_username or "").lower())
    if recipient is None or recipient["id"] == sender["id"]:
        raise MessageError(404, f"no such user {to_username!r}")
    hub.push_to_user(recipient["id"], {"type": "typing", "from": sender["username"]})
