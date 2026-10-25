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

Die Seite liefert static/index.html, die Schiffsdaten (/api/ships) und das mitgelieferte
Backend aus: Daten für Handel/Universum/Missionen/News unter /api/v1/... (hubapi.py) sowie
Konto, Profil, Sync und Community-Board unter /api/auth, /api/profile, /api/user-data,
/api/organizations und /api/community/... (accounts.py). Alles läuft im selben Prozess.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

import accounts
import hubapi
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
    "img-src 'self' data: https:; "
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
AUTH_LIMIT = 20  # Anmelde-/Registrier-Versuche pro IP und Fenster (bremst Passwort-Raten)
AUTH_WINDOW = 600.0
_hits: dict[tuple[str, str], tuple[float, int]] = {}


def _client_ip(request: web.Request) -> str:
    # Hinter Fly.io steht der Proxy davor; die echte Besucher-IP steht in Fly-Client-IP.
    return request.headers.get("Fly-Client-IP") or request.remote or "unbekannt"


def _rate_limited(ip: str, bucket: str = "api", limit: int = RATE_LIMIT, window: float = RATE_WINDOW) -> bool:
    now = time.monotonic()
    key = (bucket, ip)
    start, count = _hits.get(key, (now, 0))
    if now - start >= window:
        start, count = now, 0
    _hits[key] = (start, count + 1)
    if len(_hits) > 5000:  # Speicher begrenzen: abgelaufene Einträge wegräumen
        for k in [k for k, (s, _) in _hits.items() if now - s >= max(window, AUTH_WINDOW)]:
            del _hits[k]
    return count + 1 > limit


def _same_origin(request: web.Request) -> bool:
    """Schreibende Anfragen müssen von dieser Seite selbst kommen (Schutz vor CSRF)."""
    origin = request.headers.get("Origin")
    if not origin:
        return True  # Same-Origin-Anfragen ohne Origin-Header und Nicht-Browser-Clients
    host = request.headers.get("X-Forwarded-Host") or request.host
    return urlparse(origin).netloc == host


@web.middleware
async def protect(request: web.Request, handler):
    is_api = request.path.startswith("/api/")
    if is_api and request.method in ("POST", "PUT", "PATCH", "DELETE") and not _same_origin(request):
        resp: web.StreamResponse = web.json_response({"detail": "Anfrage von fremder Herkunft abgelehnt."}, status=403)
    elif is_api and request.path in ("/api/auth/login", "/api/auth/register") and _rate_limited(
        _client_ip(request), "auth", AUTH_LIMIT, AUTH_WINDOW
    ):
        resp = web.json_response({"detail": "Zu viele Versuche - bitte in ein paar Minuten erneut probieren."}, status=429)
        resp.headers["Retry-After"] = str(int(AUTH_WINDOW))
    elif is_api and _rate_limited(_client_ip(request)):
        resp = web.json_response({"detail": "Zu viele Anfragen - bitte kurz warten."}, status=429)
        resp.headers["Retry-After"] = str(int(RATE_WINDOW))
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc  # auch Fehlerseiten (404 ...) bekommen die Header
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    if is_api:
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


def create_app(background_task: Optional[Callable[[], Awaitable[None]]] = None) -> web.Application:
    """Baut die Anwendung. `background_task` (nur im eigenständigen Betrieb) läuft mit ihr mit."""
    app = web.Application(middlewares=[protect], client_max_size=1_048_576)
    app.add_routes(routes)
    app.add_routes(hubapi.hub_routes)
    app.add_routes(accounts.routes)

    async def _open_db(app: web.Application) -> None:
        accounts.store.open()
        log.info("Konten-Datenbank bereit: %s", accounts.store.path)

    async def _close_db(app: web.Application) -> None:
        accounts.store.close()

    app.on_startup.append(_open_db)
    app.on_cleanup.append(_close_db)

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
