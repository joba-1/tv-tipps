from __future__ import annotations
import json
import time
from pathlib import Path
import httpx
from app.logging_setup import get_logger

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(5.0)
# A failure faster than this is a dropped keep-alive connection, not an
# unreachable box — worth one retry on a fresh socket.
_STALE_CONN_SEC = 0.3
# OpenWebif timerlist state: 0 waiting, 1 prepared, 2 running, 3 ended.
_TIMER_STATE_RUNNING = 2

# One pooled client shared by all receivers (created lazily inside the running
# loop, per-request timeouts). Closed from the app's lifespan shutdown.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def reset_pool() -> None:
    """Drop every pooled connection, so the next request dials fresh.

    A receiver entering or leaving standby silently drops the TCP connections we
    keep alive. httpx hands the next request one of those dead sockets and it
    fails instantly, which reads as "receiver offline" — that is what made the
    nightly sweep skip a box we had just woken and confirmed reachable. Requests
    already in flight (a concurrent poll) fail once and recover on their next
    cycle; that is cheaper than a sweep that silently collects nothing.
    """
    await aclose()

# RC key codes (Linux input event codes)
RC_KEYS: dict[str, int] = {
    "power":  116,
    "mute":   113,
    "vol_up": 115,
    "vol_dn": 114,
    "ch_up":  402,
    "ch_dn":  403,
    "up":     103,
    "down":   108,
    "left":   105,
    "right":  106,
    "ok":     352,
    "back":   174,
    "menu":   139,
    "info":   358,
    "red":    398,
    "green":  399,
    "yellow": 400,
    "blue":   401,
}


