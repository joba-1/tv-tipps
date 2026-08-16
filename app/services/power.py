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


_DEEP_STANDBY_NEWSTATE = 1  # OpenWebif: 1 = deep standby (a real enigma2 shutdown)


async def _openwebif_powerstate(rcfg: ReceiverConfig, newstate: int,
                                what: str) -> PowerResult:
    url = f"http://{rcfg.ip}/api/powerstate?newstate={newstate}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                log.info("power.standby_sent", receiver=rcfg.name, ok=True,
                         newstate=newstate, kind=what)
                return True, None
            log.info("power.standby_sent", receiver=rcfg.name, ok=False,
                     status=resp.status_code, newstate=newstate, kind=what)
            return False, f"OpenWebif returned HTTP {resp.status_code}"
    except Exception as e:
        log.debug("power.standby_skipped", receiver=rcfg.name, reason="unreachable")
        return False, f"OpenWebif unreachable: {e}"


async def openwebif_standby(rcfg: ReceiverConfig) -> PowerResult:
    """Put receiver into light standby via OpenWebif. Works regardless of
    power_method as long as the box is currently reachable. `newstate` is
    per-receiver (rcfg.standby_newstate) because firmwares differ — VTi uses 4,
    openATV uses 5."""
    return await _openwebif_powerstate(rcfg, rcfg.standby_newstate, "light")


async def openwebif_deep_standby(rcfg: ReceiverConfig) -> PowerResult:
    """Shut enigma2 down properly. Unlike light standby this ends the process,
    which is what makes it flush its EPG cache to /etc/enigma2/epg.dat."""
    return await _openwebif_powerstate(rcfg, _DEEP_STANDBY_NEWSTATE, "deep")


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


# Nightly EPG wake: how long we wait for OpenWebif to answer after power-on,
# and how often we retry. The poll is deliberately tight — every second the box
# runs with a live HDMI output is a second it can switch the TV on, so we want
# to fire the standby command on the very first request that succeeds.
_WAKE_BOOT_TIMEOUT_SEC = 240
_WAKE_POLL_INTERVAL_SEC = 1.0
# Entering standby tears down the box's open connections and enigma2 stops
# accepting new ones for a moment, so the first probe right after the command
# fails (measured: the very next request, 3 ms later, saw the box as offline and
# the sweep skipped it — while a second later it answered /api/about in 42 ms).
# Wait for it to answer again before declaring the box ready for the sweep.
_WAKE_SETTLE_TIMEOUT_SEC = 60


async def _await_reachable(client, timeout_sec: float) -> bool:
    """Poll OpenWebif until it answers again. Two consecutive hits, because the
    first request after a standby transition can succeed on a connection that
    the box is about to drop."""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    hits = 0
    while loop.time() < deadline:
        hits = hits + 1 if await client.is_online() else 0
        if hits >= 2:
            return True
        await asyncio.sleep(_WAKE_POLL_INTERVAL_SEC)
    return False


