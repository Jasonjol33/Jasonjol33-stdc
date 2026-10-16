"""Manueller Testlauf mit echten (zuvor live abgefragten) UEX-Beispieldaten.

Prüft die komplette Verarbeitungskette OHNE Discord- oder UEX-Verbindung:
JSON -> parse_ships() -> Gruppierung -> Embeds -> Web-API. Kein pytest nötig, einfach:
    python test_smoke.py
"""

from __future__ import annotations

import sys

# --- Echte Beispieldaten, 1:1 aus den UEX-API-Live-Antworten übernommen ------

VEHICLES_SAMPLE = [
    {  # Origin 100i - klassisches Starter-Schiff, an mehreren Orten kaufbar
        "id": 1, "id_company": 195, "name": "100i", "name_full": "Origin 100i",
        "is_spaceship": 1, "is_concept": 0, "is_ground_vehicle": 0,
        "pad_type": "XS", "scu": 2, "crew": "1",
        "url_photo": "https://assets.uexcorp.space/img/vehicles/1/images/fc9508537140e21f7959e16f152c915cc8d32dc2.jpg",
        "url_store": "https://robertsspaceindustries.com/pledge/ships/origin-100/100i",
        "company_name": "Origin Jumpworks",
    },
    {  # Origin 890 Jump - teures Luxusschiff
        "id": 13, "id_company": 195, "name": "890 Jump", "name_full": "Origin 890 Jump",
        "is_spaceship": 1, "is_concept": 0, "is_ground_vehicle": 0,
        "pad_type": "XL", "scu": 388, "crew": "1",
        "url_photo": "https://assets.uexcorp.space/img/vehicles/13/images/8306aa7d009142a95a3761bbc1e605495467372f.jpg",
        "url_store": "https://robertsspaceindustries.com/pledge/ships/890-jump/890-jump",
        "company_name": "Origin Jumpworks",
    },
    {  # Anvil Arrow - an zwei Orten zum selben Preis kaufbar (testet "weitere Orte")
        "id": 20, "id_company": 13, "name": "Arrow", "name_full": "Anvil Arrow",
        "is_spaceship": 1, "is_concept": 0, "is_ground_vehicle": 0,
        "pad_type": "S", "scu": 0, "crew": "1",
        "url_photo": "https://assets.uexcorp.space/img/vehicles/20/images/8a37bdfc747d887e4ee57778f8c5995f63b382b1.jpg",
        "url_store": "https://robertsspaceindustries.com/pledge/ships/anvil-arrow/arrow",
        "company_name": "Anvil Aerospace",
    },
    {  # Anvil Ballista - ist laut API ein BODENFAHRZEUG (is_spaceship=0) -> muss rausgefiltert werden
        "id": 30, "id_company": 13, "name": "Ballista", "name_full": "Anvil Ballista",
        "is_spaceship": 0, "is_concept": 0, "is_ground_vehicle": 1,
        "pad_type": "S", "scu": 0, "crew": "2",
        "url_photo": "https://assets.uexcorp.space/img/vehicles/30/images/41143535b3ce35c42b503a673e885a8e5cbdeedd.jpg",
        "url_store": "https://robertsspaceindustries.com/pledge/ships/anvil-ballista/ballista",
        "company_name": "Anvil Aerospace",
    },
    {  # Anvil Carrack - Explorer, ebenfalls an mehreren Orten
        "id": 39, "id_company": 13, "name": "Carrack", "name_full": "Anvil Carrack",
        "is_spaceship": 1, "is_concept": 0, "is_ground_vehicle": 0,
        "pad_type": "", "scu": 456, "crew": "6",
        "url_photo": "https://assets.uexcorp.space/img/vehicles/39/images/b3d02bd705d95af9d9bb79a77645e57a910f9ac4.jpg",
        "url_store": "https://robertsspaceindustries.com/pledge/ships/carrack/carrack",
        "company_name": "Anvil Aerospace",
    },
    {  # RSI Arrastra - Concept-Schiff, hat AKTUELL KEINEN In-Game-Kaufpreis
        "id": 19, "id_company": 217, "name": "Arrastra", "name_full": "RSI Arrastra",
        "is_spaceship": 1, "is_concept": 1, "is_ground_vehicle": 0,
        "pad_type": "L", "scu": 576, "crew": "2,5",
        "url_photo": "https://assets.uexcorp.space/img/vehicles/19/images/d58818f376f3a03468fe2d34f18dfeada5e6fd8c.jpg",
        "url_store": "https://robertsspaceindustries.com/pledge/ships/arrastra/arrastra",
        "company_name": "Roberts Space Industries",
    },
]

