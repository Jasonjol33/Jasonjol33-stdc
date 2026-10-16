"""Baut die Discord-Embeds für den Schiffs-Showcase.

Discord erlaubt maximal 4096 Zeichen pro Embed-Description. Damit auch
Hersteller mit sehr vielen Schiffs-Varianten (z.B. RSI, Aegis, Anvil) sauber
dargestellt werden, wird die Zeilenliste bei Bedarf automatisch auf mehrere
Embeds ("Teil 1/2", "Teil 2/2", ...) aufgeteilt.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import discord

from uex_api import Ship

MAX_DESCRIPTION = 3800  # Sicherheitsabstand zum Discord-Limit von 4096 Zeichen
EMBED_COLOR = 0x2E86FF


def format_auec(amount: float) -> str:
    """Formatiert einen aUEC-Betrag im deutschen Zahlenformat, z.B. 1234567 -> '1.234.567'."""
    return f"{amount:,.0f}".replace(",", ".")


def ship_line(ship: Ship) -> str:
    cheapest = ship.cheapest_price
    if cheapest is None:
        tag = " *(Concept)*" if ship.is_concept else ""
        return f"**{ship.name}**{tag} — nicht im Spiel kaufbar (nur Pledge)"

    unique_terminals = sorted({p.terminal_name for p in ship.prices})
    extra_locations = len(unique_terminals) - 1
    extra = f" _(+{extra_locations} weitere Orte)_" if extra_locations > 0 else ""
    return f"**{ship.name}** — {format_auec(cheapest.price_buy)} aUEC bei {cheapest.terminal_name}{extra}"


def chunk_lines(lines: List[str], max_len: int = MAX_DESCRIPTION) -> List[str]:
    """Teilt eine Zeilenliste in mehrere Textblöcke auf, die jeweils unter max_len Zeichen bleiben."""
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1  # +1 für den Zeilenumbruch
        if current and current_len + line_len > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or ["_Keine Schiffe in dieser Kategorie._"]


def build_manufacturer_embeds(manufacturer: str, ships: List[Ship]) -> List[discord.Embed]:
    ships_sorted = sorted(ships, key=lambda s: s.name.lower())
    lines = [ship_line(s) for s in ships_sorted]
    chunks = chunk_lines(lines)

    embeds = []
    for i, chunk in enumerate(chunks, start=1):
        title = f"🏭 {manufacturer}" if len(chunks) == 1 else f"🏭 {manufacturer} ({i}/{len(chunks)})"
        embed = discord.Embed(title=title, description=chunk, color=EMBED_COLOR)
        embeds.append(embed)
    return embeds


LIST_PAGE_SIZE = 15  # Schiffe pro Seite bei /liste


def build_list_pages(ships: List[Ship]) -> List[discord.Embed]:
    """Baut die Seiten für /liste: alle Schiffe sortiert nach Hersteller + Name."""
    ships_sorted = sorted(ships, key=lambda s: (s.manufacturer.lower(), s.name.lower()))
    lines = [f"🏭 *{s.manufacturer}* — {ship_line(s)}" for s in ships_sorted]

    pages: List[List[str]] = [
        lines[i : i + LIST_PAGE_SIZE] for i in range(0, len(lines), LIST_PAGE_SIZE)
    ] or [["_Keine Schiffe gefunden._"]]

    total = len(pages)
    embeds = []
    for i, page_lines in enumerate(pages, start=1):
        embed = discord.Embed(
            title=f"📋 Alle Schiffe — In-Game-Preise ({i}/{total})",
            description="\n".join(page_lines),
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Seite {i} von {total} · {len(ships_sorted)} Schiffe insgesamt")
        embeds.append(embed)
    return embeds


def build_header_embed(
    total_ships: int,
    ingame_count: int,
    last_updated: dt.datetime,
    changes: List[str],
    web_url: Optional[str] = None,
) -> discord.Embed:
    description = (
        f"**{total_ships}** Schiffe insgesamt, davon **{ingame_count}** aktuell "
        f"in-game käuflich.\n"
        f"Alle Preise werden automatisch aktualisiert, sobald sich etwas ändert."
    )
    if web_url:
        description += f"\n🌐 [Alle Schiffe mit Bildern, Suche und Filtern im Browser]({web_url})"
    embed = discord.Embed(
        title="🚀 Star Citizen Schiffs-Showcase — In-Game-Preise (aUEC)",
        description=description,
        color=EMBED_COLOR,
    )
    if changes:
        change_text = "\n".join(changes[:15])
        if len(changes) > 15:
            change_text += f"\n… und {len(changes) - 15} weitere Änderung(en)"
        embed.add_field(name="🔄 Änderungen seit dem letzten Check", value=change_text, inline=False)
    embed.set_footer(text="Datenquelle: UEX Corp (uexcorp.space, community-gepflegt) · Zuletzt geprüft")
    embed.timestamp = last_updated
    return embed


def build_web_embed(web_url: str) -> discord.Embed:
    """Embed für /web: klickbarer Link zur Web-Seite mit Bildern, Suche und Filtern."""
    embed = discord.Embed(
        title="🌐 Star Citizen Schiffs-Showcase",
        url=web_url,  # der Titel ist selbst der Link
        description=(
            f"[Star Citizen Schiffs-Showcase]({web_url})\n\n"
            "Alle Schiffe mit **Bildern**, **Suche** und **Filtern** - "
            "mit denselben aktuellen In-Game-Preisen wie hier im Channel."
        ),
        color=EMBED_COLOR,
    )
    embed.set_footer(text="Datenquelle: UEX Corp (uexcorp.space, community-gepflegt)")
    return embed
