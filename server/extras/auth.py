"""
User registration and login (additional feature #1, together with database.py).

* Passwords are never stored. We store PBKDF2-SHA256(password, salt).
* After login the server creates a random session token, saves it in the
  database and sends it to the browser in a cookie. This is the cookie
  mechanism from Lecture 2: the cookie holds an ID, and the backend
  database maps that ID to a user. The browser then attaches the cookie
  automatically to every request: REST calls, the SSE stream, and the
  WebSocket upgrade request.
"""

import asyncio
import hashlib
import hmac
import re
import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import config
from . import database
from ..required.hub import hub, presence_event

router = APIRouter()

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")


# ------------------------------------------------------------ helpers ---

def hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                                 config.PASSWORD_ITERATIONS)
    return digest.hex()


def user_from_token(token: str | None) -> dict | None:
    """Look up which user a session cookie belongs to (None if invalid)."""
    if not token:
        return None
    return database.get_session_user(token)


def require_user(request: Request) -> dict:
    """Used by HTTP endpoints: returns the logged-in user or answers 401."""
    user = user_from_token(request.cookies.get(config.SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


def public_user(user: dict) -> dict:
    """The fields of a user that other clients are allowed to see."""
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "avatar_hue": user.get("avatar_hue"),
        "last_seen": user.get("last_seen"),
        "online": hub.is_online(user["id"]),
    }


def _start_session(response: Response, request: Request, user: dict) -> None:
    token = secrets.token_hex(32)
    database.create_session(token, user["id"])
    response.set_cookie(
        config.SESSION_COOKIE, token,
        httponly=True,                              # JavaScript cannot read it
        secure=request.url.scheme == "https",       # only sent over TLS
        samesite="strict",
        max_age=60 * 60 * 24 * 30,                  # 30 days
    )


# ------------------------------------------------------------- routes ---

class RegisterBody(BaseModel):
    username: str
    display_name: str
    password: str


class LoginBody(BaseModel):
    username: str
    password: str


class ProfileBody(BaseModel):
    display_name: str
    avatar_hue: int | None = None   # 0-359, or None for the automatic colour


@router.post("/api/register")
async def register(body: RegisterBody, request: Request, response: Response):
    username = body.username.strip().lower()
    display_name = body.display_name.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Username must be 3-20 characters: a-z, 0-9 or _")
    if not 1 <= len(display_name) <= 30:
        raise HTTPException(400, "Display name must be 1-30 characters")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if database.get_user_by_username(username):
        raise HTTPException(409, "That username is taken")

    salt = secrets.token_hex(16)
    password_hash = await asyncio.to_thread(hash_password, body.password, salt)  # slow on purpose; keep it off the event loop
    user = database.create_user(username, display_name, password_hash, salt)
    _start_session(response, request, user)
    return {"user": public_user(user)}


@router.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    user = database.get_user_by_username(body.username.strip().lower())
    if user is None:
        raise HTTPException(401, "Wrong username or password")
    attempt = await asyncio.to_thread(hash_password, body.password, user["salt"])
    # compare_digest takes the same time whether the hashes match or not.
    if not hmac.compare_digest(user["password_hash"], attempt):
        raise HTTPException(401, "Wrong username or password")
    _start_session(response, request, user)
    return {"user": public_user(user)}


@router.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        database.delete_session(token)
    response.delete_cookie(config.SESSION_COOKIE)
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request):
    return {"user": public_user(require_user(request))}


@router.patch("/api/me")
async def update_me(body: ProfileBody, request: Request):
    """Change display name / avatar colour, then tell everyone who is online."""
    me = require_user(request)
    display_name = body.display_name.strip()
    if not 1 <= len(display_name) <= 30:
        raise HTTPException(400, "Display name must be 1-30 characters")
    if body.avatar_hue is not None and not 0 <= body.avatar_hue <= 359:
        raise HTTPException(400, "avatar_hue must be 0-359")
    user = database.update_profile(me["id"], display_name, body.avatar_hue)
    hub.broadcast(presence_event(user, online=hub.is_online(user["id"])))
    return {"user": public_user(user)}


@router.get("/api/users")
async def users(request: Request):
    """
    Everyone the logged-in user can chat with: online status, last seen, and
    how far each of them has received/read MY messages (for the ticks).
    """
    me = require_user(request)
    receipts = database.receipts_for_sender(me["id"])
    result = []
    for u in database.list_users():
        if u["id"] == me["id"]:
            continue
        r = receipts.get(u["id"], {"delivered_up_to": 0, "read_up_to": 0})
        result.append({**public_user(u), **r})
    return {"users": result}
