"""Per-(user, EPG event) match-score pipeline.

Background workflow:
  - new EPG events → score_events_for_user(...) batches them to the LLM
  - LLM down       → rule_score_fallback fills rows with source='rule'
  - LLM recovers   → ai_availability_watcher upgrades source='rule' rows to LLM
  - like/dislike   → explicit override + debounced background re-rate
  - prefs/sessions → stale-mark + debounced re-rate
  - daily cron     → catch-all re-rate of remaining stale rows

The HTTP recommendations endpoint never calls the LLM directly — it just reads
match_score from this table, ordered by score within the requested context's
time window.
"""
from __future__ import annotations
import asyncio
import json
from datetime import timedelta
from sqlalchemy import select, delete, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import (
    UserEventScore, EpgEvent, Channel, User, UserLike,
)
from app.services.profile import get_profile
from app.services.channels import get_channels_for_user
from app.services.ollama import ask_json
from app.services import ollama as _ollama
from app.timezones import utcnow, to_local_str
from app.logging_setup import get_logger

log = get_logger(__name__)

# How many events we send the LLM in a single batch. Smaller batches keep the
# JSON response well under NUM_PREDICT and reduce position-bias / parse-mismatch
# halve-retries (see scoring.parse_mismatch_halve telemetry).
_BATCH_SIZE = 20
# Score threshold above which a match is "good" enough to show as a badge in
# EPG / Now & Next lists. Below this we return None so the UI hides the chip.
GOOD_MATCH_THRESHOLD = 0.7

# Debounce window for like/dislike-triggered re-rates. Multiple clicks within
# this window only kick off one background job.
_DEBOUNCE_SEC = 60
# AI watcher: probe with exponential backoff so a long Ollama outage doesn't
# turn into a flood of requests against a dead endpoint. Reset to the floor
# after a successful probe.
_AI_PROBE_FLOOR_SEC = 90        # 1.5 min between probes when freshly down
_AI_PROBE_CEILING_SEC = 30 * 60  # cap backoff at 30 min


# ─── In-process state ────────────────────────────────────────────────────────

# user_id → asyncio.Task handle for the in-flight debounced re-rate, so a new
# like inside the debounce window resets the timer instead of stacking jobs.
_rerate_handles: dict[int, asyncio.Task] = {}

# True once we've seen a successful Ollama call. Used by the watcher to avoid
# re-probing constantly when the model is responsive.
_ai_seen_alive = False


def _mark_ai_alive() -> None:
    global _ai_seen_alive
    _ai_seen_alive = True


def _mark_ai_down() -> None:
    global _ai_seen_alive
    _ai_seen_alive = False


# ─── LLM scoring prompt ─────────────────────────────────────────────────────

