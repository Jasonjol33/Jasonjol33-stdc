"""Lädt und validiert die Konfiguration aus Umgebungsvariablen (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    discord_token: str
    channel_id: int
    guild_id: Optional[int]
    update_interval_minutes: int
    uex_api_key: Optional[str]
    include_ground_vehicles: bool
    admin_role_id: Optional[int]
    # Web-Seite (läuft im selben Prozess wie der Bot, siehe webapp.py).
    # Mit Standardwerten, damit bestehender Code, der Config direkt baut, weiter funktioniert.
    web_enabled: bool = True
    web_port: int = 8080
    web_url: Optional[str] = None
    # Schiffsbilder von der offiziellen RSI-Seite: "missing" (Standard), "all" oder "off".
    rsi_images_mode: str = "missing"


@dataclass
class WebSettings:
    """Einstellungen für `python webapp.py` (ohne Bot) - braucht KEINEN DISCORD_TOKEN."""

    port: int
    uex_api_key: Optional[str]
    include_ground_vehicles: bool
    update_interval_minutes: int


def _int_or_none(raw: str) -> Optional[int]:
    raw = raw.strip()
    return int(raw) if raw.isdigit() else None


def _update_interval() -> int:
    interval_raw = os.getenv("UPDATE_INTERVAL_MINUTES", "60").strip()
    try:
        # Untergrenze von 15 Minuten, um die kostenlose UEX-API nicht unnötig zu belasten
        # (die zugrunde liegenden In-Game-Preise ändern sich laut UEX ohnehin nur stündlich).
        return max(15, int(interval_raw))
    except ValueError:
        return 60


def _web_enabled() -> bool:
    return os.getenv("WEB_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")


def _web_port() -> int:
    port = _int_or_none(os.getenv("PORT", ""))
    return port if port and 1 <= port <= 65535 else 8080


def _web_url() -> Optional[str]:
    url = os.getenv("WEB_URL", "").strip().rstrip("/")
    if not url:
        # Auf Fly.io setzt die Plattform FLY_APP_NAME automatisch - daraus lässt sich die
        # öffentliche Adresse ableiten, auch wenn WEB_URL nicht gesetzt wurde.
        fly_app = os.getenv("FLY_APP_NAME", "").strip()
        if fly_app:
            url = f"https://{fly_app}.fly.dev"
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url  # "meine-app.fly.dev" ohne Schema ergäbe in Discord keinen klickbaren Link
    return url or None


def _rsi_images_mode() -> str:
    raw = os.getenv("RSI_IMAGES", "missing").strip().lower()
    if raw in ("off", "false", "0", "no"):
        return "off"
    if raw in ("all", "prefer"):
        return "all"
    return "missing"


def load_config() -> Config:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN fehlt. Bitte in der .env-Datei eintragen "
            "(siehe README.md, Schritt 'Bot erstellen')."
        )

    channel_id = _int_or_none(os.getenv("CHANNEL_ID", ""))
    if channel_id is None:
        raise RuntimeError(
            "CHANNEL_ID fehlt oder ist ungültig. Bitte die Discord-Channel-ID "
            "(nur Ziffern) in der .env eintragen."
        )

    return Config(
        discord_token=token,
        channel_id=channel_id,
        guild_id=_int_or_none(os.getenv("GUILD_ID", "")),
        update_interval_minutes=_update_interval(),
        uex_api_key=os.getenv("UEX_API_KEY", "").strip() or None,
        include_ground_vehicles=os.getenv("INCLUDE_GROUND_VEHICLES", "false").strip().lower() == "true",
        admin_role_id=_int_or_none(os.getenv("ADMIN_ROLE_ID", "")),
        web_enabled=_web_enabled(),
        web_port=_web_port(),
        web_url=_web_url(),
        rsi_images_mode=_rsi_images_mode(),
    )


def load_web_settings() -> WebSettings:
    """Für den eigenständigen Betrieb `python webapp.py`: nur die Web-relevanten Werte."""
    return WebSettings(
        port=_web_port(),
        uex_api_key=os.getenv("UEX_API_KEY", "").strip() or None,
        include_ground_vehicles=os.getenv("INCLUDE_GROUND_VEHICLES", "false").strip().lower() == "true",
        update_interval_minutes=_update_interval(),
    )
