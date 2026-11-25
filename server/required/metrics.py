"""
Real-time server metrics for monitoring and debugging.

The protocol modules call record_in / record_out / record_message.
Once per second, tick() closes the current one-second "bucket" and
appends it to a 60-second history. That history gives the live
transmission rates (messages/s, bytes/s) shown on /dashboard.
"""

import asyncio
import json
import time
from collections import deque

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..extras import database
from .hub import hub

router = APIRouter()


class Metrics:
    def __init__(self):
        self.started_at = time.time()
        self.total_messages = 0
        self.total_bytes_in = 0
        self.total_bytes_out = 0
        self.messages_by_protocol = {"ws": 0, "http": 0}
        self.duplicates_rejected = 0
        self.errors = 0
        self._bucket = self._empty_bucket()
        self.history = deque(maxlen=60)              # last 60 seconds
        self.processing_ms = deque(maxlen=500)       # recent SEND handling times
        self.events = deque(maxlen=80)               # human-readable log
        self.server_info: dict = {}                  # filled in by main.py

    @staticmethod
    def _empty_bucket() -> dict:
        return {"messages": 0, "bytes_in": 0, "bytes_out": 0}

    # ---- called by the protocol modules ----------------------------------
    def record_in(self, nbytes: int) -> None:
        self.total_bytes_in += nbytes
        self._bucket["bytes_in"] += nbytes

    def record_out(self, nbytes: int) -> None:
        self.total_bytes_out += nbytes
        self._bucket["bytes_out"] += nbytes

    def record_message(self, protocol: str, processing_ms: float, duplicate: bool) -> None:
        if duplicate:
            self.duplicates_rejected += 1
            return
        self.total_messages += 1
        self.messages_by_protocol[protocol] = self.messages_by_protocol.get(protocol, 0) + 1
        self._bucket["messages"] += 1
        self.processing_ms.append(processing_ms)

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.events.append({"time": stamp, "text": text})
        print(f"[{stamp}] {text}", flush=True)

    # ---- once per second ---------------------------------------------------
    def tick(self) -> None:
        self._bucket["t"] = time.strftime("%H:%M:%S")
        self.history.append(self._bucket)
        self._bucket = self._empty_bucket()

    def snapshot(self) -> dict:
        conns = hub.all_connections()
        times = sorted(self.processing_ms)
        last = self.history[-1] if self.history else self._empty_bucket()
        return {
            "uptime_s": round(time.time() - self.started_at),
            "server": self.server_info,
            "connections": {
                "total": len(conns),
                "ws": sum(1 for c in conns if c.protocol == "ws"),
                "sse": sum(1 for c in conns if c.protocol == "sse"),
            },
            "online_users": hub.online_usernames(),
            "messages": {
                "total_stored": database.count_messages(),
                "since_start": self.total_messages,
                "by_protocol": self.messages_by_protocol,
                "duplicates_rejected": self.duplicates_rejected,
            },
            "rates_now": {  # the last full second
                "messages_per_s": last["messages"],
                "bytes_in_per_s": last["bytes_in"],
                "bytes_out_per_s": last["bytes_out"],
            },
            "totals": {"bytes_in": self.total_bytes_in, "bytes_out": self.total_bytes_out},
            "processing_ms": {
                "avg": round(sum(times) / len(times), 3) if times else None,
                "p95": round(times[int(len(times) * 0.95) - 1], 3) if times else None,
            },
            "errors": self.errors,
            "history": list(self.history),
            "connection_list": [c.info() for c in conns],
            "events": list(self.events)[-30:],
        }


metrics = Metrics()


async def ticker() -> None:
    """Background task started by main.py."""
    while True:
        await asyncio.sleep(1)
        metrics.tick()


# ---------------------------------------------------------------- routes ---

@router.get("/api/metrics")
async def get_metrics():
    """One snapshot as JSON (handy for debugging with a browser or curl)."""
    return metrics.snapshot()


@router.get("/api/metrics/stream")
async def stream_metrics():
    """Push a fresh snapshot every second using Server-Sent Events."""
    async def events():
        while True:
            yield f"data: {json.dumps(metrics.snapshot())}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(events(), headers={"Content-Type": "text/event-stream",
                                                "Cache-Control": "no-cache, no-transform"})
