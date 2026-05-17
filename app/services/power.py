"""Power management: generic wake/sleep per receiver based on configured power_method."""
from __future__ import annotations
import socket
import httpx
from config import ReceiverConfig, settings
from app.logging_setup import get_logger

log = get_logger(__name__)

_NEWSTATE_STANDBY = 4   # OpenWebif light standby — network stays up, WOL still works


def _send_wol(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic, (broadcast, port))


async def _wol_wake(rcfg: ReceiverConfig) -> bool:
    mac = rcfg.wol_mac
    if not mac:
        log.warning("power.wol_skipped", receiver=rcfg.name, reason="no wol_mac configured")
        return False
    try:
        _send_wol(mac)
        log.info("power.wol_sent", receiver=rcfg.name, mac=mac)
        return True
    except Exception as e:
        log.error("power.wol_error", receiver=rcfg.name, error=str(e))
        return False


async def _wol_sleep(rcfg: ReceiverConfig) -> bool:
    """Put receiver into light standby via OpenWebif (WOL can still wake it)."""
    url = f"http://{rcfg.ip}/api/powerstate?newstate={_NEWSTATE_STANDBY}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            ok = resp.status_code == 200
            log.info("power.standby_sent", receiver=rcfg.name, ok=ok)
            return ok
    except Exception:
        log.debug("power.standby_skipped", receiver=rcfg.name, reason="unreachable")
        return False


async def _intertechno_power(rcfg: ReceiverConfig, on: bool) -> bool:
    """Switch via joba-1/IntertechnoGateway. Its /change endpoint only knows the
    button names from its own HTML: 'button-a'..'button-d' to select a family,
    then 'button-1-on'..'button-3-on'/'button-x-on' (and -off variants) to fire.
    Anything else is silently ignored — that's why our old 'D2on' format never
    triggered the device. We do the two-step (family-select, then on/off)."""
    url = settings.intertechno_url
    family = rcfg.intertechno_family or settings.intertechno_family
    device = rcfg.intertechno_device if rcfg.intertechno_family else settings.intertechno_device
    if not url or not family:
        log.warning("power.intertechno_skipped", receiver=rcfg.name, reason="not configured")
        return False
    fam_letter = str(family).strip().lower()[:1]
    if fam_letter not in ("a", "b", "c", "d"):
        log.warning("power.intertechno_bad_family", receiver=rcfg.name, family=family)
        return False
    if device not in (1, 2, 3):
        log.warning("power.intertechno_bad_device", receiver=rcfg.name,
                    device=device, hint="gateway only exposes devices 1/2/3")
        return False
    family_btn = f"button-{fam_letter}"
    action_btn = f"button-{device}-{'on' if on else 'off'}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r1 = await client.post(f"{url}/change", data={"button": family_btn})
            r2 = await client.post(f"{url}/change", data={"button": action_btn})
            ok = r1.status_code < 400 and r2.status_code < 400
            log.info("power.intertechno", receiver=rcfg.name, on=on,
                     family_btn=family_btn, action_btn=action_btn,
                     status1=r1.status_code, status2=r2.status_code, ok=ok)
            return ok
    except Exception as e:
        log.error("power.intertechno_error", receiver=rcfg.name, error=str(e))
        return False


async def wake_receiver(rcfg: ReceiverConfig) -> bool:
    """Wake receiver using its configured power_method."""
    if rcfg.power_method == "wol":
        return await _wol_wake(rcfg)
    elif rcfg.power_method == "intertechno":
        return await _intertechno_power(rcfg, on=True)
    log.debug("power.wake_noop", receiver=rcfg.name, reason="power_method=none")
    return False


async def sleep_receiver(rcfg: ReceiverConfig) -> bool:
    """Put receiver to sleep/standby using its configured power_method."""
    if rcfg.power_method == "wol":
        return await _wol_sleep(rcfg)
    elif rcfg.power_method == "intertechno":
        return await _intertechno_power(rcfg, on=False)
    log.debug("power.sleep_noop", receiver=rcfg.name, reason="power_method=none")
    return False
