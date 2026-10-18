"""Tests für Phase 2a (pytest): Entkopplung von Discord, /healthz, Sicherheits-Header, Rate-Limit.

Ausführen:  python -m pytest -q test_phase2a.py
Benötigt die Pakete aus requirements.txt (aiohttp, discord.py, python-dotenv) und pytest.
"""

from __future__ import annotations

import asyncio

import pytest

import config as cfg_module
from config import load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("DISCORD_TOKEN", "CHANNEL_ID"):
        monkeypatch.delenv(k, raising=False)


# --- Config: Discord bleibt Pflicht, wenn es verlangt wird -----------------------------------

def test_strict_config_still_requires_token():
    with pytest.raises(RuntimeError):
        load_config()


def test_strict_config_still_requires_channel(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    with pytest.raises(RuntimeError):
        load_config()


def test_lenient_config_without_discord_is_web_only():
    c = load_config(require_discord=False)
    assert c.discord_enabled is False and c.discord_token == "" and c.channel_id == 0


def test_with_credentials_discord_is_enabled(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "abc")
    monkeypatch.setenv("CHANNEL_ID", "123")
    for strict in (True, False):
        c = load_config(require_discord=strict)
        assert c.discord_enabled is True and c.channel_id == 123 and c.discord_token == "abc"


def test_token_without_channel_is_not_enabled_in_lenient_mode(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "abc")
    assert load_config(require_discord=False).discord_enabled is False


# --- bot.py: Import ohne Discord-Daten, main() wählt den richtigen Modus --------------------

def test_bot_imports_without_discord_credentials():
    import bot  # darf nicht mehr mit RuntimeError abbrechen

    assert bot.config.discord_enabled is False


def test_main_runs_bot_when_discord_configured(monkeypatch):
    import bot

    calls = []
    monkeypatch.setattr(bot.config, "discord_token", "abc")
    monkeypatch.setattr(bot.config, "channel_id", 5)
    monkeypatch.setattr(bot.bot, "run", lambda token, **kw: calls.append(("bot", token)))
    monkeypatch.setattr(bot.webapp, "main", lambda: calls.append(("web-only",)))
    bot.main()
    assert calls == [("bot", "abc")]  # Discord-Bot wird gestartet, KEIN Web-only-Modus


def test_main_falls_back_to_web_only_without_credentials(monkeypatch):
    import bot

    calls = []
    monkeypatch.setattr(bot.config, "discord_token", "")
    monkeypatch.setattr(bot.config, "channel_id", 0)
    monkeypatch.setattr(bot.bot, "run", lambda *a, **k: calls.append("bot"))
    monkeypatch.setattr(bot.webapp, "main", lambda: calls.append("web-only"))
    bot.main()
    assert calls == ["web-only"]


def test_main_falls_back_to_web_only_on_login_failure(monkeypatch):
    import bot, discord

    calls = []

    def boom(*a, **k):
        raise discord.LoginFailure("ungültig")

    monkeypatch.setattr(bot.config, "discord_token", "abc")
    monkeypatch.setattr(bot.config, "channel_id", 5)
    monkeypatch.setattr(bot.bot, "run", boom)
    monkeypatch.setattr(bot.webapp, "main", lambda: calls.append("web-only"))
    bot.main()
    assert calls == ["web-only"]


# --- Web: /healthz, Header, Rate-Limit -------------------------------------------------------

def _run(coro_fn):
    from aiohttp.test_utils import TestClient, TestServer
    import webapp

    async def go():
        webapp._hits.clear()
        async with TestClient(TestServer(webapp.create_app())) as client:
            return await coro_fn(client, webapp)

    return asyncio.run(go())


def test_healthz_is_200_even_without_data():
    async def t(c, webapp):
        webapp.cache.__init__()  # leerer Zustand
        r = await c.get("/healthz")
        return r.status, await r.json()

    status, body = _run(t)
    assert status == 200 and body["status"] == "no_data" and body["ships"] == 0


def test_api_ships_is_503_without_data_but_healthz_ok():
    async def t(c, webapp):
        webapp.cache.__init__()
        return (await c.get("/api/ships")).status, (await c.get("/healthz")).status

    assert _run(t) == (503, 200)


def test_security_headers_present():
    async def t(c, webapp):
        r = await c.get("/healthz")
        return r.headers

    h = _run(t)
    for name in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options",
                 "Referrer-Policy", "Strict-Transport-Security"):
        assert name in h, name
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]


def test_rate_limit_blocks_after_limit_but_not_healthz():
    async def t(c, webapp):
        codes = [(await c.get("/api/ships")).status for _ in range(webapp.RATE_LIMIT + 5)]
        health = (await c.get("/healthz")).status
        return codes, health

    codes, health = _run(t)
    assert 429 in codes and codes[0] in (200, 503) and health == 200