def _build_scoring_prompt(
    user_name: str, profile: dict,
    history: list[dict], likes: list[dict], dislikes: list[dict],
    events: list[tuple[EpgEvent, Channel]],
) -> str:
    stated_prefs = profile.get("stated_preferences") or ""
    genres_str = ", ".join(
        f"{g['genre']} ({g['share']*100:.0f}%)"
        for g in profile.get("top_genres", [])[:17]
        if g["genre"] != "unknown"
    )
    channels_str = ", ".join(g["name"] for g in profile.get("top_channels", [])[:17])

    hist_lines = [
        f"  - {h.get('title','?')} | {h.get('channel','?')} | {h.get('genre') or 'unbekannt'} | {h.get('duration_min',0):.0f} min"
        for h in history[:40]
    ]
    hist_str = "\n".join(hist_lines) if hist_lines else "  (keine Historie)"
    likes_str = "\n".join(
        f"  - {l.get('title','?')} | {l.get('channel','?')} | {l.get('genre') or 'unbekannt'}"
        for l in likes
    ) or "  (keine)"
    dislikes_str = "\n".join(
        f"  - {d.get('title','?')} | {d.get('channel','?')} | {d.get('genre') or 'unbekannt'}"
        for d in dislikes
    ) or "  (keine)"

    cand_lines = []
    for i, (ev, ch) in enumerate(events):
        dur_min = (ev.duration_sec or 0) // 60
        desc = (ev.short_desc or "").replace("\n", " ").strip()[:80]
        desc_part = f" | {desc}" if desc else ""
        cand_lines.append(
            f"  [{i+1}] {ch.name} | {ev.title} | "
            f"{to_local_str(ev.start_time)}–{to_local_str(ev.end_time)} | "
            f"{ev.genre or '-'} | {dur_min} min{desc_part}"
        )
    cand_str = "\n".join(cand_lines)

    prefs_section = f"\nEXPLIZITE VORLIEBEN (vom Nutzer angegeben):\n{stated_prefs}\n" if stated_prefs else ""

    return f"""Du bewertest TV-Sendungen für {user_name}. Antworte NUR mit gültigem JSON.

AUFGABE: Vergib pro nummerierter Sendung in der LISTE einen Match-Score 0.00–1.00 für {user_name}.
1.00 = perfekt zum Profil. 0.00 = passt überhaupt nicht. 0.50 = neutral.
{prefs_section}
NUTZERPROFIL ({profile.get('session_count', 0)} Sitzungen, letzte 30 Tage):
- Lieblingsgenres: {genres_str or 'unbekannt'}
- Häufigste Sender: {channels_str or 'unbekannt'}
- Ø Sehdauer: {profile.get('avg_duration_min', 0):.0f} min

POSITIV BEWERTET (👍 Nutzer):
{likes_str}

NEGATIV BEWERTET (👎 Nutzer — diese und ähnliche niedrig bewerten):
{dislikes_str}

LETZTE SEHHISTORIE:
{hist_str}

LISTE:
{cand_str}

ANTWORT-FORMAT — exakt diese JSON-Struktur:
{{"scores": [{{"score": <0.0..1.0>, "reason": "<1–2 Sätze, Bezug zum Nutzer>"}}, ...]}}
Gib GENAU {len(events)} Einträge aus — einen pro Zeile in der LISTE, in DERSELBEN REIHENFOLGE wie die LISTE (Eintrag 1 = [1], Eintrag 2 = [2], usw.). Keine Index-Nummern in der Antwort. `reason` referenziert Profil/Likes/Dislikes/Historie und erklärt den Bezug konkret — kein neutrales Beschreiben der Sendung, kein Geschwafel.
"""


