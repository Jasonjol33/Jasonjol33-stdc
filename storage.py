"""Einfache JSON-Datei-Persistenz für Nachrichten-IDs und den letzten bekannten
Preisstand (wird für den Live-Änderungsvergleich bei jedem Update-Zyklus gebraucht).

Für einen Bot dieser Größenordnung reicht eine lokale JSON-Datei völlig aus -
eine echte Datenbank wäre hier unnötiger Overhead. Dank atomarem Schreiben
(Schreiben in eine .tmp-Datei + Umbenennen) übersteht die Datei auch einen
Absturz mitten im Speichervorgang unbeschadet.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Dict

log = logging.getLogger("storage")

# Über DATA_DIR (Env-Var) überschreibbar, z.B. für das persistente Fly.io-Volume
# unter /app/data (siehe fly.toml -> [mounts]). Standard: lokaler "data"-Ordner.
DATA_DIR = Path(os.getenv("DATA_DIR", "").strip() or (Path(__file__).parent / "data"))
MESSAGES_FILE = DATA_DIR / "messages.json"
SNAPSHOT_FILE = DATA_DIR / "last_prices.json"

_lock = Lock()


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> dict:
    _ensure_data_dir()
    if not path.exists():
        return {}
    with _lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Konnte %s nicht lesen (%s) - starte mit leerem Stand.", path, exc)
            return {}


def _save_json(path: Path, data: dict) -> None:
    _ensure_data_dir()
    with _lock:
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)


def load_message_ids() -> Dict[str, int]:
    """{'<Hersteller>::<Chunk-Index>': message_id}"""
    return _load_json(MESSAGES_FILE)


def save_message_ids(mapping: Dict[str, int]) -> None:
    _save_json(MESSAGES_FILE, mapping)


def load_last_prices() -> Dict[str, float]:
    """{'<vehicle_id>': cheapest_price_buy}"""
    return _load_json(SNAPSHOT_FILE)


def save_last_prices(snapshot: Dict[str, float]) -> None:
    _save_json(SNAPSHOT_FILE, snapshot)


RSI_IMAGES_FILE = DATA_DIR / "rsi_images.json"


def load_rsi_images() -> Dict[str, dict]:
    """{'<Store-URL>': {'image': '<Bild-URL oder None>', 'checked': '<ISO-Zeitstempel>'}}"""
    return _load_json(RSI_IMAGES_FILE)


def save_rsi_images(mapping: Dict[str, dict]) -> None:
    _save_json(RSI_IMAGES_FILE, mapping)
