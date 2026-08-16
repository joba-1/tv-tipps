"""Tests for app/services/power.py — the EPG wake path.

wake_for_epg exists so a normally-powered-down box can serve the nightly EPG
sweep. The important property is that light standby goes out on the *first*
OpenWebif call that succeeds: every extra second the box spends fully booted is
a second its HDMI output can switch the TV on.
"""
from __future__ import annotations
import pytest
from config import ReceiverConfig
import app.services.power as power
import app.enigma.client as enigma_client


def _rcfg(**kw) -> ReceiverConfig:
    base = dict(name="octagon", ip="10.0.0.17", default_user="", power_method="intertechno",
                intertechno_family="C", intertechno_device=2,
                intertechno_url="http://gw", standby_newstate=5, epg_wake=True)
    base.update(kw)
    return ReceiverConfig(**base)


@pytest.fixture
def spy(monkeypatch):
    """Fake the power switch, the OpenWebif standby call and the online probe.
    `boots_after` = number of probes that fail before the box answers."""
    calls: dict = {"wake": 0, "standby": 0, "probes": 0, "sleeps": 0}
    state = {"boots_after": 0, "standby_ok": True, "dead_after_standby": False}

    async def fake_wake(rcfg):
        calls["wake"] += 1
        return True, None

    async def fake_standby(rcfg):
        calls["standby"] = calls["probes"]  # probe count at the moment we fire
        return (True, None) if state["standby_ok"] else (False, "HTTP 500")

    class FakeClient:
        def __init__(self, ip, mock=False):
            pass

        async def is_online(self):
            calls["probes"] += 1
            if calls["standby"] and state["dead_after_standby"]:
                return False
            return calls["probes"] > state["boots_after"]

    monkeypatch.setattr(power, "wake_receiver", fake_wake)
    monkeypatch.setattr(power, "openwebif_standby", fake_standby)
    monkeypatch.setattr(enigma_client, "EnigmaClient", FakeClient)
    # No real waiting between probes.
    monkeypatch.setattr(power, "_WAKE_POLL_INTERVAL_SEC", 0)
    return {"calls": calls, "state": state}


class TestWakeForEpg:
    @pytest.mark.asyncio
    async def test_standby_fires_on_first_successful_probe(self, spy):
        spy["state"]["boots_after"] = 3  # box answers on the 4th probe
        ok, reason = await power.wake_for_epg(_rcfg())
        assert (ok, reason) == (True, None)
        assert spy["calls"]["wake"] == 1
        # Standby went out on the 4th probe — the first that answered — not one
        # poll interval later. The remaining probes are the post-standby settle
        # check that keeps a mid-transition box out of the sweep.
        assert spy["calls"]["standby"] == 4
        assert spy["calls"]["probes"] == 6

    @pytest.mark.asyncio
    async def test_reports_timeout_when_box_never_answers(self, spy, monkeypatch):
        spy["state"]["boots_after"] = 10**9
        monkeypatch.setattr(power, "_WAKE_BOOT_TIMEOUT_SEC", 0.05)
        ok, reason = await power.wake_for_epg(_rcfg())
        assert ok is False
        assert "did not come up" in reason

    @pytest.mark.asyncio
    async def test_failed_standby_still_counts_as_woken(self, spy):
        """The box is up and answering, which is all the sweep needs — the
        caller must still power it back down afterwards."""
        spy["state"]["standby_ok"] = False
        ok, reason = await power.wake_for_epg(_rcfg())
        assert ok is True
        assert "standby failed" in reason

    @pytest.mark.asyncio
    async def test_box_unreachable_after_standby_is_reported(self, spy, monkeypatch):
        """Entering standby drops the box's connections; if it never answers
        again the sweep can't use it, and the caller must hear about it."""
        spy["state"]["dead_after_standby"] = True
        monkeypatch.setattr(power, "_WAKE_SETTLE_TIMEOUT_SEC", 0.05)
        ok, reason = await power.wake_for_epg(_rcfg())
        assert ok is True  # still powered on — caller must switch it back off
        assert "unreachable" in reason

    @pytest.mark.asyncio
    async def test_no_probing_when_the_switch_fails(self, spy, monkeypatch):
        async def failing_wake(rcfg):
            return False, "gateway unreachable"
        monkeypatch.setattr(power, "wake_receiver", failing_wake)
        ok, reason = await power.wake_for_epg(_rcfg())
        assert (ok, reason) == (False, "gateway unreachable")
        assert spy["calls"]["probes"] == 0


class TestShutdownForEpg:
    """Cutting the mains is an unclean shutdown: enigma2 never writes epg.dat and
    the box boots with an empty EPG cache. Deep standby first, mains after — but
    the mains always come after, whatever happened before."""

    @pytest.fixture
    def steps(self, monkeypatch):
        seq: list[str] = []
        state = {"deep_ok": True, "goes_down_after": 1}

        async def fake_deep(rcfg):
            seq.append("deep_standby")
            return (True, None) if state["deep_ok"] else (False, "HTTP 500")

        async def fake_sleep(rcfg):
            seq.append("mains_off")
            return True, None

        class FakeClient:
            def __init__(self, ip, mock=False):
                self.probes = 0

            async def is_online(self):
                self.probes += 1
                return self.probes <= state["goes_down_after"]

        monkeypatch.setattr(power, "openwebif_deep_standby", fake_deep)
        monkeypatch.setattr(power, "sleep_receiver", fake_sleep)
        monkeypatch.setattr(enigma_client, "EnigmaClient", FakeClient)
        monkeypatch.setattr(power, "_WAKE_POLL_INTERVAL_SEC", 0)
        monkeypatch.setattr(power, "_SHUTDOWN_GRACE_SEC", 0)
        return {"seq": seq, "state": state}

    @pytest.mark.asyncio
    async def test_deep_standby_precedes_the_mains_cut(self, steps):
        ok, _ = await power.shutdown_for_epg(_rcfg())
        assert ok is True
        assert steps["seq"] == ["deep_standby", "mains_off"]

    @pytest.mark.asyncio
    async def test_mains_cut_even_when_deep_standby_fails(self, steps):
        """A box left powered because a command went unanswered is the one
        outcome the user would notice."""
        steps["state"]["deep_ok"] = False
        ok, _ = await power.shutdown_for_epg(_rcfg())
        assert ok is True
        assert steps["seq"] == ["deep_standby", "mains_off"]

    @pytest.mark.asyncio
    async def test_mains_cut_even_when_the_box_never_goes_down(self, steps, monkeypatch):
        steps["state"]["goes_down_after"] = 10**9  # answers forever
        monkeypatch.setattr(power, "_SHUTDOWN_TIMEOUT_SEC", 0.05)
        ok, _ = await power.shutdown_for_epg(_rcfg())
        assert ok is True
        assert steps["seq"][-1] == "mains_off"

    @pytest.mark.asyncio
    async def test_wol_receiver_is_left_reachable(self, steps):
        """A WOL box must stay on the network or it can never be woken again —
        no deep standby for those."""
        ok, _ = await power.shutdown_for_epg(_rcfg(power_method="wol", wol_mac="00:11:22:33:44:55"))
        assert steps["seq"] == ["mains_off"]  # i.e. plain sleep_receiver
