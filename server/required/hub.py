"""
The Hub keeps track of every open client connection and delivers events.

Each phone that is online has one (or more) Connection objects here, no
matter which protocol it uses (WebSocket/HSP or HTTP/SSE).

Every Connection owns an outgoing QUEUE. The hub only puts events into
queues; it never writes to a socket itself. Each protocol module runs its
own "writer" loop that takes events out of the queue and sends them.
Why? If one phone's socket is slow or dead, only its own writer gets stuck
or fails. Delivering to everybody else is never blocked
-> "server gracefully handles closed sockets without disrupting others".
"""

import asyncio
import itertools
import time

from ..extras import database

_next_id = itertools.count(1)


def presence_event(user: dict, online: bool) -> dict:
    """
    A user's status card: online/offline plus what others need to draw
    them (name, avatar colour, last seen). Sent on connect, on disconnect
    and after a profile change.
    """
    return {
        "type": "presence",
        "username": user["username"],
        "online": online,
        "display_name": user["display_name"],
        "avatar_hue": user.get("avatar_hue"),
        "last_seen": user.get("last_seen"),
    }


class Connection:
    """One open connection (one TCP socket) from one client device."""

    def __init__(self, user: dict, protocol: str, client_ip: str, client_port: int,
                 server_ip: str = "?", server_port: int = 0, via: str = "wifi"):
        self.id = next(_next_id)
        self.user = user
        self.user_id = user["id"]
        self.username = user["username"]
        self.protocol = protocol            # "ws" or "sse"
        self.client_ip = client_ip          # with client_port: half of the
        self.client_port = client_port      # TCP 4-tuple (Lecture 5)
        self.server_ip = server_ip          # the other half: the address the
        self.server_port = server_port      # client actually connected to
        self.via = via                      # "wifi" (same network) or "internet" (tunnel)
        self.connected_at = time.time()
        self.messages_in = 0
        self.messages_out = 0
        self.rtt_ms: float | None = None    # last round-trip time the client reported
        self.queue: asyncio.Queue = asyncio.Queue()

    def push(self, event: dict) -> None:
        """Hand an event to this connection's writer loop (never blocks)."""
        self.queue.put_nowait(event)

    def info(self) -> dict:
        """Summary for the metrics dashboard."""
        return {
            "id": self.id,
            "username": self.username,
            "protocol": self.protocol,
            "client": f"{self.client_ip}:{self.client_port}",
            "server": f"{self.server_ip}:{self.server_port}",
            "via": self.via,
            "connected_for": round(time.time() - self.connected_at),
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "rtt_ms": self.rtt_ms,
        }


class Hub:
    def __init__(self):
        # user_id -> set of that user's open connections (phone + laptop, ...)
        self._by_user: dict[int, set[Connection]] = {}

    def add(self, conn: Connection) -> None:
        conns = self._by_user.setdefault(conn.user_id, set())
        first = not conns
        conns.add(conn)
        if first:  # user just came online
            self.broadcast(presence_event(conn.user, online=True))

    def remove(self, conn: Connection) -> None:
        conns = self._by_user.get(conn.user_id)
        if not conns or conn not in conns:
            return
        conns.discard(conn)
        if not conns:  # that was the user's last connection
            del self._by_user[conn.user_id]
            database.set_last_seen(conn.user_id)
            user = database.get_user_by_id(conn.user_id) or conn.user
            self.broadcast(presence_event(user, online=False))

    def push_to_user(self, user_id: int, event: dict) -> None:
        """Queue an event for every open connection of one user."""
        for conn in list(self._by_user.get(user_id, ())):
            conn.push(event)

    def broadcast(self, event: dict) -> None:
        for conn in self.all_connections():
            conn.push(event)

    def all_connections(self) -> list[Connection]:
        return [c for conns in self._by_user.values() for c in conns]

    def connections_of(self, user_id: int) -> list[Connection]:
        return list(self._by_user.get(user_id, ()))

    def is_online(self, user_id: int) -> bool:
        return user_id in self._by_user

    def online_usernames(self) -> list[str]:
        return sorted({c.username for c in self.all_connections()})


# One shared hub for the whole server.
hub = Hub()
