"""AI recommendation pipeline: profile → EPG candidates → Ollama → ranked list."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timedelta
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
_CACHE_TTL = {"now": 90, "next": 90, "prime": 120, "today": 360}


def _progress_pct(ev: EpgEvent, now) -> float:
    """How far into the event we are (0..100). 0 for not-yet-started/no-duration."""
    if not ev.duration_sec or ev.duration_sec <= 0:
        return 0.0
    elapsed = (now - ev.start_time).total_seconds()
    if elapsed <= 0:
        return 0.0
    return round(min(100.0, elapsed / ev.duration_sec * 100), 1)


# How much a fully-played show is penalised vs a fresh one (subtracted from match_score).
_PROGRESS_PENALTY = 0.4
# Drop items with less than this much time remaining when serving "now" recs.
_NOW_MIN_REMAINING_SEC = 600  # 10 min


def _realtime_adjust_now(result: dict) -> dict:
    """For 'now' recs served from cache: recompute progress, drop near-ended items,
    apply freshness penalty, re-sort. Lets us pre-warm the LLM cache and serve
    accurate progress without re-asking the model as shows tick on."""
    if result.get("context") != "now":
        return result
    items = result.get("recommendations") or []
    if not items:
        return result

    from app.timezones import utcnow as _utcnow
    now = _utcnow()
    adjusted = []
    for item in items:
        st = item.get("start_time"); et = item.get("end_time")
        if not st or not et:
            continue
        try:
            start = datetime.fromisoformat(st)
            end   = datetime.fromisoformat(et)
        except ValueError:
            continue
        remaining = (end - now).total_seconds()
        if remaining < _NOW_MIN_REMAINING_SEC:
            continue
        duration = (end - start).total_seconds()
        if duration > 0:
            elapsed = max(0.0, (now - start).total_seconds())
            progress = min(100.0, elapsed / duration * 100)
        else:
            progress = 0.0
        # Match-score is the cached LLM/rule rank; apply freshness penalty on top.
        base = item.get("match_score", 0.5)
        adj_score = max(0.0, base - (progress / 100) * _PROGRESS_PENALTY)
        adjusted.append({
            **item,
            "progress_pct": round(progress, 1),
            "match_score": round(adj_score, 2),
        })
    adjusted.sort(key=lambda x: -x.get("match_score", 0))
    return {**result, "recommendations": adjusted}


def _cache_key(user_id: int, context: str) -> str:
    return hashlib.sha1(f"{user_id}:{context}".encode()).hexdigest()


def _dedupe_candidates(events: list[EpgEvent], db: Session) -> list[EpgEvent]:
    """When the same show airs simultaneously on multiple channels (parallel
    feeds, e.g. ZDF + ZDFneo), keep only one. Major-channel airings win — they
    have a stronger viewer signal and avoid the LLM ranking duplicates in
    adjacent slots, which produced misleading match-% differences."""
    if len(events) < 2:
        return events
    ch_ids = {ev.channel_id for ev in events}
    channels = {c.id: c for c in db.query(Channel).filter(Channel.id.in_(ch_ids)).all()}

    def is_major(ev: EpgEvent) -> bool:
        ch = channels.get(ev.channel_id)
        return bool(ch and ch.name.lower() in _MAJOR_CHANNELS)

    winners: dict[tuple, EpgEvent] = {}
    for ev in events:
        title = (ev.title or "").strip().lower()
        if not title:
            continue  # untitled events bypass dedupe (kept individually)
        key = (title, ev.start_time)
        cur = winners.get(key)
        if cur is None or (is_major(ev) and not is_major(cur)):
            winners[key] = ev

    winner_ids = {ev.id for ev in winners.values()}
    out = [ev for ev in events
           if ev.id in winner_ids or not (ev.title or "").strip()]
    dropped = len(events) - len(out)
    if dropped:
        log.info("recs.deduped", dropped=dropped, kept=len(out))
    return out


def _epg_candidates(context: str, channel_ids: list[int], db: Session) -> list[EpgEvent]:
    if not channel_ids:
        return []
    now = utcnow()
    q = db.query(EpgEvent).filter(EpgEvent.channel_id.in_(channel_ids))

    if context == "now":
        # Skip events with less than 10 min remaining — not worth recommending.
        q = q.filter(EpgEvent.start_time <= now, EpgEvent.end_time > now + timedelta(minutes=10))
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

    # Pull a few extra rows so the dedupe doesn't push us below 100 survivors.
    raw = q.order_by(EpgEvent.start_time).limit(150).all()
    return _dedupe_candidates(raw, db)[:100]


def rule_based_rank(candidates: list[EpgEvent], context: str, db: Session) -> list[dict]:
    now = utcnow()
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

        progress = _progress_pct(ev, now)
        # Progress penalty is NOT applied here — it is re-applied in _realtime_adjust_now()
        # against the freshly computed progress_pct, so cached results stay accurate as
        # shows age without re-running the LLM.

        scored.append((score, progress, ev, ch))

    scored.sort(key=lambda x: -x[0])
    return [
        {
            "id": ev.id,
            "event_id": ev.event_id,
            "sref": ch.sref,
            "channel_name": ch.name,
            "title": ev.title,
            "short_desc": ev.short_desc,
            "long_desc": ev.long_desc,
            "start_time": ev.start_time.isoformat(),
            "end_time": ev.end_time.isoformat(),
            "genre": ev.genre,
            "progress_pct": progress,
            "match_score": round(max(0.0, min(1.0, 0.5 + score)), 2),
            "reason": "Populärer Sender und passende Sendezeit",
        }
        for score, progress, ev, ch in scored[:8]
    ]


def _get_recent_reactions(user_id: int, db: Session, limit: int = 15) -> tuple[list[dict], list[dict]]:
    """Return (likes, dislikes) as separate lists, most recent first."""
    rows = (
        db.query(UserLike)
        .filter(UserLike.user_id == user_id)
        .order_by(UserLike.created_at.desc())
        .limit(limit * 2)
        .all()
    )
    likes, dislikes = [], []
    for r in rows:
        entry = {"title": r.title, "channel": r.channel_name or "?", "genre": r.genre}
        if r.sentiment == "dislike":
            if len(dislikes) < limit:
                dislikes.append(entry)
        else:
            if len(likes) < limit:
                likes.append(entry)
    return likes, dislikes


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
    history: list[dict], likes: list[dict], dislikes: list[dict], candidates: list[EpgEvent], db: Session,
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

    dislikes_lines = [
        f"  - {d.get('title','?')} | {d.get('channel','?')} | {d.get('genre') or 'unbekannt'}"
        for d in dislikes
    ]
    dislikes_str = "\n".join(dislikes_lines) if dislikes_lines else "  (keine)"

    now = utcnow()
    cand_lines = []
    for i, ev in enumerate(candidates):
        ch = db.get(Channel, ev.channel_id)
        if not ch:
            continue
        dur_min = (ev.duration_sec or 0) // 60
        desc = (ev.short_desc or "").replace("\n", " ").strip()[:80]
        desc_part = f" | {desc}" if desc else ""
        progress_part = ""
        if context == "now":
            p = _progress_pct(ev, now)
            if p > 0:
                progress_part = f" | bereits {p:.0f}% gelaufen"
        cand_lines.append(
            f"  [{i+1}] {ch.name} | {ev.title} | "
            f"{to_local_str(ev.start_time)}–{to_local_str(ev.end_time)} | "
            f"{ev.genre or '-'} | {dur_min} min{progress_part}{desc_part}"
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

POSITIV BEWERTET (👍 vom Nutzer, starkes positives Signal):
{likes_str}

NEGATIV BEWERTET (👎 vom Nutzer, diese und ähnliche Sendungen NICHT empfehlen):
{dislikes_str}

LETZTE SEHHISTORIE:
{hist_str}

KANDIDATENLISTE:
{cand_str}

ANTWORT-FORMAT — exakt diese JSON-Struktur, KEINE anderen Felder, KEINE Markdown-Codeblöcke, KEIN Text außerhalb des JSON.
Pflichtfelder mit genau diesen Schlüsseln in Kleinbuchstaben: "taste_summary", "ranking", "reasons".
"ranking" ist eine Liste von 5–8 Ganzzahlen (Indizes aus der KANDIDATENLISTE oben), absteigend nach Passgenauigkeit.
"reasons" ist eine Map: Schlüssel = die Indizes als Strings, Wert = ein Satz Begründung.

Konkretes Beispiel (mit Beispiel-Zahlen, ersetze sie durch die tatsächlich passenden Indizes):
{{"taste_summary":"{user_name} mag Sci-Fi und Doku.","ranking":[3,1,7,2,5],"reasons":{{"3":"Sci-Fi-Serie, passt zum Profil.","1":"Hochwertige Doku.","7":"Action-Thriller wie zuletzt geschaut.","2":"Klassischer Film.","5":"Wissenschaftsmagazin."}}}}

Wenn ein Kandidat den Hinweis "bereits X% gelaufen" trägt, dann gilt: je höher der Wert, desto weniger attraktiv — bevorzuge frischere Sendungen mit ähnlicher Passung.

Antworte JETZT NUR mit dem JSON-Objekt, beginnend mit {{ und endend mit }}."""