def _parse_scoring_response(raw: dict | str | None) -> list[tuple[float, str]]:
    """List of (score_0_to_1, reason) in response order. Caller matches them
    positionally to the input chunk. Returning [] signals "unusable response"
    so the caller can fall back / halve."""
    if not isinstance(raw, dict):
        return []
    items = raw.get("scores") or raw.get("ranking") or []
    if not isinstance(items, list):
        return []
    out: list[tuple[float, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            sc = float(it.get("score", 0.5))
        except (TypeError, ValueError):
            sc = 0.5
        sc = max(0.0, min(1.0, sc))
        reason = str(it.get("reason") or "").strip()[:240]
        out.append((sc, reason))
    return out


# ─── Rule-based fallback ────────────────────────────────────────────────────

_SHOPPING_HINTS = ("shop", "qvc", "verkauf", "hse", "channel21")
_NEWS_GENRES = ("nachrichten", "news")
_FILM_GENRES = ("spielfilm", "film", "drama", "thriller", "comedy", "krimi")
_MAJOR_CHANNELS = {
    "ard", "zdf", "pro7", "prosieben", "sat.1", "rtl", "arte", "3sat",
    "kabel eins", "kabel1", "vox", "rtl2", "rtl zwei",
}


def _rule_score(ev: EpgEvent, ch: Channel) -> float:
    name = (ch.name or "").lower()
    if any(h in name for h in _SHOPPING_HINTS):
        return 0.05
    base = 0.5
    genre = (ev.genre or "").lower()
    if any(g in genre for g in _NEWS_GENRES):
        base -= 0.15
    if any(g in genre for g in _FILM_GENRES):
        base += 0.15
    dur = ev.duration_sec or 0
    if dur >= 2700:
        base += 0.05
    if name in _MAJOR_CHANNELS:
        base += 0.1
    return max(0.0, min(1.0, base))


# ─── Upsert helpers ─────────────────────────────────────────────────────────

def _upsert_scores(
    user_id: int,
    triples: list[tuple[int, float, str | None, str]],
    db: Session,
) -> int:
    """triples = [(epg_event_id, score, reason, source), ...]"""
    if not triples:
        return 0
    now = utcnow()
    for evid, score, reason, source in triples:
        stmt = (
            sqlite_insert(UserEventScore)
            .values(
                user_id=user_id, epg_event_id=evid,
                match_score=score, reason=reason,
                source=source, rated_at=now, stale=False,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "epg_event_id"],
                set_={
                    "match_score": score, "reason": reason,
                    "source": source, "rated_at": now, "stale": False,
                },
            )
        )
        db.execute(stmt)
    db.commit()
    return len(triples)


def good_scores_for_events(
    user_id: int | None, event_ids: list[int], db: Session,
) -> dict[int, float]:
    """{ epg_event_id → match_score } for the events whose score for this user
    is ≥ GOOD_MATCH_THRESHOLD. Missing events / weak scores / no user → no entry.
    Used by /api/epg and /api/now-next to decorate list rows with a match
    badge without making them request the full Tipps slate."""
    if not user_id or not event_ids:
        return {}
    rows = (
        db.query(UserEventScore.epg_event_id, UserEventScore.match_score)
        .filter(
            UserEventScore.user_id == user_id,
            UserEventScore.epg_event_id.in_(event_ids),
            UserEventScore.match_score >= GOOD_MATCH_THRESHOLD,
        )
        .all()
    )
    return {evid: float(score) for evid, score in rows}


def _events_with_channels(event_ids: list[int], db: Session) -> list[tuple[EpgEvent, Channel]]:
    """Resolve EpgEvent+Channel ordered by start_time ascending so bulk re-rates
    score the events closest to "now" first — the user sees fresh "now"/"next"
    scores before the LLM grinds through "today"/later batches. Drops events
    past their end_time (no value scoring something that already aired)."""
    now = utcnow()
    rows = (
        db.query(EpgEvent, Channel)
        .join(Channel, Channel.id == EpgEvent.channel_id)
        .filter(EpgEvent.id.in_(event_ids), EpgEvent.end_time > now)
        .order_by(EpgEvent.start_time.asc())
        .all()
    )
    return [(ev, ch) for ev, ch in rows]


def _pending_event_ids_for_user(user_id: int, event_ids: list[int], db: Session) -> list[int]:
    """Subset of event_ids the user has no fresh row for."""
    if not event_ids:
        return []
    have = {
        r[0] for r in db.query(UserEventScore.epg_event_id).filter(
            UserEventScore.user_id == user_id,
            UserEventScore.epg_event_id.in_(event_ids),
            UserEventScore.stale == False,  # noqa: E712
        ).all()
    }
    return [i for i in event_ids if i not in have]


# ─── Public scoring entry points ────────────────────────────────────────────

async def score_events_for_user(
    user: User, event_ids: list[int], db: Session,
) -> int:
    """Rate `event_ids` for `user`. Returns count of rows written."""
    user_channel_ids = {c.id for c in get_channels_for_user(user.id, db)}
    if not user_channel_ids:
        return 0
    pending = _pending_event_ids_for_user(user.id, event_ids, db)
    if not pending:
        return 0
    rows = _events_with_channels(pending, db)
    # Skip events on channels the user doesn't see at all.
    rows = [(ev, ch) for ev, ch in rows if ch.id in user_channel_ids]
    if not rows:
        return 0

    profile = get_profile(user.id, db)
    history, likes, dislikes = _profile_context(user.id, db)

    written = 0
    for chunk in _chunks(rows, _BATCH_SIZE):
        triples = await _score_chunk(user.name, profile, history, likes, dislikes, chunk)
        if triples:
            written += _upsert_scores(user.id, triples, db)
    return written


async def _score_chunk(
    user_name: str, profile: dict,
    history: list[dict], likes: list[dict], dislikes: list[dict],
    chunk: list[tuple[EpgEvent, Channel]],
) -> list[tuple[int, float, str | None, str]]:
    """Score one batch via the LLM, fall back to rule-based on any failure."""
    prompt = _build_scoring_prompt(user_name, profile, history, likes, dislikes, chunk)
    # Hard-constrain the response shape: exactly len(chunk) entries, each with a
    # numeric 0..1 score and a string reason. Prompt instructions alone were not
    # enforcing the count — gemma4 reliably dropped one entry per batch.
    n = len(chunk)
    schema = {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "minItems": n,
                "maxItems": n,
                "items": {
                    "type": "object",
                    "properties": {
                        "score": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["score", "reason"],
                },
            },
        },
        "required": ["scores"],
    }
    raw = await ask_json(prompt, caller="scoring", format_schema=schema)
    if raw is None:
        _mark_ai_down()
        log.warning("scoring.llm_unavailable", chunk=len(chunk))
        return [(ev.id, _rule_score(ev, ch), None, "rule") for ev, ch in chunk]
    usage = _ollama.last_usage() or {}
    if usage.get("prompt_overflow") or usage.get("completion_truncated"):
        log.warning("scoring.ctx_overflow",
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    chunk=len(chunk))
        # Halve and retry once.
        if len(chunk) > 8:
            half = len(chunk) // 2
            return (
                await _score_chunk(user_name, profile, history, likes, dislikes, chunk[:half])
                + await _score_chunk(user_name, profile, history, likes, dislikes, chunk[half:])
            )
        return [(ev.id, _rule_score(ev, ch), None, "rule") for ev, ch in chunk]

    _mark_ai_alive()
    parsed = _parse_scoring_response(raw)
    # Length mismatch = the model skipped or added an event. Without an index
    # field we can no longer realign, so treat this as an unusable response
    # and fall through to halve/retry — same as a hard parse failure.
    if not parsed or len(parsed) != len(chunk):
        if len(chunk) > 12:
            log.warning("scoring.parse_mismatch_halve",
                        chunk=len(chunk), got=len(parsed))
            half = len(chunk) // 2
            return (
                await _score_chunk(user_name, profile, history, likes, dislikes, chunk[:half])
                + await _score_chunk(user_name, profile, history, likes, dislikes, chunk[half:])
            )
        log.warning("scoring.parse_mismatch_fallback_rule",
                    chunk=len(chunk), got=len(parsed))
        return [(ev.id, _rule_score(ev, ch), None, "rule") for ev, ch in chunk]

    triples: list[tuple[int, float, str | None, str]] = []
    for (ev, ch), (score, reason) in zip(chunk, parsed):
        triples.append((ev.id, score, reason or None, "llm"))
    return triples


