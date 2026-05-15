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
        """Returns 'on', 'standby', 'deep_standby', or 'unknown'."""
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

    async def send_key(self, key_name: str) -> bool:
        code = RC_KEYS.get(key_name)
        if code is None:
            log.warning("enigma.unknown_key", key=key_name)
            return False
        data = await self._get("/api/remotecontrol", params={"command": code})
        return bool(data and data.get("result"))

    def picon_url(self, picon_path: str) -> str:
        """Return full URL for a picon path from the receiver."""
        return f"{self.base_url}{picon_path}"