_RANKING_KEYS = (
    "ranking", "recommendations", "ranked", "ranking_indices", "indices",
    "selected_shows", "selected", "selection", "shows", "programmes", "programs",
    "tipps", "tips", "picks",
)
_INDEX_KEYS = ("nr", "index", "rank", "candidate", "id", "number", "nummer", "no")
_REASON_KEYS = ("reason", "begründung", "begruendung", "explanation", "warum")
_SUMMARY_KEYS = ("taste_summary", "summary", "zusammenfassung", "geschmack")


def _indices_from_list(ranked: list) -> tuple[list[int], dict[str, str]]:
    indices: list[int] = []
    embedded: dict[str, str] = {}
    for item in ranked:
        idx = None
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            idx = item
        elif isinstance(item, str):
            s = item.strip().strip("[]*. ")
            try:
                idx = int(s)
            except ValueError:
                pass
        elif isinstance(item, dict):
            for ik in _INDEX_KEYS:
                if ik in item:
                    try:
                        idx = int(item[ik])
                        break
                    except (TypeError, ValueError):
                        pass
            if idx is not None:
                for rk in _REASON_KEYS:
                    if item.get(rk):
                        embedded[str(idx)] = str(item[rk])
                        break
        if idx is not None:
            indices.append(idx)
    return indices, embedded


