"""AI recommendation pipeline: profile → EPG candidates → Ollama → ranked list."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import timedelta
from sqlalchemy.orm import Session
from app.models import EpgEvent, Channel, RecommendationCache, ViewingSession, UserLike
from app.services.profile import get_profile
from app.services.ollama import ask_json
from app.services.channels import get_channels_for_user
from app.timezones import utcnow, to_local_str, prime_range, today_remaining_range
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)

_SHOPPING_RE = re.compile(r"teleshopping|qvc|home\s*shop|pearl|beate uhse", re.I)
_NEWS_GENRES = {"nachrichten", "wetter", "werbung", "news", "weather", "magazine"}
_FILM_GENRES = {
    "movie", "film", "spielfilm", "krimi", "drama", "doku", "dokumentation",
    "thriller", "komödie", "comedy", "action", "horror", "sci-fi", "romance",
}
_MAJOR_CHANNELS = {
    "ard", "zdf", "pro7", "prosieben", "sat.1", "rtl", "arte", "3sat",
    "kabel eins", "kabel1", "vox", "rtl2", "rtl zwei",
}
_CACHE_TTL = {"now": 30, "next": 30, "prime": 120, "today": 360}


def _cache_key(user_id: int, context: str) -> str:
    return hashlib.sha1(f"{user_id}:{context}".encode()).hexdigest()


def _epg_candidates(context: str, channel_ids: list[int], db: Session) -> list[EpgEvent]:
    if not channel_ids:
        return []
    now = utcnow()
    q = db.query(EpgEvent).filter(EpgEvent.channel_id.in_(channel_ids))

    if context == "now":
        q = q.filter(EpgEvent.start_time <= now, EpgEvent.end_time > now)
    elif context == "next":
        q = q.filter(EpgEvent.start_time >= now, EpgEvent.start_time <= now + timedelta(hours=2))
    elif context == "prime":
        start_utc, end_utc = prime_range()
        q = q.filter(EpgEvent.start_time >= start_utc, EpgEvent.start_time < end_utc)
    elif context == "today":
        start_utc, end_utc = today_remaining_range()
        q = q.filter(EpgEvent.start_time >= start_utc, EpgEvent.start_time < end_utc)
    else:
        return []

    return q.order_by(EpgEvent.start_time).limit(100).all()


def rule_based_rank(candidates: list[EpgEvent], context: str, db: Session) -> list[dict]:
    scored = []
    for ev in candidates:
        ch = db.get(Channel, ev.channel_id)
        if not ch:
            continue
        if _SHOPPING_RE.search(ch.name):
            continue
        dur = ev.duration_sec or 0
        if context in ("prime", "today") and dur < 1200:
            continue

        score = 0.0
        genre_lower = (ev.genre or "").lower()
        if any(g in genre_lower for g in _NEWS_GENRES):
            score -= 0.4
        if any(g in genre_lower for g in _FILM_GENRES):
            score += 0.3
        if dur >= 2700:
            score += 0.2
        if ch.name.lower() in _MAJOR_CHANNELS:
            score += 0.2

        scored.append((score, ev, ch))

    scored.sort(key=lambda x: -x[0])
    return [
        {
            "id": ev.id,
            "sref": ch.sref,
            "channel_name": ch.name,
            "title": ev.title,
            "short_desc": ev.short_desc,
            "long_desc": ev.long_desc,
            "start_time": to_local_str(ev.start_time),
            "end_time": to_local_str(ev.end_time),
            "genre": ev.genre,
            "match_score": round(max(0.0, min(1.0, 0.5 + score)), 2),
            "reason": "Populärer Sender und passende Sendezeit",
        }
        for score, ev, ch in scored[:8]
    ]


def _get_recent_likes(user_id: int, db: Session, limit: int = 15) -> list[dict]:
    rows = (
        db.query(UserLike)
        .filter(UserLike.user_id == user_id)
        .order_by(UserLike.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {"title": r.title, "channel": r.channel_name or "?", "genre": r.genre}
        for r in rows
    ]


def _get_recent_history(user_id: int, db: Session) -> list[dict]:
    cutoff = utcnow() - timedelta(days=30)
    sessions = (
        db.query(ViewingSession)
        .filter(
            ViewingSession.user_id == user_id,
            ViewingSession.confirmed == True,  # noqa: E712
            ViewingSession.started_at >= cutoff,
        )
        .order_by(ViewingSession.started_at.desc())
        .limit(20)
        .all()
    )
    history = []
    for s in sessions:
        ch = db.get(Channel, s.channel_id)
        ev = db.get(EpgEvent, s.epg_event_id) if s.epg_event_id else None
        history.append({
            "title": ev.title if ev else "?",
            "channel": ch.name if ch else "?",
            "genre": ev.genre if ev else None,
            "duration_min": (s.duration_sec or 0) / 60,
        })
    return history


def _build_prompt(
    user_name: str, context: str, profile: dict,
    history: list[dict], likes: list[dict], candidates: list[EpgEvent], db: Session,
) -> str:
    ctx_labels = {
        "now": "gerade jetzt läuft",
        "next": "in den nächsten 2 Stunden",
        "prime": "Primetime heute Abend",
        "today": "heute noch",
    }
    ctx_label = ctx_labels.get(context, context)

    stated_prefs = profile.get("stated_preferences") or ""
    genres_str = ", ".join(
        f"{g['genre']} ({g['share']*100:.0f}%)"
        for g in profile.get("top_genres", [])[:5]
        if g["genre"] != "unknown"
    )
    channels_str = ", ".join(g["name"] for g in profile.get("top_channels", [])[:5])

    hist_lines = [
        f"  - {h.get('title','?')} | {h.get('channel','?')} | {h.get('genre') or 'unbekannt'} | {h.get('duration_min',0):.0f} min"
        for h in history[:20]
    ]
    hist_str = "\n".join(hist_lines) if hist_lines else "  (keine Historie)"

    likes_lines = [
        f"  - {l.get('title','?')} | {l.get('channel','?')} | {l.get('genre') or 'unbekannt'}"
        for l in likes
    ]
    likes_str = "\n".join(likes_lines) if likes_lines else "  (keine)"

    cand_lines = []
    for i, ev in enumerate(candidates):
        ch = db.get(Channel, ev.channel_id)
        if not ch:
            continue
        dur_min = (ev.duration_sec or 0) // 60
        desc = (ev.short_desc or "").replace("\n", " ").strip()[:80]
        desc_part = f" | {desc}" if desc else ""
        cand_lines.append(
            f"  [{i+1}] {ch.name} | {ev.title} | "
            f"{to_local_str(ev.start_time)}–{to_local_str(ev.end_time)} | "
            f"{ev.genre or '-'} | {dur_min} min{desc_part}"
        )
    cand_str = "\n".join(cand_lines) if cand_lines else "  (keine Sendungen)"

    prefs_section = f"\nEXPLIZITE VORLIEBEN (vom Nutzer angegeben):\n{stated_prefs}\n" if stated_prefs else ""

    return f"""Du bist ein TV-Empfehlungssystem für {user_name}. Antworte NUR mit gültigem JSON ohne Markdown.

