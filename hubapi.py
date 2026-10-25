"""Daten-API für die Hub-Seiten (Handel, Universum, Mission Control, News, Mining).

Warum es dieses Modul gibt
--------------------------
Die Seite hat ihre Daten früher direkt im Browser von fremden Servern geholt. Das scheiterte an
der Content-Security-Policy (`connect-src 'self'`), und der Drittdienst war nicht zuverlässig
erreichbar. Jetzt holt der SERVER die Daten, rechnet sie aus und liefert sie unter
`/api/v1/...` an die Seite aus:

* Eine Quelle (UEX Corp, dieselbe wie für die Schiffspreise), ein Cache, kein Ansturm von
  Browsern auf die UEX-API (Limit dort: 60 Anfragen/Minute).
* Fällt UEX kurz aus, bleibt der letzte gute Stand sichtbar (`stale: true`).
* Nichts wird erfunden: Was UEX nicht liefert (z. B. Koordinaten, echte Vertragslisten), wird
  als solches gekennzeichnet.

Die Berechnungen (Routen, Marktübersicht, Erze, Ortssuche) sind reine Funktionen ohne
Netzwerkzugriff und lassen sich mit Beispieldaten testen (siehe test_hub.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp
from aiohttp import web

from uex_api import API_BASE, USER_AGENT, UexApiError, _get_json

log = logging.getLogger("hubapi")

# ----------------------------------------------------------------------------------------
# Kleine Hilfen
# ----------------------------------------------------------------------------------------

MARKET_TTL = 15 * 60  # UEX-Preise ändern sich nicht sekündlich; 15 min schonen die API
LOCATIONS_TTL = 6 * 3600  # Orte ändern sich nur mit Patches
NEWS_TTL = 10 * 60

NEWS_URLS = (
    "https://api.star-citizen.wiki/api/v2/comm-links?page[size]=10&sort=-created_at",
    "https://api.star-citizen.wiki/api/comm-links?page[number]=1&page[size]=10&sort=-created_at",
)


def norm(text: Any) -> str:
    """Vergleichsschlüssel: klein, ohne Leerzeichen/Sonderzeichen ("Area 18" == "area18")."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def num(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if n == n and n not in (float("inf"), float("-inf")) else 0.0