def _parse_llm_ranking(raw: dict) -> tuple[list[int], dict[str, str], str]:
    """Extract (indices, reasons_by_idx, taste_summary) from a model dict.
    Tolerates several plausible shapes since small models drift from the
    requested schema. Returns ([], {}, "") if nothing usable can be salvaged."""
    # 1. Try known ranking keys, pick the best yield
    best_indices: list[int] = []
    best_embedded: dict[str, str] = {}
    for k in _RANKING_KEYS:
        v = raw.get(k)
        if isinstance(v, list) and v:
            ix, em = _indices_from_list(v)
            if len(ix) > len(best_indices):
                best_indices, best_embedded = ix, em

    # 2. Fallback: any list-valued key we haven't tried, pick best yield
    if not best_indices:
        for k, v in raw.items():
            if k in _RANKING_KEYS or not isinstance(v, list) or not v:
                continue
            ix, em = _indices_from_list(v)
            if len(ix) > len(best_indices):
                best_indices, best_embedded = ix, em

    # Explicit reasons block takes priority over embedded ones
    reasons = best_embedded
    explicit = raw.get("reasons")
    if isinstance(explicit, dict):
        reasons = {**best_embedded, **{str(k): str(v) for k, v in explicit.items()}}

    summary = ""
    for sk in _SUMMARY_KEYS:
        v = raw.get(sk)
        if isinstance(v, str) and v.strip():
            summary = v.strip()
            break

    return best_indices, reasons, summary


