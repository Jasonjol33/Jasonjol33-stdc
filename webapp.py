"""Web-Showcase: die Schiffsliste als eigene Seite mit großen Bildern, Suche, Filtern
und einem Detail-Fenster pro Schiff - als Ergänzung zum Discord-Showcase.

Zwei Betriebsarten:

1. Im Bot-Prozess (Normalfall, `python bot.py`): bot.py startet den Webserver im selben
   Event-Loop und füttert ihn nach jedem erfolgreichen UEX-Abruf mit `publish_ships()`.
   Es gibt dadurch weiterhin nur EINEN UEX-Abruf, und Discord und Web-Seite zeigen denselben
   Stand. Die Seite hängt nicht von Discord ab: Selbst wenn das Posten im Channel scheitert,
   ist der frische Stand schon auf der Seite.

2. Eigenständig (`python webapp.py`): holt die Daten selbst von UEX. Praktisch, um die Seite
   lokal anzusehen, ohne Discord-Token und ohne den Bot zu starten.

Die Seite ist öffentlich und rein lesend; sie liefert nur static/index.html und die
Schiffsdaten als JSON (/api/ships) aus.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import hashlib
import hmac
import secrets
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp
from aiohttp import web

from config import WebSettings, load_web_settings
from uex_api import Ship, UexApiError, fetch_ships

log = logging.getLogger("webapp")

STATIC_DIR = Path(__file__).parent / "static"


def ship_to_dict(ship: Ship) -> dict:
    cheapest = ship.cheapest_price
    unique_terminals = sorted({p.terminal_name for p in ship.prices})
    return {
        "id": ship.id,
        "name": ship.name,
        "name_full": ship.name_full,
        "manufacturer": ship.manufacturer,
        "is_concept": ship.is_concept,
        "available_ingame": ship.available_ingame,
        "image_url": ship.image_url,
        "image_url_alt": ship.image_url_alt,
        "store_url": ship.store_url,
        "pad_type": ship.pad_type,
        "scu": ship.scu,
        "crew": ship.crew,
        "roles": ship.roles,
        "cheapest_price": cheapest.price_buy if cheapest else None,
        "cheapest_terminal": cheapest.terminal_name if cheapest else None,
        "extra_locations": max(0, len(unique_terminals) - 1),
        "all_prices": [
            {"terminal": p.terminal_name, "price": p.price_buy}
            for p in sorted(ship.prices, key=lambda p: p.price_buy)
        ],
    }


class ShipCache:
    """Hält den letzten guten Schiffsstand und die fertig serialisierte API-Antwort.

    Die JSON-Antwort wird nur bei Änderungen neu gebaut, nicht pro Besucher-Anfrage: Die
    Seite ist öffentlich und läuft im selben Prozess wie der Bot - ein Ansturm von
    Anfragen soll möglichst wenig Rechenzeit kosten.
    """

    def __init__(self) -> None:
        self.last_updated: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self._ships: list[dict] = []
        self._body: bytes = b""
        self._rebuild()

    @property
    def has_data(self) -> bool:
        return bool(self._ships)

    @property
    def body(self) -> bytes:
        return self._body

    def update(self, ships: dict[int, Ship]) -> None:
        """Neuer, erfolgreich geladener Stand (hebt eine frühere Störungsmeldung auf)."""
        self._ships = [ship_to_dict(s) for s in ships.values()]
        self.last_updated = datetime.now(timezone.utc)
        self.last_error = None
        self._rebuild()

    def mark_failed(self, reason: str) -> None:
        """Letzter Abruf fehlgeschlagen: Der alte Stand bleibt sichtbar, die Seite warnt dezent."""
        self.last_error = reason or "unbekannter Fehler"
        self._rebuild()

    def health(self) -> dict:
        """Kurzstatus für /healthz."""
        return {
            "status": "ok" if self.has_data else "no_data",
            "ships": len(self._ships),
            "stale": self.last_error is not None,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }

    def _rebuild(self) -> None:
        payload = {
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            # True = der letzte UEX-Abruf ist fehlgeschlagen, gezeigt wird der letzte gute Stand.
            # Bewusst nur ein Flag: interne Fehlertexte gehören nicht an Besucher, nur ins Log.
            "stale": self.last_error is not None,
            "total_ships": len(self._ships),
            "ingame_count": sum(1 for s in self._ships if s["available_ingame"]),
            "ships": self._ships,
        }
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")


cache = ShipCache()


def publish_ships(ships: dict[int, Ship]) -> None:
    """Vom Bot aufgerufen: frischer, erfolgreich geladener Stand für die Web-Seite."""
    cache.update(ships)


def publish_failure(reason: str) -> None:
    """Vom Bot aufgerufen: der letzte UEX-Abruf ist fehlgeschlagen (alter Stand bleibt sichtbar)."""
    cache.mark_failed(reason)


# --------------------------------------------------------------------------------------
# Sicherheits-Header und Rate-Limit
# --------------------------------------------------------------------------------------

# Die Seite ist eine einzelne HTML-Datei mit Inline-Skript/-Style; Bilder kommen von UEX/RSI/Wiki
# (alle https), Schriften von Google Fonts. Alles andere ist gesperrt.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

RATE_LIMIT = 120  # Anfragen pro IP und Fenster
RATE_WINDOW = 60.0  # Sekunden
_hits: dict[str, tuple[float, int]] = {}


def _client_ip(request: web.Request) -> str:
    # Hinter Fly.io steht der Proxy davor; die echte Besucher-IP steht in Fly-Client-IP.
    return request.headers.get("Fly-Client-IP") or request.remote or "unbekannt"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    start, count = _hits.get(ip, (now, 0))
    if now - start >= RATE_WINDOW:
        start, count = now, 0
    _hits[ip] = (start, count + 1)
    if len(_hits) > 5000:  # Speicher begrenzen: abgelaufene Einträge wegräumen
        for k in [k for k, (s, _) in _hits.items() if now - s >= RATE_WINDOW]:
            del _hits[k]
    return count + 1 > RATE_LIMIT


@web.middleware
async def protect(request: web.Request, handler):
    if request.path.startswith("/api/") and _rate_limited(_client_ip(request)):
        resp: web.StreamResponse = web.Response(status=429, text="Zu viele Anfragen - bitte kurz warten.")
        resp.headers["Retry-After"] = str(int(RATE_WINDOW))
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc  # auch Fehlerseiten (404 ...) bekommen die Header
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


routes = web.RouteTableDef()


@routes.get("/")
async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


@routes.get("/healthz")
async def handle_health(request: web.Request) -> web.Response:
    # Liveness: Antwort 200 heißt "der Webserver lebt" - auch wenn UEX gerade keine Daten liefert,
    # damit eine UEX-Störung nicht als Absturz der App gewertet wird. Die Details stehen im Body.
    return web.json_response(cache.health(), headers={"Cache-Control": "no-store"})


def _extract_api_rows(payload, keys=("data", "items", "results")):
    """Normalize common list and paginated JSON response shapes without losing rows."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _extract_api_rows(value, keys)
            if nested:
                return nested
    return []


