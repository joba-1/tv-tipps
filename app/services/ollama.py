"""Thin async wrapper around the local Ollama /api/generate endpoint."""
from __future__ import annotations
import contextvars
import json
import threading
from collections import defaultdict
import httpx
from app.logging_setup import get_logger
from app.services.forensics import dump_failure
from config import settings

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(240.0)
# format=json (set on payload) already grammar-constrains the OUTPUT shape, so we
# can give temperature room for ranking variety without risking structure drift.
# temperature=0.2 → still mostly deterministic on strong signals, lets ties between
#                   similar-strength candidates break differently across calls.
# num_ctx=16384   → prefix (~3,700) + 50-event LISTE (~2,500) + output (~1,400)
#                    fits with ~9k headroom. VRAM cost ~2 GB KV cache extra vs
#                    8192 — fine on the 16 GB GPU.
# num_predict=5000 → for batch=100, measured ~39 tokens/event under the schema
#                    → ~3900 typical, 5000 leaves ~28 % headroom. Output gen
#                    dominates wall time, so don't over-allocate (no extra cost
#                    when unused but the cap protects against runaway responses).
NUM_CTX = 16384
NUM_PREDICT = 5000
# Caller is overflowing when input alone exceeds ctx, or when the sum is within
# this margin of ctx (Ollama silently truncates in either case).
_CTX_SAFETY_MARGIN = 100
_OPTIONS = {"num_ctx": NUM_CTX, "num_predict": NUM_PREDICT, "temperature": 0.2}


# ── Usage stats ──────────────────────────────────────────────────────────────
# Per-caller running counters for prompt + completion tokens reported by Ollama.
# Reset on process restart; query via /api/admin/ollama-stats.

class _Acc:
    __slots__ = ("count", "sum", "min", "max")

    def __init__(self) -> None:
        self.count = 0
        self.sum = 0
        self.min = 0
        self.max = 0

    def add(self, v: int) -> None:
        if v <= 0:
            return
        self.count += 1
        self.sum += v
        self.min = v if self.min == 0 else min(self.min, v)
        self.max = max(self.max, v)

    def as_dict(self) -> dict:
        avg = (self.sum / self.count) if self.count else 0
        return {"count": self.count, "sum": self.sum, "min": self.min,
                "max": self.max, "avg": round(avg, 1)}


_stats_lock = threading.Lock()
_prompt_stats: dict[str, _Acc] = defaultdict(_Acc)
_completion_stats: dict[str, _Acc] = defaultdict(_Acc)

# Per-call usage info exposed to the caller for overflow handling.
_last_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ollama_last_usage", default=None
)


def last_usage() -> dict | None:
    """Usage of the most recent ask_json call in this async task.
    Keys: prompt_tokens, completion_tokens, num_ctx,
          prompt_overflow (input was truncated by Ollama),
          completion_truncated (output likely truncated)."""
    return _last_usage.get()


def _record(caller: str, prompt_tokens: int, completion_tokens: int, ctx: int) -> None:
    with _stats_lock:
        _prompt_stats[caller].add(prompt_tokens)
        _completion_stats[caller].add(completion_tokens)
    pct = round(100.0 * prompt_tokens / ctx, 1) if ctx else 0
    prompt_overflow = prompt_tokens > ctx
    # Truncation can hit either the input context (ctx) or the output cap
    # (num_predict). The ctx-budget check alone misses the common case where
    # the response runs into the num_predict ceiling well before ctx is full.
    ctx_full = (prompt_tokens + completion_tokens) >= (ctx - _CTX_SAFETY_MARGIN)
    output_capped = completion_tokens >= (NUM_PREDICT - _CTX_SAFETY_MARGIN)
    completion_truncated = ctx_full or output_capped
    _last_usage.set({
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "num_ctx": ctx,
        "prompt_overflow": prompt_overflow,
        "completion_truncated": completion_truncated,
    })
    log.info("ollama.usage", caller=caller, model=settings.ollama_model,
             prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
             num_ctx=ctx, ctx_used_pct=pct,
             prompt_overflow=prompt_overflow,
             completion_truncated=completion_truncated)