def _extract_indices_from_text(text: str, max_count: int = 8) -> list[int]:
    """Salvage indices from non-JSON model output. Two safe shapes:
      (1) bracketed: '[1]\\n[7]\\n[3]' or '1. **[1]**'
      (2) pure numeric list: '3, 11, 12, 13' — only when the entire payload is
          digits + separators, so prose digits ('5 stars', '2026') don't leak."""
    if not isinstance(text, str) or not text.strip():
        return []
    import re as _re
    out: list[int] = []
    seen: set[int] = set()

    # (1) Bracketed numbers — preferred, never picks up prose digits.
    for m in _re.finditer(r"\[(\d+)\]", text):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= max_count:
            return out
    if out:
        return out

    # (2) Pure numeric list — fall through only if the entire response is
    # digits + separators (commas, semicolons, dots, whitespace, hyphens).
    if _re.fullmatch(r"[\s\d,;.\-]+", text):
        for m in _re.finditer(r"\d+", text):
            try:
                n = int(m.group(0))
            except ValueError:
                continue
            if n in seen or n == 0:
                continue
            seen.add(n)
            out.append(n)
            if len(out) >= max_count:
                break
    return out


# Keep num_ctx (set in ollama.py) and budget aligned. We aim to stay well below
# num_ctx so the model has room to generate a complete JSON response without
# truncation — cutoff JSON would be unparseable.
_NUM_CTX_TOKENS = 16384
_PROMPT_TOKEN_BUDGET = 12000  # leaves ~4K for response
_BATCH_SIZE = 40              # candidates per LLM call


