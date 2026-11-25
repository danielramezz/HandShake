"""
End-to-end tests of the transport guarantees, using FastAPI's TestClient.
Each TestClient has its own cookie jar, so it acts like a separate phone.

Run:  python -m pytest
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from server import config
from server.required import hsp
from server.main import app


@pytest.fixture()
def phones(tmp_path, monkeypatch):
    """Two logged-in 'phones' (alice, bob) on a fresh database."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    with TestClient(app) as alice, TestClient(app) as bob:
        for client, name in ((alice, "alice"), (bob, "bob")):
            r = client.post("/api/register",
                            json={"username": name, "display_name": name.title(), "password": "secret1"})
            assert r.status_code == 200, r.text
        yield alice, bob


def new_id() -> str:
    return str(uuid.uuid4())


def hello(ws, last_id=0):
    """Do the HSP handshake and return the backlog messages."""
    ws.send_text(hsp.encode("HELLO", {"last-id": last_id}))
    command, _, _ = hsp.decode(ws.receive_text())
    assert command == "WELCOME"
    backlog = []
    while True:
        command, headers, body = hsp.decode(ws.receive_text())
        if command == "SYNCED":
            return backlog
        if command == "MSG":
            backlog.append((headers, body))


def receive_until(ws, wanted):
    """Read frames until one with the wanted command arrives (skip PRESENCE etc.)."""
    while True:
        command, headers, body = hsp.decode(ws.receive_text())
        if command == wanted:
            return headers, body


def test_http_send_is_delivered_live_over_websocket(phones):
    alice, bob = phones
    with bob.websocket_connect("/ws") as bob_ws:
        hello(bob_ws)
        r = alice.post("/api/messages", json={"client_msg_id": new_id(), "to": "bob", "body": "hi bob"})
        assert r.status_code == 200 and r.json()["duplicate"] is False
        headers, body = receive_until(bob_ws, "MSG")
        assert body == "hi bob" and headers["from"] == "alice"


def test_retransmission_is_not_duplicated(phones):
    alice, bob = phones
    msg = {"client_msg_id": new_id(), "to": "bob", "body": "only once"}
    first = alice.post("/api/messages", json=msg).json()
    again = alice.post("/api/messages", json=msg).json()   # e.g. the ACK was lost
    assert again["duplicate"] is True
    assert again["id"] == first["id"]
    history = bob.get("/api/messages").json()["messages"]
    assert [m["body"] for m in history] == ["only once"]


def test_offline_phone_gets_missed_messages_on_reconnect(phones):
    alice, bob = phones
    with alice.websocket_connect("/ws") as alice_ws:
        hello(alice_ws)
        for i in range(3):  # bob is offline the whole time
            alice_ws.send_text(hsp.encode("SEND", {"msg-id": new_id(), "to": "bob", "enc": "none"}, f"m{i}"))
            headers, _ = receive_until(alice_ws, "ACK")
            assert headers["duplicate"] == "no"

    with bob.websocket_connect("/ws") as bob_ws:     # bob comes online
        backlog = hello(bob_ws, last_id=0)
        assert [body for _, body in backlog] == ["m0", "m1", "m2"]
        # ...and asking again from the last id gives nothing new
    with bob.websocket_connect("/ws") as bob_ws:
        last = int(backlog[-1][0]["id"])
        assert hello(bob_ws, last_id=last) == []


def test_bad_frame_gets_error_but_connection_survives(phones):
    alice, _ = phones
    with alice.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_text("this is not HSP")
        headers, _ = receive_until(ws, "ERROR")
        assert headers["code"] == "400"
        ws.send_text(hsp.encode("PING", {"t": 123}))      # still works afterwards
        headers, _ = receive_until(ws, "PONG")
        assert headers["t"] == "123"


def test_unknown_recipient_is_rejected(phones):
    alice, _ = phones
    r = alice.post("/api/messages", json={"client_msg_id": new_id(), "to": "nobody", "body": "x"})
    assert r.status_code == 404


def test_websocket_requires_login(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    with TestClient(app) as stranger:
        with pytest.raises(Exception):
            with stranger.websocket_connect("/ws") as ws:
                ws.receive_text()


# ------------------------------------------------- receipts, typing, profile ---

def test_read_receipt_is_cumulative_and_reaches_sender(phones):
    alice, bob = phones
    ids = [alice.post("/api/messages", json={"client_msg_id": new_id(), "to": "bob", "body": f"m{i}"}).json()["id"]
           for i in range(3)]
    with alice.websocket_connect("/ws") as alice_ws:
        hello(alice_ws, last_id=ids[-1])
        # bob read up to the 2nd message, over HTTP
        r = bob.post("/api/receipts", json={"to": "alice", "type": "read", "up_to": ids[1]})
        assert r.json()["changed"] is True
        headers, _ = receive_until(alice_ws, "RECEIPT")
        assert headers["from"] == "bob"
        assert headers["read"] == str(ids[1]) and headers["delivered"] == str(ids[1])  # read implies delivered
    # going backwards changes nothing (receipts only grow)
    assert bob.post("/api/receipts", json={"to": "alice", "type": "read", "up_to": ids[0]}).json()["changed"] is False
    # claiming to have read a message that does not exist is clamped to the latest one
    bob.post("/api/receipts", json={"to": "alice", "type": "read", "up_to": 10**9})
    bob_entry = next(u for u in alice.get("/api/users").json()["users"] if u["username"] == "bob")
    assert bob_entry["read_up_to"] == ids[-1]


def test_typing_is_forwarded_but_not_stored(phones):
    alice, bob = phones
    with bob.websocket_connect("/ws") as bob_ws:
        hello(bob_ws)
        assert alice.post("/api/typing", json={"to": "bob"}).status_code == 200
        headers, _ = receive_until(bob_ws, "TYPING")
        assert headers["from"] == "alice"
    assert bob.get("/api/messages").json()["messages"] == []


def test_last_seen_is_recorded_when_user_goes_offline(phones):
    alice, bob = phones
    with alice.websocket_connect("/ws") as ws:
        hello(ws)
    entry = next(u for u in bob.get("/api/users").json()["users"] if u["username"] == "alice")
    assert entry["online"] is False and entry["last_seen"] is not None


def test_profile_update_is_broadcast(phones):
    alice, bob = phones
    with bob.websocket_connect("/ws") as bob_ws:
        hello(bob_ws)
        r = alice.patch("/api/me", json={"display_name": "Alice A.", "avatar_hue": 200})
        assert r.status_code == 200
        while True:
            headers, _ = receive_until(bob_ws, "PRESENCE")
            if headers["user"] == "alice" and headers["name"] == "Alice A.":
                assert headers["hue"] == "200"
                break
    assert alice.patch("/api/me", json={"display_name": "", "avatar_hue": 1}).status_code == 400
