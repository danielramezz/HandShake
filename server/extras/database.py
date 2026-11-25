"""
SQLite storage for users, sessions and messages (additional feature #1).

Why a database?
  * Messages survive a server restart and a phone going offline.
  * A phone that reconnects asks for "every message after id N",
    so the server must remember messages -> no message loss.
  * UNIQUE(sender_id, client_msg_id) makes the database itself reject a
    retransmitted copy of a message -> no message duplication.

The server is a single asyncio process, so one shared connection is enough.
"""

import sqlite3
import time
from pathlib import Path

_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    display_name  TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    salt          TEXT    NOT NULL,
    created_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,      -- value of the session cookie
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- global sequence number
    sender_id     INTEGER NOT NULL REFERENCES users(id),
    recipient_id  INTEGER NOT NULL REFERENCES users(id),
    client_msg_id TEXT    NOT NULL,   -- id chosen by the sending phone
    body          TEXT    NOT NULL,   -- plaintext OR ciphertext (E2EE)
    encrypted     INTEGER NOT NULL DEFAULT 0,
    protocol      TEXT    NOT NULL,   -- 'ws' or 'http' (for statistics)
    sent_at       REAL,               -- sender's clock (ms)
    server_ts     REAL    NOT NULL,   -- server's clock (ms)
    UNIQUE (sender_id, client_msg_id) -- duplicate protection
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_sender    ON messages(sender_id, id);

-- Read receipts: "reader has received / read every message from sender
-- with id <= N". One row per pair, so it works like a cumulative ACK.
CREATE TABLE IF NOT EXISTS receipts (
    reader_id       INTEGER NOT NULL REFERENCES users(id),
    sender_id       INTEGER NOT NULL REFERENCES users(id),
    delivered_up_to INTEGER NOT NULL DEFAULT 0,
    read_up_to      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (reader_id, sender_id)
);
"""

# Columns added after the first version. _migrate() adds them to old databases.
NEW_USER_COLUMNS = {
    "last_seen": "REAL",          # when the user's last socket closed (ms)
    "avatar_hue": "INTEGER",      # chosen avatar colour 0-359 (NULL = automatic)
}


def init(path: Path) -> None:
    """Open (or create) the database file and make sure the tables exist."""
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row          # rows behave like dicts
    _conn.execute("PRAGMA journal_mode=WAL")  # safer writes
    _conn.executescript(SCHEMA)
    _migrate()
    _conn.commit()


def _migrate() -> None:
    """Add columns that older database files do not have yet."""
    existing = {row["name"] for row in _conn.execute("PRAGMA table_info(users)")}
    for name, sql_type in NEW_USER_COLUMNS.items():
        if name not in existing:
            _conn.execute(f"ALTER TABLE users ADD COLUMN {name} {sql_type}")


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("database.init() was not called")
    return _conn


def now_ms() -> float:
    return time.time() * 1000


# ----------------------------------------------------------------- users ---

def create_user(username: str, display_name: str, password_hash: str, salt: str) -> dict:
    cur = _db().execute(
        "INSERT INTO users (username, display_name, password_hash, salt, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, display_name, password_hash, salt, now_ms()),
    )
    _db().commit()
    return get_user_by_id(cur.lastrowid)


def get_user_by_id(user_id: int) -> dict | None:
    row = _db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    row = _db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    rows = _db().execute(
        "SELECT id, username, display_name, last_seen, avatar_hue FROM users ORDER BY username")
    return [dict(r) for r in rows]


def update_profile(user_id: int, display_name: str, avatar_hue: int | None) -> dict:
    _db().execute("UPDATE users SET display_name = ?, avatar_hue = ? WHERE id = ?",
                  (display_name, avatar_hue, user_id))
    _db().commit()
    return get_user_by_id(user_id)


def set_last_seen(user_id: int) -> float:
    stamp = now_ms()
    _db().execute("UPDATE users SET last_seen = ? WHERE id = ?", (stamp, user_id))
    _db().commit()
    return stamp


# -------------------------------------------------------------- sessions ---

def create_session(token: str, user_id: int) -> None:
    _db().execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, now_ms()),
    )
    _db().commit()


def get_session_user(token: str) -> dict | None:
    row = _db().execute(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
        "WHERE sessions.token = ?",
        (token,),
    ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    _db().execute("DELETE FROM sessions WHERE token = ?", (token,))
    _db().commit()


# -------------------------------------------------------------- messages ---

_MESSAGE_SELECT = """
    SELECT m.id, m.client_msg_id, m.body, m.encrypted, m.protocol,
           m.sent_at, m.server_ts,
           s.username AS sender, r.username AS recipient
    FROM messages m
    JOIN users s ON s.id = m.sender_id
    JOIN users r ON r.id = m.recipient_id
