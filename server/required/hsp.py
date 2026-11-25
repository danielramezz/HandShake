"""
HSP/1.0 - the Handshake Protocol (our custom application-layer protocol #1).

HSP is carried inside WebSocket text frames. Its format copies the HTTP
message format from Lecture 2: a start line, header lines, a blank line,
then the body.

    HSP/1.0 SEND\r\n                  <- start line: version + command
    msg-id: 3f2a9c1e-...\r\n          <- header lines  "name: value"
    to: bob\r\n
    enc: none\r\n
    content-length: 11\r\n            <- body size in UTF-8 bytes
    \r\n                              <- blank line = end of headers
    Hello, Bob!                       <- body

A protocol defines (Lecture 1): message TYPES, SYNTAX, SEMANTICS and RULES.
  * Types     -> the COMMANDS sets below
  * Syntax    -> encode() / decode() in this file
  * Semantics -> explained in README.md (protocol table)
  * Rules     -> protocol_ws.py (e.g. HELLO must be the first frame)

This file is pure (no networking) so it is easy to unit-test.
The browser has a twin of this file: ui/required/hsp.js
"""

VERSION = "HSP/1.0"
CRLF = "\r\n"

# Commands a client may send to the server.
CLIENT_COMMANDS = {"HELLO", "SEND", "RECEIPT", "TYPING", "PING", "BYE"}
# Commands the server may send to a client.
SERVER_COMMANDS = {"WELCOME", "MSG", "ACK", "SYNCED", "PRESENCE", "RECEIPT", "TYPING", "PONG", "ERROR"}
COMMANDS = CLIENT_COMMANDS | SERVER_COMMANDS

MAX_FRAME_BYTES = 64 * 1024  # reject anything bigger (protects the server)


class HSPError(Exception):
    """Raised when a frame does not follow the HSP syntax."""


def encode(command: str, headers: dict | None = None, body: str = "") -> str:
    """Build one HSP frame (a string) from a command, headers and a body."""
    if command not in COMMANDS:
        raise HSPError(f"unknown command {command!r}")

    lines = [f"{VERSION} {command}"]
    for name, value in (headers or {}).items():
        value = str(value)
        # A newline inside a value would break the line-based format.
        if "\r" in value or "\n" in value:
            raise HSPError(f"header {name!r} contains a newline")
        lines.append(f"{name.lower()}: {value}")

    # content-length is always added last and always computed by us.
    lines.append(f"content-length: {len(body.encode('utf-8'))}")
    return CRLF.join(lines) + CRLF + CRLF + body


def decode(frame: str) -> tuple[str, dict, str]:
    """
    Parse one HSP frame and return (command, headers, body).
    Raises HSPError if the frame is malformed.
    """
    if len(frame.encode("utf-8")) > MAX_FRAME_BYTES:
        raise HSPError("frame too large")

    # The first blank line separates the head from the body.
    head, sep, body = frame.partition(CRLF + CRLF)
    if not sep:
        raise HSPError("missing blank line after headers")

    lines = head.split(CRLF)
    start_line = lines[0].split(" ")
    if len(start_line) != 2:
        raise HSPError("bad start line")
    version, command = start_line
    if version != VERSION:
        raise HSPError(f"unsupported version {version!r}")  # like HTTP 505
    if command not in COMMANDS:
        raise HSPError(f"unknown command {command!r}")

    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon or not name.strip():
            raise HSPError(f"bad header line {line!r}")
        headers[name.strip().lower()] = value.strip()

    # content-length lets the receiver check the body arrived complete.
    if "content-length" not in headers:
        raise HSPError("missing content-length")
    try:
        expected = int(headers["content-length"])
    except ValueError:
        raise HSPError("content-length is not a number")
    if expected != len(body.encode("utf-8")):
        raise HSPError("content-length does not match body size")

    return command, headers, body