def _profile_context(user_id: int, db: Session) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (history, likes, dislikes) using the same shape recommendations.py
    builds — copied locally so the two modules don't have to import each other."""
    from app.services.recommendations import _get_recent_history, _get_recent_reactions
    history = _get_recent_history(user_id, db)
    likes, dislikes = _get_recent_reactions(user_id, db)
    return history, likes, dislikes


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ─── Stale-marking + invalidation ───────────────────────────────────────────

def mark_user_llm_rows_stale(user_id: int, except_event_id: int | None, db: Session) -> int:
    """Mark every LLM-sourced score row for the user as stale. Explicit rows
    (manual likes/dislikes) are NOT touched — the user's judgement survives a
    re-rate. Optionally skip one event id (the one just liked/disliked, which
    already has the freshest possible score)."""
    q = (
        db.query(UserEventScore)
        .filter(
            UserEventScore.user_id == user_id,
            UserEventScore.source.in_(("llm", "rule")),
        )
    )
    if except_event_id is not None:
        q = q.filter(UserEventScore.epg_event_id != except_event_id)
    n = q.update({UserEventScore.stale: True}, synchronize_session=False)
    db.commit()
    return n


def set_explicit_score(user_id: int, epg_event_id: int, liked: bool, db: Session) -> None:
    _upsert_scores(
        user_id,
        [(
            epg_event_id,
            1.0 if liked else 0.0,
            None,
            "explicit_like" if liked else "explicit_dislike",
        )],
        db,
    )


