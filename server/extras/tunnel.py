"""
Optional remote access through a Cloudflare quick tunnel.

    python -m server.main --share          (or:  start.bat --share)

Why is this needed? (Lecture 8: NAT)
  The laptop only has a PRIVATE address (192.168.x.y) behind the Wi-Fi
  router's NAT. A phone on the Internet cannot open a connection to it:
  the router has no NAT-table entry telling it where to send that packet.

How the tunnel gets around NAT:
  1. `cloudflared` opens an OUTGOING connection from the laptop to Cloudflare.
     NAT allows outgoing connections, so this works on any network.
  2. Cloudflare gives us a public address: https://<random-words>.trycloudflare.com
  3. A remote phone connects to Cloudflare; Cloudflare forwards the request
     down the connection that is already open, to our server on port 8443.

Local Wi-Fi stays the default (and the demo mode): nothing here runs unless
--share is given. Secret chats stay end-to-end encrypted through the tunnel.
"""

import atexit
import platform
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Callable

WINDOWS_DOWNLOAD = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
                    "cloudflared-windows-amd64.exe")
PUBLIC_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared(tools_dir: Path) -> str | None:
    """Find the cloudflared program; on Windows download it once if missing."""
    on_path = shutil.which("cloudflared")
    if on_path:
        return on_path
    local = tools_dir / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    if local.exists():
        return str(local)
    if sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64"):
        print("  Downloading cloudflared from GitHub (one time only) ...", flush=True)
        tools_dir.mkdir(parents=True, exist_ok=True)
        _download(WINDOWS_DOWNLOAD, local)
        return str(local)
    return None


def _download(url: str, target: Path) -> None:
    """
    Prefer Windows' own curl.exe: it trusts the Windows certificate store, which
    matters when antivirus software (e.g. Avira) inspects HTTPS traffic.
    """
    partial = target.with_suffix(".part")   # renamed only when complete
    try:
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if curl:
            subprocess.run([curl, "-L", "--fail", "-s", "-o", str(partial), url], check=True, timeout=300)
        else:
            urllib.request.urlretrieve(url, partial)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def start_tunnel(origin_url: str, tools_dir: Path, on_public_url: Callable[[str], None]):
    """
    Start `cloudflared tunnel --url <origin>` in the background.
    on_public_url(url) is called once, as soon as Cloudflare assigns the address.
    """
    try:
        exe = find_cloudflared(tools_dir)
    except Exception as exc:  # no internet, blocked download, ...: keep serving the Wi-Fi
        print(f"  Remote access unavailable (could not get cloudflared: {exc}). Wi-Fi mode still works.")
        return None
    if exe is None:
        print("  Remote access needs cloudflared. macOS: `brew install cloudflared`; "
              "other systems: https://github.com/cloudflare/cloudflared/releases")
        return None

    process = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", origin_url,
         "--no-tls-verify"],            # our certificate is self-signed (local CA)
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    atexit.register(process.terminate)  # stop the tunnel when the server stops

    def watch_output():
        announced = False
        for line in process.stdout:     # keep reading so the pipe never fills up
            match = PUBLIC_URL_RE.search(line)
            if match and not announced:
                announced = True
                on_public_url(match.group(0))

    threading.Thread(target=watch_output, daemon=True).start()
    return process


def real_client(peer_ip: str, peer_port: int, headers) -> tuple[str, int, str]:
    """
    Who is really on the other end of a connection?
    Through the tunnel, our TCP peer is cloudflared on 127.0.0.1, and Cloudflare
    puts the real address in the CF-Connecting-IP header. We only believe that
    header when the connection really comes from the local tunnel process.
    Returns (ip, port, "internet" | "wifi").
    """
    forwarded = headers.get("cf-connecting-ip")
    if forwarded and peer_ip in ("127.0.0.1", "::1"):
        return forwarded, 0, "internet"
    return peer_ip, peer_port, "wifi"
