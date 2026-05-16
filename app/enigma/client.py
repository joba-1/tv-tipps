from __future__ import annotations
import json
from pathlib import Path
import httpx
from app.logging_setup import get_logger

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(5.0)

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

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        if self.mock:
            # Map path to fixture name (best effort)
            name = path.strip("/").replace("/", "_").split("?")[0]
            return self._load_fixture(name)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(f"{self.base_url}{path}", params=params)
                r.raise_for_status()
                return r.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as e:
            log.warning("enigma.request_failed", ip=self.ip, path=path, error=str(e))
            return None

    async def is_online(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
                r = await client.head(f"{self.base_url}/api/about")
                return r.status_code < 500
        except (httpx.RequestError, httpx.HTTPStatusError):
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

    async def add_timer(
        self, sref: str, begin: int, end: int, name: str,
        description: str = "", justplay: int = 0,
    ) -> bool:
        """Schedule a recording timer via OpenWebif /api/timeradd.
        `begin`/`end` are unix epoch seconds. `justplay=0` records, `1` just zaps."""
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
        data = await self._get("/api/timeradd", params=params)
        return bool(data and data.get("result"))

    async def send_key(self, key_name: str) -> bool:
        code = RC_KEYS.get(key_name)
        if code is None:
            log.warning("enigma.unknown_key", key=key_name)
            return False
        data = await self._get("/api/remotecontrol", params={"command": code})
        return bool(data and data.get("result"))

    async def screenshot(self) -> bytes | None:
        """Fetch a screenshot from the receiver via /grab (PNG default, JPEG fallback)."""
        if self.mock:
            return None
        for params in ({}, {"format": "jpg"}):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                    r = await client.get(f"{self.base_url}/grab", params=params)
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
