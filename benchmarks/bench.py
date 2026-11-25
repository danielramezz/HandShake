"""
Performance benchmarks for the report (the three required metrics).

Run from the project folder while the server is running:

    python -m benchmarks.bench latency            # Metric 1+2: latency, HSP/WS vs HTTP/SSE
    python -m benchmarks.bench scale              # Metric 3: latency vs number of connections
    python -m benchmarks.bench crypto             # Extra feature: encrypted vs plaintext

    options: --server https://192.168.1.23:8443  --n 200  --size 100

The sender and the receiver run in THIS one process, so both timestamps come
from the same clock (time.perf_counter). That avoids clock-skew errors
between two devices. To measure over real Wi-Fi, run this script on a second
laptop and point --server at the server laptop.

Every result is printed as a table and saved in benchmarks/results/*.json.
"""

import argparse
import asyncio
import base64
import json
import os
import ssl
import statistics
import time
import uuid
from pathlib import Path

import httpx
import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from server.required import hsp

RESULTS_DIR = Path(__file__).parent / "results"
SKIP_HISTORY = 10**12   # HELLO last-id larger than any id: "send me no old messages"

TLS = ssl.create_default_context()
TLS.check_hostname = False          # the server uses a self-signed certificate
TLS.verify_mode = ssl.CERT_NONE


# ------------------------------------------------------------- clients ---

async def login(server: str, username: str) -> str:
    """Log in (or register) a benchmark user and return its session cookie."""
    async with httpx.AsyncClient(verify=False) as c:
        body = {"username": username, "password": "benchmark", "display_name": username}
        r = await c.post(f"{server}/api/login", json=body)
        if r.status_code != 200:
            r = await c.post(f"{server}/api/register", json=body)
            r.raise_for_status()
        return r.cookies["hs_session"]


class WsClient:
    """Protocol 1 client: HSP over WebSocket."""

    def __init__(self, server, username, cookie):
        self.url = server.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        self.username, self.cookie = username, cookie
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.bytes_sent = 0

    async def start(self):
        self.ws = await websockets.connect(self.url, ssl=TLS if self.url.startswith("wss") else None,
                                           additional_headers={"Cookie": f"hs_session={self.cookie}"},
                                           max_queue=None)
        await self.ws.send(hsp.encode("HELLO", {"last-id": SKIP_HISTORY}))
        while True:  # wait for the handshake to finish
            command, _, _ = hsp.decode(await self.ws.recv())
            if command == "SYNCED":
                break
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        async for frame in self.ws:
            command, headers, body = hsp.decode(frame)
            if command == "MSG" and headers["to"] == self.username:
                await self.inbox.put((time.perf_counter(), body))

    async def send(self, to, body, encrypted=False):
        frame = hsp.encode("SEND", {"msg-id": str(uuid.uuid4()), "to": to,
                                    "enc": "aes-gcm" if encrypted else "none"}, body)
        self.bytes_sent += len(frame.encode())
        await self.ws.send(frame)

    async def stop(self):
        self.reader.cancel()
        await self.ws.close()


class HttpClient:
    """Protocol 2 client: POST to send, Server-Sent Events to receive."""

    def __init__(self, server, username, cookie):
        self.server, self.username = server, username
        self.http = httpx.AsyncClient(verify=False, cookies={"hs_session": cookie}, timeout=None)
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.bytes_sent = 0

    async def start(self):
        ready = asyncio.Event()
        self.reader = asyncio.create_task(self._read(ready))
        await ready.wait()

    async def _read(self, ready):
        async with self.http.stream("GET", f"{self.server}/api/stream?after={SKIP_HISTORY}") as r:
            event = {}
            async for line in r.aiter_lines():
                if line == "":
                    if event.get("event") == "synced":
                        ready.set()
                    elif event.get("event") == "message":
                        m = json.loads(event["data"])
                        if m["to"] == self.username:
                            await self.inbox.put((time.perf_counter(), m["body"]))
                    event = {}
                elif not line.startswith(":"):
                    key, _, value = line.partition(": ")
                    event[key] = value

    async def send(self, to, body, encrypted=False):
        payload = {"client_msg_id": str(uuid.uuid4()), "to": to, "body": body, "encrypted": encrypted}
        self.bytes_sent += len(json.dumps(payload).encode())
        r = await self.http.post(f"{self.server}/api/messages", json=payload)
        r.raise_for_status()

    async def stop(self):
        self.reader.cancel()
        await self.http.aclose()


