"""Füllt Datenlücken (SCU, Crew, Pad, Bild, Store-Link) aus dem Star Citizen Wiki (api.star-citizen.wiki).

UEX bleibt die Hauptquelle. Fehlt dort bei einem Schiff ein Wert, wird er hier ergänzt -
vorhandene UEX-Werte werden NIE überschrieben. Die Wiki-Liste wird 24 h lokal zwischengespeichert
(data/wiki_vehicles.json); schlägt der Abruf fehl, wird der letzte Stand benutzt. Es wirft nie
eine Exception nach außen: im schlimmsten Fall bleibt alles wie vorher.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import aiohttp

from storage import DATA_DIR, _load_json, _save_json
from uex_api import Ship

log = logging.getLogger("wiki_data")

WIKI_URL = "https://api.star-citizen.wiki/api/v2/vehicles"
USER_AGENT = "StarCitizenShowcaseBot/1.0 (Fan-Projekt; siehe README)"
CACHE_FILE = DATA_DIR / "wiki_vehicles.json"
CACHE_MAX_AGE = timedelta(hours=24)
PAGE_SIZE = 100
MAX_PAGES = 15

_SIZE_TO_PAD = {"vehicle": "", "snub": "XS", "small": "S", "medium": "M", "large": "L", "capital": "XL"}


def norm(name: Optional[str]) -> str:
    """Vergleichsschlüssel: klein, ohne Sonderzeichen/Leerzeichen."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _pick(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _crew_text(v) -> str:
    if isinstance(v, dict):
        lo, hi = int(_num(_pick(v, "min", "min_crew"))), int(_num(_pick(v, "max", "max_crew")))
        if hi and lo and lo != hi:
            return f"{lo}-{hi}"
        return str(hi or lo) if (hi or lo) else ""
    n = int(_num(v))
    return str(n) if n else ""


def _image(v: dict) -> Optional[str]:
    img = _pick(v, "image", "thumbnail", "image_url", "media")
    if isinstance(img, list) and img:
        img = img[0]
    if isinstance(img, dict):
        img = _pick(img, "source_url", "url", "src", "original")
    return img if isinstance(img, str) and img.startswith("https://") else None


def simplify(v: dict) -> dict:
    """Ein Wiki-Eintrag -> nur die Felder, die wir brauchen (tolerant gegenüber Schema-Varianten)."""
    size = _pick(v, "size_class", "size")
    if isinstance(size, dict):
        size = _pick(size, "label", "name", "en_EN")
    return {
        "scu": _num(_pick(v, "cargo_capacity", "cargo", "scu")),
        "crew": _crew_text(_pick(v, "crew", "crew_size")),
        "pad": _SIZE_TO_PAD.get(str(size or "").strip().lower(), ""),
        "image": _image(v),
        "store_url": _pick(v, "pledge_url", "store_url", "url_store"),
    }


def _names(v: dict) -> list[str]:
    out = [v.get("name"), v.get("slug"), v.get("name_full")]
    mf = v.get("manufacturer")
    if isinstance(mf, dict) and v.get("name"):
        out.append(f"{mf.get('name', '')} {v['name']}")
    return [n for n in out if n]


async def _download() -> Dict[str, dict]:
    index: Dict[str, dict] = {}
    async with aiohttp.ClientSession() as session:
        for page in range(1, MAX_PAGES + 1):
            async with session.get(
                WIKI_URL,
                params={"page": page, "page[size]": PAGE_SIZE, "limit": PAGE_SIZE},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                payload = await resp.json(content_type=None)
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not rows:
                break
            for v in rows:
                if isinstance(v, dict):
                    entry = simplify(v)
                    for n in _names(v):
                        index.setdefault(norm(n), entry)
            meta = payload.get("meta") or {} if isinstance(payload, dict) else {}
            last = meta.get("last_page")
            if (last and page >= int(last)) or len(rows) < PAGE_SIZE and not last:
                break
            await asyncio.sleep(0.5)
    return index


async def load_index() -> Dict[str, dict]:
    cache = _load_json(CACHE_FILE)
    try:
        ts = datetime.fromisoformat(cache.get("fetched", ""))
    except ValueError:
        ts = None
    if ts and cache.get("index") and datetime.now(timezone.utc) - ts < CACHE_MAX_AGE:
        return cache["index"]
    try:
        index = await _download()
        if index:
            _save_json(CACHE_FILE, {"fetched": datetime.now(timezone.utc).isoformat(), "index": index})
            log.info("Wiki-Daten geladen: %d Namen.", len(index))
            return index
        log.warning("Wiki lieferte keine Fahrzeuge - nutze letzten Stand.")
    except Exception as exc:
        log.warning("Wiki-Abruf fehlgeschlagen (%s) - nutze letzten Stand.", exc or exc.__class__.__name__)
    return cache.get("index") or {}


def fill_gaps(ships: Dict[int, Ship], index: Dict[str, dict]) -> int:
    """Ergänzt fehlende Werte. Gibt die Zahl der Schiffe zurück, bei denen etwas ergänzt wurde."""
    changed = 0
    for s in ships.values():
        w = index.get(norm(s.name_full)) or index.get(norm(s.name))
        if not w:
            continue
        touched = False
        if not s.scu and w["scu"]:
            s.scu, touched = w["scu"], True
        if not s.crew and w["crew"]:
            s.crew, touched = w["crew"], True
        if not s.pad_type and w["pad"]:
            s.pad_type, touched = w["pad"], True
        if not s.store_url and w["store_url"]:
            s.store_url, touched = w["store_url"], True
        if not s.image_url and w["image"]:
            s.image_url, touched = w["image"], True
        elif not s.image_url_alt and w["image"] and w["image"] != s.image_url:
            s.image_url_alt, touched = w["image"], True
        changed += touched
    return changed


def report_gaps(ships: Dict[int, Ship]) -> None:
    """Loggt, bei welchen Schiffen nach allen Quellen noch etwas fehlt (nur fürs Log, nicht für Besucher)."""
    missing = [
        f"{s.name} ({', '.join(k for k, ok in (('SCU', s.scu), ('Crew', s.crew), ('Pad', s.pad_type), ('Bild', s.image_url)) if not ok)})"
        for s in ships.values()
        if not (s.scu and s.crew and s.pad_type and s.image_url)
    ]
    if missing:
        log.info("Nach allen Quellen noch lückenhaft: %d Schiffe, z.B. %s", len(missing), "; ".join(missing[:8]))
