"""Tests für das Hub-Backend (Konto, Profil, Sync, Community) und die Daten-API (Handel, Orte, Erze, Missionen).

Ohne Discord, ohne Internet: UEX-Antworten werden durch Beispieldaten ersetzt.
Ausführen:  python test_hub.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Datenverzeichnis VOR dem Import der Module setzen (storage liest es beim Import)
_TMP = tempfile.mkdtemp(prefix="hubtest_")
os.environ["DATA_DIR"] = _TMP

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import accounts
import hubapi
import webapp

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILED.append(name)


# --------------------------- Beispieldaten (Form wie UEX /2.0) ---------------------------

COMMODITIES = [
    {"id": 1, "name": "Gold", "kind": "Metal", "is_illegal": 0, "is_raw": 0, "price_sell": 6000},
    {"id": 2, "name": "Quantanium (Raw)", "kind": "Mineral", "is_illegal": 0, "is_raw": 1, "price_sell": 8000},
    {"id": 3, "name": "Quantanium", "kind": "Mineral", "is_illegal": 0, "is_raw": 0, "price_sell": 10000},
    {"id": 4, "name": "Neon", "kind": "Drug", "is_illegal": 1, "is_raw": 0, "price_sell": 3000},
]


def price(cid, term, tid, buy, sell, system="Stanton", city="", station="", **kw):
    return {"id_commodity": cid, "terminal_name": term, "id_terminal": tid, "price_buy": buy, "price_sell": sell,
            "star_system_name": system, "city_name": city, "space_station_name": station, **kw}


PRICES = [
    price(1, "Admin - Area 18", 10, 5000, 0, city="Area 18", scu_buy=200),
    price(1, "Admin - Lorville", 11, 0, 6200, city="Lorville"),
    price(1, "Rest Stop", 12, 0, 6600, station="Pyro Gateway", system="Pyro"),
    price(4, "Black Market", 13, 1000, 0, city="Grim HEX"),
    price(4, "Dealer", 14, 0, 3500, city="Lorville"),
    price(3, "Refinery", 15, 0, 10000, city="Lorville"),
]

LOCATION_GROUPS = {
    "star_systems": [{"name": "Stanton", "star_system_name": "Stanton"}, {"name": "Pyro"}],
    "planets": [{"name": "MicroTech", "star_system_name": "Stanton"}],
    "moons": [{"name": "Calliope", "planet_name": "microTech", "star_system_name": "Stanton"}],
    "space_stations": [{"name": "Baijini Point", "planet_name": "ArcCorp", "star_system_name": "Stanton"}],
    "cities": [{"name": "Area 18", "planet_name": "ArcCorp", "star_system_name": "Stanton"}],
}


# ---------------------------------- reine Berechnungen ----------------------------------


def test_pure() -> None:
    print("Berechnungen")
    r = hubapi.compute_routes(COMMODITIES, PRICES, cargo=32, budget=0, illegal="hide")
    check("legale Routen gefunden", len(r) == 2, str(len(r)))
    best = r[0]
    check("beste Route: Gold Area 18 -> Pyro (Marge 1600)", best["commodity"] == "Gold" and best["profitPerScu"] == 1600, str(best))
    check("Gewinn = Marge x Menge (Vorrat 200 > Fracht 32)", best["profit"] == 1600 * 32)
    check("Systemwechsel erkannt", best["crossSystem"] is True)
    check("illegale Ware ausgeblendet", all(not x["illegal"] for x in r))
    check("Budget begrenzt Menge", hubapi.compute_routes(COMMODITIES, PRICES, cargo=100, budget=10000)[0]["units"] == 2)
    check("nur illegal", {x["commodity"] for x in hubapi.compute_routes(COMMODITIES, PRICES, illegal="only")} == {"Neon"})
    check("Ziel-Filter 'lorville'", all("Lorville" in x["to"] for x in hubapi.compute_routes(COMMODITIES, PRICES, dest="lorville")))
    check("Start-Filter 'area18' (Leerzeichen egal)", len(hubapi.compute_routes(COMMODITIES, PRICES, origin="area18")) >= 1)
    check("Suche nach Ware", {x["commodity"] for x in hubapi.compute_routes(COMMODITIES, PRICES, query="gold")} == {"Gold"})
    check("nie gleiches Terminal", all(x["from"] != x["to"] for x in r))
    check("Kein Gewinn, keine Route", hubapi.compute_routes(COMMODITIES, [price(1, "A", 1, 100, 0), price(1, "B", 2, 0, 90)]) == [])

    board = hubapi.build_market_board(COMMODITIES, PRICES)
    check("Marktübersicht ohne Illegales", [b["name"] for b in board] == ["Gold"], str(board))
    check("Marge in Marktübersicht", board[0]["marginPerScu"] == 1600)

    ores = hubapi.build_ores(COMMODITIES)
    check("Erz mit Raffinerie-Aufschlag", ores == [{"name": "Quantanium", "rawSell": 8000.0, "refinedSell": 10000.0, "refineUplift": 25.0}], str(ores))

    rows = hubapi.build_locations(LOCATION_GROUPS)
    check("Orte zusammengeführt", {x["name"] for x in rows} >= {"Stanton", "Pyro", "MicroTech", "Calliope", "Baijini Point", "Area 18"})
    check("Suche 'area18' findet 'Area 18'", [x["name"] for x in hubapi.search_locations(rows, "area18")] == ["Area 18"])
    check("Systemfilter", {x["system"] for x in hubapi.search_locations(rows, "", "stanton")} == {"Stanton"})
    check("Suche nach Mutterkörper", "Calliope" in [x["name"] for x in hubapi.search_locations(rows, "microtech")])


# ------------------------------------- HTTP-Ebene --------------------------------------


async def fake_market():
    return {"commodities": COMMODITIES, "prices": PRICES, "fetched_at": time.time()}


async def fake_locations():
    return hubapi.build_locations(LOCATION_GROUPS)


async def fake_news():
    return {"data": [{"title": "Patch-Hinweise", "created_at": "2026-10-01T10:00:00Z", "rsi_url": "https://robertsspaceindustries.com/comm-link/x"}]}


async def test_http() -> None:
    hubapi.service = hubapi.HubService()
    hubapi.service._market_raw = fake_market  # type: ignore[method-assign]
    hubapi.service._locations_raw = fake_locations  # type: ignore[method-assign]
    hubapi.service._news_raw = fake_news  # type: ignore[method-assign]
    accounts.store = accounts.Store(Path(_TMP) / "hub.db")
    # Die Handler lesen die Modul-Variablen zur Laufzeit: die frisch gebauten Instanzen gelten sofort.

    app = webapp.create_app()
    async with TestClient(TestServer(app)) as c:
        await _scenarios(c)


async def _scenarios(c: TestClient) -> None:
    H = {"Origin": f"http://{c.server.host}:{c.server.port}"}

    print("Handel / Orte / Missionen / News")
    r = await c.get("/api/v1/trade/market")
    d = await r.json()
    check("Markt 200 mit Daten", r.status == 200 and d["data"][0]["name"] == "Gold", str(d))
    r = await c.get("/api/v1/trade/trader-routes?cargo=32&budget=150000&illegal=hide&limit=30&sort=profit")
    d = await r.json()
    check("Routen 200", r.status == 200 and len(d["data"]) == 2 and d["data"][0]["profit"] == 48000 and d["data"][0]["units"] == 30, str(d))
    r = await c.get("/api/v1/trade/trader-routes?illegal=all")
    check("Alle Routen inkl. illegal", len((await r.json())["data"]) == 3)
    r = await c.get("/api/v1/locations?q=area18&system=stanton")
    d = await r.json()
    check("Ortssuche", [x["name"] for x in d["data"]] == ["Area 18"], str(d))
    r = await c.get("/api/v1/locations/systems")
    check("Systeme", {x["name"] for x in (await r.json())["data"]} == {"Stanton", "Pyro"})
    r = await c.get("/api/v1/mining/ores")
    check("Erze", (await r.json())["data"][0]["name"] == "Quantanium")
    r = await c.get("/api/v1/missions")
    d = await r.json()
    check("Missionen: ehrlich als redaktionell gekennzeichnet", d["source"] == "redaktionell" and len(d["data"]) >= 6 and "keine Live" in d["source_label"])
    r = await c.get("/api/v1/news")
    check("News", (await r.json())["data"][0]["title"] == "Patch-Hinweise")

    # Quelle fällt aus -> alter Stand bleibt, mit stale-Flag
    async def broken():
        raise RuntimeError("UEX down")

    hubapi.service._market_raw = broken  # type: ignore[method-assign]
    hubapi.service.cache._data["market"] = (time.time() - 99999, hubapi.service.cache.peek("market"))
    r = await c.get("/api/v1/trade/market")
    d = await r.json()
    check("Ausfall: letzter Stand mit stale=true", r.status == 200 and d["stale"] is True)
    hubapi.service.cache._data.pop("market")
    hubapi.service.cache.errors.pop("market", None)
    r = await c.get("/api/v1/trade/market")
    check("Ausfall ohne Cache: 503 mit Klartext", r.status == 503 and "detail" in await r.json())

    print("Konto")
    r = await c.get("/api/auth/me")
    check("ohne Anmeldung: 401", r.status == 401)
    r = await c.post("/api/auth/register", json={"username": "pilot@example.com", "password": "kurz"}, headers=H)
    check("zu kurzes Passwort abgelehnt", r.status == 400)
    r = await c.post("/api/auth/register", json={"username": "pilot@example.com", "password": "geheim1234"}, headers=H)
    check("Registrierung 201", r.status == 201)
    check("Cookie HttpOnly + SameSite", "httponly" in r.headers["Set-Cookie"].lower() and "samesite=lax" in r.headers["Set-Cookie"].lower())
    r = await c.get("/api/auth/me")
    check("angemeldet nach Registrierung", r.status == 200 and (await r.json())["username"] == "pilot@example.com")
    r = await c.post("/api/auth/register", json={"username": "PILOT@example.com", "password": "geheim1234"}, headers=H)
    check("doppelter Name (Groß/Klein egal): 409", r.status == 409)

    r = await c.post("/api/auth/logout", headers=H)
    check("Abmelden 204", r.status == 204)
    r = await c.get("/api/auth/me")
    check("nach Abmelden 401", r.status == 401)
    r = await c.post("/api/auth/login", json={"username": "pilot@example.com", "password": "falsch1234"}, headers=H)
    check("falsches Passwort: 401", r.status == 401)
    r = await c.post("/api/auth/login", json={"username": "pilot@example.com", "password": "geheim1234"}, headers=H)
    check("Login 200", r.status == 200)

    print("Profil & Sync")
    r = await c.get("/api/profile")
    check("leeres Profil mit Standardrolle", (await r.json())["role"] == "Allrounder")
    r = await c.put("/api/profile", json={"name": "Maverick", "org": "Sky Crew", "role": "Miner", "discord": "https://discord.gg/abc", "bio": "Fliege gern abends"}, headers=H)
    check("Profil speichern", r.status == 200)
    r = await c.put("/api/profile", json={"name": "x", "discord": "javascript:alert(1)"}, headers=H)
    check("javascript:-Link abgelehnt", r.status == 400)
    r = await c.get("/api/profile")
    check("Profil gelesen", (await r.json())["org"] == "Sky Crew")
    r = await c.get("/api/organizations")
    d = await r.json()
    check("Organisationsverzeichnis zeigt freiwillige Einträge, ohne E-Mail", d[0]["org"] == "Sky Crew" and "pilot@example.com" not in str(d), str(d))

    r = await c.get("/api/user-data")
    check("Sync anfangs leer", await r.json() == {})
    payload = {"fleet": [{"id": "1", "status": "owned"}], "missions": [{"name": "Test"}], "evil": {"x": 1}}
    r = await c.put("/api/user-data", json=payload, headers=H)
    check("Sync speichern", r.status == 200)
    d = await (await c.get("/api/user-data")).json()
    check("Sync gelesen, unbekannte Schlüssel verworfen", d == {"fleet": payload["fleet"], "missions": payload["missions"]}, str(d))

    print("Community")
    r = await c.post("/api/community/posts", json={"pilot_name": "Maverick", "activity": "Mining", "role": "Pilot", "when": "heute", "description": "Suche Crew"}, headers=H)
    check("Crew-Gesuch anlegen", r.status == 201)
    posts = await (await c.get("/api/community/posts")).json()
    check("Gesuch sichtbar, als eigenes markiert", posts[0]["pilot_name"] == "Maverick" and posts[0]["mine"] is True)
    r = await c.post("/api/community/events", json={"title": "Convoy", "event_type": "Community Flight", "starts_at": "2026-11-01T20:00", "description": "x", "max_attendees": 2}, headers=H)
    eid = (await r.json())["id"]
    check("Event anlegen", r.status == 201)
    r = await c.post(f"/api/community/events/{eid}/rsvp", json={}, headers=H)
    check("Teilnehmen", (await r.json())["rsvp_count"] == 1)
    r = await c.post(f"/api/community/events/{eid}/rsvp", json={}, headers=H)
    check("doppeltes Teilnehmen zählt nicht doppelt", (await r.json())["rsvp_count"] == 1)

    # Zweite Person: eigene Sitzung (eigener Cookie-Speicher) gegen denselben Server
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s2:
        def u(path: str):
            return c.make_url(path)

        await s2.post(u("/api/auth/register"), json={"username": "zweiter", "password": "passwort99"}, headers=H)
        r = await s2.post(u(f"/api/community/events/{eid}/rsvp"), json={}, headers=H)
        check("zweite Zusage", r.status == 200)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s3:
        await s3.post(c.make_url("/api/auth/register"), json={"username": "dritter", "password": "passwort99"}, headers=H)
        r = await s3.post(c.make_url(f"/api/community/events/{eid}/rsvp"), json={}, headers=H)
        check("volles Event: 409", r.status == 409)
        posts = await (await s3.get(c.make_url("/api/community/posts"))).json()
        r = await s3.delete(c.make_url(f"/api/community/posts/{posts[0]['id']}"), headers=H)
        check("fremdes Gesuch löschen verboten (404)", r.status == 404)
    r = await c.delete(f"/api/community/posts/{posts[0]['id']}", headers=H)
    check("eigenes Gesuch löschen", r.status == 204)

    print("Schutz")
    r = await c.post("/api/community/posts", json={"pilot_name": "x", "activity": "a", "role": "r"}, headers={"Origin": "https://evil.example"})
    check("fremde Herkunft: 403 (CSRF)", r.status == 403)
    r = await c.post("/api/auth/login", data="kein json", headers={**H, "Content-Type": "application/json"})
    check("kaputtes JSON: 400", r.status == 400)
    r = await c.put("/api/user-data", data=b"{" + b"1" * 2_000_000 + b"}", headers={**H, "Content-Type": "application/json"})
    check("zu große Anfrage abgelehnt", r.status in (400, 413))
    r = await c.get("/")
    check("Startseite ausgeliefert + Sicherheits-Header", r.status == 200 and "Content-Security-Policy" in r.headers)
    html = await r.text()
    check("Seite nutzt eigene API statt Drittserver", "citizenhq.space/api/v1'" not in html and "const API='/api/v1'" in html)
    check("loadMissions ist definiert", "async function loadMissions" in html)
    r = await c.get("/api/auth/me")
    check("Antwort nicht cachebar", r.headers.get("Cache-Control") == "no-store")


async def main_async() -> None:
    await test_http()


if __name__ == "__main__":
    test_pure()
    asyncio.run(main_async())
    print()
    if FAILED:
        print(f"{len(FAILED)} Test(s) fehlgeschlagen:", *FAILED, sep="\n  - ")
        sys.exit(1)
    print("Alle Tests bestanden.")
