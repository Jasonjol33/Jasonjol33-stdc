"""Konten, Pilotprofile, Sync und Community-Board - das "mitgelieferte Backend" der Seite.

* Speicher: eine SQLite-Datei (`DATA_DIR/hub.db`, auf Fly.io liegt sie im Volume). Nur Python-
  Standardbibliothek, keine zusätzliche Installation.
* Passwörter werden mit scrypt (zufälliges Salz) gehasht und nie im Klartext gespeichert.
* Sitzungen: zufälliger Token im HttpOnly-Cookie (SameSite=Lax, auf HTTPS zusätzlich Secure).
  In der Datenbank liegt nur der SHA-256-Hash des Tokens.
* Alle Datenbankzugriffe laufen in einem Thread (`asyncio.to_thread`), damit der Event-Loop des
  Discord-Bots nie blockiert.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Optional

from aiohttp import web

from storage import DATA_DIR

log = logging.getLogger("accounts")

COOKIE = "scx_session"
SESSION_DAYS = 30
MAX_JSON_BYTES = 512 * 1024
MAX_POSTS_PER_USER = 20
MAX_EVENTS_PER_USER = 20
USER_DATA_KEYS = ("fleet", "builds", "buildCompare", "routes", "crews", "events", "missions", "guide", "check")
ROLES = {"Allrounder", "Pilot / Combat", "Trader / Cargo", "Miner", "Salvager", "Explorer", "Medic / Support", "Gunner"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  salt BLOB NOT NULL, pw_hash BLOB NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS profiles(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT '', org TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'Allrounder',
  discord TEXT NOT NULL DEFAULT '', bio TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_data(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  body TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  pilot_name TEXT NOT NULL, activity TEXT NOT NULL, role TEXT NOT NULL, when_text TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL, event_type TEXT NOT NULL, starts_at TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '', max_attendees INTEGER NOT NULL DEFAULT 8, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rsvps(
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY(event_id, user_id));
"""


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def _clean(value: Any, maxlen: int, *, required: bool = False, field: str = "Feld") -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value if value is not None else "")).strip()
    if required and not text:
        raise ApiError(400, f"{field} darf nicht leer sein.")
    return text[:maxlen]


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