@routes.get("/api/mission-feed")
async def handle_mission_feed(request: web.Request) -> web.Response:
    """Same-origin proxy for CitizenHQ's documented mission endpoint."""
    url = "https://citizenhq.space/api/v1/missions"
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "StarCitizenShowcase/1.2"},
        ) as session:
            async with session.get(url) as upstream:
                payload_text = await upstream.text()
                if upstream.status != 200:
                    log.warning("CitizenHQ missions returned HTTP %s", upstream.status)
                    return web.json_response({
                        "message": "CitizenHQ lieferte einen Fehler.",
                        "upstream_status": upstream.status,
                    }, status=502)
                try:
                    payload = json.loads(payload_text)
                except (ValueError, TypeError):
                    return web.json_response({"message": "CitizenHQ lieferte kein gültiges JSON."}, status=502)

        rows = _extract_api_rows(payload, ("missions", "items", "results", "data", "records"))
        if not rows:
            log.warning("CitizenHQ missions response had no recognizable rows")
            return web.json_response({
                "message": "Die Missionsquelle lieferte keine erkennbaren Missionsdatensätze.",
                "response_type": type(payload).__name__,
                "top_level_keys": list(payload.keys())[:30] if isinstance(payload, dict) else [],
            }, status=502)
        return web.json_response({"data": rows, "source": "CitizenHQ", "count": len(rows)})
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("CitizenHQ missions proxy failed: %s", type(exc).__name__)
        return web.json_response({
            "message": "CitizenHQ konnte vom Server nicht erreicht werden.",
            "error": type(exc).__name__,
        }, status=502)