CLIENTS = {"ws": WsClient, "http": HttpClient}


async def make_client(server, protocol, username):
    client = CLIENTS[protocol](server, username, await login(server, username))
    await client.start()
    return client


# ------------------------------------------------------------- helpers ---

def summarize(samples_ms: list[float]) -> dict:
    s = sorted(samples_ms)
    return {
        "count": len(s),
        "mean_ms": round(statistics.mean(s), 3),
        "p50_ms": round(s[len(s) // 2], 3),
        "p95_ms": round(s[max(0, int(len(s) * 0.95) - 1)], 3),
        "max_ms": round(s[-1], 3),
        "stdev_ms": round(statistics.pstdev(s), 3),
    }


def payload(size: int) -> str:
    return ("x" * size)[:size]


def save(name: str, data: dict) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(data, indent=2))
    print(f"\nsaved {path}")


def table(headers, rows):
    print("\n| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        print("| " + " | ".join(str(c) for c in row) + " |")


async def one_way_latency(sender, receiver, n, body, transform=None, untransform=None):
    """Stop-and-wait: send one message, wait until it arrives, repeat."""
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        wire = transform(body) if transform else body
        await sender.send(receiver.username, wire, encrypted=transform is not None)
        t1, got = await asyncio.wait_for(receiver.inbox.get(), 10)
        if untransform:
            got = untransform(got)
            t1 = time.perf_counter()   # include decryption time
        assert got == body, "payload corrupted"
        samples.append((t1 - t0) * 1000)
    return samples


# ---------------------------------------------------------- benchmarks ---

async def bench_latency(args):
    """Metric 1 (latency) + Metric 2 (protocol comparison)."""
    results = {}
    for protocol in ("ws", "http"):
        a = await make_client(args.server, protocol, "bench_a")
        b = await make_client(args.server, protocol, "bench_b")
        await one_way_latency(a, b, 10, payload(args.size))          # warm-up
        a.bytes_sent = 0
        samples = await one_way_latency(a, b, args.n, payload(args.size))
        results[protocol] = {**summarize(samples), "bytes_per_message_sent": round(a.bytes_sent / args.n)}
        await a.stop(); await b.stop()

    table(["Protocol", "n", "mean ms", "p50", "p95", "max", "bytes/msg (send)"],
          [[("HSP/WebSocket" if p == "ws" else "HTTP POST + SSE"), r["count"], r["mean_ms"], r["p50_ms"],
            r["p95_ms"], r["max_ms"], r["bytes_per_message_sent"]] for p, r in results.items()])
    save("latency", {"server": args.server, "payload_chars": args.size, "results": results})


async def bench_scale(args):
    """Metric 3: delivery latency while N clients all send at once (ring: i -> i+1)."""
    rows, out = [], {}
    for n_clients in [int(x) for x in args.clients.split(",")]:
        clients = await asyncio.gather(*[make_client(args.server, args.protocol, f"bench_{i}")
                                         for i in range(n_clients)])
        per_client = args.k

        async def sender(i):
            target = clients[(i + 1) % n_clients]
            for _ in range(per_client):
                await clients[i].send(target.username, f"{time.perf_counter()}|{payload(args.size)}")

        async def receiver(c):
            lat = []
            for _ in range(per_client):
                t1, body = await asyncio.wait_for(c.inbox.get(), 30)
                lat.append((t1 - float(body.split("|", 1)[0])) * 1000)
            return lat

        start = time.perf_counter()
        receivers = [asyncio.create_task(receiver(c)) for c in clients]
        await asyncio.gather(*[sender(i) for i in range(n_clients)])
        latencies = [x for lat in await asyncio.gather(*receivers) for x in lat]
        wall = time.perf_counter() - start
        stats = {**summarize(latencies), "throughput_msg_per_s": round(len(latencies) / wall, 1)}
        out[n_clients] = stats
        rows.append([n_clients, stats["count"], stats["mean_ms"], stats["p95_ms"], stats["max_ms"],
                     stats["throughput_msg_per_s"]])
        await asyncio.gather(*[c.stop() for c in clients])

    table(["Clients", "messages", "mean ms", "p95", "max", "msg/s"], rows)
    save("scale", {"server": args.server, "protocol": args.protocol, "messages_per_client": args.k,
                   "results": out})


async def bench_crypto(args):
    """Extra-feature comparison: AES-256-GCM end-to-end encryption vs plaintext."""
    # Same key derivation as the browser (ui/extras/crypto.js).
    salt = ("handshake-e2ee:" + ":".join(sorted(["bench_a", "bench_b"]))).encode()
    t = time.perf_counter()
    key = PBKDF2HMAC(hashes.SHA256(), 32, salt, 150_000).derive(b"benchmark-secret")
    derive_ms = (time.perf_counter() - t) * 1000
    aes = AESGCM(key)

    def encrypt(text):
        iv = os.urandom(12)
        return base64.b64encode(iv + aes.encrypt(iv, text.encode(), None)).decode()

    def decrypt(b64):
        raw = base64.b64decode(b64)
        return aes.decrypt(raw[:12], raw[12:], None).decode()

    a = await make_client(args.server, "ws", "bench_a")
    b = await make_client(args.server, "ws", "bench_b")
    body = payload(args.size)
    await one_way_latency(a, b, 10, body)  # warm-up

    a.bytes_sent = 0
    plain = await one_way_latency(a, b, args.n, body)
    plain_bytes = a.bytes_sent / args.n
    a.bytes_sent = 0
    enc = await one_way_latency(a, b, args.n, body, encrypt, decrypt)
    enc_bytes = a.bytes_sent / args.n
    await a.stop(); await b.stop()

    results = {"plaintext": {**summarize(plain), "frame_bytes": round(plain_bytes)},
               "aes_gcm": {**summarize(enc), "frame_bytes": round(enc_bytes)},
               "key_derivation_ms_once": round(derive_ms, 1)}
    table(["Mode", "n", "mean ms", "p50", "p95", "frame bytes"],
          [["plaintext", len(plain), results["plaintext"]["mean_ms"], results["plaintext"]["p50_ms"],
            results["plaintext"]["p95_ms"], results["plaintext"]["frame_bytes"]],
           ["AES-256-GCM", len(enc), results["aes_gcm"]["mean_ms"], results["aes_gcm"]["p50_ms"],
            results["aes_gcm"]["p95_ms"], results["aes_gcm"]["frame_bytes"]]])
    print(f"\nPBKDF2 key derivation (once per chat): {derive_ms:.1f} ms")
    save("crypto", {"server": args.server, "payload_chars": args.size, "results": results})


def main():
    parser = argparse.ArgumentParser(description="Handshake benchmarks")
    parser.add_argument("test", choices=["latency", "scale", "crypto"])
    parser.add_argument("--server", default="https://127.0.0.1:8443")
    parser.add_argument("--n", type=int, default=200, help="messages per measurement")
    parser.add_argument("--size", type=int, default=100, help="payload size in characters")
    parser.add_argument("--clients", default="2,10,25,50", help="client counts for 'scale'")
    parser.add_argument("--k", type=int, default=20, help="messages per client for 'scale'")
    parser.add_argument("--protocol", choices=["ws", "http"], default="ws", help="protocol for 'scale'")
    args = parser.parse_args()
    asyncio.run({"latency": bench_latency, "scale": bench_scale, "crypto": bench_crypto}[args.test](args))


if __name__ == "__main__":
    main()
