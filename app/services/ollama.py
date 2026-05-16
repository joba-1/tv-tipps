"""Thin async wrapper around the local Ollama /api/generate endpoint."""
from __future__ import annotations
import json
import httpx
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(240.0)
# temperature=0 → fully deterministic, max coherence with schema instructions.
# num_ctx=16384 → fits the largest prompts ("today" with 100 candidates + history).
_OPTIONS = {"num_ctx": 16384, "temperature": 0.0}


async def ask_json(prompt: str) -> dict | str | None:
    """POST prompt to Ollama. Returns:
      - dict: parsed JSON object
      - str:  raw text response when JSON parse fails (caller may salvage)
      - None: transport/HTTP failure
    Retries once on parse failure with a corrective hint.
    """
    payload: dict = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        # Grammar-constrain the model to valid JSON regardless of how chatty it is.
        # The prompt still dictates which keys/shape to produce.
        "format": "json",
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
                raw = (body.get("response") or "").strip()
                last_raw = raw
                # Extract JSON object/array from response (strip any surrounding prose)
                import re as _re
                m = _re.search(r'\{.*\}', raw, _re.DOTALL)
                candidate = m.group(0) if m else raw
                result = json.loads(candidate)
                log.info("ollama.ok", model=settings.ollama_model, attempt=attempt)
                return result
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            log.warning("ollama.request_failed", attempt=attempt, error=str(e))
            return None
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("ollama.parse_failed", attempt=attempt, error=str(e),
                        raw_snippet=raw[:400])
            if attempt == 0:
                payload["prompt"] = (
                    prompt
                    + f"\n\nFEHLER: Vorherige Antwort war kein gültiges JSON: {e}. Bitte nur gültiges JSON zurückgeben."
                )
            else:
                return last_raw or None
    return last_raw or None