"""


def _to_public(row: sqlite3.Row) -> dict:
    """Convert a database row into the message format both protocols send."""
    return {
        "id": row["id"],
        "client_msg_id": row["client_msg_id"],
        "from": row["sender"],
        "to": row["recipient"],
        "body": row["body"],
        "encrypted": bool(row["encrypted"]),
        "protocol": row["protocol"],
        "sent_at": row["sent_at"],
        "server_ts": row["server_ts"],
    }


def save_message(sender_id: int, recipient_id: int, client_msg_id: str, body: str,
                 encrypted: bool, protocol: str, sent_at: float | None) -> tuple[dict, bool]:
    """
    Store a message. Returns (message, is_duplicate).

    If this sender already sent a message with the same client_msg_id
    (a retransmission), nothing new is stored and the ORIGINAL message
    is returned with is_duplicate=True.
    """
    cur = _db().execute(
        "INSERT OR IGNORE INTO messages "
        "(sender_id, recipient_id, client_msg_id, body, encrypted, protocol, sent_at, server_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sender_id, recipient_id, client_msg_id, body, int(encrypted), protocol, sent_at, now_ms()),
    )
    _db().commit()
    is_duplicate = cur.rowcount == 0

    row = _db().execute(
        _MESSAGE_SELECT + " WHERE m.sender_id = ? AND m.client_msg_id = ?",
        (sender_id, client_msg_id),
    ).fetchone()
    return _to_public(row), is_duplicate


def messages_after(user_id: int, after_id: int) -> list[dict]:
    """Every message sent BY or TO this user with id > after_id, oldest first."""
    rows = _db().execute(
        _MESSAGE_SELECT + " WHERE (m.sender_id = ? OR m.recipient_id = ?) AND m.id > ? "
        "ORDER BY m.id",
        (user_id, user_id, after_id),
    )
    return [_to_public(r) for r in rows]


def latest_message_id(sender_id: int, recipient_id: int) -> int:
    """Highest message id sent from sender to recipient (0 if none)."""
    row = _db().execute("SELECT MAX(id) FROM messages WHERE sender_id = ? AND recipient_id = ?",
                        (sender_id, recipient_id)).fetchone()
    return row[0] or 0


# -------------------------------------------------------------- receipts ---

def update_receipt(reader_id: int, sender_id: int, kind: str, up_to: int) -> dict | None:
    """
    Move a receipt forward. Receipts only ever grow (like a cumulative ACK),
    and "read" implies "delivered". Returns the new state, or None if nothing
    changed (then there is nothing to tell the sender).
    """
    row = _db().execute("SELECT delivered_up_to, read_up_to FROM receipts "
                        "WHERE reader_id = ? AND sender_id = ?", (reader_id, sender_id)).fetchone()
    delivered, read = (row["delivered_up_to"], row["read_up_to"]) if row else (0, 0)
    new_read = max(read, up_to) if kind == "read" else read
    new_delivered = max(delivered, up_to, new_read)
    if (new_delivered, new_read) == (delivered, read):
        return None
    _db().execute(
        "INSERT INTO receipts (reader_id, sender_id, delivered_up_to, read_up_to) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (reader_id, sender_id) DO UPDATE SET "
        "delivered_up_to = excluded.delivered_up_to, read_up_to = excluded.read_up_to",
        (reader_id, sender_id, new_delivered, new_read))
    _db().commit()
    return {"delivered_up_to": new_delivered, "read_up_to": new_read}


def receipts_for_sender(sender_id: int) -> dict[int, dict]:
    """How far each person has received / read the messages that sender_id sent them."""
    rows = _db().execute("SELECT reader_id, delivered_up_to, read_up_to FROM receipts "
                         "WHERE sender_id = ?", (sender_id,))
    return {r["reader_id"]: {"delivered_up_to": r["delivered_up_to"], "read_up_to": r["read_up_to"]}
            for r in rows}


def count_messages() -> int:
    return _db().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
