"""Discord-Bot: Live-Showcase aller Star-Citizen-Schiffe mit In-Game-Kaufpreisen (aUEC).

Datenquelle: UEX Corp API (https://uexcorp.space) - ein von der Community
betriebenes, nicht-offizielles Star-Citizen-Projekt.

Ablauf:
  1. Beim Start werden alle Schiffe + aktuelle In-Game-Preise geladen.
  2. Pro Hersteller wird eine (oder bei Bedarf mehrere) Embed-Nachricht(en) im
     konfigurierten Channel gepostet - bzw. bei einem Neustart wiederverwendet,
     da die Nachrichten-IDs lokal gespeichert werden.
  3. Ein Hintergrund-Task prüft alle X Minuten erneut die Preise (Standard: 60).
     Bei Änderungen wird NUR die betroffene Hersteller-Nachricht bearbeitet und
     im Kopf-Embed ein kurzes Änderungsprotokoll angezeigt - kein Spam, kein
     kompletter Neuaufbau des Showcase.
  4. Im selben Prozess läuft die Web-Seite (webapp.py): Sie zeigt denselben Datenstand mit
     Bildern, Suche und Filtern und wird nach jedem UEX-Abruf mit den frischen Daten
     versorgt - es gibt weiterhin nur einen Abruf. Abschaltbar mit WEB_ENABLED=false.

Starten mit: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import discord
from discord.ext import commands, tasks

from config import load_config
from embeds import build_header_embed, build_list_pages, build_manufacturer_embeds, build_web_embed, format_auec
from storage import load_last_prices, load_message_ids, save_last_prices, save_message_ids
from uex_api import Ship, UexApiError, fetch_ships
import rsi_images  # Schiffsbilder von der offiziellen RSI-Seite
import wiki_data  # Datenlücken (SCU, Crew, Pad, Bild) aus dem Star Citizen Wiki füllen
import webapp  # Web-Seite, läuft im selben Prozess wie der Bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# Ohne Discord-Zugangsdaten bricht der Import nicht mehr ab: dann läuft nur die Web-Seite (siehe main()).
config = load_config(require_discord=False)


class ShowcaseBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Läuft genau einmal pro Prozessstart, nach dem Login und vor der Gateway-Verbindung:
        # früher als on_ready und unabhängig davon, wie oft Discord die Verbindung neu aufbaut.
        await ensure_web_server()
        # Erster Datenstand für die Web-Seite, ohne auf das Discord-Gateway (on_ready) zu warten.
        asyncio.create_task(prefetch_web_data())


# Wir brauchen keine privilegierten Intents (kein Message Content, keine
# Member-Liste) - das hält die Einrichtung im Discord Developer Portal simpel.
intents = discord.Intents.default()
bot = ShowcaseBot(command_prefix="!sc-showcase-unused!", intents=intents)

HEADER_KEY = "__header__"

web_runner = None  # gesetzt, sobald die Web-Seite läuft; None = (noch) nicht gestartet
rsi_task: "asyncio.Task | None" = None  # läuft die RSI-Bildsuche gerade im Hintergrund?
latest_ships: Dict[int, Ship] = {}  # jüngster erfolgreicher Stand (verhindert veraltetes Neu-Veröffentlichen)


def group_by_manufacturer(ships: Dict[int, Ship]) -> Dict[str, List[Ship]]:
    groups: Dict[str, List[Ship]] = defaultdict(list)
    for ship in ships.values():
        groups[ship.manufacturer].append(ship)
    return dict(sorted(groups.items(), key=lambda kv: kv[0].lower()))


def compute_changes(ships: Dict[int, Ship], last_prices: Dict[str, float]) -> List[str]:
    """Vergleicht die aktuellen Preise mit dem letzten gespeicherten Stand."""
    changes = []
    for ship in ships.values():
        cheapest = ship.cheapest_price
        key = str(ship.id)
        old_price = last_prices.get(key)
        new_price = cheapest.price_buy if cheapest else None

        if old_price is None and new_price is not None:
            changes.append(f"🆕 **{ship.name}** ist jetzt in-game kaufbar: {format_auec(new_price)} aUEC")
        elif old_price is not None and new_price is None:
            changes.append(f"❌ **{ship.name}** ist nicht mehr in-game kaufbar")
        elif old_price is not None and new_price is not None and old_price != new_price:
            arrow = "🔺" if new_price > old_price else "🔻"
            changes.append(f"{arrow} **{ship.name}**: {format_auec(old_price)} → {format_auec(new_price)} aUEC")
    return changes


async def ensure_web_server() -> None:
    """Startet die Web-Seite (einmalig) im selben Event-Loop wie der Bot.

    Ein Fehler hier (z.B. Port belegt) darf den Bot nie stoppen: Der Discord-Showcase läuft
    dann einfach ohne Web-Seite weiter. Mehrfachaufrufe sind harmlos.
    """
    global web_runner
    if not config.web_enabled or web_runner is not None:
        return
    try:
        web_runner = await webapp.start_server(port=config.web_port)
    except Exception:
        log.exception(
            "Web-Seite konnte nicht gestartet werden (Port %d belegt?) - der Bot läuft ohne sie weiter.",
            config.web_port,
        )
        return
    log.info("Web-Seite läuft auf Port %d%s.", config.web_port, f" ({config.web_url})" if config.web_url else "")


async def _enrich_rsi_images(ships: Dict[int, Ship]) -> None:
    """Hintergrund: fehlende RSI-Bilder holen und die Web-Seite danach einmal aktualisieren."""
    try:
        found = await rsi_images.enrich(ships, config.rsi_images_mode)
        if found and ships is latest_ships:
            rsi_images.apply_cached(ships, config.rsi_images_mode)
            webapp.publish_ships(ships)
    except Exception:
        log.exception("RSI-Bildsuche fehlgeschlagen - die Seite läuft mit den vorhandenen Bildern weiter.")
    await _fill_gaps_from_wiki(ships)


async def _fill_gaps_from_wiki(ships: Dict[int, Ship]) -> None:
    """Hintergrund: Lücken aus dem Star Citizen Wiki füllen und die Seite danach aktualisieren."""
    try:
        index = await wiki_data.load_index()
        if wiki_data.fill_gaps(ships, index) and ships is latest_ships:
            webapp.publish_ships(ships)
        wiki_data.report_gaps(ships)
    except Exception:
        log.exception("Wiki-Abgleich fehlgeschlagen - die Seite läuft mit den UEX-Daten weiter.")


async def prefetch_web_data() -> None:
    """Holt den ersten Schiffsstand sofort nach dem Start, nur für die Web-Seite.

    Bisher kam der erste Stand erst in on_ready (nach der Gateway-Verbindung): Bei einer
    Discord-Verzögerung blieb die Seite bei "Verbindung wird hergestellt". Hat der normale
    Update-Zyklus schon Daten geliefert, wird dieses Ergebnis verworfen.
    """
    global latest_ships
    try:
        ships = await fetch_ships(
            api_key=config.uex_api_key,
            include_ground_vehicles=config.include_ground_vehicles,
        )
    except Exception as exc:  # on_ready/run_update_cycle meldet Fehler dann sichtbar für die Seite
        log.warning("Erster Web-Abruf bei UEX fehlgeschlagen (%s) - der Update-Zyklus versucht es erneut.", exc)
        return
    if not ships or webapp.cache.has_data:
        return
    latest_ships = ships
    rsi_images.apply_cached(ships, config.rsi_images_mode)
    webapp.publish_ships(ships)
    start_rsi_enrichment(ships)
    log.info("Web-Seite vorab mit %d Schiffen versorgt (ohne auf Discord zu warten).", len(ships))


def start_rsi_enrichment(ships: Dict[int, Ship]) -> None:
    global rsi_task
    if rsi_task is not None and not rsi_task.done():
        return
    rsi_task = asyncio.create_task(_enrich_rsi_images(ships))


async def get_channel() -> discord.abc.Messageable:
    channel = bot.get_channel(config.channel_id)
    if channel is None:
        channel = await bot.fetch_channel(config.channel_id)
    return channel


async def sync_manufacturer_message(
    channel: discord.abc.Messageable,
    key: str,
    embeds: List[discord.Embed],
    message_ids: Dict[str, int],
) -> None:
    """Postet neue Embeds oder bearbeitet vorhandene Nachrichten für eine Gruppe.

    Manufacturer mit vielen Schiffen können auf mehrere Embeds/Nachrichten
    aufgeteilt sein - dafür wird pro Chunk-Index ein eigener Storage-Key genutzt.
    """
    for idx, embed in enumerate(embeds):
        storage_key = f"{key}::{idx}"
        message_id = message_ids.get(storage_key)
        edited = False
        if message_id:
            try:
                msg = channel.get_partial_message(message_id)
                await msg.edit(embed=embed)
                edited = True
            except discord.NotFound:
                log.info("Nachricht für %s existiert nicht mehr, poste neu.", storage_key)
            except discord.HTTPException as exc:
                log.warning("Konnte Nachricht für %s nicht bearbeiten (%s), poste neu.", storage_key, exc)

        if not edited:
            new_msg = await channel.send(embed=embed)
            message_ids[storage_key] = new_msg.id

    # Überzählige alte Nachrichten entfernen, falls eine Gruppe jetzt weniger
    # Chunks braucht als beim letzten Durchlauf (z.B. weil Schiffe entfernt wurden).
    idx = len(embeds)
    while True:
        storage_key = f"{key}::{idx}"
        old_id = message_ids.pop(storage_key, None)
        if old_id is None:
            break
        try:
            await channel.get_partial_message(old_id).delete()
        except discord.HTTPException:
            pass
        idx += 1



async def sync_events_to_discord() -> None:
    """Aktualisiert die Event-/Teilnehmerübersicht als eine Nachricht im selben Channel wie das Schiffs-Showcase."""
    if not config.discord_enabled or not bot.is_ready():
        return
    events = webapp.load_events() if hasattr(webapp, "load_events") else []
    # Events nach Startzeit sortieren; vergangene Termine bleiben sichtbar, aber hinten.
    now = datetime.now(timezone.utc)
    def event_dt(e):
        raw = str(e.get("starts_at", ""))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (ValueError, TypeError):
            return datetime.max.replace(tzinfo=timezone.utc)
    events = sorted((e for e in events if isinstance(e, dict)), key=event_dt)[:5]
    embed = discord.Embed(
        title="📅 Event-Kalender & Teilnehmerübersicht",
        description="Anmeldungen aus dem Star Citizen Showcase werden hier automatisch aktualisiert.",
        color=0xF28C45,
        timestamp=now,
    )
    if not events:
        embed.add_field(name="Noch keine Events", value="Erstelle ein Event im Web-Kalender, um es hier anzuzeigen.", inline=False)
    else:
        for event in events:
            title = str(event.get("title") or "Event")[:240]
            raw_date = str(event.get("starts_at") or "Zeit offen")
            try:
                dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                date_text = f"<t:{int(dt.timestamp())}:F>"
            except (ValueError, TypeError, OverflowError):
                date_text = raw_date[:80]
            rsvps = event.get("rsvps", [])
            if not isinstance(rsvps, list):
                rsvps = []
            names = [str(r.get("pilot_name", "Pilot")) for r in rsvps if isinstance(r, dict)]
            max_attendees = int(event.get("max_attendees", 8) or 8)
            roster = "\n".join(f"• {name}" for name in names[:20]) or "Noch keine Anmeldungen"
            if len(names) > 20:
                roster += f"\n… und {len(names)-20} weitere"
            desc = str(event.get("description") or "Kein Briefing hinterlegt.")[:500]
            value = f"**Aktivität:** {str(event.get('event_type') or 'Event')[:60]}\n**Zeit:** {date_text}\n**Teilnehmer:** {len(names)}/{max_attendees}\n\n{desc}\n\n**Angemeldete Piloten**\n{roster}"
            embed.add_field(name=title[:256], value=value[:850], inline=False)
    embed.set_footer(text="Star Citizen Showcase · automatisch synchronisiert")
    message_ids = load_message_ids()
    await sync_manufacturer_message(await get_channel(), "__events__", [embed], message_ids)
    save_message_ids(message_ids)

webapp.set_event_sync_callback(sync_events_to_discord)

async def run_update_cycle(initial: bool = False) -> None:
    log.info("Lade aktuelle Schiffsdaten von UEX Corp...")
    try:
        ships = await fetch_ships(
            api_key=config.uex_api_key,
            include_ground_vehicles=config.include_ground_vehicles,
        )
    except UexApiError as exc:
        log.error("Fehler beim Laden der UEX-Daten: %s", exc)
        webapp.publish_failure(str(exc))
        return
    except Exception:
        log.exception("Unerwarteter Fehler beim Laden der UEX-Daten.")
        webapp.publish_failure("Unerwarteter Fehler beim Abruf.")
        return

    if not ships:
        log.warning("UEX hat 0 Schiffe zurückgegeben - überspringe diesen Zyklus sicherheitshalber.")
        webapp.publish_failure("UEX lieferte 0 Schiffe.")
        return

    # Web-Seite sofort mit dem frischen Stand versorgen - bewusst VOR allem, was Discord
    # betrifft: Die Seite soll auch dann aktuell sein, wenn das Posten im Channel scheitert.
    global latest_ships
    latest_ships = ships
    rsi_images.apply_cached(ships, config.rsi_images_mode)  # bereits bekannte RSI-Bilder (nur Datei lesen)
    webapp.publish_ships(ships)
    start_rsi_enrichment(ships)  # Neues im Hintergrund nachholen, blockiert nichts

    last_prices = load_last_prices()
    changes = [] if initial else compute_changes(ships, last_prices)

    try:
        channel = await get_channel()
    except discord.HTTPException:
        log.exception("Konnte den konfigurierten Channel (%s) nicht laden.", config.channel_id)
        return

    message_ids = load_message_ids()
    groups = group_by_manufacturer(ships)
    ingame_count = sum(1 for s in ships.values() if s.available_ingame)

    header_embed = build_header_embed(
        total_ships=len(ships),
        ingame_count=ingame_count,
        last_updated=datetime.now(timezone.utc),
        changes=changes,
        web_url=config.web_url if config.web_enabled else None,
    )
    await sync_manufacturer_message(channel, HEADER_KEY, [header_embed], message_ids)

    for manufacturer, ships_in_group in groups.items():
        manufacturer_embeds = build_manufacturer_embeds(manufacturer, ships_in_group)
        await sync_manufacturer_message(channel, manufacturer, manufacturer_embeds, message_ids)

    save_message_ids(message_ids)
    save_last_prices({str(s.id): s.cheapest_price.price_buy for s in ships.values() if s.cheapest_price})

    if changes:
        log.info("%d Preisänderung(en) erkannt und im Showcase aktualisiert.", len(changes))
    else:
        log.info("Showcase geprüft - keine Änderungen seit dem letzten Durchlauf.")


@tasks.loop(minutes=60)  # Startwert; wird in on_ready per Config überschrieben
async def update_loop() -> None:
    await run_update_cycle(initial=False)


@bot.event
async def on_ready() -> None:
    log.info("Eingeloggt als %s (ID: %s)", bot.user, getattr(bot.user, "id", "?"))

    try:
        if config.guild_id:
            guild_obj = discord.Object(id=config.guild_id)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            log.info("%d Slash-Command(s) sofort für Server %s synchronisiert.", len(synced), config.guild_id)
        else:
            synced = await bot.tree.sync()
            log.info(
                "%d Slash-Command(s) global synchronisiert (kann bis zu ~1 Stunde dauern, "
                "bis sie überall sichtbar sind; GUILD_ID in der .env setzen für sofortige Sichtbarkeit).",
                len(synced),
            )
    except discord.HTTPException:
        log.exception("Slash-Commands konnten nicht synchronisiert werden.")

    if not update_loop.is_running():
        update_loop.change_interval(minutes=config.update_interval_minutes)
        await run_update_cycle(initial=True)
        update_loop.start()
        log.info("Update-Loop gestartet (alle %d Minuten).", config.update_interval_minutes)
    try:
        await sync_events_to_discord()
    except Exception:
        log.exception("Event-Kalender konnte beim Start nicht in Discord synchronisiert werden.")


def _is_admin(interaction: discord.Interaction) -> bool:
    if config.admin_role_id is None:
        return True
    member = interaction.user
    if isinstance(member, discord.Member):
        return any(role.id == config.admin_role_id for role in member.roles)
    return False


class ListPager(discord.ui.View):
    """Vor/Zurück-Buttons für die /liste-Ergebnisse. Für alle im Channel bedienbar."""

    def __init__(self, embeds: List[discord.Embed], timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.index = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.embeds) - 1

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(label="Weiter ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = min(len(self.embeds) - 1, self.index + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


@bot.tree.command(name="liste", description="Zeigt eine durchblätterbare Liste aller Star-Citizen-Schiffe mit In-Game-Preisen.")
async def liste_command(interaction: discord.Interaction) -> None:
    # Nicht ephemeral -> für alle im Channel sichtbar.
    await interaction.response.defer(ephemeral=False)
    try:
        ships = await fetch_ships(
            api_key=config.uex_api_key,
            include_ground_vehicles=config.include_ground_vehicles,
        )
    except Exception:
        await interaction.followup.send("Die UEX-API ist gerade nicht erreichbar. Bitte später erneut versuchen.")
        return

    pages = build_list_pages(list(ships.values()))
    view = ListPager(pages) if len(pages) > 1 else None
    await interaction.followup.send(embed=pages[0], view=view)


@bot.tree.command(name="schiff", description="Zeigt den aktuellen In-Game-Kaufpreis eines Star-Citizen-Schiffs.")
@discord.app_commands.describe(name="Name des Schiffs (oder ein Teil davon), z.B. 'Carrack' oder 'Hornet'")
async def schiff_command(interaction: discord.Interaction, name: str) -> None:
    # Nicht ephemeral -> Antwort ist für alle im Channel sichtbar, nicht nur für den Fragenden.
    await interaction.response.defer(ephemeral=False)
    try:
        ships = await fetch_ships(
            api_key=config.uex_api_key,
            include_ground_vehicles=config.include_ground_vehicles,
        )
    except Exception:
        await interaction.followup.send("Die UEX-API ist gerade nicht erreichbar. Bitte später erneut versuchen.")
        return

    rsi_images.apply_cached(ships, config.rsi_images_mode)
    matches = [s for s in ships.values() if name.lower() in s.name.lower()]
    if not matches:
        await interaction.followup.send(f"Kein Schiff gefunden, das zu „{name}“ passt.")
        return

    matches.sort(key=lambda s: len(s.name))
    ship = matches[0]
    cheapest = ship.cheapest_price

    embed = discord.Embed(title=f"{ship.manufacturer} — {ship.name}", color=0x2E86FF)
    if ship.image_url:
        embed.set_thumbnail(url=ship.image_url)
    if cheapest:
        locations = sorted({p.terminal_name for p in ship.prices})
        embed.add_field(name="Günstigster In-Game-Preis", value=f"{format_auec(cheapest.price_buy)} aUEC", inline=True)
        embed.add_field(name="Ort", value=cheapest.terminal_name, inline=True)
        weitere = [l for l in locations if l != cheapest.terminal_name]
        if weitere:
            embed.add_field(name="Weitere Verkaufsorte", value=", ".join(weitere), inline=False)
    else:
        tag = " (Concept-Schiff)" if ship.is_concept else ""
        embed.add_field(name="In-Game-Preis", value=f"Nicht im Spiel kaufbar{tag} — nur als Pledge erhältlich.", inline=False)
    if len(matches) > 1:
        embed.set_footer(text=f"{len(matches) - 1} weitere Treffer für „{name}“ - für genauere Suche mehr vom Namen angeben.")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="web", description="Zeigt den Link zur Web-Seite mit allen Schiffen, Bildern, Suche und Filtern.")
async def web_command(interaction: discord.Interaction) -> None:
    if not config.web_enabled:
        await interaction.response.send_message("Die Web-Seite ist auf diesem Bot abgeschaltet.", ephemeral=True)
        return
    if not config.web_url:
        # Ohne öffentliche Adresse gibt es nichts, was wir verlinken könnten.
        await interaction.response.send_message(
            "Die Web-Seite läuft, aber es ist keine öffentliche Adresse hinterlegt. "
            "Bitte `WEB_URL` in der Bot-Konfiguration setzen (z.B. `https://meine-app.fly.dev`).",
            ephemeral=True,
        )
        return

    # Öffentlich sichtbar, plus ein Link-Button zum direkten Öffnen.
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Web-Seite öffnen", style=discord.ButtonStyle.link, url=config.web_url))
    await interaction.response.send_message(embed=build_web_embed(config.web_url), view=view)


@bot.tree.command(name="aktualisieren", description="Aktualisiert den Schiffs-Showcase sofort manuell.")
async def refresh_command(interaction: discord.Interaction) -> None:
    if not _is_admin(interaction):
        # Die Ablehnung bleibt ephemeral (nur für dich sichtbar) - macht bei einer
        # fehlenden Berechtigung mehr Sinn, als das öffentlich im Channel zu zeigen.
        await interaction.response.send_message("Dafür fehlt dir die nötige Rolle.", ephemeral=True)
        return
    # Ab hier öffentlich sichtbar für alle im Channel.
    await interaction.response.send_message("Aktualisiere den Showcase, einen Moment...")
    await run_update_cycle(initial=False)
    await interaction.followup.send("✅ Showcase wurde aktualisiert.")


def main() -> None:
    if not config.discord_enabled:
        log.warning(
            "DISCORD_TOKEN oder CHANNEL_ID fehlt - starte NUR die Web-Seite (ohne Discord-Bot). "
            "Zugangsdaten per .env bzw. `fly secrets set` nachtragen, um den Bot zu aktivieren."
        )
        webapp.main()
        return
    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error(
            "Login fehlgeschlagen: Der DISCORD_TOKEN ist ungültig oder abgelaufen - "
            "die Web-Seite läuft trotzdem weiter (ohne Discord-Bot)."
        )
        webapp.main()


if __name__ == "__main__":
    main()