@routes.get("/api/citizenhq/{path:.*}")
async def handle_citizenhq_proxy(request: web.Request) -> web.Response:
    """Same-origin proxy for CitizenHQ API calls, avoiding browser CORS/network failures."""
    import re

    path = request.match_info.get("path", "").strip("/")
    if not path or not re.fullmatch(r"[A-Za-z0-9/_-]+", path):
        return web.json_response({"message": "Ungültiger API-Pfad."}, status=400)
    url = f"https://citizenhq.space/api/v1/{path}"
    if request.query_string:
        url += "?" + request.query_string
    timeout = aiohttp.ClientTimeout(total=18, connect=5)
    headers = {"Accept": "application/json", "User-Agent": "StarCitizenShowcase/1.2"}

    # Der Universe Explorer benötigt eine echte Liste. CitizenHQ kann abhängig von
    # Datenstand/API-Antwort ein leeres oder anders verschachteltes Ergebnis liefern.
    # In diesem Fall wird die Star Citizen Wiki API als zweite Datenquelle verwendet.
    if path == "locations":
        query = request.query.get("q", "").strip()
        system = request.query.get("system", "").strip().lower()
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as upstream:
                    raw = await upstream.text()
                    try:
                        payload = json.loads(raw)
                    except (ValueError, TypeError):
                        payload = None
                    rows = _extract_api_rows(payload, ("data", "items", "results", "locations", "records"))
                    # Some APIs wrap their list in several object layers.
                    for _ in range(4):
                        if rows or not isinstance(payload, dict):
                            break
                        next_payload = next((payload.get(k) for k in ("data", "results", "result", "locations") if isinstance(payload.get(k), dict)), None)
                        if next_payload is None:
                            break
                        payload = next_payload
                        rows = _extract_api_rows(payload, ("data", "items", "results", "locations", "records"))
                    if rows:
                        return web.json_response({"data": rows, "source": "CitizenHQ", "count": len(rows)}, headers={"Cache-Control": "no-store"})
                    log.warning("CitizenHQ locations returned no recognizable rows; trying Star Citizen Wiki API (HTTP %s)", upstream.status)

                # Use documented public Wiki endpoint as fallback. Search locally so this
                # also works when the upstream's filter parameter names change.
                wiki_rows = []
                for page in range(1, 6):
                    wiki_url = "https://api.star-citizen.wiki/api/locations"
                    params = {"page[number]": str(page), "page[size]": "200"}
                    async with session.get(wiki_url, params=params) as fallback:
                        if fallback.status != 200:
                            log.warning("Star Citizen Wiki locations returned HTTP %s", fallback.status)
                            break
                        try:
                            wiki_payload = await fallback.json(content_type=None)
                        except (ValueError, aiohttp.ContentTypeError):
                            break
                    page_rows = _extract_api_rows(wiki_payload, ("data", "items", "results", "locations", "records"))
                    if not page_rows and isinstance(wiki_payload, dict):
                        # Common paginated shape: {data: {data: [...]}}.
                        nested = wiki_payload.get("data")
                        if isinstance(nested, dict):
                            page_rows = _extract_api_rows(nested, ("data", "items", "results", "locations", "records"))
                    if not page_rows:
                        break
                    wiki_rows.extend(page_rows)
                    if query and any(query.casefold() in str(x.get("name", "")).casefold() for x in wiki_rows if isinstance(x, dict)):
                        break
                    meta = wiki_payload.get("meta", {}) if isinstance(wiki_payload, dict) else {}
                    last_page = meta.get("last_page") or meta.get("lastPage") if isinstance(meta, dict) else None
                    if last_page and page >= int(last_page):
                        break
                    if len(page_rows) < 200:
                        break

                filtered = []
                for item in wiki_rows:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("label") or item.get("title") or "")
                    item_system = str(item.get("system") or item.get("systemName") or "")
                    if query and query.casefold() not in name.casefold() and query.casefold() not in str(item.get("parentName") or "").casefold():
                        continue
                    if system and system not in item_system.casefold():
                        continue
                    normalized = dict(item)
                    normalized.setdefault("name", name or "Unbekannter Ort")
                    normalized.setdefault("system", item_system)
                    normalized.setdefault("type", item.get("type") or item.get("kind") or item.get("category") or "Ort")
                    filtered.append(normalized)
                if filtered:
                    return web.json_response({"data": filtered[:100], "source": "Star Citizen Wiki API", "count": len(filtered)}, headers={"Cache-Control": "no-store"})
                return web.json_response({"data": [], "source": "CitizenHQ + Star Citizen Wiki API", "count": 0, "message": "Keine passenden Orte in den Datenquellen gefunden."}, headers={"Cache-Control": "no-store"})
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Standortquellen fehlgeschlagen: %s", type(exc).__name__)
            return web.json_response({"data": [], "source": "Standort-API", "message": "Standortquellen sind nicht erreichbar.", "error": type(exc).__name__}, status=502)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as upstream:
                body = await upstream.read()
                content_type = upstream.headers.get("Content-Type", "application/json")
                return web.Response(
                    body=body,
                    status=upstream.status,
                    content_type=content_type.split(";", 1)[0],
                    headers={"Cache-Control": "no-store", "X-Proxy-Source": "CitizenHQ"},
                )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("CitizenHQ-Proxy fehlgeschlagen für %s: %s", path, type(exc).__name__)
        return web.json_response({
            "message": "CitizenHQ konnte vom Server nicht erreicht werden.",
            "endpoint": path,
            "error": type(exc).__name__,
        }, status=502)