async def wake_for_epg(rcfg: ReceiverConfig) -> PowerResult:
    """Power a box up for an EPG sweep, then drop it into light standby as soon
    as OpenWebif responds.

    Light standby keeps the network stack and the EPG API alive but blanks the
    video output, so the box is usable as an EPG source without driving the TV.
    We cannot suppress the HDMI signal for the boot itself — that happens in the
    box's firmware before anything of ours is reachable — so the standby command
    goes out on the first successful OpenWebif call. Set the box's own startup
    behaviour to "standby" (and disable HDMI-CEC one-touch-play) if the TV must
    never wake at all.
    """
    import asyncio
    from app.enigma.client import EnigmaClient, reset_pool

    ok, reason = await wake_receiver(rcfg)
    if not ok:
        return False, reason

    client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAKE_BOOT_TIMEOUT_SEC
    while loop.time() < deadline:
        if await client.is_online():
            boot_sec = round(_WAKE_BOOT_TIMEOUT_SEC - (deadline - loop.time()))
            standby_ok, standby_reason = await openwebif_standby(rcfg)
            # The box drops its open connections on the way into standby. Bin
            # the pool before the sweep inherits a dead socket and concludes the
            # receiver is offline.
            await reset_pool()
            settled = await _await_reachable(client, _WAKE_SETTLE_TIMEOUT_SEC)
            log.info("power.epg_wake_up", receiver=rcfg.name, boot_sec=boot_sec,
                     standby=standby_ok, settled=settled)
            if not settled:
                return True, (f"unreachable {_WAKE_SETTLE_TIMEOUT_SEC}s after standby"
                              " — EPG sweep will skip it")
            if not standby_ok:
                # The box is up and answering, which is all the sweep needs —
                # report success but keep the reason so the caller can log it.
                return True, f"online but standby failed: {standby_reason}"
            return True, None
        await asyncio.sleep(_WAKE_POLL_INTERVAL_SEC)

    log.warning("power.epg_wake_timeout", receiver=rcfg.name,
                timeout_sec=_WAKE_BOOT_TIMEOUT_SEC)
    return False, f"receiver did not come up within {_WAKE_BOOT_TIMEOUT_SEC}s"


# Clean shutdown before pulling the mains: how long we wait for enigma2 to
# actually go down, and how long we let the flash settle afterwards.
_SHUTDOWN_TIMEOUT_SEC = 90
_SHUTDOWN_GRACE_SEC = 5


async def _await_unreachable(client, timeout_sec: float) -> bool:
    """Wait until the box stops answering — our only visible sign that enigma2
    has finished shutting down. Two consecutive misses, so a blip mid-shutdown
    doesn't read as "down" while it is still writing."""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    misses = 0
    while loop.time() < deadline:
        misses = 0 if await client.is_online() else misses + 1
        if misses >= 2:
            return True
        await asyncio.sleep(_WAKE_POLL_INTERVAL_SEC)
    return False


async def shutdown_for_epg(rcfg: ReceiverConfig) -> PowerResult:
    """Put a box away after an EPG sweep, giving enigma2 the chance to persist
    its EPG cache first.

    Cutting the mains is an unclean shutdown: enigma2 never writes
    /etc/enigma2/epg.dat, so the box boots with an empty EPG cache every single
    night and can only offer the transponder it happens to be tuned to. That is
    what makes each night's harvest a lottery. Deep standby ends the process
    properly, so the next boot starts warm and the zap tour only has to top up.

    The mains are cut either way — a receiver left powered because a shutdown
    command went unanswered is the one outcome the user would actually notice.
    """
    import asyncio
    from app.enigma.client import EnigmaClient, reset_pool

    if rcfg.power_method != "intertechno":
        # WOL boxes must stay reachable on the network to be woken again.
        return await sleep_receiver(rcfg)

    ok, reason = await openwebif_deep_standby(rcfg)
    if ok:
        await reset_pool()
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        went_down = await _await_unreachable(client, _SHUTDOWN_TIMEOUT_SEC)
        if went_down:
            await asyncio.sleep(_SHUTDOWN_GRACE_SEC)
        else:
            log.warning("power.shutdown_not_confirmed", receiver=rcfg.name,
                        timeout_sec=_SHUTDOWN_TIMEOUT_SEC)
        log.info("power.clean_shutdown", receiver=rcfg.name, confirmed=went_down)
    else:
        log.warning("power.deep_standby_failed", receiver=rcfg.name, reason=reason)

    return await sleep_receiver(rcfg)


async def sleep_receiver(rcfg: ReceiverConfig) -> PowerResult:
    """Put receiver to sleep/standby using its configured power_method.
    Returns (ok, reason)."""
    if rcfg.power_method == "wol":
        return await _wol_sleep(rcfg)
    elif rcfg.power_method == "intertechno":
        return await _intertechno_power(rcfg, on=False)
    log.debug("power.sleep_noop", receiver=rcfg.name, reason="power_method=none")
    return False, "power_method is 'none' for this receiver"
