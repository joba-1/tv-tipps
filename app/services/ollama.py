"""Thin async wrapper around the local Ollama /api/generate endpoint."""
from __future__ import annotations
import json
import httpx
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(180.0)
_OPTIONS = {"num_ctx": 8192, "temperature": 0.1}


async def ask_json(prompt: str) -> dict | None:
    """POST prompt to Ollama, return parsed JSON dict or None on failure.

    Relies on prompt instructions to produce JSON. Retries once on parse failure.
    """
    payload: dict = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": _OPTIONS,
    }
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(f"{settings.ollama_url}/api/generate", json=payload)
                r.raise_for_status()
                body = r.json()
                raw = (body.get("response") or "").strip()
                # Extract JSON object/array from response (strip any surrounding prose)
                import re as _re
                m = _re.search(r'\{.*\}', raw, _re.DOTALL)
                if m:
                    raw = m.group(0)
                result = json.loads(raw)
                log.info("ollama.ok", model=settings.ollama_model, attempt=attempt)
                return result
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            log.warning("ollama.request_failed", attempt=attempt, error=str(e))
            return None
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("ollama.parse_failed", attempt=attempt, error=str(e))
            if attempt == 0:
                payload["prompt"] = (
                    prompt
                    + f"\n\nFEHLER: Vorherige Antwort war kein gültiges JSON: {e}. Bitte nur gültiges JSON zurückgeben."
                )
            else:
                return None
    return None
