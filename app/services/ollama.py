"""Thin async wrapper around the local Ollama /api/generate endpoint."""
from __future__ import annotations
import json
import httpx
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(60.0)
_OPTIONS = {"num_ctx": 16384, "temperature": 0.3}


async def ask_json(prompt: str) -> dict | None:
    """POST prompt to Ollama, return parsed JSON dict or None on failure.

    Retries once if the response is not valid JSON, appending the error
    to the prompt so the model can self-correct.
    """
    payload: dict = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": _OPTIONS,
    }
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(f"{settings.ollama_url}/api/generate", json=payload)
                r.raise_for_status()
                raw = r.json().get("response", "")
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
