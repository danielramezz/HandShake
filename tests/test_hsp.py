"""Unit tests for the HSP/1.0 encoder/decoder.  Run:  python -m pytest"""

import pytest

from server.required import hsp


def test_round_trip():
    frame = hsp.encode("SEND", {"msg-id": "abc12345", "to": "bob"}, "Hello, Bob!")
    command, headers, body = hsp.decode(frame)
    assert command == "SEND"
    assert headers["to"] == "bob"
    assert headers["content-length"] == "11"
    assert body == "Hello, Bob!"


def test_frame_looks_like_http():
    frame = hsp.encode("PING", {"t": 1})
    assert frame.startswith("HSP/1.0 PING\r\n")
    assert frame.endswith("\r\n\r\n")  # empty body


def test_unicode_and_newlines_in_body():
    body = "سلام 👋\nsecond line\r\n\r\nstill body"
    command, headers, decoded = hsp.decode(hsp.encode("SEND", {"to": "bob"}, body))
    assert decoded == body
    # length counts UTF-8 BYTES, not characters
    assert int(headers["content-length"]) == len(body.encode("utf-8"))


@pytest.mark.parametrize("bad", [
    "HSP/1.0 SEND\r\nto: bob\r\n",                          # no blank line
    "HSP/2.0 SEND\r\ncontent-length: 0\r\n\r\n",            # wrong version
    "HSP/1.0 DANCE\r\ncontent-length: 0\r\n\r\n",           # unknown command
    "HSP/1.0 SEND\r\nto bob\r\ncontent-length: 0\r\n\r\n",  # header without colon
    "HSP/1.0 SEND\r\nto: bob\r\n\r\nhi",                    # no content-length
    "HSP/1.0 SEND\r\ncontent-length: 5\r\n\r\nhi",          # length mismatch
    "HSP/1.0 SEND\r\ncontent-length: x\r\n\r\nhi",          # length not a number
])
def test_malformed_frames_are_rejected(bad):
    with pytest.raises(hsp.HSPError):
        hsp.decode(bad)


def test_header_value_with_newline_is_refused():
    with pytest.raises(hsp.HSPError):
        hsp.encode("SEND", {"to": "bob\r\nevil: 1"}, "x")