class Store:
    """Synchrone SQLite-Zugriffe; Aufrufer nutzen `await store.run(...)`."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "hub.db"
        self._lock = Lock()
        self._db: Optional[sqlite3.Connection] = None
        self._hash_slots = asyncio.Semaphore(2)  # scrypt braucht ~16 MB - nicht beliebig parallel

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    async def run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        def job() -> Any:
            assert self._db is not None, "Datenbank nicht geöffnet"
            with self._lock:
                return fn(self._db)

        return await asyncio.to_thread(job)

    async def hash_pw(self, password: str, salt: bytes) -> bytes:
        async with self._hash_slots:
            return await asyncio.to_thread(_hash_password, password, salt)


store = Store()

# ---------------------------------------------------------------------------------------
# Hilfen für Anfragen
# ---------------------------------------------------------------------------------------


async def read_json(request: web.Request) -> dict:
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        raise ApiError(413, "Die Anfrage ist zu groß.")
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise ApiError(400, "Ungültige Anfrage (JSON erwartet).")
    if not isinstance(data, dict):
        raise ApiError(400, "Ungültige Anfrage (Objekt erwartet).")
    return data


async def current_user(request: web.Request) -> Optional[sqlite3.Row]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    return await store.run(
        lambda db: db.execute(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?",
            (th, int(time.time())),
        ).fetchone()
    )


async def require_user(request: web.Request) -> sqlite3.Row:
    user = await current_user(request)
    if user is None:
        raise ApiError(401, "Bitte melde dich an.")
    return user


def _is_https(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


async def _start_session(request: web.Request, resp: web.StreamResponse, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    exp = int(time.time()) + SESSION_DAYS * 86400
    await store.run(lambda db: db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (th, user_id, exp)))
    resp.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax",
                    secure=_is_https(request), path="/")


def api_handler(fn):
    """Wandelt ApiError in saubere JSON-Fehlerantworten um."""

    async def wrapper(request: web.Request) -> web.StreamResponse:
        try:
            return await fn(request)
        except ApiError as e:
            return web.json_response({"detail": e.detail}, status=e.status)

    return wrapper


routes = web.RouteTableDef()

# ---------------------------------------------------------------------------------------
# Konto
# ---------------------------------------------------------------------------------------

USERNAME_RE = re.compile(r"^[^\s<>\"'\\]{3,120}$")


async def _credentials(request: web.Request) -> tuple[str, str]:
    body = await read_json(request)
    username = _clean(body.get("username"), 120)
    password = str(body.get("password") or "")
    if not USERNAME_RE.match(username):
        raise ApiError(400, "Benutzername/E-Mail: 3 bis 120 Zeichen, ohne Leerzeichen.")
    if not 8 <= len(password) <= 128:
        raise ApiError(400, "Das Passwort muss 8 bis 128 Zeichen lang sein.")
    return username, password


@routes.post("/api/auth/register")
@api_handler
async def register(request: web.Request) -> web.Response:
    username, password = await _credentials(request)
    salt = secrets.token_bytes(16)
    pw_hash = await store.hash_pw(password, salt)

    def insert(db: sqlite3.Connection) -> int:
        try:
            cur = db.execute("INSERT INTO users(username,salt,pw_hash,created_at) VALUES(?,?,?,?)",
                             (username, salt, pw_hash, int(time.time())))
        except sqlite3.IntegrityError:
            raise ApiError(409, "Dieser Name ist bereits vergeben.")
        db.execute("INSERT INTO profiles(user_id,name,updated_at) VALUES(?,?,?)", (cur.lastrowid, "", 0))
        return int(cur.lastrowid)

    uid = await store.run(insert)
    resp = web.json_response({"username": username}, status=201)
    await _start_session(request, resp, uid)
    return resp


@routes.post("/api/auth/login")
@api_handler
async def login(request: web.Request) -> web.Response:
    username, password = await _credentials(request)
    row = await store.run(lambda db: db.execute("SELECT id,username,salt,pw_hash FROM users WHERE username=?", (username,)).fetchone())
    # Auch bei unbekanntem Namen hashen, damit die Antwortzeit nichts verrät.
    salt = row["salt"] if row else b"\x00" * 16
    candidate = await store.hash_pw(password, salt)
    if not row or not hmac.compare_digest(candidate, row["pw_hash"]):
        raise ApiError(401, "Benutzername oder Passwort ist falsch.")
    resp = web.json_response({"username": row["username"]})
    await _start_session(request, resp, row["id"])
    return resp


@routes.post("/api/auth/logout")
@api_handler
async def logout(request: web.Request) -> web.Response:
    token = request.cookies.get(COOKIE)
    if token:
        th = hashlib.sha256(token.encode()).hexdigest()
        await store.run(lambda db: db.execute("DELETE FROM sessions WHERE token_hash=?", (th,)))
    resp = web.Response(status=204)
    resp.del_cookie(COOKIE, path="/")
    return resp


@routes.get("/api/auth/me")
@api_handler
async def me(request: web.Request) -> web.Response:
    user = await require_user(request)
    return web.json_response({"username": user["username"]})


# ---------------------------------------------------------------------------------------
# Profil, Sync, Organisationsverzeichnis
# ---------------------------------------------------------------------------------------


def _profile_dict(row: Optional[sqlite3.Row]) -> dict:
    if not row:
        return {"name": "", "org": "", "role": "Allrounder", "discord": "", "bio": ""}
    return {k: row[k] for k in ("name", "org", "role", "discord", "bio")}


@routes.get("/api/profile")
@api_handler
async def get_profile(request: web.Request) -> web.Response:
    user = await require_user(request)
    row = await store.run(lambda db: db.execute("SELECT * FROM profiles WHERE user_id=?", (user["id"],)).fetchone())
    return web.json_response(_profile_dict(row))


@routes.put("/api/profile")
@api_handler
async def put_profile(request: web.Request) -> web.Response:
    user = await require_user(request)
    body = await read_json(request)
    role = _clean(body.get("role"), 40) or "Allrounder"
    discord = _clean(body.get("discord"), 200)
    if discord and not re.match(r"^https?://", discord, re.I):
        raise ApiError(400, "Der Discord-Link muss mit https:// beginnen.")
    p = {"name": _clean(body.get("name"), 40), "org": _clean(body.get("org"), 60),
         "role": role if role in ROLES else "Allrounder", "discord": discord, "bio": _clean(body.get("bio"), 400)}
    await store.run(lambda db: db.execute(
        "INSERT INTO profiles(user_id,name,org,role,discord,bio,updated_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,org=excluded.org,role=excluded.role,"
        "discord=excluded.discord,bio=excluded.bio,updated_at=excluded.updated_at",
        (user["id"], p["name"], p["org"], p["role"], p["discord"], p["bio"], int(time.time()))))
    return web.json_response(p)


@routes.get("/api/user-data")
@api_handler
async def get_user_data(request: web.Request) -> web.Response:
    user = await require_user(request)
    row = await store.run(lambda db: db.execute("SELECT body FROM user_data WHERE user_id=?", (user["id"],)).fetchone())
    return web.Response(text=row["body"] if row else "{}", content_type="application/json")


@routes.put("/api/user-data")
@api_handler
async def put_user_data(request: web.Request) -> web.Response:
    user = await require_user(request)
    body = await read_json(request)
    clean = {k: body[k] for k in USER_DATA_KEYS if k in body and isinstance(body[k], (list, dict))}
    text = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) > MAX_JSON_BYTES:
        raise ApiError(413, "Die Daten sind zu groß für die Synchronisierung.")
    await store.run(lambda db: db.execute(
        "INSERT INTO user_data(user_id,body,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at",
        (user["id"], text, int(time.time()))))
    return web.json_response({"ok": True})


@routes.get("/api/organizations")
@api_handler
async def organizations(request: web.Request) -> web.Response:
    """Öffentlich sind nur Profile, bei denen die Person selbst eine Organisation eingetragen hat."""
    rows = await store.run(lambda db: db.execute(
        "SELECT u.username, p.name, p.org, p.role, p.bio, p.discord FROM profiles p JOIN users u ON u.id=p.user_id "
        "WHERE p.org<>'' ORDER BY p.updated_at DESC LIMIT 100").fetchall())
    return web.json_response([{"org": r["org"], "name": r["name"] or r["username"].split("@")[0],
                               "role": r["role"], "bio": r["bio"], "discord": r["discord"]} for r in rows])


# ---------------------------------------------------------------------------------------
# Community: Crew-Gesuche und Events
# ---------------------------------------------------------------------------------------


@routes.get("/api/community/posts")
@api_handler
async def list_posts(request: web.Request) -> web.Response:
    user = await current_user(request)
    uid = user["id"] if user else -1
    rows = await store.run(lambda db: db.execute("SELECT * FROM posts ORDER BY id DESC LIMIT 100").fetchall())
    return web.json_response([{"id": r["id"], "pilot_name": r["pilot_name"], "activity": r["activity"], "role": r["role"],
                               "when": r["when_text"], "description": r["description"],
                               "created_at": r["created_at"], "mine": r["user_id"] == uid} for r in rows])


@routes.post("/api/community/posts")
@api_handler
async def create_post(request: web.Request) -> web.Response:
    user = await require_user(request)
    b = await read_json(request)
    vals = (user["id"], _clean(b.get("pilot_name"), 40, required=True, field="Das Rufzeichen"),
            _clean(b.get("activity"), 40, required=True, field="Die Aktivität"),
            _clean(b.get("role"), 40, required=True, field="Die Rolle"),
            _clean(b.get("when"), 80), _clean(b.get("description"), 400), int(time.time()))

    def insert(db: sqlite3.Connection) -> int:
        if db.execute("SELECT COUNT(*) FROM posts WHERE user_id=?", (user["id"],)).fetchone()[0] >= MAX_POSTS_PER_USER:
            raise ApiError(429, f"Du kannst höchstens {MAX_POSTS_PER_USER} Gesuche gleichzeitig veröffentlichen.")
        return db.execute("INSERT INTO posts(user_id,pilot_name,activity,role,when_text,description,created_at) "
                          "VALUES(?,?,?,?,?,?,?)", vals).lastrowid

    pid = await store.run(insert)
    return web.json_response({"id": pid}, status=201)


@routes.delete("/api/community/posts/{pid}")
@api_handler
async def delete_post(request: web.Request) -> web.Response:
    user = await require_user(request)
    pid = int(request.match_info["pid"]) if request.match_info["pid"].isdigit() else -1
    n = await store.run(lambda db: db.execute("DELETE FROM posts WHERE id=? AND user_id=?", (pid, user["id"])).rowcount)
    if not n:
        raise ApiError(404, "Eintrag nicht gefunden oder nicht von dir.")
    return web.Response(status=204)


@routes.get("/api/community/events")
@api_handler
async def list_events(request: web.Request) -> web.Response:
    user = await current_user(request)
    uid = user["id"] if user else -1
    rows = await store.run(lambda db: db.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM rsvps r WHERE r.event_id=e.id) AS rsvp_count, "
        "EXISTS(SELECT 1 FROM rsvps r WHERE r.event_id=e.id AND r.user_id=?) AS joined "
        "FROM events e ORDER BY e.starts_at ASC, e.id DESC LIMIT 100", (uid,)).fetchall())
    return web.json_response([{"id": r["id"], "title": r["title"], "event_type": r["event_type"],
                               "starts_at": r["starts_at"], "description": r["description"],
                               "max_attendees": r["max_attendees"], "rsvp_count": r["rsvp_count"],
                               "joined": bool(r["joined"]), "mine": r["user_id"] == uid} for r in rows])


@routes.post("/api/community/events")
@api_handler
async def create_event(request: web.Request) -> web.Response:
    user = await require_user(request)
    b = await read_json(request)
    try:
        max_att = max(2, min(100, int(b.get("max_attendees") or 8)))
    except (TypeError, ValueError):
        max_att = 8
    vals = (user["id"], _clean(b.get("title"), 80, required=True, field="Der Eventname"),
            _clean(b.get("event_type"), 40, required=True, field="Die Aktivität"),
            _clean(b.get("starts_at"), 40), _clean(b.get("description"), 500), max_att, int(time.time()))

    def insert(db: sqlite3.Connection) -> int:
        if db.execute("SELECT COUNT(*) FROM events WHERE user_id=?", (user["id"],)).fetchone()[0] >= MAX_EVENTS_PER_USER:
            raise ApiError(429, f"Du kannst höchstens {MAX_EVENTS_PER_USER} Events gleichzeitig anlegen.")
        return db.execute("INSERT INTO events(user_id,title,event_type,starts_at,description,max_attendees,created_at) "
                          "VALUES(?,?,?,?,?,?,?)", vals).lastrowid

    eid = await store.run(insert)
    return web.json_response({"id": eid}, status=201)


@routes.delete("/api/community/events/{eid}")
@api_handler
async def delete_event(request: web.Request) -> web.Response:
    user = await require_user(request)
    eid = int(request.match_info["eid"]) if request.match_info["eid"].isdigit() else -1
    n = await store.run(lambda db: db.execute("DELETE FROM events WHERE id=? AND user_id=?", (eid, user["id"])).rowcount)
    if not n:
        raise ApiError(404, "Event nicht gefunden oder nicht von dir.")
    return web.Response(status=204)


@routes.post("/api/community/events/{eid}/rsvp")
@api_handler
async def rsvp_event(request: web.Request) -> web.Response:
    user = await require_user(request)
    eid = int(request.match_info["eid"]) if request.match_info["eid"].isdigit() else -1

    def join(db: sqlite3.Connection) -> int:
        ev = db.execute("SELECT max_attendees FROM events WHERE id=?", (eid,)).fetchone()
        if not ev:
            raise ApiError(404, "Event nicht gefunden.")
        if db.execute("SELECT 1 FROM rsvps WHERE event_id=? AND user_id=?", (eid, user["id"])).fetchone():
            return db.execute("SELECT COUNT(*) FROM rsvps WHERE event_id=?", (eid,)).fetchone()[0]
        if db.execute("SELECT COUNT(*) FROM rsvps WHERE event_id=?", (eid,)).fetchone()[0] >= ev["max_attendees"]:
            raise ApiError(409, "Das Event ist bereits voll.")
        db.execute("INSERT INTO rsvps(event_id,user_id) VALUES(?,?)", (eid, user["id"]))
        return db.execute("SELECT COUNT(*) FROM rsvps WHERE event_id=?", (eid,)).fetchone()[0]

    count = await store.run(join)
    return web.json_response({"rsvp_count": count})
