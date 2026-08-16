"""Sprach-Pakete (Language Packs).

Jede Sprache ist eine JSON-Datei in ./lang/<code>.json mit key -> Text.
Neue Sprache = neue Datei ablegen; sie erscheint automatisch in der Auswahl.
Der Schluessel "_name" enthaelt den Anzeigenamen der Sprache.
"""
import json
from pathlib import Path

LANG_DIR = Path(__file__).parent / "lang"
FALLBACK = "en"
_cache: dict = {}


def available() -> list:
    """Vorhandene Sprachcodes (Dateinamen ohne .json)."""
    langs = sorted(p.stem for p in LANG_DIR.glob("*.json"))
    return langs or ["en"]


def _load(lang: str) -> dict:
    if lang not in _cache:
        try:
            _cache[lang] = json.loads((LANG_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache[lang] = {}
    return _cache[lang]


def name(lang: str) -> str:
    return _load(lang).get("_name", lang)


def translator(lang: str):
    """Liefert eine Funktion t(key, **kwargs). Fallback: Englisch, dann key."""
    primary = _load(lang)
    fb = _load(FALLBACK) if lang != FALLBACK else {}

    def t(key: str, **kwargs) -> str:
        s = primary.get(key)
        if s is None:
            s = fb.get(key, key)
        if kwargs:
            try:
                return s.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return s
        return s

    return t
