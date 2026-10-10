"""Anbindung an die UEX Corp API (https://uexcorp.space) für Star-Citizen-Schiffsdaten.

Genutzte Endpunkte (Stand: UEX API 2.0, öffentlich einsehbar, kein Auth-Token nötig):
  - GET /vehicles                        -> Stammdaten aller Fahrzeuge/Schiffe
  - GET /vehicles_purchases_prices_all   -> alle In-Game-Kaufpreise (aUEC) an allen Terminals

Wichtig: UEX ist ein von der Community betriebenes, nicht-offizielles Projekt
("UEX is a fictional in-game entity and not affiliated with Cloud Imperium Games").
Die Daten können daher gelegentlich fehlerhaft oder leicht veraltet sein. Laut UEX
werden die In-Game-Kaufpreise ohnehin nur stündlich neu abgeglichen - häufigeres
Abfragen als das würde also keine "frischeren" Daten liefern.

Nicht jedes Schiff ist in-game (für aUEC) kaufbar - viele Concept-Schiffe und
Sonder-/Jubiläums-Varianten gibt es aktuell nur im Pledge-Store gegen Echtgeld.
Solche Schiffe tauchen hier mit einer leeren `prices`-Liste auf, statt einfach
weggelassen zu werden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp

API_BASE = "https://api.uexcorp.uk/2.0"
USER_AGENT = "StarCitizenShowcaseBot/1.0 (Discord-Showcase-Bot; siehe README)"

log = logging.getLogger("uex_api")


class UexApiError(RuntimeError):
    """Wird ausgelöst, wenn die UEX-API einen Fehler zurückgibt oder nicht erreichbar ist."""


@dataclass
class PriceEntry:
    terminal_name: str
    price_buy: float


@dataclass
class Ship:
    id: int
    name: str
    name_full: str
    manufacturer: str
    is_concept: bool
    pad_type: str
    scu: float
    crew: str
    image_url: Optional[str]
    store_url: Optional[str]
    prices: List[PriceEntry] = field(default_factory=list)

    @property
    def cheapest_price(self) -> Optional[PriceEntry]:
        if not self.prices:
            return None
        return min(self.prices, key=lambda p: p.price_buy)

    @property
    def available_ingame(self) -> bool:
        return bool(self.prices)


async def _get_json(session: aiohttp.ClientSession, path: str, api_key: Optional[str]) -> list:
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{API_BASE}/{path}"
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            raise UexApiError(f"UEX-API antwortete mit HTTP {resp.status} für /{path}")
        payload = await resp.json(content_type=None)
        if payload.get("status") != "ok":
            raise UexApiError(f"UEX-API meldete einen Fehler für /{path}: {payload.get('message')}")
        return payload["data"]


def parse_ships(
    vehicles_raw: list,
    prices_raw: list,
    include_ground_vehicles: bool = False,
) -> Dict[int, Ship]:
    """Reine Verarbeitungslogik, getrennt von den Netzwerkaufrufen - dadurch lässt
    sich diese Funktion einfach mit Beispieldaten testen, ohne die UEX-API oder
    Discord zu berühren.
    """
    ships: Dict[int, Ship] = {}
    for v in vehicles_raw:
        if not include_ground_vehicles and not v.get("is_spaceship"):
            continue
        vehicle_id = v["id"]
        ships[vehicle_id] = Ship(
            id=vehicle_id,
            name=v.get("name") or v.get("name_full") or f"Schiff #{vehicle_id}",
            name_full=v.get("name_full") or v.get("name") or f"Schiff #{vehicle_id}",
            manufacturer=v.get("company_name") or "Unbekannter Hersteller",
            is_concept=bool(v.get("is_concept")),
            pad_type=v.get("pad_type") or "",
            scu=v.get("scu") or 0,
            crew=v.get("crew") or "",
            image_url=v.get("url_photo") or None,
            store_url=v.get("url_store") or None,
        )

    skipped_no_match = 0
    for p in prices_raw:
        ship = ships.get(p["id_vehicle"])
        if ship is None:
            # z.B. ein Bodenfahrzeug, das wir bewusst ausgeschlossen haben
            skipped_no_match += 1
            continue
        ship.prices.append(
            PriceEntry(
                terminal_name=p.get("terminal_name") or "Unbekannter Ort",
                price_buy=float(p["price_buy"]),
            )
        )

    log.info(
        "%d Schiffe geladen, davon %d aktuell in-game kaufbar (%d Preiseinträge ohne Zuordnung übersprungen).",
        len(ships),
        sum(1 for s in ships.values() if s.available_ingame),
        skipped_no_match,
    )
    return ships


async def fetch_ships(
    api_key: Optional[str] = None,
    include_ground_vehicles: bool = False,
) -> Dict[int, Ship]:
    """Lädt alle Schiffe inkl. aktueller In-Game-Kaufpreise von UEX Corp.

    Gibt ein Dict {vehicle_id: Ship} zurück. Jedes Schiff enthält seine aktuellen
    In-Game-Kaufpreise (falls vorhanden) an allen Terminals, an denen es aktuell
    verkauft wird. Schiffe ohne Eintrag in `vehicles_purchases_prices_all` haben
    eine leere `prices`-Liste (= aktuell nicht in-game kaufbar).
    """
    async with aiohttp.ClientSession() as session:
        vehicles_raw = await _get_json(session, "vehicles", api_key)
        prices_raw = await _get_json(session, "vehicles_purchases_prices_all", api_key)

    return parse_ships(vehicles_raw, prices_raw, include_ground_vehicles)
