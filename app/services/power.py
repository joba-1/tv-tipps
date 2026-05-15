"""Power management: WOL for box15, IntertechnoGateway RF switch for box17."""
from __future__ import annotations
import socket
import httpx
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)

# OpenWebif newstate values
_NEWSTATE_STANDBY = 4       # light standby: network stays up, WOL works
_NEWSTATE_DEEP_STANDBY = 5  # deep standby: cuts power, no WOL


def _send_wol(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic, (broadcast, port))


async def wake_box15() -> bool:
    """Send WOL magic packet to box15 (must be in light standby, not deep standby)."""
    mac = settings.box15_mac
    if not mac or mac.replace(":", "") == "0" * 12 or mac == "00:00:00:00:00:00":
        log.warning("power.wol_skipped", reason="BOX15_MAC not configured")
        return False
    try:
        _send_wol(mac)
        log.info("power.wol_sent", receiver="box15", mac=mac)
        return True
    except Exception as e:
        log.error("power.wol_error", receiver="box15", error=str(e))
        return False


async def sleep_box15() -> bool:
    """Put box15 into light standby via OpenWebif (so WOL can wake it later)."""
    rcfg = next((r for r in settings.receivers if r.name == "box15"), None)
    if not rcfg:
        return False
    url = f"http://{rcfg.ip}/api/powerstate?newstate={_NEWSTATE_STANDBY}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            ok = resp.status_code == 200
            log.info("power.standby_sent", receiver="box15", ok=ok)
            return ok
    except Exception:
        # Box is likely already off — that's fine
        log.debug("power.standby_skipped", receiver="box15", reason="unreachable")
        return False


async def power_on_box17() -> bool:
    """Switch box17 mains power on via IntertechnoGateway."""
    return await _intertechno(on=True)


async def power_off_box17() -> bool:
    """Switch box17 mains power off via IntertechnoGateway."""
    return await _intertechno(on=False)


async def _intertechno(on: bool) -> bool:
    url = settings.intertechno_url
    family = settings.intertechno_family
    device = settings.intertechno_device
    if not url or not family:
        log.warning("power.intertechno_skipped", reason="INTERTECHNO_URL/FAMILY not configured")
        return False
    # IntertechnoGateway POST /change  params: button=<family><device><on|off>
    # e.g. family="A", device=1, on=True  →  button="A1on"
    button = f"{family}{device}{'on' if on else 'off'}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(f"{url}/change", data={"button": button})
            ok = resp.status_code == 200
            log.info("power.intertechno", receiver="box17", on=on, button=button, ok=ok)
            return ok
    except Exception as e:
        log.error("power.intertechno_error", receiver="box17", error=str(e))
        return False