@routes.get("/api/ships")
async def handle_ships(request: web.Request) -> web.Response:
    # Solange noch gar kein Datenstand existiert (Start läuft noch oder UEX war bisher nicht
    # erreichbar), ehrlich mit 503 antworten statt mit einer leeren "Alles ok"-Liste.
    return web.Response(
        body=cache.body,
        status=200 if cache.has_data else 503,
        content_type="application/json",
        charset="utf-8",
    )



# Kleine optionale Konto-API für Pilotprofil und Synchronisierung.
# Konten werden mit PBKDF2-Hashes gespeichert; Passwörter werden nie im Klartext abgelegt.
ACCOUNT_DIR = Path(os.getenv("DATA_DIR", "").strip() or (Path(__file__).parent / "data"))
ACCOUNT_FILE = ACCOUNT_DIR / "accounts.json"
_SESSIONS: dict[str, str] = {}
_ACCOUNT_LOCK = __import__("threading").Lock()


def _accounts() -> dict:
    ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return json.loads(ACCOUNT_FILE.read_text(encoding="utf-8")) if ACCOUNT_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        log.exception("Kontodatei konnte nicht gelesen werden.")
        return {}


def _save_accounts(data: dict) -> None:
    ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ACCOUNT_FILE)


def _account_name(raw: str) -> str:
    return str(raw or "").strip().lower()[:120]


def _password_hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return salt.hex(), digest.hex()


def _current_user(request: web.Request) -> str | None:
    token = request.cookies.get("scx_session", "")
    return _SESSIONS.get(token)


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"message": message}, status=status)