def get_stats() -> dict:
    """Snapshot of token usage per caller since process start."""
    with _stats_lock:
        callers = sorted(set(_prompt_stats) | set(_completion_stats))
        per_caller = {}
        total_p = _Acc()
        total_c = _Acc()
        for c in callers:
            per_caller[c] = {
                "prompt": _prompt_stats[c].as_dict(),
                "completion": _completion_stats[c].as_dict(),
            }
            # Aggregate totals by replaying the running stats. We can't recover
            # per-call values, so totals show the union of min/max and summed counts.
            p = _prompt_stats[c]
            t = _completion_stats[c]
            if p.count:
                total_p.count += p.count
                total_p.sum += p.sum
                total_p.min = p.min if total_p.min == 0 else min(total_p.min, p.min)
                total_p.max = max(total_p.max, p.max)
            if t.count:
                total_c.count += t.count
                total_c.sum += t.sum
                total_c.min = t.min if total_c.min == 0 else min(total_c.min, t.min)
                total_c.max = max(total_c.max, t.max)
        return {
            "model": settings.ollama_model,
            "num_ctx": NUM_CTX,
            "per_caller": per_caller,
            "total": {"prompt": total_p.as_dict(), "completion": total_c.as_dict()},
        }


def reset_stats() -> None:
    with _stats_lock:
        _prompt_stats.clear()
        _completion_stats.clear()




# ── Client ───────────────────────────────────────────────────────────────────

async def ask_json(
    prompt: str,
    caller: str = "unknown",
    format_schema: dict | None = None,
) -> dict | str | None:
    """POST prompt to Ollama. Returns:
      - dict: parsed JSON object
      - str:  raw text response when JSON parse fails (caller may salvage)
      - None: transport/HTTP failure
    Retries once on parse failure with a corrective hint.
    `caller` tags the call for the usage-stats endpoint (e.g. "recs", "i18n").
    `format_schema`, if given, is a full JSON Schema passed to Ollama's `format`
    field — grammar-constrains output shape (incl. array length via min/maxItems).
    Falls back to plain `"json"` (any valid JSON object) when omitted.
    """
    payload: dict = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        # Grammar-constrain the model to valid JSON regardless of how chatty it is.
        # The prompt still dictates which keys/shape to produce.
        "format": format_schema if format_schema is not None else "json",
        "options": _OPTIONS,
    }
    last_raw = ""
    for attempt in range(2):
        raw = ""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(f"{settings.ollama_url}/api/generate", json=payload)
                r.raise_for_status()
                body = r.json()
                # Thinking models (e.g. qwen3.5) with format=json emit the JSON
                # into `thinking` and leave `response` empty. Fall back to it.
                raw = (body.get("response") or "").strip()
                if not raw:
                    raw = (body.get("thinking") or "").strip()
                last_raw = raw
                _record(caller,
                        int(body.get("prompt_eval_count") or 0),
                        int(body.get("eval_count") or 0),
                        NUM_CTX)
                # Extract JSON object/array from response (strip any surrounding prose)
                import re as _re
                m = _re.search(r'\{.*\}', raw, _re.DOTALL)
                candidate = m.group(0) if m else raw
                result = json.loads(candidate)
                log.info("ollama.ok", model=settings.ollama_model, attempt=attempt, caller=caller)
                return result
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            log.warning("ollama.request_failed", attempt=attempt, error=repr(e), caller=caller)
            return None
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("ollama.parse_failed", attempt=attempt, error=repr(e),
                        raw_snippet=raw[:400], caller=caller)
            dump_failure("ollama", tag=f"{caller}_a{attempt}", caller=caller,
                         attempt=attempt, error=str(e),
                         prompt=payload["prompt"], raw=raw)
            if attempt == 0:
                payload["prompt"] = (
                    prompt
                    + f"\n\nFEHLER: Vorherige Antwort war kein gültiges JSON: {e}. Bitte nur gültiges JSON zurückgeben."
                )
            else:
                return last_raw or None
    return last_raw or None
