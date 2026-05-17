"""UI string translation service.

Priority per-language: curated static/i18n/{lang}.json > DB-cached AI > German fallback.
AI translation is triggered once as a background batch when a new language is first seen.
"""
from __future__ import annotations
import json
from pathlib import Path
from sqlalchemy.orm import Session
from app.models import Translation
from app.timezones import utcnow
from app.logging_setup import get_logger

log = get_logger(__name__)

_STATIC_DIR = Path("static/i18n")
_BATCH_JOBS: set[str] = set()  # in-memory: langs currently being translated by Ollama

_LANG_NAMES: dict[str, str] = {
    "en": "English", "fr": "French", "es": "Spanish", "it": "Italian",
    "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "ru": "Russian",
    "pt": "Portuguese", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "cs": "Czech", "sk": "Slovak", "hu": "Hungarian",
    "ro": "Romanian", "hr": "Croatian", "sl": "Slovenian",
}


def normalize(lang: str) -> str:
    """Normalise 'en-US' → 'en'. Returns '' if the code looks invalid."""
    code = (lang or "").split("-")[0].strip().lower()
    return code if code.isalpha() and 2 <= len(code) <= 3 else ""


def _source() -> dict:
    return json.loads((_STATIC_DIR / "de.json").read_text(encoding="utf-8"))


def _curated(lang: str) -> dict:
    path = _STATIC_DIR / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def get_translations(lang: str, db: Session) -> tuple[dict, list[str]]:
    """Return (merged string dict, list of keys still missing from the target lang).

    Merge order: curated file wins > DB-cached AI > German source fallback.
    """
    src = _source()
    if lang == "de":
        return src, []

    curated = _curated(lang)
    db_rows = {t.key: t.value for t in db.query(Translation).filter_by(lang=lang).all()}

    merged = dict(src)       # German fallback for anything missing
    merged.update(db_rows)   # AI-translated overrides
    merged.update(curated)   # Curated overrides all

    missing = [k for k in src if k not in curated and k not in db_rows]
    return merged, missing


def is_translating(lang: str) -> bool:
    return lang in _BATCH_JOBS


def _build_prompt(lang: str, to_translate: dict) -> str:
    lang_name = _LANG_NAMES.get(lang, lang)
    pairs = json.dumps(to_translate, ensure_ascii=False, indent=2)
    return (
        f"You are a UI translation engine. Translate the German UI strings below to {lang_name}.\n"
        "Output strict JSON only, no markdown.\n\n"
        "Rules:\n"
        "- Preserve {placeholders} like {receiver}, {lang}, {time}, {n}, {pct}, {target}, {method}, {error} exactly.\n"
        "- Preserve unicode glyphs: ★ ⚡ 💤 ▶ ↺ ⚠️ — ✓\n"
        "- Keep translations concise — these are UI labels, not prose.\n"
        f'- For "genre.*" keys, use the natural {lang_name} word for the German TV genre (e.g. genre.krimi → "Crime").\n'
        "- If a key has no good local equivalent, keep the German value unchanged.\n\n"
        f"Source (German):\n{pairs}\n\n"
        'Return:\n{"translations": {"key1": "translated value", ...}}'
    )


async def translate_missing(lang: str, missing: list[str]) -> None:
    """One-shot batch Ollama call. Opens its own DB session (runs as background task)."""
    if not missing or lang in _BATCH_JOBS:
        return
    _BATCH_JOBS.add(lang)
    try:
        from app.database import SessionLocal
        from app.services.ollama import ask_json

        src = _source()
        payload = {k: src[k] for k in missing if k in src}
        prompt = _build_prompt(lang, payload)
        log.info("i18n.batch_start", lang=lang, keys=len(payload))

        result = await ask_json(prompt, caller="i18n")
        if not isinstance(result, dict) or "translations" not in result:
            log.warning("i18n.batch_failed", lang=lang)
            return

        db = SessionLocal()
        try:
            now = utcnow()
            stored = 0
            for k, v in result["translations"].items():
                if k not in src or not isinstance(v, str) or not v.strip():
                    continue
                row = db.query(Translation).filter_by(lang=lang, key=k).first()
                if row:
                    row.value = v
                    row.generated_at = now
                else:
                    db.add(Translation(lang=lang, key=k, value=v,
                                       source="ai", generated_at=now))
                stored += 1
            db.commit()
            log.info("i18n.batch_done", lang=lang, stored=stored)
        finally:
            db.close()
    finally:
        _BATCH_JOBS.discard(lang)
