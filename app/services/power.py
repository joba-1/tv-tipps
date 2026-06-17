"""Power management: generic wake/sleep per receiver based on configured power_method.

Each function returns ``(ok, reason)``. ``reason`` is ``None`` on success and a
short human-readable string on failure — callers surface it directly so the user
sees the real cause instead of a generic warning.
"""
from __future__ import annotations
import socket
import httpx
from config import ReceiverConfig, settings
from app.logging_setup import get_logger

log = get_logger(__name__)

PowerResult = tuple[bool, str | None]


def _send_wol(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic, (broadcast, port))


async def _wol_wake(rcfg: ReceiverConfig) -> PowerResult:
    mac = rcfg.wol_mac
    if not mac:
        log.warning("power.wol_skipped", receiver=rcfg.name, reason="no wol_mac configured")
        return False, "wol_mac not configured"
    try:
        _send_wol(mac)
        log.info("power.wol_sent", receiver=rcfg.name, mac=mac)
        return True, None
    except Exception as e:
        log.error("power.wol_error", receiver=rcfg.name, error=str(e))
        return False, f"WOL send failed: {e}"


async def openwebif_standby(rcfg: ReceiverConfig) -> PowerResult:
    """Put receiver into light standby via OpenWebif. Works regardless of
    power_method as long as the box is currently reachable. `newstate` is
    per-receiver (rcfg.standby_newstate) because firmwares differ — VTi uses 4,
    openATV uses 5."""
    url = f"http://{rcfg.ip}/api/powerstate?newstate={rcfg.standby_newstate}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                log.info("power.standby_sent", receiver=rcfg.name, ok=True,
                         newstate=rcfg.standby_newstate)
                return True, None
            log.info("power.standby_sent", receiver=rcfg.name, ok=False,
                     status=resp.status_code, newstate=rcfg.standby_newstate)
            return False, f"OpenWebif returned HTTP {resp.status_code}"
    except Exception as e:
        log.debug("power.standby_skipped", receiver=rcfg.name, reason="unreachable")
        return False, f"OpenWebif unreachable: {e}"


# Backwards-compat alias — sleep_receiver still routes here for WOL boxes.
_wol_sleep = openwebif_standby


async def _intertechno_power(rcfg: ReceiverConfig, on: bool) -> PowerResult:
    """Switch via joba-1/IntertechnoGateway. Its /change endpoint only knows the
    button names from its own HTML: 'button-a'..'button-d' to select a family,
    then 'button-1-on'..'button-3-on'/'button-x-on' (and -off variants) to fire.
    Anything else is silently ignored — that's why our old 'D2on' format never
    triggered the device. We do the two-step (family-select, then on/off).

    URL precedence: per-receiver rcfg.intertechno_url wins over the global
    settings.intertechno_url, so each box can use its own gateway.
    """
    url = (rcfg.intertechno_url or settings.intertechno_url or "").rstrip("/")
    family = rcfg.intertechno_family or settings.intertechno_family
    device = rcfg.intertechno_device if rcfg.intertechno_family else settings.intertechno_device
    if not url:
        log.warning("power.intertechno_skipped", receiver=rcfg.name, reason="no intertechno_url")
        return False, "intertechno_url not configured"
    if not family:
        log.warning("power.intertechno_skipped", receiver=rcfg.name, reason="no intertechno_family")
        return False, "intertechno_family not configured"
    fam_letter = str(family).strip().lower()[:1]
    if fam_letter not in ("a", "b", "c", "d"):
        log.warning("power.intertechno_bad_family", receiver=rcfg.name, family=family)
        return False, f"invalid intertechno_family '{family}' (must be A-D)"
    if device not in (1, 2, 3):
        log.warning("power.intertechno_bad_device", receiver=rcfg.name,
                    device=device, hint="gateway only exposes devices 1/2/3")
        return False, f"invalid intertechno_device {device} (must be 1-3)"
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
            if ok:
                return True, None
            return False, f"gateway returned HTTP {r1.status_code}/{r2.status_code}"
    except Exception as e:
        log.error("power.intertechno_error", receiver=rcfg.name, error=str(e))
        return False, f"gateway unreachable: {e}"


async def wake_receiver(rcfg: ReceiverConfig) -> PowerResult:
    """Wake receiver using its configured power_method. Returns (ok, reason)."""
    if rcfg.power_method == "wol":
        return await _wol_wake(rcfg)
    elif rcfg.power_method == "intertechno":
        return await _intertechno_power(rcfg, on=True)
    log.debug("power.wake_noop", receiver=rcfg.name, reason="power_method=none")
    return False, "power_method is 'none' for this receiver"


async def sleep_receiver(rcfg: ReceiverConfig) -> PowerResult:
    """Put receiver to sleep/standby using its configured power_method.
    Returns (ok, reason)."""
    if rcfg.power_method == "wol":
        return await _wol_sleep(rcfg)
    elif rcfg.power_method == "intertechno":
        return await _intertechno_power(rcfg, on=False)
    log.debug("power.sleep_noop", receiver=rcfg.name, reason="power_method=none")
    return False, "power_method is 'none' for this receiver"