def _first(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


class TtlCache:
    """Cache mit Ablaufzeit. Bei einem Fehler wird der letzte gute Stand weiterverwendet."""

    def __init__(self) -> None:
        self._data: Dict[str, tuple] = {}  # key -> (zeitpunkt, wert)
        self._locks: Dict[str, asyncio.Lock] = {}
        self.errors: Dict[str, str] = {}

    def peek(self, key: str):
        item = self._data.get(key)
        return item[1] if item else None

    def age(self, key: str) -> Optional[float]:
        item = self._data.get(key)
        return time.time() - item[0] if item else None

    async def get(self, key: str, ttl: float, fetch: Callable[[], Awaitable[Any]]):
        """Liefert (wert, stale). Wirft nur, wenn es weder frische noch alte Daten gibt."""
        item = self._data.get(key)
        if item and time.time() - item[0] < ttl:
            return item[1], key in self.errors
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            item = self._data.get(key)  # inzwischen von einer parallelen Anfrage gefüllt?
            if item and time.time() - item[0] < ttl:
                return item[1], key in self.errors
            try:
                value = await fetch()
            except Exception as exc:  # noqa: BLE001 - jede Störung -> alter Stand
                self.errors[key] = str(exc) or exc.__class__.__name__
                log.warning("Abruf '%s' fehlgeschlagen: %s", key, self.errors[key])
                if item:
                    # Kurz zurückstellen, damit nicht jede Anfrage UEX erneut anklopft.
                    self._data[key] = (time.time() - ttl + min(60, ttl), item[1])
                    return item[1], True
                raise
            self._data[key] = (time.time(), value)
            self.errors.pop(key, None)
            return value, False


# ----------------------------------------------------------------------------------------
# Reine Berechnungen (ohne Netzwerk)
# ----------------------------------------------------------------------------------------


def place_of(row: dict) -> str:
    """Kürzester sinnvoller Ortsname eines UEX-Preiseintrags (Stadt/Station/Außenposten/Mond/Planet)."""
    return _first(
        row, "city_name", "space_station_name", "outpost_name", "poi_name", "moon_name", "planet_name", "orbit_name"
    )


def terminal_label(row: dict) -> str:
    name = _first(row, "terminal_name", "terminal_displayname", "terminal_nickname") or "Unbekanntes Terminal"
    place = place_of(row)
    if place and norm(place) not in norm(name):
        name = f"{name} · {place}"
    return name


def _haystack(row: dict) -> str:
    keys = (
        "terminal_name", "terminal_displayname", "terminal_nickname", "city_name", "space_station_name",
        "outpost_name", "poi_name", "moon_name", "planet_name", "orbit_name", "star_system_name",
    )
    return norm(" ".join(str(row.get(k) or "") for k in keys))


def index_commodities(commodities: List[dict]) -> Dict[int, dict]:
    return {int(c["id"]): c for c in commodities if isinstance(c, dict) and c.get("id") is not None}


def compute_routes(
    commodities: List[dict],
    prices: List[dict],
    *,
    cargo: int = 32,
    budget: float = 0,
    origin: str = "",
    dest: str = "",
    query: str = "",
    illegal: str = "hide",  # hide | all | only
    limit: int = 30,
) -> List[dict]:
    """Berechnet Handelsrouten: Ware an Terminal A kaufen, an Terminal B verkaufen.

    UEX-Felder: `price_buy` = Preis, den du am Terminal beim KAUFEN zahlst;
    `price_sell` = Preis, den das Terminal beim VERKAUFEN an dich zahlt.
    Gewinn pro SCU = Verkaufspreis(B) - Kaufpreis(A). Die Menge wird durch Fracht, Kapital und
    (falls bekannt) den Vorrat am Kaufterminal begrenzt. Das ist eine Schätzung, keine Garantie.
    """
    cargo = max(1, int(cargo or 1))
    budget = max(0.0, float(budget or 0))
    cidx = index_commodities(commodities)
    q = norm(query)
    o, d = norm(origin), norm(dest)

    by_commodity: Dict[int, Dict[str, list]] = {}
    for p in prices:
        try:
            cid = int(p.get("id_commodity"))
        except (TypeError, ValueError):
            continue
        com = cidx.get(cid)
        if com is None:
            continue
        is_illegal = bool(com.get("is_illegal"))
        if (illegal == "hide" and is_illegal) or (illegal == "only" and not is_illegal):
            continue
        slot = by_commodity.setdefault(cid, {"buy": [], "sell": []})
        if num(p.get("price_buy")) > 0:
            slot["buy"].append(p)
        if num(p.get("price_sell")) > 0:
            slot["sell"].append(p)

    routes: List[dict] = []
    for cid, slot in by_commodity.items():
        com = cidx[cid]
        cname = _first(com, "name", "code") or f"Ware {cid}"
        buys = slot["buy"]
        sells = slot["sell"]
        if o:
            buys = [b for b in buys if o in _haystack(b)]
        if d:
            sells = [s for s in sells if d in _haystack(s)]
        if q and q not in norm(cname):
            # Suchtext darf auch ein Terminal/Ziel sein
            buys_q = [b for b in buys if q in _haystack(b)]
            sells_q = [s for s in sells if q in _haystack(s)]
            if not buys_q and not sells_q:
                continue
            buys = buys_q or buys
            sells = sells_q or sells
        # Begrenzen, damit das Kreuzprodukt klein bleibt: die besten Kauf-/Verkaufsorte genügen
        buys = sorted(buys, key=lambda r: num(r["price_buy"]))[:8]
        sells = sorted(sells, key=lambda r: -num(r["price_sell"]))[:8]
        for b in buys:
            for s in sells:
                if b is s or b.get("id_terminal") == s.get("id_terminal"):
                    continue
                buy_p, sell_p = num(b["price_buy"]), num(s["price_sell"])
                per_scu = sell_p - buy_p
                if per_scu <= 0:
                    continue
                units = cargo
                if budget > 0:
                    units = min(units, int(budget // buy_p))
                stock = num(b.get("scu_buy") or b.get("scu_sell_stock"))
                if stock > 0:
                    units = min(units, int(stock))
                if units <= 0:
                    continue
                demand = num(s.get("scu_sell"))
                sys_a, sys_b = _first(b, "star_system_name"), _first(s, "star_system_name")
                routes.append(
                    {
                        "commodity": cname,
                        "from": terminal_label(b),
                        "to": terminal_label(s),
                        "fromSystem": sys_a,
                        "toSystem": sys_b,
                        "buy": round(buy_p, 2),
                        "sell": round(sell_p, 2),
                        "profitPerScu": round(per_scu, 2),
                        "units": units,
                        "profit": round(per_scu * units),
                        "stockScu": stock or None,
                        "demandScu": demand or None,
                        "illegal": bool(com.get("is_illegal")),
                        "crossSystem": bool(sys_a and sys_b and norm(sys_a) != norm(sys_b)),
                    }
                )
    routes.sort(key=lambda r: (-r["profit"], -r["profitPerScu"]))
    return routes[: max(1, min(int(limit or 30), 100))]


def build_market_board(commodities: List[dict], prices: List[dict], limit: int = 12) -> List[dict]:
    """Marktübersicht: je Ware die beste Marge zwischen günstigstem Kauf- und teuerstem Verkaufsort."""
    cidx = index_commodities(commodities)
    best_buy: Dict[int, dict] = {}
    best_sell: Dict[int, dict] = {}
    for p in prices:
        try:
            cid = int(p.get("id_commodity"))
        except (TypeError, ValueError):
            continue
        if cid not in cidx or cidx[cid].get("is_illegal"):
            continue
        pb, ps = num(p.get("price_buy")), num(p.get("price_sell"))
        if pb > 0 and (cid not in best_buy or pb < num(best_buy[cid]["price_buy"])):
            best_buy[cid] = p
        if ps > 0 and (cid not in best_sell or ps > num(best_sell[cid]["price_sell"])):
            best_sell[cid] = p
    rows = []
    for cid, s in best_sell.items():
        b = best_buy.get(cid)
        if not b or b.get("id_terminal") == s.get("id_terminal"):
            continue
        margin = num(s["price_sell"]) - num(b["price_buy"])
        if margin <= 0:
            continue
        com = cidx[cid]
        rows.append(
            {
                "name": _first(com, "name", "code"),
                "category": (_first(com, "kind") or "Ware").upper(),
                "price": round(num(s["price_sell"]), 2),
                "marginPerScu": round(margin, 2),
                "location": f"Kauf: {terminal_label(b)} → Verkauf: {terminal_label(s)}"[:118],
            }
        )
    rows.sort(key=lambda r: -r["marginPerScu"])
    return rows[: max(1, limit)]


def build_ores(commodities: List[dict]) -> List[dict]:
    """Erz-Voreinstellungen für den Mining-Rechner (Rohpreis, raffinierter Preis, Aufschlag)."""
    by_name = {norm(c.get("name")): c for c in commodities if isinstance(c, dict)}
    out = []
    for c in commodities:
        if not isinstance(c, dict) or not (c.get("is_raw") or c.get("is_extractable")):
            continue
        name = _first(c, "name")
        if not name:
            continue
        base = re.sub(r"\s*\(raw\)\s*$", "", name, flags=re.I).strip()
        refined = by_name.get(norm(base)) if norm(base) != norm(name) else None
        raw_sell = num(c.get("price_sell"))
        ref_sell = num(refined.get("price_sell")) if refined else 0.0
        item = {"name": base, "rawSell": round(raw_sell, 2) if raw_sell else None}
        if ref_sell and raw_sell:
            item["refinedSell"] = round(ref_sell, 2)
            item["refineUplift"] = round((ref_sell / raw_sell - 1) * 100, 1)
        if item["rawSell"] or "refinedSell" in item:
            out.append(item)
    out.sort(key=lambda x: x["name"].lower())
    return out


def build_locations(groups: Dict[str, List[dict]]) -> List[dict]:
    """Führt UEX-Listen (Systeme, Planeten, Monde, Stationen, Städte, ...) zu einer Ortsliste zusammen."""
    type_names = {
        "star_systems": "Sternensystem", "planets": "Planet", "moons": "Mond", "space_stations": "Raumstation",
        "cities": "Stadt", "outposts": "Außenposten", "poi": "Point of Interest",
    }
    rows, seen = [], set()
    for key, label in type_names.items():
        for r in groups.get(key, []):
            if not isinstance(r, dict):
                continue
            name = _first(r, "name", "nickname")
            if not name or r.get("is_available") in (0, "0") and key in ("planets", "moons", "star_systems"):
                continue
            system = name if key == "star_systems" else _first(r, "star_system_name")
            parent = "" if key == "star_systems" else _first(r, "city_name", "moon_name", "planet_name", "orbit_name")
            ident = (key, norm(name), norm(system))
            if ident in seen:
                continue
            seen.add(ident)
            rows.append({"name": name, "type": r.get("subtype") or label if key == "poi" else label,
                         "system": system, "parentName": parent})
    order = {v: i for i, v in enumerate(type_names.values())}
    rows.sort(key=lambda x: (order.get(x["type"], 99), x["name"].lower()))
    return rows


def search_locations(rows: List[dict], q: str = "", system: str = "", limit: int = 60) -> List[dict]:
    qn, sn = norm(q), norm(system)
    out = []
    for r in rows:
        if sn and norm(r.get("system")) != sn:
            continue
        if qn and qn not in norm(r["name"]) and qn not in norm(r.get("parentName")) and qn not in norm(r.get("type")):
            continue
        out.append(r)
    if qn:  # Treffer, die mit dem Suchtext beginnen, zuerst
        out.sort(key=lambda r: (not norm(r["name"]).startswith(qn), len(r["name"])))
    return out[:limit]


# Redaktioneller Katalog: UEX und das Wiki liefern keine Live-Vertragslisten. Statt Aufträge oder
# Belohnungen zu erfinden, beschreibt dieser Katalog die Auftragstypen des Spiels ohne Zahlen.
MISSION_CATALOG: List[dict] = [
    {"title": "Kopfgeld & Bounty Hunting", "category": "KAMPF", "system": "Stanton · Pyro", "role": "Risiko: mittel bis hoch",
     "description": "Gesuchte Ziele aufspüren und ausschalten oder stellen. Schiffe mit starker Bewaffnung und gutem Schild sind sinnvoll. Aufträge findest du im Mission Manager (Spielmenü)."},
    {"title": "Frachtaufträge (Cargo Hauling)", "category": "LOGISTIK", "system": "Stanton · Pyro · Nyx", "role": "Risiko: niedrig bis mittel",
     "description": "Fracht zwischen Stationen und Außenposten transportieren. Kapazität, Ladeweg und Rückroute vorher planen; die Handelsroute im Handelsbereich hilft beim Vergleich."},
    {"title": "Paketlieferung (Delivery)", "category": "LOGISTIK", "system": "Stanton", "role": "Risiko: niedrig",
     "description": "Kurze Lieferaufträge, gut für den Einstieg: Paket abholen, am Ziel abgeben. Ideal, um Navigation und Quantum Travel zu üben."},
    {"title": "Mining-Einsätze", "category": "INDUSTRIE", "system": "Stanton · Pyro", "role": "Risiko: mittel",
     "description": "Erz abbauen und raffinieren. Ertrag hängt von Ausbeute, Raffineriegebühr und Transport ab. Der Mining-Rechner unter Tools zeigt eine Schätzung."},
    {"title": "Salvage & Bergung", "category": "INDUSTRIE", "system": "Stanton · Pyro", "role": "Risiko: mittel",
     "description": "Wracks zerlegen und Material verkaufen. Lagerkapazität und sicherer Rückweg zählen mehr als die größte Ladung."},
    {"title": "Medizinische Rettung", "category": "SUPPORT", "system": "Stanton", "role": "Risiko: mittel",
     "description": "Verletzte Spieler oder NPCs bergen und zu einer Klinik bringen. Ein Schiff mit Medbett und eine Crew mit Medic machen den Unterschied."},
    {"title": "Ermittlung & Aufklärung", "category": "ERKUNDUNG", "system": "Stanton · Pyro · Nyx", "role": "Risiko: niedrig bis mittel",
     "description": "Orte erkunden, Spuren sammeln, Objekte finden. Treibstoff und Rückweg einplanen, abgelegene Orte sind oft gefährlich."},
    {"title": "Eskorte & Sicherung", "category": "KAMPF", "system": "Stanton · Pyro", "role": "Risiko: hoch",
     "description": "Frachter oder Gruppen begleiten und Angriffe abwehren. Funktioniert am besten mit abgestimmter Crew; nutze dafür das Crew Board."},
    {"title": "Gemeinsame Community-Flüge", "category": "COMMUNITY", "system": "alle Systeme", "role": "Risiko: je nach Event",
     "description": "Konvois, Mining-Sessions oder Trainingsflüge mit anderen Piloten. Events legst du im Event-Kalender der Community an."},
]


def fetch_external_missions_url() -> str:
    return os.getenv("MISSIONS_FEED_URL", "").strip()


# ----------------------------------------------------------------------------------------
# Dienst mit Netzwerkzugriff
# ----------------------------------------------------------------------------------------


class HubService:
    """Holt UEX-Daten (mit Cache) und stellt sie den Routen bereit."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("UEX_API_KEY", "").strip() or None
        self.cache = TtlCache()

    # -- Rohdaten ----------------------------------------------------------------------

    async def _market_raw(self) -> dict:
        async with aiohttp.ClientSession() as session:
            commodities, prices = await asyncio.gather(
                _get_json(session, "commodities", self.api_key),
                _get_json(session, "commodities_prices_all", self.api_key),
            )
        if not prices:
            raise UexApiError("UEX lieferte keine Marktpreise.")
        log.info(
            "Marktdaten geladen: %d Waren, %d Preiseinträge (Felder: %s) -> %d Marktkarten, %d Routen möglich.",
            len(commodities), len(prices), ", ".join(sorted(prices[0].keys())[:14]) if isinstance(prices[0], dict) else "?",
            len(build_market_board(commodities, prices)), len(compute_routes(commodities, prices, illegal="all", limit=100)),
        )
        return {"commodities": commodities, "prices": prices, "fetched_at": time.time()}

    async def market(self):
        return await self.cache.get("market", MARKET_TTL, self._market_raw)

    async def _locations_raw(self) -> List[dict]:
        endpoints = ["star_systems", "planets", "moons", "space_stations", "cities", "outposts", "poi"]
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                *[_get_json(session, e, self.api_key) for e in endpoints], return_exceptions=True
            )
        groups: Dict[str, List[dict]] = {}
        for name, res in zip(endpoints, results):
            if isinstance(res, Exception):
                log.warning("UEX /%s nicht verfügbar: %s", name, res)
                continue
            groups[name] = res
        rows = build_locations(groups)
        if not rows:
            raise UexApiError("UEX lieferte keine Orte.")
        return rows

    async def locations(self):
        return await self.cache.get("locations", LOCATIONS_TTL, self._locations_raw)

    async def _news_raw(self) -> Any:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        last: Optional[Exception] = None
        async with aiohttp.ClientSession() as session:
            for url in NEWS_URLS:
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            raise UexApiError(f"Wiki-API antwortete mit HTTP {resp.status}")
                        data = await resp.json(content_type=None)
                    items = data.get("data") if isinstance(data, dict) else data
                    if isinstance(items, list) and items:
                        return {"data": items[:10]}
                    raise UexApiError("Wiki-API lieferte keine Meldungen.")
                except Exception as exc:  # noqa: BLE001
                    last = exc
        raise last or UexApiError("News-Quelle nicht erreichbar.")

    async def news(self):
        return await self.cache.get("news", NEWS_TTL, self._news_raw)

    async def _external_missions(self) -> List[dict]:
        url = fetch_external_missions_url()
        if not url.startswith("https://"):
            return []
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise UexApiError(f"Missions-Feed antwortete mit HTTP {resp.status}")
                data = await resp.json(content_type=None)
        items = data.get("data") if isinstance(data, dict) else data
        out = []
        for x in items if isinstance(items, list) else []:
            if isinstance(x, dict) and (x.get("title") or x.get("name")):
                out.append({k: x[k] for k in ("title", "name", "category", "system", "role", "description", "summary", "url")
                            if k in x and isinstance(x[k], str)})
        return out[:30]

    async def missions(self) -> dict:
        external: List[dict] = []
        if fetch_external_missions_url():
            try:
                external, _ = await self.cache.get("missions_ext", 600, self._external_missions)
            except Exception:  # noqa: BLE001
                external = []
        if external:
            return {"data": external, "source": "extern", "source_label": "externer Missions-Feed"}
        return {
            "data": MISSION_CATALOG,
            "source": "redaktionell",
            "source_label": "redaktioneller Auftragskatalog (keine Live-Vertragsliste)",
        }


service = HubService()

# ----------------------------------------------------------------------------------------
# HTTP-Routen  (/api/v1/...)
# ----------------------------------------------------------------------------------------

hub_routes = web.RouteTableDef()


def _json(data: Any, *, stale: bool = False, status: int = 200) -> web.Response:
    if isinstance(data, dict):
        data = {**data, "stale": stale}
    return web.json_response(data, status=status, dumps=lambda o: __import__("json").dumps(o, ensure_ascii=False))


def _unavailable(what: str) -> web.Response:
    return web.json_response(
        {"detail": f"{what} ist gerade nicht erreichbar. Bitte später erneut versuchen."}, status=503
    )


def _int_param(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(request.query.get(name, default)))))
    except (TypeError, ValueError):
        return default


@hub_routes.get("/api/v1/trade/market")
async def api_market(request: web.Request) -> web.Response:
    try:
        raw, stale = await service.market()
    except Exception:  # noqa: BLE001
        return _unavailable("Die Marktdatenquelle")
    rows = build_market_board(raw["commodities"], raw["prices"])
    return _json({"data": rows, "updated": int(raw["fetched_at"]), "source": "UEX Corp"}, stale=stale)


@hub_routes.get("/api/v1/trade/trader-routes")
async def api_routes(request: web.Request) -> web.Response:
    try:
        raw, stale = await service.market()
    except Exception:  # noqa: BLE001
        return _unavailable("Die Marktdatenquelle")
    q = request.query
    illegal = q.get("illegal", "all")
    rows = compute_routes(
        raw["commodities"], raw["prices"],
        cargo=_int_param(request, "cargo", 32, 1, 100000),
        budget=_int_param(request, "budget", 0, 0, 10**10),
        origin=q.get("origin", "")[:80], dest=q.get("dest", "")[:80], query=q.get("q", "")[:80],
        illegal=illegal if illegal in ("hide", "only", "all") else "all",
        limit=_int_param(request, "limit", 30, 1, 100),
    )
    return _json({"data": rows, "updated": int(raw["fetched_at"]), "source": "UEX Corp"}, stale=stale)


@hub_routes.get("/api/v1/trade/routes")
async def api_routes_legacy(request: web.Request) -> web.Response:
    return await api_routes(request)


@hub_routes.get("/api/v1/locations")
async def api_locations(request: web.Request) -> web.Response:
    try:
        rows, stale = await service.locations()
    except Exception:  # noqa: BLE001
        return _unavailable("Die Standortdatenquelle")
    found = search_locations(rows, request.query.get("q", "")[:80], request.query.get("system", "")[:40])
    return _json({"data": found, "source": "UEX Corp", "has_coordinates": False}, stale=stale)


@hub_routes.get("/api/v1/locations/systems")
async def api_systems(request: web.Request) -> web.Response:
    try:
        rows, stale = await service.locations()
    except Exception:  # noqa: BLE001
        return _unavailable("Die Standortdatenquelle")
    counts: Dict[str, int] = {}
    for r in rows:
        if r["system"]:
            counts[r["system"]] = counts.get(r["system"], 0) + 1
    data = [{"name": n, "description": f"{c} Orte in der Community-Datenbank"}
            for n, c in sorted(counts.items(), key=lambda kv: -kv[1])]
    return _json({"data": data}, stale=stale)


@hub_routes.get("/api/v1/mining/ores")
async def api_ores(request: web.Request) -> web.Response:
    try:
        raw, stale = await service.market()
    except Exception:  # noqa: BLE001
        return _unavailable("Die Erzdatenquelle")
    return _json({"data": build_ores(raw["commodities"]), "source": "UEX Corp"}, stale=stale)


@hub_routes.get("/api/v1/missions")
async def api_missions(request: web.Request) -> web.Response:
    return _json(await service.missions())


@hub_routes.get("/api/v1/news")
async def api_news(request: web.Request) -> web.Response:
    try:
        data, stale = await service.news()
    except Exception:  # noqa: BLE001
        return _unavailable("Der Comm-Link-Feed")
    return _json(data, stale=stale)


@hub_routes.get("/api/v1/ships/{ship_id}")
async def api_ship(request: web.Request) -> web.Response:
    import webapp  # späte Importe vermeiden einen Zirkelbezug

    wanted = request.match_info["ship_id"]
    for s in webapp.cache._ships:  # noqa: SLF001 - gleicher Prozess, fertig serialisierter Stand
        if str(s.get("id")) == wanted:
            return _json({"data": s, "loadout_available": False})
    return web.json_response({"detail": "Schiff nicht gefunden."}, status=404)