@routes.post("/api/auth/register")
async def auth_register(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_error("Ungültige Anfrage.")
    username = _account_name(body.get("username"))
    password = str(body.get("password") or "")
    if len(username) < 3:
        return _json_error("Benutzername/E-Mail muss mindestens 3 Zeichen haben.")
    if len(password) < 8 or len(password) > 1024:
        return _json_error("Passwort muss mindestens 8 Zeichen haben.")
    with _ACCOUNT_LOCK:
        data = _accounts()
        if username in data:
            return _json_error("Dieses Konto existiert bereits.", 409)
        salt, digest = _password_hash(password)
        data[username] = {"salt": salt, "password_hash": digest, "profile": {}, "user_data": {}}
        _save_accounts(data)
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = username
    response = web.json_response({"username": username, "ok": True})
    response.set_cookie("scx_session", token, httponly=True, secure=request.secure,
                        samesite="Lax", max_age=60 * 60 * 24 * 30, path="/")
    return response


@routes.post("/api/auth/login")
async def auth_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_error("Ungültige Anfrage.")
    username = _account_name(body.get("username"))
    password = str(body.get("password") or "")
    data = _accounts()
    user = data.get(username)
    if not user:
        return _json_error("Benutzername oder Passwort ist falsch.", 401)
    try:
        expected = bytes.fromhex(user["password_hash"])
        salt = bytes.fromhex(user["salt"])
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    except (KeyError, ValueError):
        return _json_error("Kontodaten sind beschädigt.", 500)
    if not hmac.compare_digest(actual, expected):
        return _json_error("Benutzername oder Passwort ist falsch.", 401)
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = username
    response = web.json_response({"username": username, "ok": True})
    response.set_cookie("scx_session", token, httponly=True, secure=request.secure,
                        samesite="Lax", max_age=60 * 60 * 24 * 30, path="/")
    return response


@routes.get("/api/auth/me")
async def auth_me(request: web.Request) -> web.Response:
    username = _current_user(request)
    return web.json_response({"username": username, "authenticated": bool(username)})


@routes.post("/api/auth/logout")
async def auth_logout(request: web.Request) -> web.Response:
    token = request.cookies.get("scx_session", "")
    _SESSIONS.pop(token, None)
    response = web.json_response({"ok": True})
    response.del_cookie("scx_session", path="/")
    return response


@routes.get("/api/profile")
async def account_get_profile(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    return web.json_response(_accounts().get(username, {}).get("profile", {}))


@routes.put("/api/profile")
async def account_put_profile(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    try:
        profile = await request.json()
    except Exception:
        return _json_error("Ungültige Profildaten.")
    if not isinstance(profile, dict):
        return _json_error("Ungültige Profildaten.")
    with _ACCOUNT_LOCK:
        data = _accounts()
        if username not in data:
            return _json_error("Konto nicht gefunden.", 401)
        data[username]["profile"] = {k: str(v)[:5000] for k, v in profile.items() if k in {"name", "org", "role", "discord", "bio"}}
        _save_accounts(data)
    return web.json_response({"ok": True})


@routes.get("/api/user-data")
async def account_get_user_data(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    return web.json_response(_accounts().get(username, {}).get("user_data", {}))


@routes.put("/api/user-data")
async def account_put_user_data(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    try:
        user_data = await request.json()
    except Exception:
        return _json_error("Ungültige Synchronisierungsdaten.")
    if not isinstance(user_data, dict):
        return _json_error("Ungültige Synchronisierungsdaten.")
    allowed = {"fleet", "builds", "buildCompare", "routes", "crews", "events", "missions", "guide", "check"}
    cleaned = {k: user_data[k] for k in allowed if k in user_data}
    if len(json.dumps(cleaned, ensure_ascii=False)) > 2_000_000:
        return _json_error("Synchronisierungsdaten sind zu groß.", 413)
    with _ACCOUNT_LOCK:
        data = _accounts()
        if username not in data:
            return _json_error("Konto nicht gefunden.", 401)
        data[username]["user_data"] = cleaned
        _save_accounts(data)
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------------------
# Star Citizen Wiki: Comm-Link-Feed als Same-Origin-Proxy (CSP erlaubt nur connect-src 'self')
# --------------------------------------------------------------------------------------

_WIKI_NEWS_TTL = 300.0  # Sekunden
_wiki_news_cache: dict = {"ts": 0.0, "body": None}


@routes.get("/api/wiki/comm-links")
async def handle_wiki_comm_links(request: web.Request) -> web.Response:
    """Holt die neuesten Comm-Links serverseitig und cached sie kurz (schont das Rate-Limit)."""
    now = time.monotonic()
    cached = _wiki_news_cache["body"]
    if cached is not None and now - _wiki_news_cache["ts"] < _WIKI_NEWS_TTL:
        return web.Response(body=cached, content_type="application/json", charset="utf-8",
                            headers={"Cache-Control": "public, max-age=120", "X-Proxy-Cache": "HIT"})
    url = "https://api.star-citizen.wiki/api/comm-links"
    params = {"page[number]": "1", "page[size]": "10", "sort": "-created_at"}
    timeout = aiohttp.ClientTimeout(total=12, connect=5)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "StarCitizenShowcase/1.2"},
        ) as session:
            async with session.get(url, params=params) as upstream:
                body = await upstream.read()
                if upstream.status != 200:
                    log.warning("Star Citizen Wiki comm-links returned HTTP %s", upstream.status)
                    if cached is not None:  # lieber veraltete Daten als gar keine
                        return web.Response(body=cached, content_type="application/json", charset="utf-8",
                                            headers={"X-Proxy-Cache": "STALE"})
                    return web.json_response({"message": "Wiki-API lieferte einen Fehler.",
                                              "upstream_status": upstream.status}, status=502)
                try:
                    json.loads(body)
                except (ValueError, TypeError):
                    return web.json_response({"message": "Wiki-API lieferte kein gültiges JSON."}, status=502)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("Wiki-Comm-Link-Proxy fehlgeschlagen: %s", type(exc).__name__)
        if cached is not None:
            return web.Response(body=cached, content_type="application/json", charset="utf-8",
                                headers={"X-Proxy-Cache": "STALE"})
        return web.json_response({"message": "Wiki-API nicht erreichbar.", "error": type(exc).__name__}, status=502)
    _wiki_news_cache.update(ts=now, body=body)
    return web.Response(body=body, content_type="application/json", charset="utf-8",
                        headers={"Cache-Control": "public, max-age=120", "X-Proxy-Cache": "MISS"})


# --------------------------------------------------------------------------------------
# Community-Board (Crew-Gesuche, Events) und Organisationsverzeichnis
# Lesen ist öffentlich, Schreiben/Löschen/RSVP braucht ein angemeldetes Konto.
# --------------------------------------------------------------------------------------

COMMUNITY_FILE = ACCOUNT_DIR / "community.json"
_EVENT_PUBLISHER: Optional[Callable[[dict], Awaitable[None]]] = None
_COMMUNITY_LOCK = __import__("threading").Lock()
_MAX_POSTS = 200
_MAX_EVENTS = 200


def _community() -> dict:
    ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(COMMUNITY_FILE.read_text(encoding="utf-8")) if COMMUNITY_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        log.exception("Community-Datei konnte nicht gelesen werden.")
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("posts", [])
    data.setdefault("events", [])
    return data


def _save_community(data: dict) -> None:
    ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = COMMUNITY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(COMMUNITY_FILE)


def _clip(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _public_post(p: dict) -> dict:
    return {k: p.get(k, "") for k in ("id", "pilot_name", "activity", "role", "when", "description", "created_at")}


def set_event_publisher(callback: Optional[Callable[[dict], Awaitable[None]]]) -> None:
    """Registriert den Discord-Publisher; bleibt im reinen Web-Betrieb optional."""
    global _EVENT_PUBLISHER
    _EVENT_PUBLISHER = callback


def create_community_event(*, owner: str, title: str, event_type: str = "", starts_at: str = "", description: str = "", max_attendees: int = 8) -> dict:
    """Legt ein Event an und gibt es im internen Format zurück (Website und Slash-Command)."""
    title = _clip(title, 120)
    if len(title) < 3:
        raise ValueError("Bitte einen Event-Titel mit mindestens 3 Zeichen angeben.")
    event = {
        "id": secrets.token_urlsafe(8), "owner": _clip(owner, 120), "title": title,
        "event_type": _clip(event_type, 60), "starts_at": _clip(starts_at, 40),
        "description": _clip(description, 1000), "max_attendees": max(1, min(500, int(max_attendees or 8))),
        "rsvps": [], "discord_rsvps": {}, "discord_message_id": None,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _COMMUNITY_LOCK:
        data = _community()
        data["events"] = (data["events"] + [event])[-_MAX_EVENTS:]
        _save_community(data)
    return event


def get_community_events() -> list[dict]:
    return list(_community()["events"])


def get_community_event(event_id: str) -> Optional[dict]:
    return next((e for e in _community()["events"] if e.get("id") == event_id), None)


def set_discord_rsvp(event_id: str, user_id: int, display_name: str, status: str) -> Optional[dict]:
    if status not in ("yes", "no"):
        return None
    with _COMMUNITY_LOCK:
        data = _community()
        event = next((e for e in data["events"] if e.get("id") == event_id), None)
        if event is None:
            return None
        responses = event.setdefault("discord_rsvps", {})
        responses[str(user_id)] = {"name": _clip(display_name, 100), "status": status}
        _save_community(data)
        return dict(event)


def set_event_discord_message(event_id: str, message_id: int, channel_id: int) -> None:
    with _COMMUNITY_LOCK:
        data = _community()
        event = next((e for e in data["events"] if e.get("id") == event_id), None)
        if event is not None:
            event["discord_message_id"] = int(message_id)
            event["discord_channel_id"] = int(channel_id)
            _save_community(data)


def _public_event(e: dict) -> dict:
    responses = e.get("discord_rsvps", {})
    yes_names = [v.get("name", "Spieler") for v in responses.values() if isinstance(v, dict) and v.get("status") == "yes"]
    no_names = [v.get("name", "Spieler") for v in responses.values() if isinstance(v, dict) and v.get("status") == "no"]
    return {
        "id": e.get("id"), "title": e.get("title", ""), "event_type": e.get("event_type", ""),
        "starts_at": e.get("starts_at", ""), "description": e.get("description", ""),
        "max_attendees": e.get("max_attendees", 8), "rsvp_count": len(e.get("rsvps", [])),
        "can_count": len(yes_names), "cannot_count": len(no_names),
        "can_names": yes_names, "cannot_names": no_names,
        "created_at": e.get("created_at", ""),
    }


@routes.get("/api/community/posts")
async def community_posts(request: web.Request) -> web.Response:
    return web.json_response([_public_post(p) for p in reversed(_community()["posts"])])


@routes.post("/api/community/posts")
async def community_create_post(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    try:
        body = await request.json()
    except Exception:
        return _json_error("Ungültige Anfrage.")
    if not isinstance(body, dict):
        return _json_error("Ungültige Anfrage.")
    name = _clip(body.get("pilot_name"), 80)
    if len(name) < 2:
        return _json_error("Bitte einen Piloten-/Crewnamen angeben.")
    post = {
        "id": secrets.token_urlsafe(8), "owner": username, "pilot_name": name,
        "activity": _clip(body.get("activity"), 60), "role": _clip(body.get("role"), 60),
        "when": _clip(body.get("when"), 80), "description": _clip(body.get("description"), 1000),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _COMMUNITY_LOCK:
        data = _community()
        data["posts"] = (data["posts"] + [post])[-_MAX_POSTS:]
        _save_community(data)
    return web.json_response(_public_post(post), status=201)


@routes.delete("/api/community/posts/{post_id}")
async def community_delete_post(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    post_id = request.match_info["post_id"]
    with _COMMUNITY_LOCK:
        data = _community()
        target = next((p for p in data["posts"] if p.get("id") == post_id), None)
        if target is None:
            return _json_error("Eintrag nicht gefunden.", 404)
        if target.get("owner") != username:
            return _json_error("Nur der Ersteller kann den Eintrag löschen.", 403)
        data["posts"] = [p for p in data["posts"] if p.get("id") != post_id]
        _save_community(data)
    return web.json_response({"ok": True})


@routes.get("/api/community/events")
async def community_events(request: web.Request) -> web.Response:
    return web.json_response([_public_event(e) for e in _community()["events"]])


@routes.post("/api/community/events")
async def community_create_event(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    try:
        body = await request.json()
    except Exception:
        return _json_error("Ungültige Anfrage.")
    if not isinstance(body, dict):
        return _json_error("Ungültige Anfrage.")
    title = _clip(body.get("title"), 120)
    if len(title) < 3:
        return _json_error("Bitte einen Event-Titel angeben.")
    try:
        max_att = max(1, min(500, int(body.get("max_attendees") or 8)))
    except (TypeError, ValueError):
        max_att = 8
    try:
        event = create_community_event(
            owner=username, title=title, event_type=_clip(body.get("event_type"), 60),
            starts_at=_clip(body.get("starts_at"), 40), description=_clip(body.get("description"), 1000),
            max_attendees=max_att,
        )
    except ValueError as exc:
        return _json_error(str(exc))
    if _EVENT_PUBLISHER is not None:
        try:
            await _EVENT_PUBLISHER(event)
        except Exception:
            log.exception("Event %s wurde gespeichert, aber nicht in Discord veröffentlicht.", event.get("id"))
    return web.json_response(_public_event(event), status=201)


@routes.post("/api/community/events/{event_id}/rsvp")
async def community_rsvp(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    event_id = request.match_info["event_id"]
    with _COMMUNITY_LOCK:
        data = _community()
        event = next((e for e in data["events"] if e.get("id") == event_id), None)
        if event is None:
            return _json_error("Event nicht gefunden.", 404)
        rsvps = event.setdefault("rsvps", [])
        if username not in rsvps:
            if len(rsvps) >= int(event.get("max_attendees") or 8):
                return _json_error("Das Event ist voll.", 409)
            rsvps.append(username)
            _save_community(data)
        result = _public_event(event)
    return web.json_response(result)


@routes.delete("/api/community/events/{event_id}")
async def community_delete_event(request: web.Request) -> web.Response:
    username = _current_user(request)
    if not username:
        return _json_error("Bitte zuerst anmelden.", 401)
    event_id = request.match_info["event_id"]
    with _COMMUNITY_LOCK:
        data = _community()
        target = next((e for e in data["events"] if e.get("id") == event_id), None)
        if target is None:
            return _json_error("Event nicht gefunden.", 404)
        if target.get("owner") != username:
            return _json_error("Nur der Ersteller kann das Event löschen.", 403)
        data["events"] = [e for e in data["events"] if e.get("id") != event_id]
        _save_community(data)
    return web.json_response({"ok": True})


@routes.get("/api/organizations")
async def organizations_directory(request: web.Request) -> web.Response:
    """Öffentliches Verzeichnis: nur Profile, die eine Organisation eingetragen haben.
    Es werden ausschließlich bewusst öffentliche Profilfelder ausgegeben (keine Kontonamen)."""
    rows = []
    for account in _accounts().values():
        prof = account.get("profile") if isinstance(account, dict) else None
        if isinstance(prof, dict) and str(prof.get("org", "")).strip():
            rows.append({k: str(prof.get(k, ""))[:500] for k in ("org", "name", "role", "bio", "discord")})
    return web.json_response(rows[:100])


def create_app(background_task: Optional[Callable[[], Awaitable[None]]] = None) -> web.Application:
    """Baut die Anwendung. `background_task` (nur im eigenständigen Betrieb) läuft mit ihr mit."""
    app = web.Application(middlewares=[protect])
    app.add_routes(routes)

    if background_task is not None:

        async def _start(app: web.Application) -> None:
            app["background_task"] = asyncio.create_task(background_task())

        async def _stop(app: web.Application) -> None:
            task = app.get("background_task")
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        app.on_startup.append(_start)
        app.on_cleanup.append(_stop)
    return app


async def start_server(port: int = 8080, host: str = "0.0.0.0") -> web.AppRunner:
    """Startet den Webserver im bereits laufenden Event-Loop (Betrieb im Bot-Prozess).

    Wirft eine Exception, wenn der Port nicht gebunden werden kann - der Aufrufer (bot.py)
    fängt sie ab, damit ein Web-Problem den Discord-Bot nie stoppt.
    """
    runner = web.AppRunner(create_app(), access_log=None)  # kein Zugriffs-Log im Bot-Log
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


# --------------------------------------------------------------------------------------
# Eigenständiger Betrieb (python webapp.py): Daten selbst bei UEX abfragen
# --------------------------------------------------------------------------------------


async def refresh_from_uex(settings: WebSettings) -> None:
    """Holt die Schiffsdaten selbst von UEX und legt sie in den Cache."""
    try:
        ships = await fetch_ships(
            api_key=settings.uex_api_key,
            include_ground_vehicles=settings.include_ground_vehicles,
        )
    except (UexApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # Erwartbar (UEX kurz down, Timeout, ...): Warnung genügt, kein Traceback.
        reason = str(exc) or exc.__class__.__name__
        log.warning("UEX-Abruf fehlgeschlagen, behalte bisherigen Stand: %s", reason)
        cache.mark_failed(reason)
        return
    except Exception:  # wirklich unerwartet - Seite trotzdem am Leben halten
        log.exception("Unerwarteter Fehler beim UEX-Abruf.")
        cache.mark_failed("Unerwarteter Fehler beim Abruf.")
        return

    if not ships:
        log.warning("UEX hat 0 Schiffe zurückgegeben - behalte den bisherigen Stand.")
        cache.mark_failed("UEX lieferte 0 Schiffe.")
        return

    cache.update(ships)
    log.info("Schiffsdaten aktualisiert: %d Schiffe geladen.", len(ships))
    try:  # Lücken (SCU, Crew, Pad, Bild) aus dem Star Citizen Wiki füllen
        import wiki_data

        if wiki_data.fill_gaps(ships, await wiki_data.load_index()):
            cache.update(ships)
        wiki_data.report_gaps(ships)
    except Exception:
        log.exception("Wiki-Abgleich fehlgeschlagen - Seite läuft mit den UEX-Daten weiter.")


async def standalone_refresh_loop(settings: WebSettings) -> None:
    failures = 0
    while True:
        await refresh_from_uex(settings)
        if cache.last_error:
            # Nach einem Fehler nicht das volle Intervall warten (UEX hat gelegentlich kurze
            # Downtimes), aber auch nicht hämmern: 1, 2, 4, 8 ... Minuten, höchstens das Intervall.
            failures = min(failures + 1, 10)
            delay = min(60 * 2 ** (failures - 1), settings.update_interval_minutes * 60)
        else:
            failures = 0
            delay = settings.update_interval_minutes * 60
        await asyncio.sleep(delay)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = load_web_settings()
    app = create_app(background_task=lambda: standalone_refresh_loop(settings))
    log.info(
        "Starte Web-Showcase eigenständig (ohne Bot) auf http://localhost:%d - UEX-Abruf alle %d Minuten.",
        settings.port,
        settings.update_interval_minutes,
    )
    web.run_app(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