PRICES_SAMPLE = [
    {"id": 44, "id_vehicle": 1, "id_terminal": 148, "price_buy": 1146600, "vehicle_name": "100i", "terminal_name": "Astro Armada"},
    {"id": 206, "id_vehicle": 1, "id_terminal": 149, "price_buy": 1089270, "vehicle_name": "100i", "terminal_name": "New Deal Lorville"},
    {"id": 46, "id_vehicle": 13, "id_terminal": 148, "price_buy": 65356200, "vehicle_name": "890 Jump", "terminal_name": "Astro Armada"},
    {"id": 205, "id_vehicle": 13, "id_terminal": 149, "price_buy": 62088400, "vehicle_name": "890 Jump", "terminal_name": "New Deal Lorville"},
    {"id": 2, "id_vehicle": 20, "id_terminal": 148, "price_buy": 1984500, "vehicle_name": "Arrow", "terminal_name": "Astro Armada"},
    {"id": 354, "id_vehicle": 20, "id_terminal": 791, "price_buy": 1984500, "vehicle_name": "Arrow", "terminal_name": "Teach's Levski"},
    {"id": 10, "id_vehicle": 30, "id_terminal": 148, "price_buy": 1481760, "vehicle_name": "Ballista", "terminal_name": "Astro Armada"},
    {"id": 159, "id_vehicle": 30, "id_terminal": 149, "price_buy": 1407670, "vehicle_name": "Ballista", "terminal_name": "New Deal Lorville"},
    {"id": 11, "id_vehicle": 39, "id_terminal": 148, "price_buy": 34398000, "vehicle_name": "Carrack", "terminal_name": "Astro Armada"},
    # Arrastra (id 19) bewusst OHNE Preiseintrag -> testet den "nicht im Spiel kaufbar"-Fall
]