AUFGABE: Wähle aus der nummerierten KANDIDATENLISTE die passendsten Sendungen für {user_name} aus.
Antworte ausschließlich mit den Nummern aus der Liste (keine neuen Sendungen erfinden).
{prefs_section}
NUTZERPROFIL ({profile.get('session_count', 0)} Sitzungen, letzte 30 Tage):
- Lieblingsgenres: {genres_str or 'unbekannt'}
- Häufigste Sender: {channels_str or 'unbekannt'}
- Ø Sehdauer: {profile.get('avg_duration_min', 0):.0f} min

EXPLIZIT GEMERKT (👍 markiert, starkes positives Signal):
{likes_str}

LETZTE SEHHISTORIE:
{hist_str}

KANDIDATENLISTE:
{cand_str}

JSON-Antwort (genau dieses Format, keine anderen Felder):
{{"taste_summary": "Ein Satz über {user_name}s Geschmack", "ranking": [nr1, nr2, nr3, ...], "reasons": {{"nr": "Ein Satz Begründung"}}}}

Regeln: 5–8 Nummern aus der Liste, nach Passgenauigkeit absteigend."""


async def _try_ollama(
    user_name: str, context: str, profile: dict,
    history: list[dict], likes: list[dict], candidates: list[EpgEvent], db: Session,
) -> dict | None:
    """Ask LLM to rank candidate indices, then fill in metadata server-side."""
    prompt = _build_prompt(user_name, context, profile, history, likes, candidates, db)
    raw = await ask_json(prompt)
    if not raw or not isinstance(raw, dict) or "ranking" not in raw:
        log.warning("recommendations.bad_llm_response", raw_type=type(raw).__name__, keys=list(raw.keys()) if isinstance(raw, dict) else None)
        return None

    # Build candidate lookup by 1-based index
    indexed: dict[int, tuple[EpgEvent, object]] = {}
    for i, ev in enumerate(candidates):
        ch = db.get(Channel, ev.channel_id)
        if ch:
            indexed[i + 1] = (ev, ch)

    reasons: dict = raw.get("reasons") or {}
    items = []
    for nr in raw["ranking"]:
        try:
            nr_int = int(nr)
        except (TypeError, ValueError):
            continue
        if nr_int not in indexed:
            continue
        ev, ch = indexed[nr_int]
        score = (len(raw["ranking"]) - len(items)) / len(raw["ranking"])
        items.append({
            "id": ev.id,
            "sref": ch.sref,
            "channel_name": ch.name,
            "title": ev.title,
            "short_desc": ev.short_desc,
            "long_desc": ev.long_desc,
            "start_time": to_local_str(ev.start_time),
            "end_time": to_local_str(ev.end_time),
            "genre": ev.genre,
            "match_score": round(score, 2),
            "reason": reasons.get(str(nr_int), reasons.get(str(nr), "")),
        })

    if not items:
        return None
    return {
        "taste_summary": raw.get("taste_summary", ""),
        "recommendations": items,
    }


def _save_cache(user_id: int, context: str, result: dict, ttl_min: int, db: Session) -> None:
    now = utcnow()
    key = _cache_key(user_id, context)
    db.query(RecommendationCache).filter(
        RecommendationCache.user_id == user_id,
        RecommendationCache.prompt_hash == key,
    ).delete()
    db.add(RecommendationCache(
        user_id=user_id,
        generated_at=now,
        valid_until=now + timedelta(minutes=ttl_min),
        prompt_hash=key,
        response=json.dumps(result),
    ))
    db.commit()


async def get_recommendations(user_id: int, user_name: str, context: str, db: Session) -> dict:
    now = utcnow()
    key = _cache_key(user_id, context)

    cached = (
        db.query(RecommendationCache)
        .filter(
            RecommendationCache.user_id == user_id,
            RecommendationCache.prompt_hash == key,
            RecommendationCache.valid_until > now,
        )
        .order_by(RecommendationCache.generated_at.desc())
        .first()
    )
    if cached:
        data = json.loads(cached.response)
        data["cached"] = True
        return data

    channel_ids = [c.id for c in get_channels_for_user(user_id, db)]
    candidates = _epg_candidates(context, channel_ids, db)

    profile = get_profile(user_id, db)
    # Skip cold-start gate when stated preferences or any likes are set — AI has enough context.
    like_count = db.query(UserLike).filter_by(user_id=user_id).count()
    cold_start = (
        profile.get("session_count", 0) < 10
        and not profile.get("stated_preferences")
        and like_count == 0
    )

    ai_result = None
    if not cold_start and candidates:
        history = _get_recent_history(user_id, db)
        likes = _get_recent_likes(user_id, db)
        ai_result = await _try_ollama(user_name, context, profile, history, likes, candidates, db)

    if ai_result is not None:
        result = {
            "context": context,
            "user_name": user_name,
            "taste_summary": ai_result.get("taste_summary", ""),
            "recommendations": ai_result.get("recommendations", []),
            "cached": False,
            "cold_start": False,
        }
    else:
        recs = rule_based_rank(candidates, context, db)
        result = {
            "context": context,
            "user_name": user_name,
            "taste_summary": (
                "Noch zu wenig Daten — zeige populäre Sendungen"
                if cold_start
                else "KI nicht verfügbar — Empfehlungen nach Regeln"
            ),
            "recommendations": recs,
            "cached": False,
            "cold_start": cold_start,
        }

    _save_cache(user_id, context, result, _CACHE_TTL.get(context, 30), db)
    return result
