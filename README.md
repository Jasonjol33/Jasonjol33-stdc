# Star Citizen Schiffs-Showcase-Bot

Ein Discord-Bot, der **alle Star-Citizen-Raumschiffe** mit ihren aktuellen
**In-Game-Kaufpreisen (aUEC)** automatisch in einem Channel anzeigt und sich
selbst aktualisiert, sobald sich ein Preis ändert.

Zusätzlich gibt es eine **eigene Web-Seite** (siehe [Web-Showcase](#web-showcase-eigene-seite)),
auf der sich derselbe Datenstand mit großen Bildern, Suche, Filtern und allen
Kauf-Orten pro Schiff durchstöbern lässt.

## Bevor du loslegst: ehrliche Einordnung

Damit keine falschen Erwartungen entstehen:

- **Datenquelle:** [UEX Corp](https://uexcorp.space) – ein sehr etabliertes,
  aber **von der Community betriebenes** Projekt, nicht offiziell von Cloud
  Imperium Games. UEX selbst weist darauf hin, dass gelegentlich Fehler
  vorkommen können. In der Praxis ist die Datenqualität aber sehr gut und wird
  auch von vielen anderen bekannten Star-Citizen-Tools genutzt.
- **"Alle" Schiffe:** Der Bot zeigt wirklich **jedes** Raumschiff aus der
  UEX-Datenbank an (aktuell ca. 200+). Da du explizit **nur In-Game-Preise**
  wolltest (keine Pledge-/Echtgeld-Preise): Schiffe, die es aktuell nur im
  Pledge-Store gibt (z.B. viele Concept-Schiffe), werden **trotzdem
  aufgeführt** – aber ehrlich als "nicht im Spiel kaufbar" markiert, statt
  einfach zu fehlen oder einen falschen Preis zu zeigen.
- **"Live":** Es gibt keine öffentliche Schnittstelle, die Preisänderungen in
  Echtzeit (Sekunde für Sekunde) meldet. Die In-Game-Kaufpreise ändern sich in
  Star Citizen ohnehin nur mit Balancing-Patches, nicht minütlich. Der Bot
  prüft deshalb **alle 60 Minuten** (einstellbar) automatisch auf Änderungen
  und passt dann nur die betroffenen Nachrichten an – das ist die technisch
  sinnvollste Definition von "live" für diesen Anwendungsfall.
- Bodenfahrzeuge (Rover, Panzer etc.) werden standardmäßig **nicht**
  angezeigt, da explizit nach "Schiffen" gefragt wurde. Lässt sich in der
  `.env` per `INCLUDE_GROUND_VEHICLES=true` einschalten.
- **Web-Seite:** Zusätzlich zeigt eine eigene Seite denselben Datenstand mit größeren
  Bildern, Suche und Filtern (siehe [Web-Showcase](#web-showcase-eigene-seite)). Sie ist
  öffentlich erreichbar, sobald du den Bot auf Fly.io deployst – wer das nicht möchte,
  schaltet sie mit `WEB_ENABLED=false` ab.

## Voraussetzungen

- Python 3.10 oder neuer
- Ein Discord-Account mit Admin-Rechten auf dem Server, auf dem der Bot laufen soll
- Ein Rechner/Server, der **dauerhaft** läuft (siehe Abschnitt "Hosting" unten)

## Einrichtung

### 1. Bot im Discord Developer Portal erstellen

1. Gehe zu <https://discord.com/developers/applications> und logge dich ein.
2. Klicke auf **"New Application"**, gib einen Namen ein (z.B. "SC Schiffs-Showcase").
3. Gehe links auf **"Bot"** → **"Reset Token"** / **"Copy"**, um deinen Bot-Token zu kopieren.
   ⚠️ Diesen Token niemals teilen – er gehört in die `.env`-Datei (siehe Schritt 4).
4. Unter **"Privileged Gateway Intents"** muss nichts aktiviert werden – der Bot braucht
   keine davon.

### 2. Bot auf deinen Server einladen

1. Gehe links auf **"OAuth2" → "URL Generator"**.
2. Scopes: hake **`bot`** und **`applications.commands`** an.
3. Bot Permissions: **View Channel**, **Send Messages**, **Embed Links**,
   **Manage Messages** (optional, nur damit alte Nachrichten bei Bedarf
   sauber aufgeräumt werden), **Read Message History**.
4. Kopiere die generierte URL unten, öffne sie im Browser und wähle deinen Server aus.

### 3. Channel-ID herausfinden

1. In Discord: Nutzereinstellungen → Erweitert → **Entwicklermodus** aktivieren.
2. Rechtsklick auf den Channel, in dem der Showcase erscheinen soll → **"ID kopieren"**.

### 4. Projekt konfigurieren

1. Kopiere `.env.example` zu `.env`.
2. Trage mindestens `DISCORD_TOKEN` (aus Schritt 1) und `CHANNEL_ID` (aus Schritt 3) ein.
3. Optional: `GUILD_ID` (Rechtsklick auf den Server-Namen → "ID kopieren") einträgen,
   damit die Slash-Commands **sofort** funktionieren statt erst nach bis zu einer Stunde.

### 5. Installation

```bash
pip install -r requirements.txt
```

### 6. Starten

```bash
python bot.py
```

Beim ersten Start postet der Bot automatisch eine Kopf-Nachricht (Übersicht)
sowie eine Nachricht pro Hersteller mit allen zugehörigen Schiffen. Das kann
beim ersten Mal ein paar Sekunden dauern.

Gleichzeitig startet die Web-Seite: <http://localhost:8080> (siehe
[Web-Showcase](#web-showcase-eigene-seite)).

## Befehle

Alle Befehle antworten **öffentlich im Channel** (für jeden sichtbar), nicht
nur für die Person, die den Befehl ausgeführt hat.

- **`/liste`** – zeigt eine durchblätterbare Liste **aller** Schiffe mit
  ihrem aktuellen In-Game-Preis (15 pro Seite, mit "Zurück/Weiter"-Buttons).
- **`/schiff <name>`** – zeigt den aktuellen günstigsten In-Game-Preis und
  Kauf-Ort für ein einzelnes Schiff (z.B. `/schiff Carrack`).
- **`/web`** – postet den Link zur Web-Seite (Bilder, Suche, Filter) samt "Öffnen"-Button. Die Adresse kommt aus `WEB_URL` (auf Fly.io automatisch aus dem App-Namen abgeleitet); ohne diese Adresse (oder mit `WEB_ENABLED=false`) antwortet der Bot nur privat mit einem Hinweis.
- **`/aktualisieren`** – erzwingt eine sofortige Aktualisierung, statt auf den
  nächsten automatischen Check zu warten. Kann optional über `ADMIN_ROLE_ID`
  in der `.env` auf eine bestimmte Rolle beschränkt werden (die Ablehnung bei
  fehlender Rolle bleibt privat, der Rest ist wieder öffentlich).

> Hinweis: Discord erlaubt bei Slash-Commands nur Kleinbuchstaben im Namen,
> daher heißt der Befehl `/liste` statt `/List`.

## Web-Showcase (eigene Seite)

Zusätzlich zum Discord-Channel gibt es eine Web-Seite, die denselben Datenstand
zeigt – nur mit deutlich mehr Platz als ein Discord-Embed:

- große Schiffsbilder in einem Karten-Raster (Look: Sci-Fi-Terminal mit abgeschrägten Panels)
- Suche, Filter nach Hersteller und Verfügbarkeit (alle / kaufbar / nur Pledge)
  und Sortierung nach Name, Preis oder Hersteller
- Klick auf ein Schiff öffnet ein Detail-Fenster mit **allen** Kauf-Terminals samt
  Preisen (im Discord-Embed passt nur der günstigste Preis rein)
- läuft auf Desktop und Handy; bei einer UEX-Störung bleibt der letzte gute Stand sichtbar
  (die Statuszeile weist dann auf einen evtl. veralteten Stand hin)

**So hängt das zusammen:**

- Die Seite läuft **im selben Prozess wie der Bot** (`webapp.py`, gestartet von `bot.py`),
  also auch auf derselben Fly.io-Maschine. Der Bot gibt nach jedem UEX-Abruf seinen Stand an
  die Seite weiter: **ein Abruf, ein Stand** – Channel und Web-Seite zeigen immer dasselbe.
- Ein Problem mit der Web-Seite (z.B. Port belegt) stoppt den Bot nicht: Er meldet es im Log
  und läuft ohne Web-Seite weiter. Mit `WEB_ENABLED=false` lässt sie sich ganz abschalten.
- Die Bilder kommen direkt von UEX (`url_photo`) und werden im Browser der Besucher von dort
  geladen. Fehlt ein Bild oder lädt es nicht, zeigt die Karte einen Platzhalter. Die
  Bildqualität hängt also davon ab, was UEX liefert.
- Die Seite ist **öffentlich** erreichbar, aber rein lesend: keine Logins, keine gespeicherten
  Eingaben, und Zugangsdaten (Token & Co.) werden nie ausgeliefert.

### Ansehen

- **Zusammen mit dem Bot:** `python bot.py` starten und <http://localhost:8080> öffnen.
- **Nur die Seite, ohne Discord:** `python webapp.py`. Das braucht keinen `DISCORD_TOKEN` und
  holt die Daten selbst von UEX (praktisch zum Ausprobieren und für Design-Anpassungen).
  Nicht gleichzeitig mit dem Bot starten, beide wollen denselben Port.

### Einstellungen (`.env`, alle optional)

| Variable | Bedeutung |
|---|---|
| `WEB_ENABLED` | `true` (Standard) oder `false`: Web-Seite zusammen mit dem Bot starten oder nicht |
| `PORT` | Port der Web-Seite, Standard `8080` (auf Fly.io identisch zu `internal_port` in der `fly.toml`) |
| `WEB_URL` | Öffentliche Adresse, z.B. `https://meine-app.fly.dev`. Wenn gesetzt, verlinkt das Kopf-Embed im Channel die Seite |
| `RSI_IMAGES` | `missing` (Standard), `all` oder `off`: Schiffsbilder von der offiziellen RSI-Seite ergänzen (siehe unten) |

### Schiffsbilder von der RSI-Seite

Die Bilder kommen standardmäßig von UEX. Zusätzlich liest der Bot zu jedem Schiff die Pledge-Store-Seite
auf `robertsspaceindustries.com` (die Adresse liefert UEX) und übernimmt deren Vorschaubild (`og:image`).

- `RSI_IMAGES=missing` (Standard): Lücken werden aufgefüllt; bei vorhandenem UEX-Bild springt das
  RSI-Bild ein, falls es nicht lädt.
- `RSI_IMAGES=all`: das RSI-Bild hat Vorrang.
- `RSI_IMAGES=off`: nur UEX-Bilder, es wird nichts bei RSI abgerufen.

Rücksicht auf RSI: Es gibt keine offizielle Bild-API, deshalb wird nur streng nacheinander mit Pause und
höchstens 40 Seiten pro Update-Zyklus abgerufen, jede Seite nur einmal (Ergebnis liegt in
`data/rsi_images.json`). Bei einer Blockade (HTTP 403/429) bricht der Bot sofort ab. Die Bilder lädt der
Browser der Besucher direkt von RSI. Fehlt ein Bild, bleibt alles wie vorher. Die Seite nennt Cloud Imperium
Games im Fußbereich als Urheber; die Nutzungsbedingungen von CIG für Fan-Projekte gelten für dich.
Ob es klappt, siehst du im Log: `RSI-Bilder: ... Bilder gefunden`.

## Hub-Seiten: Handel, Universum, Missionen, Konto

Außer dem Schiffs-Showcase hat die Seite weitere Bereiche. Sie laufen komplett über den eigenen
Server (`hubapi.py` und `accounts.py`, im selben Prozess wie der Bot) - der Browser spricht nur
noch mit `/api/...` der eigenen Seite und nie direkt mit Drittanbietern.

| Bereich | Quelle | Hinweis |
|---|---|---|
| **Handel & Industrie** (Marktbewegungen, Handelsrouten) | UEX Corp (`commodities`, `commodities_prices_all`) | Routen werden serverseitig berechnet: Kaufpreis am Terminal A, Verkaufspreis am Terminal B, begrenzt durch Fracht, Kapital und Vorrat. Eine Schätzung, keine Garantie. |
| **Universum** (Ortssuche, Systeme) | UEX Corp (Systeme, Planeten, Monde, Stationen, Städte, Außenposten, POIs) | UEX liefert keine Koordinaten. Die Karte zeigt deshalb eine schematische Übersicht der Treffer, keine maßstäblichen Positionen und keine Flugzeiten. |
| **Mission Control** (Missionen aus dem Datenfeed) | redaktioneller Auftragskatalog | Es gibt keine öffentliche Live-Liste von Verträgen. Der Katalog beschreibt Auftragstypen ohne erfundene Belohnungen und ist als solcher gekennzeichnet. Optional kann `MISSIONS_FEED_URL` einen eigenen JSON-Feed einbinden. |
| **News** (Comm-Links) | Star Citizen Wiki API | Server-Cache 10 Minuten. |
| **Mining-Rechner** (Erz-Voreinstellungen) | UEX Corp | Roh- und Raffinerie-Preise aus den Warenpreisen. |
| **Pilotprofil, Konto, Sync, Crew Board, Events, Organisationsverzeichnis** | eigene SQLite-Datenbank `data/hub.db` | Siehe unten. |

Alle UEX-Daten werden 15 Minuten (Orte 6 Stunden) zwischengespeichert. Fällt UEX aus, bleibt der letzte
Stand sichtbar; gibt es noch gar keinen, antwortet die Seite mit einer klaren Fehlermeldung statt leer.

### Konto & Community (mitgeliefertes Backend)

- **Konto:** Registrieren/Anmelden mit E-Mail oder Pilotname und Passwort (8-128 Zeichen). Passwörter
  werden mit scrypt und eigenem Salz gehasht, nie im Klartext gespeichert. Die Sitzung liegt in einem
  `HttpOnly`-Cookie (`SameSite=Lax`, auf HTTPS zusätzlich `Secure`); in der Datenbank steht nur der Hash des Tokens.
- **Schutz:** höchstens 20 Anmelde-/Registrierversuche pro IP und 10 Minuten, Herkunftsprüfung bei
  schreibenden Anfragen (CSRF), Größenlimit für Anfragen, Eingaben werden gekürzt und bereinigt.
- **Sync:** Hangar, Builds, Routen, Einsatzplan und Fortschritt werden mit dem Konto geräteübergreifend gespeichert.
  Ohne Anmeldung bleibt alles wie bisher lokal im Browser.
- **Crew Board & Events:** lesen darf jeder, veröffentlichen/teilnehmen nur angemeldete Piloten; löschen nur der Ersteller.
  Jede Person kann höchstens 20 Gesuche und 20 Events gleichzeitig haben; volle Events nehmen keine Zusagen mehr an.
- **Organisationsverzeichnis:** zeigt nur Profile, bei denen die Person selbst eine Organisation eingetragen hat.
  E-Mail-Adressen werden nie ausgeliefert.
- **Daten:** Die Datenbank liegt im Datenverzeichnis (`DATA_DIR`, auf Fly.io im Volume) und überlebt Neustarts.
  Sichere `data/hub.db` mit, wenn du Konten behalten willst.

### Tests

```bash
python test_hub.py        # Konto, Profil, Sync, Community, Handel, Orte, Erze, Missionen, Schutzmechanismen
python test_smoke.py      # Schiffsdaten, Embeds, Web-API
python -m pytest test_phase2a.py
```

## Hosting auf Fly.io

Der Bot muss durchgehend laufen, um Änderungen mitzubekommen und auf
Slash-Commands zu reagieren. Das Projekt ist fertig für **Fly.io** vorbereitet
(`Dockerfile` + `fly.toml`) und läuft dort als Hintergrund-Prozess, der nebenbei die Web-Seite ausliefert
(Port 8080, siehe [Web-Seite auf Fly.io](#web-seite-auf-flyio)).

### Wichtig: Secrets bleiben aus GitHub raus

Der Discord-Token & Co. werden **nie** ins Repository committet und landen
auch **nicht** in GitHub Actions als Klartext. Es gibt zwei getrennte Stellen:

1. **`fly secrets`** – die eigentlichen Bot-Zugangsdaten (`DISCORD_TOKEN`,
   `CHANNEL_ID`, ...). Diese setzt du **einmalig direkt bei Fly.io** (siehe
   unten) – sie erscheinen nirgends in GitHub.
2. **`FLY_API_TOKEN`** – ein einziges GitHub-Secret, das GitHub Actions nur
   erlaubt, ein Deployment auf Fly.io auszulösen. Es hat keinen Zugriff auf
   deinen Discord-Bot-Token.

`.env`, `.git`, `data/` etc. sind zusätzlich über `.dockerignore` und
`.gitignore` ausgeschlossen, damit nichts davon versehentlich ins
Docker-Image oder ins Repo gelangt.

### Einmalige Einrichtung

```bash
# flyctl installieren (falls noch nicht vorhanden): https://fly.io/docs/flyctl/install/
fly auth login

# Im Projektordner: erstellt die App bei Fly.io (fly.toml ist schon vorbereitet,
# bei der Frage nach einer bestehenden fly.toml also "ja, übernehmen" wählen)
fly launch --no-deploy

# Persistentes Volume für Nachrichten-IDs/Preisstand anlegen (Name muss zu
# [[mounts]] source in der fly.toml passen, Region an deine primary_region anpassen)
fly volumes create sc_showcase_data --region fra --size 1

# Bot-Secrets direkt bei Fly.io setzen (NICHT in GitHub!)
fly secrets set DISCORD_TOKEN=dein-token CHANNEL_ID=123456789012345678
# optional zusätzlich: GUILD_ID=..., ADMIN_ROLE_ID=..., UEX_API_KEY=...,
#                       UPDATE_INTERVAL_MINUTES=60, INCLUDE_GROUND_VEHICLES=true

# Erstes Deployment
fly deploy
```

### Automatisches Deployment über GitHub Actions

In `.github/workflows/fly-deploy.yml` liegt bereits ein Workflow, der bei
jedem Push auf `main` automatisch `fly deploy` ausführt. Dafür brauchst du in
GitHub nur **ein** Secret:

1. Fly.io-API-Token erzeugen: `fly tokens create deploy -x 999999h` (oder im
   Fly.io-Dashboard unter "Account" → "Access Tokens").
2. In GitHub: Repo → **Settings** → **Secrets and variables** → **Actions** →
   **New repository secret** → Name `FLY_API_TOKEN`, Wert einfügen.

Ab jetzt reicht `git push` auf `main`, damit die neue Version automatisch auf
Fly.io läuft – ohne dass irgendein Bot-Secret jemals in GitHub sichtbar war.

### Logs & Status

```bash
fly logs          # laufende Logs ansehen
fly status         # Status der Maschine(n) prüfen
fly secrets list   # gesetzte Secret-Namen (nicht die Werte) einsehen
```

### Web-Seite auf Fly.io

Die Web-Seite braucht **keine zusätzliche App**: Sie läuft in derselben Maschine wie der Bot,
die `fly.toml` enthält dafür den Block `[http_service]`. Nach dem nächsten `fly deploy` (bzw.
dem Push auf `main`) ist sie unter `https://<app-name>.fly.dev` erreichbar. Beim ersten Deploy
mit HTTP-Dienst vergibt Fly der App dafür automatisch eine öffentliche Adresse.

Zwei Dinge sind dabei bewusst so eingestellt – bitte nicht ändern:

- `auto_stop_machines = false` und `auto_start_machines = false`: Fly darf die Maschine nicht
  anhand des Web-Traffics stoppen oder starten, sonst wäre der Bot offline, sobald gerade
  niemand die Seite besucht.
- **Nur eine Maschine.** Der Bot darf nicht doppelt laufen (er würde doppelt posten). Nicht
  hochskalieren (`fly scale count` o.ä.); `fly status` sollte genau eine laufende Maschine zeigen.

Tipp: In der `fly.toml` unter `[env]` die (auskommentierte) Zeile `WEB_URL` mit deiner Adresse
aktivieren, dann verlinkt das Kopf-Embed im Discord-Channel die Seite.

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| `DISCORD_TOKEN fehlt` beim Start | `.env` prüfen, wurde sie aus `.env.example` kopiert und ausgefüllt? |
| Bot startet, aber postet nichts | Stimmt die `CHANNEL_ID`? Hat der Bot "Nachrichten senden" + "Embeds einfügen" im Ziel-Channel? |
| Slash-Commands erscheinen nicht | Ohne `GUILD_ID` kann das bis zu 1 Stunde dauern (Discord-seitige Verzögerung). `GUILD_ID` setzen für sofortige Sync. |
| `Login fehlgeschlagen` | Token falsch/abgelaufen – im Developer Portal unter "Bot" neu generieren. |
| "UEX-API ist gerade nicht erreichbar" | UEX hat gelegentlich kurze Downtimes; der Bot versucht es beim nächsten Zyklus automatisch erneut. |
| Web-Seite lokal nicht erreichbar | Läuft `python bot.py`? Im Log steht dann "Web-Seite läuft auf Port 8080". Steht dort "konnte nicht gestartet werden", ist der Port belegt (läuft noch `python webapp.py`?). `PORT` in der `.env` ändern. |
| Web-Seite auf Fly.io nicht erreichbar | Nach dem Deploy `fly status` und `fly ips list` prüfen. `internal_port` in der `fly.toml` muss zu `PORT` passen. |
| Web-Seite zeigt "Schiffsdaten gerade nicht erreichbar" | Der Bot hatte noch keinen erfolgreichen UEX-Abruf (frisch gestartet oder UEX war down). Sobald der erste Abruf klappt, lädt sich die Seite von selbst nach. |
| Handel/Universum/News zeigen "nicht erreichbar" | Im Log nach `Abruf '...' fehlgeschlagen` suchen: UEX oder die Wiki-API war nicht erreichbar. Beim nächsten erfolgreichen Abruf füllt sich die Seite von selbst. Die Zeile `Marktdaten geladen: ...` im Log zeigt, was UEX geliefert hat. |
| Konto erstellen schlägt fehl | Die Datenbank liegt in `DATA_DIR` (auf Fly.io `/app/data`). Das Volume muss existieren und beschreibbar sein (`fly volumes list`). |
| Web-Seite zeigt "Stand evtl. veraltet" | Der letzte UEX-Abruf ist fehlgeschlagen, gezeigt wird der letzte gute Stand. Erholt sich beim nächsten erfolgreichen Abruf von selbst. |

## Mögliche Erweiterungen (nicht enthalten, aber gut ergänzbar)

- Zusätzliche Anzeige der Pledge-Preise (Echtgeld) zum Vergleich
- Preisverlauf als Diagramm über die Zeit
- Rollen-Ping bei bestimmten Preisänderungen (z.B. nur bei Schiffen einer Wunschliste)
- `/vergleich <schiff1> <schiff2>` zum direkten Preisvergleich zweier Schiffe
- Eigener Bild-Cache bzw. -Proxy für die Schiffsbilder, statt dass jeder Besucher direkt bei UEX lädt

## Projektstruktur

```
bot.py                          Haupt-Bot: Discord-Verbindung, Update-Loop, Slash-Commands
uex_api.py                      Anbindung an die UEX-Corp-API + Datenmodell
embeds.py                       Baut die Discord-Embeds (Showcase-Optik, /liste-Seiten)
storage.py                      Lokale JSON-Persistenz (Nachrichten-IDs, letzter Preisstand)
config.py                       Lädt/prüft die .env-Konfiguration
webapp.py                       Web-Seite: Webserver, Sicherheits-Header, Rate-Limit, /api/ships; läuft im Bot-Prozess (oder allein: `python webapp.py`)
hubapi.py                       Daten-API /api/v1/...: Markt, Handelsrouten, Orte, Erze, Missionen, News (UEX, mit Cache)
accounts.py                     Backend: Konto, Profil, Sync, Crew Board, Events, Organisationsverzeichnis (SQLite)
static/index.html               Die Web-Seite selbst (HTML/CSS/JS in einer Datei)
test_hub.py                     Tests für Konto-Backend und Hub-Daten-API (`python test_hub.py`)
test_smoke.py                   Test mit echten Beispieldaten, unabhängig von Discord/UEX (`python test_smoke.py`)
Dockerfile                      Container-Image für Fly.io
fly.toml                        Fly.io-App-Konfiguration (Bot + Web-Seite auf einer Maschine, Volume, HTTP-Port 8080)
.dockerignore                   Hält .env/.git/data aus dem Docker-Image raus
.github/workflows/fly-deploy.yml  GitHub Actions: automatisches `fly deploy` bei Push auf main
```