def main() -> int:
    from uex_api import parse_ships
    from embeds import build_header_embed, build_manufacturer_embeds, chunk_lines, ship_line, format_auec
    from bot import group_by_manufacturer, compute_changes

    errors = []

    def check(label: str, condition: bool):
        status = "OK  " if condition else "FEHLER"
        print(f"[{status}] {label}")
        if not condition:
            errors.append(label)

    # --- 1) Parsing & Filterung -------------------------------------------------
    ships = parse_ships(VEHICLES_SAMPLE, PRICES_SAMPLE, include_ground_vehicles=False)
    check("6 Fahrzeuge geladen, 1 Bodenfahrzeug (Ballista) rausgefiltert -> 5 Schiffe", len(ships) == 5)
    check("Ballista (id 30) korrekt ausgeschlossen", 30 not in ships)
    check("Carrack (id 39) vorhanden", 39 in ships)

    arrow = ships[20]
    check("Arrow hat 2 Preis-Einträge (zwei Terminals)", len(arrow.prices) == 2)
    check("Arrow guenstigster Preis = 1.984.500", arrow.cheapest_price.price_buy == 1984500)

    arrastra = ships[19]
    check("Arrastra (Concept) hat KEINEN Preis", arrastra.cheapest_price is None)
    check("Arrastra gilt als nicht in-game verfuegbar", arrastra.available_ingame is False)

    # --- 2) Inklusive Bodenfahrzeuge ---------------------------------------------
    ships_with_ground = parse_ships(VEHICLES_SAMPLE, PRICES_SAMPLE, include_ground_vehicles=True)
    check("Mit include_ground_vehicles=True sind es 6 Fahrzeuge", len(ships_with_ground) == 6)
    check("Ballista hat dann ebenfalls 2 Preise", len(ships_with_ground[30].prices) == 2)

    # --- 3) Formatierung ----------------------------------------------------------
    check("format_auec(1234567) -> '1.234.567'", format_auec(1234567) == "1.234.567")
    line = ship_line(arrow)
    check("Zeile für Arrow enthält Preis und '+1 weitere Orte'", "1.984.500 aUEC" in line and "weitere Orte" in line)
    line_concept = ship_line(arrastra)
    check("Zeile für Arrastra markiert 'nicht im Spiel kaufbar' + Concept-Tag", "nicht im Spiel kaufbar" in line_concept and "Concept" in line_concept)

    # --- 4) Gruppierung nach Hersteller -------------------------------------------
    groups = group_by_manufacturer(ships)
    check("3 Hersteller-Gruppen (Origin, Anvil, RSI)", len(groups) == 3)
    check("Anvil-Gruppe enthält Arrow + Carrack (2 Schiffe)", len(groups.get("Anvil Aerospace", [])) == 2)

    # --- 5) Embeds bauen & Discord-Limits einhalten --------------------------------
    for manufacturer, group_ships in groups.items():
        manufacturer_embeds = build_manufacturer_embeds(manufacturer, group_ships)
        for e in manufacturer_embeds:
            check(f"Embed '{e.title}' unter 4096 Zeichen", len(e.description or "") <= 4096)
            check(f"Embed '{e.title}' Gesamtlaenge unter 6000 Zeichen", len(e) <= 6000)

    header = build_header_embed(total_ships=5, ingame_count=4, last_updated=__import__("datetime").datetime.now(), changes=["🔺 Test: 1 → 2 aUEC"])
    check("Header-Embed enthält Gesamt- und Verfügbarkeits-Anzahl", "5" in header.description and "4" in header.description)
    check("Header-Embed hat ein Änderungsfeld", len(header.fields) == 1)

    # --- 6) chunk_lines mit sehr vielen Zeilen (erzwingt Aufteilung) ---------------
    many_lines = [f"**Testschiff {i}** — {format_auec(i * 1000)} aUEC bei Irgendwo Station" for i in range(400)]
    chunks = chunk_lines(many_lines)
    check("Viele Zeilen werden auf mehrere Chunks aufgeteilt", len(chunks) > 1)
    check("Jeder Chunk bleibt unter dem Limit", all(len(c) <= 3800 for c in chunks))
    rejoined = "\n".join(chunks).split("\n")
    check("Beim Aufteilen geht keine Zeile verloren", len(rejoined) == len(many_lines))

    # --- 7) Änderungserkennung (compute_changes) -----------------------------------
    # 890 Jump (id 13) ist am günstigsten bei New Deal Lorville = 62.088.400 -> das ist der
    # korrekte "alte" Vergleichswert, damit dieser Fall als UNVERÄNDERT gilt.
    old_prices = {"1": 1000000, "13": 62088400, "20": 1984500}  # 39 (Carrack) fehlt -> "neu kaufbar"
    changes = compute_changes(ships, old_prices)
    joined = " | ".join(changes)
    check("Preiserhöhung bei 100i wird als 🔺 erkannt", any("🔺" in c and "100i" in c for c in changes))
    check("Neuer Kauf-Preis für Carrack wird als 🆕 erkannt", any("🆕" in c and "Carrack" in c for c in changes))
    check("Unveränderter Preis (890 Jump) erzeugt KEINE Änderungsmeldung", "890 Jump" not in joined)

    # --- 8) Web-Seite (webapp.py) ----------------------------------------------------
    import asyncio
    import os
    from aiohttp.test_utils import TestClient, TestServer
    import webapp
    from config import load_config

    d_arrow = webapp.ship_to_dict(arrow)
    check("Web: Arrow günstigster Preis 1.984.500 + 1 weiterer Ort",
          d_arrow["cheapest_price"] == 1984500 and d_arrow["extra_locations"] == 1)
    check("Web: Preisliste pro Schiff aufsteigend sortiert (100i)",
          [p["price"] for p in webapp.ship_to_dict(ships[1])["all_prices"]] == [1089270, 1146600])
    d_arrastra = webapp.ship_to_dict(arrastra)
    check("Web: Arrastra (Concept) nicht kaufbar, ohne Preisliste",
          d_arrastra["available_ingame"] is False and d_arrastra["all_prices"] == [] and d_arrastra["is_concept"] is True)

    async def web_roundtrip() -> dict:
        res = {}
        webapp.cache = webapp.ShipCache()  # frischer Zustand, unabhängig von früheren Läufen
        client = TestClient(TestServer(webapp.create_app()))
        await client.start_server()
        try:
            res["leer"] = (await client.get("/api/ships")).status
            webapp.publish_ships(ships)
            r = await client.get("/api/ships")
            res["mit_daten"] = (r.status, await r.json())
            webapp.publish_failure("Test-Störung")
            res["gestört"] = await (await client.get("/api/ships")).json()
            webapp.publish_ships(ships)
            res["erholt"] = (await (await client.get("/api/ships")).json())["stale"]
            res["index"] = (await client.get("/")).status
        finally:
            await client.close()
        return res

    web_res = asyncio.run(web_roundtrip())
    check("Web: ohne Datenstand antwortet /api/ships ehrlich mit 503", web_res["leer"] == 503)
    status, payload = web_res["mit_daten"]
    check("Web: mit Datenstand 200, 5 Schiffe, 4 kaufbar",
          status == 200 and payload["total_ships"] == 5 and payload["ingame_count"] == 4 and payload["stale"] is False)
    check("Web: fehlgeschlagener Abruf -> alter Stand bleibt, 'stale' = true",
          web_res["gestört"]["stale"] is True and web_res["gestört"]["total_ships"] == 5)
    check("Web: nächster erfolgreicher Abruf hebt 'stale' wieder auf", web_res["erholt"] is False)
    check("Web: Startseite wird ausgeliefert", web_res["index"] == 200)

    header_link = build_header_embed(total_ships=5, ingame_count=4, last_updated=__import__("datetime").datetime.now(),
                                     changes=[], web_url="https://beispiel.fly.dev")
    check("Kopf-Embed verlinkt die Web-Seite, wenn eine URL gesetzt ist", "https://beispiel.fly.dev" in (header_link.description or ""))
    check("Kopf-Embed ohne URL bleibt unverändert (kein Link)", "http" not in (header.description or ""))

    from embeds import build_web_embed
    web_embed = build_web_embed("https://beispiel.fly.dev")
    check("/web-Embed enthält den Link zur Web-Seite", "https://beispiel.fly.dev" in (web_embed.description or ""))

    # --- 9) RSI-Bilder (rsi_images.py) -----------------------------------------------
    import rsi_images

    page = ('<head><meta property="og:title" content="X">'
            '<meta property="og:image" content="https://media.robertsspaceindustries.com/abc/carrack.jpg"></head>')
    check("RSI: og:image wird aus den Meta-Tags gelesen",
          rsi_images.extract_image_url(page, "https://robertsspaceindustries.com/pledge/ships/carrack/carrack")
          == "https://media.robertsspaceindustries.com/abc/carrack.jpg")
    check("RSI: relatives og:image wird zu einer vollen RSI-Adresse",
          rsi_images.extract_image_url('<meta content="/media/x.jpg" property="og:image">',
                                       "https://robertsspaceindustries.com/pledge/ships/a/b")
          == "https://robertsspaceindustries.com/media/x.jpg")
    check("RSI: Bilder von fremden Hosts werden verworfen",
          rsi_images.extract_image_url('<meta property="og:image" content="https://boese.example/x.jpg">',
                                       "https://robertsspaceindustries.com/a") is None)
    check("RSI: Seite ohne og:image liefert None", rsi_images.extract_image_url("<head></head>", "https://robertsspaceindustries.com/a") is None)
    check("RSI: nur https auf robertsspaceindustries.com ist erlaubt",
          rsi_images.is_rsi_url("https://media.robertsspaceindustries.com/a.jpg")
          and not rsi_images.is_rsi_url("http://robertsspaceindustries.com/a")
          and not rsi_images.is_rsi_url("https://robertsspaceindustries.com.evil.example/a")
          and not rsi_images.is_rsi_url(None))

    carrack_url = ships[39].store_url
    fake_cache = {
        carrack_url: {"image": "https://media.robertsspaceindustries.com/abc/carrack.jpg", "checked": "2026-01-01T00:00:00+00:00"},
    }
    rsi_images.load_rsi_images = lambda: fake_cache
    ships_missing = parse_ships(VEHICLES_SAMPLE, PRICES_SAMPLE)
    uex_before = ships_missing[39].image_url
    rsi_images.apply_cached(ships_missing, "missing")
    check("RSI missing: vorhandenes UEX-Bild bleibt, RSI wird Ersatzbild",
          ships_missing[39].image_url == uex_before and ships_missing[39].image_url_alt == fake_cache[carrack_url]["image"])
    ships_missing[39].image_url = None
    ships_missing[39].image_url_alt = None
    rsi_images.apply_cached(ships_missing, "missing")
    check("RSI missing: fehlendes UEX-Bild wird durch das RSI-Bild gefüllt",
          ships_missing[39].image_url == fake_cache[carrack_url]["image"])
    ships_all = parse_ships(VEHICLES_SAMPLE, PRICES_SAMPLE)
    rsi_images.apply_cached(ships_all, "all")
    rsi_images.apply_cached(ships_all, "all")  # zweiter Aufruf darf nichts verfälschen
    check("RSI all: RSI-Bild hat Vorrang, UEX-Bild wird Ersatz (auch nach doppeltem Aufruf)",
          ships_all[39].image_url == fake_cache[carrack_url]["image"] and ships_all[39].image_url_alt == uex_before)
    ships_off = parse_ships(VEHICLES_SAMPLE, PRICES_SAMPLE)
    rsi_images.apply_cached(ships_off, "off")
    check("RSI off: nichts wird verändert", ships_off[39].image_url == uex_before and ships_off[39].image_url_alt is None)
    generic = "https://media.robertsspaceindustries.com/logo.png"
    fake_cache.clear()
    fake_cache.update({f"https://robertsspaceindustries.com/pledge/ships/s{i}": {"image": generic, "checked": "x"} for i in range(4)})
    fake_cache[ships[39].store_url] = {"image": generic, "checked": "x"}
    ships_generic = parse_ships(VEHICLES_SAMPLE, PRICES_SAMPLE)
    ships_generic[39].image_url = None
    rsi_images.apply_cached(ships_generic, "missing")
    check("RSI: ein Bild, das bei vielen Schiffen identisch ist (Logo), wird nicht verwendet", ships_generic[39].image_url is None)
    check("Web-API liefert image_url_alt mit", "image_url_alt" in webapp.ship_to_dict(ships_missing[39]))
    os.environ.setdefault("DISCORD_TOKEN", "test-token")  # nur damit load_config() im Test nicht abbricht
    os.environ.setdefault("CHANNEL_ID", "1")
    os.environ["RSI_IMAGES"] = "aus"
    check("Config: RSI_IMAGES mit unbekanntem Wert -> missing", load_config().rsi_images_mode == "missing")
    os.environ["RSI_IMAGES"] = "off"
    check("Config: RSI_IMAGES=off", load_config().rsi_images_mode == "off")
    os.environ.pop("RSI_IMAGES", None)

    old_env = {k: os.environ.get(k) for k in ("WEB_ENABLED", "PORT", "WEB_URL")}
    try:
        os.environ.update({"WEB_ENABLED": "false", "PORT": "9090", "WEB_URL": "meine-app.fly.dev/"})
        cfg = load_config()
        check("Config: WEB_ENABLED=false, PORT=9090, WEB_URL ohne Schema wird zu https://...",
              cfg.web_enabled is False and cfg.web_port == 9090 and cfg.web_url == "https://meine-app.fly.dev")
        os.environ.update({"WEB_ENABLED": "", "PORT": "kaputt", "WEB_URL": ""})
        cfg = load_config()
        check("Config: Standardwerte bei leeren/ungültigen Eingaben (an, Port 8080, kein Link)",
              cfg.web_enabled is True and cfg.web_port == 8080 and cfg.web_url is None)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print()
    if errors:
        print(f"{len(errors)} Prüfung(en) fehlgeschlagen:")
        for e in errors:
            print(" -", e)
        return 1
    print(f"Alle Prüfungen erfolgreich ({len(VEHICLES_SAMPLE)} Beispiel-Fahrzeuge, echte UEX-Daten).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