def clear_explicit_score(user_id: int, epg_event_id: int, db: Session) -> None:
    """User un-liked / un-disliked: remove the explicit row so the event falls
    back to whatever the LLM/rule rates it next time."""
    db.query(UserEventScore).filter(
        UserEventScore.user_id == user_id,
        UserEventScore.epg_event_id == epg_event_id,
        UserEventScore.source.in_(("explicit_like", "explicit_dislike")),
    ).delete(synchronize_session=False)
    db.commit()


# ─── Debounced background re-rate ───────────────────────────────────────────

def schedule_rerate(user_id: int) -> None:
    """Cancel any pending re-rate for this user and queue a fresh one. The
    debounce lets a burst of likes coalesce into a single background pass."""
    existing = _rerate_handles.get(user_id)
    if existing and not existing.done():
        existing.cancel()
    _rerate_handles[user_id] = asyncio.create_task(_rerate_after_debounce(user_id))


async def _rerate_after_debounce(user_id: int) -> None:
    try:
        await asyncio.sleep(_DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            return
        event_ids = _stale_future_event_ids(user_id, db)
        if not event_ids:
            return
        # Force re-score by clearing the stale rows for the LLM/rule sources
        # we're about to overwrite. _pending_event_ids_for_user skips rows with
        # stale=False, so we must first invalidate or directly re-rate.
        # Easiest: re-rate unconditionally for this set.
        await _rerate_specific(user, event_ids, db)
    finally:
        db.close()


def _stale_future_event_ids(user_id: int, db: Session) -> list[int]:
    """All future-window event ids the user has a stale LLM/rule row for."""
    now = utcnow()
    rows = (
        db.query(UserEventScore.epg_event_id)
        .join(EpgEvent, EpgEvent.id == UserEventScore.epg_event_id)
        .filter(
            UserEventScore.user_id == user_id,
            UserEventScore.stale == True,  # noqa: E712
            UserEventScore.source.in_(("llm", "rule")),
            EpgEvent.end_time > now,
        )
        .all()
    )
    return [r[0] for r in rows]


async def _rerate_specific(user: User, event_ids: list[int], db: Session) -> int:
    """Force a re-rate of the given event_ids regardless of current stale flag."""
    rows = _events_with_channels(event_ids, db)
    if not rows:
        return 0
    user_channel_ids = {c.id for c in get_channels_for_user(user.id, db)}
    rows = [(ev, ch) for ev, ch in rows if ch.id in user_channel_ids]
    if not rows:
        return 0
    profile = get_profile(user.id, db)
    history, likes, dislikes = _profile_context(user.id, db)
    written = 0
    for chunk in _chunks(rows, _BATCH_SIZE):
        triples = await _score_chunk(user.name, profile, history, likes, dislikes, chunk)
        written += _upsert_scores(user.id, triples, db)
    log.info("scoring.rerate_done", user_id=user.id, written=written)
    return written


# ─── EPG ingest entry point ─────────────────────────────────────────────────

async def score_events_for_all_users(event_ids: list[int]) -> int:
    """Called from refresh_epg_service after a batch of new/updated events lands.
    Runs sequentially across users so we don't pile up parallel LLM jobs."""
    if not event_ids:
        return 0
    db = SessionLocal()
    total = 0
    try:
        for user in db.query(User).all():
            total += await score_events_for_user(user, event_ids, db)
    finally:
        db.close()
    if total:
        log.info("scoring.ingest_done", events=len(event_ids), rows=total)
    return total


# ─── AI availability watcher ────────────────────────────────────────────────

async def ai_availability_watcher() -> None:
    """If we ever fell back to rule-scored rows, retry the LLM with exponential
    backoff and upgrade those rows to LLM scores the moment Ollama answers
    again — without waiting for the nightly cron. We never poll faster than the
    floor or slower than the ceiling, so a long outage doesn't flood the down
    endpoint, and a quick recovery is noticed within ~90 s."""
    backoff = _AI_PROBE_FLOOR_SEC
    while True:
        try:
            await asyncio.sleep(backoff)
            db = SessionLocal()
            try:
                rule_users = _users_with_rule_rows(db)
                if not rule_users:
                    # Nothing to upgrade — drift back to the floor.
                    backoff = _AI_PROBE_FLOOR_SEC
                    continue
                # Cheap probe: ask for an empty-JSON object.
                probe = await ask_json('Antwort: {"ok":true}', caller="scoring_probe")
                if not isinstance(probe, dict):
                    _mark_ai_down()
                    backoff = min(backoff * 2, _AI_PROBE_CEILING_SEC)
                    log.info("scoring.ai_still_down", next_probe_sec=backoff)
                    continue
                _mark_ai_alive()
                backoff = _AI_PROBE_FLOOR_SEC
                for user_id in rule_users:
                    user = db.get(User, user_id)
                    if not user:
                        continue
                    event_ids = _rule_future_event_ids(user_id, db)
                    if event_ids:
                        log.info("scoring.ai_recovered_upgrade",
                                 user_id=user_id, rule_rows=len(event_ids))
                        await _rerate_specific(user, event_ids, db)
            finally:
                db.close()
        except Exception as e:
            log.warning("scoring.watcher_error", error=str(e))
            backoff = min(backoff * 2, _AI_PROBE_CEILING_SEC)


def _users_with_rule_rows(db: Session) -> list[int]:
    rows = (
        db.query(UserEventScore.user_id)
        .filter(UserEventScore.source == "rule")
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


def _rule_future_event_ids(user_id: int, db: Session) -> list[int]:
    now = utcnow()
    rows = (
        db.query(UserEventScore.epg_event_id)
        .join(EpgEvent, EpgEvent.id == UserEventScore.epg_event_id)
        .filter(
            UserEventScore.user_id == user_id,
            UserEventScore.source == "rule",
            EpgEvent.end_time > now,
        )
        .all()
    )
    return [r[0] for r in rows]


# ─── Bootstrap + daily catch-all ────────────────────────────────────────────

async def bootstrap_unscored() -> None:
    """On startup: rate every future EPG event that no user has a row for yet.
    Interleaves users so neither one starves while the other's queue grinds —
    each round-trip scores one batch per user before moving on.
    Idempotent and cheap when the table is already populated."""
    db = SessionLocal()
    try:
        now = utcnow()
        future_ids = [
            r[0] for r in db.query(EpgEvent.id)
            .filter(EpgEvent.end_time > now)
            .order_by(EpgEvent.start_time.asc())
            .all()
        ]
        if not future_ids:
            return
        users = db.query(User).all()
        # Per-user queues of pending event_ids, in start_time order.
        queues: list[tuple[User, list[int]]] = []
        for user in users:
            pending = _pending_event_ids_for_user(user.id, future_ids, db)
            if pending:
                queues.append((user, pending))
                log.info("scoring.bootstrap_start",
                         user_id=user.id, pending=len(pending))
        if not queues:
            return
        # Round-robin one batch per user per pass until every queue is drained.
        while any(q for _, q in queues):
            for user, pending in queues:
                if not pending:
                    continue
                chunk_ids = pending[:_BATCH_SIZE]
                del pending[:_BATCH_SIZE]
                await score_events_for_user(user, chunk_ids, db)
        for user, _ in queues:
            log.info("scoring.bootstrap_done", user_id=user.id)
    finally:
        db.close()


async def get_recommendations_from_scores(
    user_id: int, user_name: str, context: str, db: Session, limit: int = 8,
) -> dict:
    """Score-backed recs path. No LLM on the hot path — pure SQL + a small
    Python pass for freshness/progress. Falls back to a rule-based snapshot
    if the user has no stored scores yet (e.g., bootstrap still warming)."""
    from sqlalchemy import and_
    from app.timezones import prime_range, today_remaining_range
    from app.services.recommendations import (
        rule_based_rank, _epg_candidates, _progress_pct,
    )

    now = utcnow()

    # Each context defines a precise SQL window:
    #   now    → already airing (start_time ≤ now < end_time)
    #   next   → starts strictly after now, within 2 hours
    #   prime  → starts inside tonight's prime window
    #   today  → starts inside the remainder of today
    if context == "now":
        win_filter = and_(EpgEvent.start_time <= now, EpgEvent.end_time > now)
    elif context == "next":
        win_filter = and_(EpgEvent.start_time > now,
                          EpgEvent.start_time <= now + timedelta(hours=2))
    elif context == "prime":
        win_start, win_end = prime_range()
        win_filter = and_(EpgEvent.start_time >= win_start,
                          EpgEvent.start_time <  win_end)
    elif context == "today":
        win_start, win_end = today_remaining_range()
        win_filter = and_(EpgEvent.start_time >= win_start,
                          EpgEvent.start_time <  win_end)
    else:
        return {
            "context": context, "user_name": user_name, "taste_summary": "",
            "recommendations": [], "cached": False, "cold_start": False,
        }

    # Restrict to the user's channels — recommend only what the user can watch.
    user_channel_ids = {c.id for c in get_channels_for_user(user_id, db)}
    if not user_channel_ids:
        return {
            "context": context, "user_name": user_name, "taste_summary": "",
            "recommendations": [], "cached": False, "cold_start": True,
        }

    # All scored rows in the window for this user.
    scored = (
        db.query(UserEventScore, EpgEvent, Channel)
        .join(EpgEvent, EpgEvent.id == UserEventScore.epg_event_id)
        .join(Channel, Channel.id == EpgEvent.channel_id)
        .filter(
            UserEventScore.user_id == user_id,
            win_filter,
            Channel.id.in_(user_channel_ids),
        )
        .all()
    )

    # All events in the window the user could watch, so we can supplement.
    all_events = (
        db.query(EpgEvent, Channel)
        .join(Channel, Channel.id == EpgEvent.channel_id)
        .filter(win_filter, Channel.id.in_(user_channel_ids))
        .all()
    )

    by_event_id: dict[int, dict] = {}
    for ues, ev, ch in scored:
        by_event_id[ev.id] = {
            "score": ues.match_score, "reason": ues.reason or "",
            "ev": ev, "ch": ch, "source": ues.source,
        }
    unscored: list[int] = []
    for ev, ch in all_events:
        if ev.id in by_event_id:
            continue
        # Rule-based stop-gap so the page is full even while LLM is still
        # bootstrapping this user's scores.
        by_event_id[ev.id] = {
            "score": _rule_score(ev, ch), "reason": "",
            "ev": ev, "ch": ch, "source": "rule_inline",
        }
        unscored.append(ev.id)

    # Background scoring for events we just rule-scored — next page hit gets
    # real LLM signal. Fire-and-forget; the scorer is idempotent and skips
    # events that already have fresh rows. The task owns the lifetime of its
    # own DB session so we don't leak connections back to the pool while the
    # 20-30s LLM call is in flight.
    if unscored:
        user_obj = db.get(User, user_id)
        if user_obj:
            async def _bg():
                bg_db = SessionLocal()
                try:
                    await score_events_for_user(user_obj, unscored, bg_db)
                except Exception as e:
                    log.warning("scoring.bg_failed", user_id=user_id, error=str(e))
                finally:
                    bg_db.close()
            asyncio.create_task(_bg())

    # Sort by score desc, then start_time asc for stability, build items.
    items = []
    for entry in sorted(by_event_id.values(),
                        key=lambda e: (-e["score"], e["ev"].start_time)):
        ev = entry["ev"]; ch = entry["ch"]
        # For "now": skip events with less than 10 min remaining.
        if context == "now" and (ev.end_time - now).total_seconds() < 600:
            continue
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
            "match_score": round(entry["score"], 2),
            "reason": entry["reason"],
        })
        if len(items) >= limit:
            break

    return {
        "context": context, "user_name": user_name,
        "taste_summary": "",
        "recommendations": items,
        "cached": False, "cold_start": not scored,
        "regenerating": bool(unscored),
    }


async def daily_rerate_stale() -> None:
    """Catch-all: re-rate any stale rows that the debounced path missed."""
    db = SessionLocal()
    try:
        for user in db.query(User).all():
            event_ids = _stale_future_event_ids(user.id, db)
            if event_ids:
                log.info("scoring.daily_rerate_start",
                         user_id=user.id, events=len(event_ids))
                await _rerate_specific(user, event_ids, db)
    finally:
        db.close()
