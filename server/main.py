"""
Handshake server entry point.

    python -m server.main             # HTTPS on port 8443 (normal use)
    python -m server.main --http      # plain HTTP (debugging only: no E2EE on phones)

One process serves everything on one port:
    /            the web client (static files from ui/)
    /dashboard   live server metrics
    /api/...     REST endpoints: login, Protocol 2 (HTTP + SSE), metrics
    /ws          Protocol 1 (HSP over WebSocket)
"""

import argparse
import asyncio
import mimetypes
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .extras import auth, database, tunnel                      # our additional features
from .required import network, protocol_http, protocol_ws    # what the spec requires
from .required.metrics import metrics, router as metrics_router, ticker


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at start-up (before `yield`) and once at shutdown (after)."""
    database.init(config.DB_PATH)
    tick_task = asyncio.create_task(ticker())
    metrics.log("server started")
    yield
    tick_task.cancel()


app = FastAPI(title="Handshake", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(protocol_http.router)
app.include_router(protocol_ws.router)
app.include_router(metrics_router)


@app.get("/dashboard")
async def dashboard():
    return FileResponse(config.CLIENT_DIR / "dashboard.html")


@app.get("/api/join")
async def join_info():
    """Address + QR code that the dashboard shows so phones can join."""
    url = metrics.server_info.get("url", "")
    info = {"url": url, "qr_svg": network.qr_svg(url) if url else ""}
    if url.startswith("https"):
        info["cert_url"] = f"{url}/ca.cer"
        info["cert_qr_svg"] = network.qr_svg(info["cert_url"])
    public_url = metrics.server_info.get("public_url")
    if public_url:  # only with --share (remote access through the tunnel)
        info["public_url"] = public_url
        info["public_qr_svg"] = network.qr_svg(public_url)
    return info


@app.get("/ca.cer")
async def ca_certificate():
    """
    The Handshake Local CA, for phones to install once (needed on iPhone).
    This MIME type makes iOS offer to install it as a configuration profile.
    """
    der = network.ca_certificate_der(config.CERT_DIR)
    if der is None:
        raise HTTPException(404, "Server runs without TLS, so there is no certificate")
    return Response(der, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="Handshake-CA.cer"'})


# The install manifest ("Add to Home Screen") needs its proper media type.
mimetypes.add_type("application/manifest+json", ".webmanifest")

# Static files last, so they never shadow the API routes above.
# StaticFiles answers conditional GETs (If-None-Match / If-Modified-Since)
# with "304 Not Modified", the browser caching from Lecture 2.
app.mount("/", StaticFiles(directory=config.CLIENT_DIR, html=True), name="client")


def main() -> None:
    parser = argparse.ArgumentParser(description="Handshake messaging server")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    parser.add_argument("--http", action="store_true", help="disable TLS (debug only)")
    parser.add_argument("--share", action="store_true",
                        help="also reachable from outside this Wi-Fi, through a Cloudflare tunnel")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # so the QR code prints on Windows
    except Exception:
        pass

    lan_ip = network.get_lan_ip()
    scheme = "http" if args.http else "https"
    url = f"{scheme}://{lan_ip}:{args.port}"
    metrics.server_info = {"ip": lan_ip, "port": args.port, "scheme": scheme, "url": url,
                           "private_ip": network.is_private(lan_ip)}

    ssl_options = {}
    if not args.http:
        cert, key = network.ensure_certificate(config.CERT_DIR, network.all_ipv4_addresses())
        ssl_options = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    print("\n  HANDSHAKE server")
    print(f"  Phones on the same Wi-Fi open:  {url}")
    print(f"  Live metrics dashboard:         {url}/dashboard")
    if lan_ip == "127.0.0.1":
        print("  WARNING: no network interface found - connect the laptop to Wi-Fi first.")
    if not args.http:
        print(f"  First time on a phone? Install the certificate: {url}/ca.cer")
        print("  (iPhone steps: docs/RUNNING.md -> 'iPhone setup')")
    print()
    network.print_qr(url)

    if args.share:
        def announce(public_url: str) -> None:
            metrics.server_info["public_url"] = public_url
            metrics.log(f"remote access on: {public_url}")
            print(f"\n  REMOTE ACCESS (any network, no certificate needed):  {public_url}\n", flush=True)
            network.print_qr(public_url)

        print("  Starting remote access tunnel ...", flush=True)
        tunnel.start_tunnel(f"{scheme}://localhost:{args.port}", config.TOOLS_DIR, announce)

    # host 0.0.0.0 = listen on every network interface, not just localhost.
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning",
                ws_ping_interval=20, ws_ping_timeout=20, **ssl_options)


if __name__ == "__main__":
    main()
