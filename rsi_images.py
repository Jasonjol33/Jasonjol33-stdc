"""Ergänzt Schiffsbilder von der offiziellen RSI-Pledge-Store-Seite (robertsspaceindustries.com).

Für RSI gibt es keine offizielle, öffentliche Schiffs-API. UEX liefert aber zu jedem Schiff die
Adresse der Pledge-Store-Seite (`store_url`). Dieses Modul liest dort das Vorschaubild aus den
Meta-Tags (`og:image`) und merkt es sich dauerhaft in `data/rsi_images.json`.

Bewusst zurückhaltend gegenüber der RSI-Seite:
  - nur Seiten von robertsspaceindustries.com werden abgerufen (kein beliebiges Ziel aus UEX-Daten),
  - streng nacheinander mit Pause, pro Durchlauf höchstens MAX_PAGES_PER_RUN Seiten,
  - jede Seite wird nur EINMAL abgerufen; ein Treffer bleibt dauerhaft gespeichert,
    ein Fehlschlag wird erst nach RETRY_NEGATIVE_AFTER erneut versucht,
  - bei HTTP 403/429 (Blockade/Limit) bricht der Durchlauf sofort ab,
  - die Bilder selbst lädt der Browser der Besucher direkt von RSI; der Bot lädt keine Bilder.

Modi (Umgebungsvariable RSI_IMAGES): "missing" (Standard: nur Lücken füllen, bei vorhandenem
UEX-Bild dient das RSI-Bild als Ersatz), "all" (RSI-Bild bevorzugen), "off".
Fehlt ein Bild oder lädt es nicht, bleibt die Seite wie bisher - es geht nie etwas kaputt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Dict, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from storage import load_rsi_images, save_rsi_images
from uex_api import Ship

log = logging.getLogger("rsi_images")

RSI_DOMAIN = "robertsspaceindustries.com"
USER_AGENT = "StarCitizenShowcaseBot/1.0 (Fan-Projekt, Discord-Showcase; siehe README)"
REQUEST_DELAY = 2.0  # Sekunden Pause zwischen zwei Seitenabrufen
MAX_PAGES_PER_RUN = 40  # pro Update-Zyklus; der Rest folgt im nächsten Zyklus
RETRY_NEGATIVE_AFTER = timedelta(days=7)
MAX_HTML_BYTES = 512 * 1024  # die Meta-Tags stehen im <head>; mehr muss nie gelesen werden
# Taucht dasselbe Bild bei so vielen verschiedenen Schiffen auf, ist es ein Allerwelts-Bild
# (z.B. Seiten-Logo) und kein Schiffsfoto - dann wird es nicht verwendet.
GENERIC_IMAGE_THRESHOLD = 4

_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def is_rsi_url(url: Optional[str]) -> bool:
    """True nur für https-Adressen auf robertsspaceindustries.com (inkl. Subdomains wie media.)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return parsed.scheme == "https" and (host == RSI_DOMAIN or host.endswith("." + RSI_DOMAIN))


def extract_image_url(html: str, base_url: str) -> Optional[str]:
    """Liest das Vorschaubild (og:image / twitter:image) aus dem HTML einer Store-Seite."""
    found: Dict[str, str] = {}
    for tag in _META_RE.findall(html):
        attrs = {
            m.group(1).lower(): unescape(m.group(2) if m.group(2) is not None else m.group(3))
            for m in _ATTR_RE.finditer(tag)
        }
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = (attrs.get("content") or "").strip()
        if key in ("og:image:secure_url", "og:image", "twitter:image") and content and key not in found:
            found[key] = content
    for key in ("og:image:secure_url", "og:image", "twitter:image"):
        raw = found.get(key)
        if raw:
            url = urljoin(base_url, raw)
            if is_rsi_url(url):  # nur RSI-Adressen: landet später in <img src>
                return url
    return None


def apply_cached(ships: Dict[int, Ship], mode: str) -> None:
    """Trägt bereits bekannte RSI-Bilder in die Schiffe ein (schnell, nur aus der lokalen Datei)."""
    if mode == "off":
        return
    cache = load_rsi_images()
    counts = Counter(e.get("image") for e in cache.values() if e.get("image"))
    for ship in ships.values():
        entry = cache.get(ship.store_url or "")
        rsi = entry.get("image") if entry else None
        if not rsi or not is_rsi_url(rsi) or counts[rsi] >= GENERIC_IMAGE_THRESHOLD:
            continue
        if rsi in (ship.image_url, ship.image_url_alt):
            continue  # schon eingetragen (z.B. zweiter Aufruf auf denselben Objekten)
        uex = ship.image_url
        if mode == "all" or not uex:
            ship.image_url, ship.image_url_alt = rsi, uex
        else:
            ship.image_url_alt = rsi  # UEX-Bild bleibt; RSI springt ein, falls es nicht lädt


async def _fetch_page(session: aiohttp.ClientSession, url: str) -> tuple[str, Optional[str]]:
    """Gibt (Status, HTML) zurück. Status: ok | not_found | blocked | error."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=20),
            max_redirects=3,
        ) as resp:
            if resp.status in (404, 410):
                return "not_found", None
            if resp.status in (403, 429):
                return "blocked", None
            if resp.status != 200:
                return "error", None
            if not is_rsi_url(str(resp.url)):  # Weiterleitung auf eine fremde Seite
                return "error", None
            data = bytearray()
            async for chunk in resp.content.iter_chunked(16384):
                data.extend(chunk)
                if len(data) >= MAX_HTML_BYTES:
                    break
            return "ok", bytes(data).decode(resp.charset or "utf-8", errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "error", None


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


async def enrich(ships: Dict[int, Ship], mode: str) -> int:
    """Ruft fehlende Store-Seiten ab und speichert die Bilder. Gibt die Zahl neu gefundener Bilder zurück."""
    if mode == "off":
        return 0
    cache = load_rsi_images()
    now = datetime.now(timezone.utc)

    todo: list[str] = []
    for ship in ships.values():
        url = ship.store_url
        if not url or not is_rsi_url(url) or url in todo:
            continue
        if mode == "missing" and ship.image_url:
            continue
        entry = cache.get(url)
        if entry:
            if entry.get("image"):
                continue
            checked = _parse_ts(entry.get("checked"))
            if checked and now - checked < RETRY_NEGATIVE_AFTER:
                continue
        todo.append(url)
    todo = todo[:MAX_PAGES_PER_RUN]
    if not todo:
        return 0

    found = no_image = 0
    async with aiohttp.ClientSession() as session:
        for i, url in enumerate(todo):
            if i:
                await asyncio.sleep(REQUEST_DELAY)
            status, html = await _fetch_page(session, url)
            if status == "blocked":
                log.warning("RSI antwortet mit 403/429 - breche die Bildsuche für diesen Durchlauf ab.")
                break
            if status == "error":
                continue  # vorübergehend (Netz, 5xx): nicht als Fehlschlag merken, später erneut
            image = extract_image_url(html, url) if status == "ok" else None
            cache[url] = {"image": image, "checked": now.isoformat()}
            if image:
                found += 1
            else:
                no_image += 1
    save_rsi_images(cache)
    log.info("RSI-Bilder: %d Seiten geprüft, %d Bilder gefunden, %d ohne Bild.", found + no_image, found, no_image)
    if no_image and not found:
        log.warning("Keine einzige RSI-Seite lieferte ein Bild (og:image fehlt?) - Seitenaufbau evtl. geändert.")
    return found