def _estimate_tokens(text: str) -> int:
    """Rough conservative estimate; ~3 chars per token works for German +
    structured text. Used only to warn / size batches, not to truncate."""
    return max(1, len(text) // 3)


async def _try_ollama_single_batch(
    user_name: str, context: str, profile: dict,
    history: list[dict], likes: list[dict], dislikes: list[dict],
    candidates: list[EpgEvent], db: Session,
) -> dict | None:
    """One LLM call over a single batch of candidates."""
    prompt = _build_prompt(user_name, context, profile, history, likes, dislikes, candidates, db)
    est = _estimate_tokens(prompt)
    if est > _PROMPT_TOKEN_BUDGET:
        log.warning("recs.prompt_oversize",
                    est_tokens=est, budget=_PROMPT_TOKEN_BUDGET,
                    ctx=_NUM_CTX_TOKENS, candidates=len(candidates))
    else:
        log.info("recs.prompt_size", est_tokens=est, candidates=len(candidates))

    raw = await ask_json(prompt, caller="recs")
    if raw is None:
        log.warning("recommendations.bad_llm_response", raw_type="None", keys=None)
        return None

    if isinstance(raw, dict):
        indices, reasons, summary = _parse_llm_ranking(raw)
        if not indices:
            log.warning("recommendations.bad_llm_response",
                        raw_type="dict", keys=list(raw.keys()))
            return None
    else:
        # Raw text fallback for models that refuse JSON (rare with format=json).
        indices = _extract_indices_from_text(raw)
        reasons, summary = {}, ""
        if not indices:
            log.warning("recommendations.bad_llm_response",
                        raw_type="str", keys=None)
            return None
        log.info("recommendations.salvaged_from_text", count=len(indices))

    # Build candidate lookup by 1-based index
    indexed: dict[int, tuple[EpgEvent, object]] = {}
    for i, ev in enumerate(candidates):
        ch = db.get(Channel, ev.channel_id)
        if ch:
            indexed[i + 1] = (ev, ch)

    items = []
    now = utcnow()
    for nr_int in indices:
        if nr_int not in indexed:
            continue
        ev, ch = indexed[nr_int]
        score = (len(indices) - len(items)) / max(1, len(indices))
        items.append({
            "id": ev.id,
            "event_id": ev.event_id,
            "sref": ch.sref,
            "channel_name": ch.name,
            "title": ev.title,
            "short_desc": ev.short_desc,
            "long_desc": ev.long_desc,
            "start_time": ev.start_time.isoformat(),
            "end_time": ev.end_time.isoformat(),
            "genre": ev.genre,
            "progress_pct": _progress_pct(ev, now),
            "match_score": round(score, 2),
            "reason": reasons.get(str(nr_int), ""),
        })

    if not items:
        return None
    return {"taste_summary": summary, "recommendations": items}


async def _try_ollama(
    user_name: str, context: str, profile: dict,
    history: list[dict], likes: list[dict], dislikes: list[dict],
    candidates: list[EpgEvent], db: Session,
) -> dict | None:
    """Rank candidates via the LLM. Splits into batches when the candidate list
    is large, then runs a second-phase rerank over the merged survivors so the
    final order reflects a global comparison, not just within-batch picks."""
    if not candidates:
        return None

    if len(candidates) <= _BATCH_SIZE:
        return await _try_ollama_single_batch(
            user_name, context, profile, history, likes, dislikes, candidates, db
        )

    # Phase 1: split into batches and rank each
    batches = [candidates[i:i + _BATCH_SIZE]
               for i in range(0, len(candidates), _BATCH_SIZE)]
    log.info("recs.batched", batches=len(batches),
             candidates=len(candidates), batch_size=_BATCH_SIZE)

    survivors: list[dict] = []
    seen_ids: set[int] = set()
    summary = ""
    for idx, batch in enumerate(batches):
        result = await _try_ollama_single_batch(
            user_name, context, profile, history, likes, dislikes, batch, db
        )
        if result is None:
            continue
        if not summary and result.get("taste_summary"):
            summary = result["taste_summary"]
        for item in result.get("recommendations", []):
            ev_id = item.get("id")
            if ev_id in seen_ids:
                continue
            seen_ids.add(ev_id)
            survivors.append(item)

    if not survivors:
        return None

    # Few enough to skip Phase 2 — within-batch ranking is good enough.
    if len(survivors) <= 8:
        return {"taste_summary": summary, "recommendations": survivors}

    # Phase 2: rerank the combined survivors so cross-batch order is consistent.
    survivor_events: list[EpgEvent] = []
    for item in survivors:
        ev = db.get(EpgEvent, item["id"])
        if ev:
            survivor_events.append(ev)
    log.info("recs.rerank", survivors=len(survivor_events))
    final = await _try_ollama_single_batch(
        user_name, context, profile, history, likes, dislikes, survivor_events, db
    )
    if final is not None:
        if not final.get("taste_summary"):
            final["taste_summary"] = summary
        return final
    # Phase 2 failed; degrade to Phase 1 top picks
    return {"taste_summary": summary, "recommendations": survivors[:8]}


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


# Per-(user, context) regen lock — prevents stampedes when many requests hit
# a stale cache at the same time.
_regen_pending: set[tuple[int, str]] = set()


async def _regen_in_background(user_id: int, user_name: str, context: str) -> None:
    """Refresh one user×context cache without blocking any user request."""
    import asyncio  # noqa: F401  (kept local to avoid top-level import)
    key = (user_id, context)
    if key in _regen_pending:
        return
    _regen_pending.add(key)
    try:
        from app.database import SessionLocal as _SL
        db = _SL()
        try:
            await get_recommendations(user_id, user_name, context, db, force_refresh=True)
            log.info("recs.bg_regen_done", user_id=user_id, context=context)
        finally:
            db.close()
    except Exception as e:
        log.warning("recs.bg_regen_failed",
                    user_id=user_id, context=context, error=str(e))
    finally:
        _regen_pending.discard(key)


async def get_recommendations(
    user_id: int, user_name: str, context: str, db: Session,
    force_refresh: bool = False,
) -> dict:
    """User-facing requests never wait on the LLM:
      - fresh cache       → serve immediately
      - stale cache       → serve stale, trigger LLM regen in background
      - no cache          → return rule-based snapshot, trigger LLM in background
    Only the background path (force_refresh=True, also called by the warmer) does
    the slow LLM call."""
    import asyncio
    now = utcnow()
    key = _cache_key(user_id, context)

    if not force_refresh:
        # Latest cache entry regardless of freshness.
        cached = (
            db.query(RecommendationCache)
            .filter(
                RecommendationCache.user_id == user_id,
                RecommendationCache.prompt_hash == key,
            )
            .order_by(RecommendationCache.generated_at.desc())
            .first()
        )
        if cached:
            is_fresh = cached.valid_until > now
            data = json.loads(cached.response)
            data["cached"] = True
            if not is_fresh:
                age_min = (now - cached.generated_at).total_seconds() / 60
                log.info("recs.serving_stale",
                         user_id=user_id, context=context, age_min=round(age_min, 1))
                data["regenerating"] = True
                asyncio.create_task(_regen_in_background(user_id, user_name, context))
            adjusted = _realtime_adjust_now(data)
            # For "now": if the realtime filter drained everything (cached events
            # all ended), fall through to a fresh rule-based snapshot so the user
            # sees current candidates immediately while the LLM regen finishes.
            if context == "now" and not adjusted.get("recommendations"):
                log.info("recs.now_cache_drained",
                         user_id=user_id, age_min=round((now - cached.generated_at).total_seconds() / 60, 1))
                if not is_fresh:
                    pass  # regen already scheduled above
                else:
                    asyncio.create_task(_regen_in_background(user_id, user_name, context))
            else:
                return adjusted

    # No cache for this user×context yet — build the rule-based slate first so
    # the user has something to look at immediately; queue the LLM in background.
    channel_ids = [c.id for c in get_channels_for_user(user_id, db)]
    candidates = _epg_candidates(context, channel_ids, db)
    profile = get_profile(user_id, db)
    reaction_count = db.query(UserLike).filter_by(user_id=user_id).count()
    cold_start = (
        profile.get("session_count", 0) < 10
        and not profile.get("stated_preferences")
        and reaction_count == 0
    )

    if force_refresh:
        # Background warming — actually run the LLM.
        ai_result = None
        if not cold_start and candidates:
            history = _get_recent_history(user_id, db)
            likes, dislikes = _get_recent_reactions(user_id, db)
            ai_result = await _try_ollama(
                user_name, context, profile, history, likes, dislikes, candidates, db
            )
        if ai_result is not None:
            result = {
                "context": context, "user_name": user_name,
                "taste_summary": ai_result.get("taste_summary", ""),
                "recommendations": ai_result.get("recommendations", []),
                "cached": False, "cold_start": False,
            }
        else:
            recs = rule_based_rank(candidates, context, db)
            result = {
                "context": context, "user_name": user_name,
                "taste_summary": (
                    "Noch zu wenig Daten — zeige populäre Sendungen" if cold_start
                    else "KI nicht verfügbar — Empfehlungen nach Regeln"
                ),
                "recommendations": recs,
                "cached": False, "cold_start": cold_start,
            }
        _save_cache(user_id, context, result, _CACHE_TTL.get(context, 30), db)
        return _realtime_adjust_now(result)

    # User-facing path, truly cold cache: respond fast with rule-based, cache it
    # briefly, and queue the LLM regen.
    recs = rule_based_rank(candidates, context, db)
    result = {
        "context": context, "user_name": user_name,
        "taste_summary": (
            "Noch zu wenig Daten — zeige populäre Sendungen" if cold_start
            else "Erste Empfehlungen — KI-Auswahl wird im Hintergrund vorbereitet…"
        ),
        "recommendations": recs,
        "cached": False, "cold_start": cold_start,
        # Tells the frontend to poll faster until an LLM result lands.
        "regenerating": not cold_start,
    }
    # Short TTL so the background LLM result replaces this quickly.
    _save_cache(user_id, context, result, 5, db)
    asyncio.create_task(_regen_in_background(user_id, user_name, context))
    return _realtime_adjust_now(result)
