"""Central settings for the Handshake server (one place to change them)."""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent   # the handshake/ folder
CLIENT_DIR = ROOT_DIR / "ui"                         # the web app (served as static files)
LOCAL_DIR = ROOT_DIR / "local"                       # this laptop only, never uploaded to GitHub
# SQLite database folder. HANDSHAKE_DATA_DIR lets a test server use its own
# database without touching the real one.
DATA_DIR = Path(os.environ.get("HANDSHAKE_DATA_DIR", LOCAL_DIR / "data"))
CERT_DIR = LOCAL_DIR / "certs"                       # TLS certificates (local CA + server)
TOOLS_DIR = LOCAL_DIR / "tools"                      # cloudflared.exe for --share (downloaded once)

DB_PATH = DATA_DIR / "handshake.db"

DEFAULT_PORT = 8443          # HTTPS port the phones connect to

SESSION_COOKIE = "hs_session"
PASSWORD_ITERATIONS = 200_000  # PBKDF2 rounds for password hashing

MAX_BODY_CHARS = 24000       # longest body we accept (4000 typed chars, encrypted + base64)
SSE_KEEPALIVE_SECONDS = 15   # comment line sent on idle SSE streams