class EnigmaClient:
    def __init__(self, ip: str, mock: bool = False, fixtures_dir: str = "tests/fixtures"):
        self.ip = ip
        self.mock = mock
        self.base_url = f"http://{ip}"
        self._fixtures = Path(fixtures_dir)

    def _load_fixture(self, name: str) -> dict | None:
        path = self._fixtures / f"{name}.json"
        if path.exists():
            return json.loads(path.read_text())
        return None

    async def _get(self, path: str, params: dict | None = None,
                   idempotent: bool = True) -> dict | None:
        if self.mock:
            # Map path to fixture name (best effort)
            name = path.strip("/").replace("/", "_").split("?")[0]
            return self._load_fixture(name)
        for attempt in (1, 2):
            started = time.monotonic()
            try:
                r = await _get_client().get(f"{self.base_url}{path}",
                                            params=params, timeout=_TIMEOUT)
                r.raise_for_status()
                return r.json()
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as e:
                # A connection the box dropped fails in milliseconds with
                # something like RemoteProtocolError('illegal request line') —
                # observed on the first zap after a standby transition. Retry
                # that on a fresh socket; a real outage fails slowly and is not
                # retried. Non-idempotent calls (keypresses, timer writes) never
                # retry: a request the box did receive must not run twice.
                fast = time.monotonic() - started <= _STALE_CONN_SEC
                if attempt == 1 and idempotent and fast:
                    continue
                # Demoted to info: receivers go offline routinely (Intertechno
                # cut, deep standby) — _infer_power_state turns this into the
                # UI's "off" state. Worth noting, not an error.
                log.info("enigma.request_failed", ip=self.ip, path=path, error=repr(e))
                return None
        return None

    async def is_online(self) -> bool:
        # Tight timeout because this fires on every page-load via /api/receivers
        # and /api/remote/timers; a slow receiver shouldn't block the UI.
        for attempt in (1, 2):
            started = time.monotonic()
            try:
                r = await _get_client().head(f"{self.base_url}/api/about",
                                             timeout=httpx.Timeout(1.5))
                return r.status_code < 500
            except (httpx.RequestError, httpx.HTTPStatusError):
                # A keep-alive connection the box dropped (it does that on every
                # standby transition) fails within milliseconds; httpx evicts it,
                # so the retry gets a fresh socket. A genuinely unreachable box
                # fails slowly — don't pay that timeout twice.
                if attempt == 2 or time.monotonic() - started > _STALE_CONN_SEC:
                    return False
        return False

    async def get_power_state(self) -> str:
        """Returns 'on', 'standby', or 'unknown'.
        Deep-standby boxes don't respond to HTTP at all, so they map to 'unknown'
        (distinguishable from light standby only by client.is_online() returning False)."""
        if self.mock:
            data = self._load_fixture("powerstate")
        else:
            data = await self._get("/api/powerstate")
        if data is None:
            return "unknown"
        if data.get("instandby"):
            return "standby"
        return "on"

    async def get_all_services(self) -> dict | None:
        if self.mock:
            return self._load_fixture("getallservices")
        return await self._get("/api/getallservices")

    async def get_epg_now(self, bref: str) -> dict | None:
        if self.mock:
            return self._load_fixture("epgnow")
        return await self._get("/api/epgnow", params={"bRef": bref})

    async def get_epg_next(self, bref: str) -> dict | None:
        if self.mock:
            return self._load_fixture("epgnext")
        return await self._get("/api/epgnext", params={"bRef": bref})

    async def get_epg_service(self, sref: str, hours: int = 24) -> dict | None:
        if self.mock:
            return self._load_fixture("epgservice")
        return await self._get("/api/epgservice", params={"sRef": sref, "hours": hours})

    async def get_current(self) -> dict | None:
        if self.mock:
            return self._load_fixture("getcurrent")
        return await self._get("/api/getcurrent")

    async def zap(self, sref: str) -> bool:
        data = await self._get("/api/zap", params={"sRef": sref})
        return bool(data and data.get("result"))

    async def list_timers(self) -> list[dict]:
        """Return active timer entries from /api/timerlist."""
        data = await self._get("/api/timerlist")
        if not data:
            return []
        return data.get("timers", []) or []

    async def is_recording(self) -> bool:
        """Is a timer writing a file right now?

        A box recording from standby still reports `instandby: true`, so the
        power-state probe cannot see it. That matters for the mains switch: a
        recording survives our zap tour (enigma2 refuses to retune a busy tuner)
        but not a power cut. `justplay` timers only change channel and hold no
        file, so they do not count."""
        now = time.time()
        for t in await self.list_timers():
            if t.get("disabled") or t.get("justplay"):
                continue
            if t.get("state") == _TIMER_STATE_RUNNING:
                return True
            # Firmwares that don't report state: fall back to the time window.
            begin, end = t.get("begin"), t.get("end")
            if (isinstance(begin, (int, float)) and isinstance(end, (int, float))
                    and begin <= now <= end):
                return True
        return False

    async def user_claim(self) -> str | None:
        """Who owns the box right now — "viewer", "recording", or None if it is
        ours to zap around and switch off. Both answers mean hands off: someone
        is watching, or a timer is mid-recording."""
        if await self.get_power_state() == "on":
            return "viewer"
        if await self.is_recording():
            return "recording"
        return None

    async def delete_timer(self, sref: str, begin: int, end: int) -> bool:
        """Cancel a timer matching sref+begin+end (exact epoch seconds)."""
        params = {"sRef": sref, "begin": str(begin), "end": str(end)}
        data = await self._get("/api/timerdelete", params=params, idempotent=False)
        return bool(data and data.get("result"))

    async def add_timer(
        self, sref: str, begin: int, end: int, name: str,
        description: str = "", justplay: int = 0, eit: int | None = None,
    ) -> dict | None:
        """Schedule a recording timer via OpenWebif /api/timeradd.
        `begin`/`end` are unix epoch seconds. `justplay=0` records, `1` just zaps.
        Passing `eit` (EPG event id) helps OWIF bind the timer to the real EPG
        slot, which avoids the "manual timer didn't match an event" failure mode
        that's common when the timer window is padded around the broadcast."""
        params = {
            "sRef": sref,
            "begin": str(begin),
            "end": str(end),
            "name": name,
            "description": description or "",
            "repeated": "0",
            "afterEvent": "3",  # auto: receiver decides standby/deep-standby
            "disabled": "0",
            "justplay": str(justplay),
        }
        if eit is not None:
            params["eit"] = str(eit)
        return await self._get("/api/timeradd", params=params, idempotent=False)

    async def send_key(self, key_name: str) -> bool:
        code = RC_KEYS.get(key_name)
        if code is None:
            log.warning("enigma.unknown_key", key=key_name)
            return False
        data = await self._get("/api/remotecontrol", params={"command": code},
                               idempotent=False)
        return bool(data and data.get("result"))

    async def screenshot(self) -> bytes | None:
        """Fetch a screenshot from the receiver via /grab (PNG default, JPEG fallback)."""
        if self.mock:
            return None
        for params in ({}, {"format": "jpg"}):
            try:
                r = await _get_client().get(f"{self.base_url}/grab", params=params,
                                            timeout=httpx.Timeout(10.0))
                r.raise_for_status()
                ct = r.headers.get("content-type", "")
                if ct.startswith("image/"):
                    log.info("enigma.screenshot_ok", ip=self.ip, params=params, ct=ct, size=len(r.content))
                    return r.content
                log.warning("enigma.screenshot_not_image", ip=self.ip, params=params,
                            content_type=ct, body_preview=r.text[:120])
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                log.warning("enigma.screenshot_failed", ip=self.ip, params=params, error=str(e))
        return None

    def picon_url(self, picon_path: str) -> str:
        """Return full URL for a picon path from the receiver."""
        return f"{self.base_url}{picon_path}"
