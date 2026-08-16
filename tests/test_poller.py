"""Tests for the poller's viewing-session state machine (app/services/poller.py).

_poll_receiver opens its own DB session via poller.SessionLocal and creates an
EnigmaClient per poll — both are monkeypatched: the sessionmaker onto a shared
in-memory engine, the client onto a fake fed from the FAKE dict.
"""
from __future__ import annotations
import pytest
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app import models
from app.timezones import utcnow
import app.services.poller as poller
import app.services.active_viewer as active_viewer

SREF_A = "1:0:1:AAAA:1:1:0:0:0:0:"
SREF_B = "1:0:1:BBBB:1:1:0:0:0:0:"

# Per-test knobs the fake client reads on every call.
FAKE = {"power": "on", "sref": SREF_A, "online": True, "recording": False}


class FakeEnigmaClient:
    def __init__(self, ip, mock=False):
        pass

    async def is_online(self):
        return FAKE["online"]

    async def user_claim(self):
        if FAKE["power"] == "on":
            return "viewer"
        return "recording" if FAKE["recording"] else None

    async def get_power_state(self):
        return FAKE["power"]

    async def get_current(self):
        if FAKE["sref"] is None:
            return None  # box unreachable
        return {"info": {"ref": FAKE["sref"], "result": True, "name": "Chan"},
                "now": {"id": 1, "title": "Some Show"}}


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @sa_event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(poller, "SessionLocal", TestSession)
    monkeypatch.setattr(poller, "EnigmaClient", FakeEnigmaClient)
    poller._state.clear()
    active_viewer._active.clear()
    FAKE.update({"power": "on", "sref": SREF_A, "online": True, "recording": False})

    db = TestSession()
    now = utcnow()
    receiver = models.Receiver(name="box1", ip="10.0.0.1", power_state="unknown",
                               default_user="alice")
    ch_a = models.Channel(sref=SREF_A, name="ChanA", last_seen=now)
    ch_b = models.Channel(sref=SREF_B, name="ChanB", last_seen=now)
    user = models.User(slug="alice", name="Alice", created_at=now)
    user2 = models.User(slug="bob", name="Bob", created_at=now)
    db.add_all([receiver, ch_a, ch_b, user, user2])
    # A currently-airing EPG event on channel A for event linking.
    ev = models.EpgEvent(channel_id=1, title="Now Show",
                         start_time=now - timedelta(minutes=20),
                         end_time=now + timedelta(minutes=70),
                         duration_sec=5400, cached_at=now)
    db.add(ev)
    db.commit()
    yield SimpleNamespace(db=db, Session=TestSession)
    db.close()


def _sessions(env):
    env.db.expire_all()
    return env.db.query(models.ViewingSession).order_by(models.ViewingSession.id).all()


def _age_session_start(env, seconds: int) -> None:
    """Pretend the current session started `seconds` ago (in-memory state and
    the DB row, so both confirmation and duration math see the same clock)."""
    state = poller._get_state("box1")
    state["session_start"] = state["session_start"] - timedelta(seconds=seconds)
    row = env.db.get(models.ViewingSession, state["session_id"])
    row.started_at = row.started_at - timedelta(seconds=seconds)
    env.db.commit()


class TestSessionStateMachine:
    @pytest.mark.asyncio
    async def test_first_poll_opens_unconfirmed_session(self, env):
        await poller._poll_receiver("box1")
        rows = _sessions(env)
        assert len(rows) == 1
        assert rows[0].confirmed is False
        assert rows[0].ended_at is None
        assert poller._get_state("box1")["last_sref"] == SREF_A

    @pytest.mark.asyncio
    async def test_confirm_after_min_watch_with_default_user(self, env):
        await poller._poll_receiver("box1")
        _age_session_start(env, poller.settings.min_watch_sec + 5)
        await poller._poll_receiver("box1")
        rows = _sessions(env)
        assert rows[0].confirmed is True
        assert rows[0].attribution_method == "location_heuristic"
        assert rows[0].confidence == pytest.approx(0.4)
        user = env.db.query(models.User).filter_by(slug="alice").one()
        assert rows[0].user_id == user.id
        assert rows[0].epg_event_id is not None  # linked to the airing event

    @pytest.mark.asyncio
    async def test_active_viewer_beats_default_user(self, env):
        active_viewer.record_active("box1", "bob")
        await poller._poll_receiver("box1")
        _age_session_start(env, poller.settings.min_watch_sec + 5)
        await poller._poll_receiver("box1")
        rows = _sessions(env)
        bob = env.db.query(models.User).filter_by(slug="bob").one()
        assert rows[0].user_id == bob.id
        assert rows[0].attribution_method == "browser_active"
        assert rows[0].confidence == 1.0

    @pytest.mark.asyncio
    async def test_channel_change_closes_confirmed_opens_new(self, env):
        await poller._poll_receiver("box1")
        _age_session_start(env, poller.settings.min_watch_sec + 5)
        await poller._poll_receiver("box1")  # confirm on A
        FAKE["sref"] = SREF_B
        await poller._poll_receiver("box1")
        rows = _sessions(env)
        assert len(rows) == 2
        assert rows[0].ended_at is not None
        assert rows[0].duration_sec > 0
        assert rows[1].ended_at is None and rows[1].confirmed is False

    @pytest.mark.asyncio
    async def test_standby_discards_unconfirmed_session(self, env):
        await poller._poll_receiver("box1")
        FAKE["power"] = "standby"
        await poller._poll_receiver("box1")
        rows = _sessions(env)
        # The unconfirmed row stays in the DB but must not be resumable:
        # state is fully reset so a later wake starts fresh.
        state = poller._get_state("box1")
        assert state["session_id"] is None
        assert state["last_sref"] is None
        assert rows[0].confirmed is False and rows[0].ended_at is None

    @pytest.mark.asyncio
    async def test_standby_closes_confirmed_session(self, env):
        await poller._poll_receiver("box1")
        _age_session_start(env, poller.settings.min_watch_sec + 5)
        await poller._poll_receiver("box1")  # confirm
        FAKE["power"] = "standby"
        await poller._poll_receiver("box1")
        rows = _sessions(env)
        assert rows[0].confirmed is True
        assert rows[0].ended_at is not None

    @pytest.mark.asyncio
    async def test_unknown_channel_opens_no_session(self, env):
        FAKE["sref"] = "1:0:1:FFFF:1:1:0:0:0:0:"  # not in channels table
        await poller._poll_receiver("box1")
        assert _sessions(env) == []


class TestOrphanSweep:
    def test_orphans_closed_or_discarded_on_startup(self, env):
        now = utcnow()
        db = env.db
        rcv = db.query(models.Receiver).one()
        rcv.last_seen = now - timedelta(minutes=5)
        confirmed = models.ViewingSession(
            channel_id=1, receiver_id=rcv.id, started_at=now - timedelta(hours=2),
            confirmed=True, source="ir_remote")
        unconfirmed = models.ViewingSession(
            channel_id=1, receiver_id=rcv.id, started_at=now - timedelta(hours=2),
            confirmed=False, source="ir_remote")
        db.add_all([confirmed, unconfirmed])
        db.commit()

        poller._close_orphan_sessions()

        rows = _sessions(env)
        assert len(rows) == 1  # unconfirmed discarded
        assert rows[0].confirmed is True
        assert rows[0].ended_at == rcv.last_seen  # best effort: receiver's last_seen
        assert rows[0].duration_sec > 0


# ── Nightly EPG wake ──────────────────────────────────────────────────────────

class TestNightlyEpgWake:
    """A box that is normally powered off contributes no EPG at all, because the
    sweep only talks to receivers that answer HTTP. _nightly_full_sweep wakes the
    ones flagged epg_wake and powers them back down afterwards."""

    @pytest.fixture
    def wake_env(self, env, monkeypatch):
        calls = SimpleNamespace(woken=[], slept=[], swept=0, primed=[])

        async def fake_wake(rcfg):
            calls.woken.append(rcfg.name)
            return True, None

        async def fake_sleep(rcfg):
            calls.slept.append(rcfg.name)
            return True, None

        async def fake_sweep(full=False):
            calls.swept += 1

        async def fake_prime(rcfg):
            calls.primed.append(rcfg.name)

        monkeypatch.setattr(poller, "wake_for_epg", fake_wake)
        monkeypatch.setattr(poller, "shutdown_for_epg", fake_sleep)
        monkeypatch.setattr(poller, "_refresh_all_epg", fake_sweep)
        monkeypatch.setattr(poller, "_prime_woken_receiver", fake_prime)
        # A receiver we woke for EPG sits in light standby; "on" is the signal
        # that a person switched it on and it is no longer ours to power off.
        FAKE["power"] = "standby"
        FAKE["recording"] = False
        return SimpleNamespace(env=env, calls=calls)

    def _flag(self, env, *, epg_wake: bool) -> None:
        rcv = env.db.query(models.Receiver).one()
        rcv.epg_wake = epg_wake
        rcv.power_method = "intertechno"
        env.db.commit()

    @pytest.mark.asyncio
    async def test_offline_flagged_receiver_is_woken_and_powered_back_down(self, wake_env):
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = False
        await poller._nightly_full_sweep()
        assert wake_env.calls.woken == ["box1"]
        assert wake_env.calls.slept == ["box1"]
        assert wake_env.calls.swept == 1

    @pytest.mark.asyncio
    async def test_unflagged_receiver_is_left_alone(self, wake_env):
        self._flag(wake_env.env, epg_wake=False)
        FAKE["online"] = False
        await poller._nightly_full_sweep()
        assert wake_env.calls.woken == []
        assert wake_env.calls.slept == []
        assert wake_env.calls.swept == 1

    @pytest.mark.asyncio
    async def test_already_online_receiver_is_not_powered_off_afterwards(self, wake_env):
        """We only switch off what we switched on — a box the user is watching
        must survive the sweep."""
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = True
        await poller._nightly_full_sweep()
        assert wake_env.calls.woken == []
        assert wake_env.calls.slept == []

    @pytest.mark.asyncio
    async def test_failed_wake_still_switches_the_mains_back_off(self, wake_env, monkeypatch):
        """The power-on command went out before the box failed to appear, so the
        mains may be live with a receiver that never booted. Observed on
        2026-08-16: wake timed out after 240 s and nothing switched it back."""
        async def failing_wake(rcfg):
            return False, "receiver did not come up within 240s"
        monkeypatch.setattr(poller, "wake_for_epg", failing_wake)
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = False
        await poller._nightly_full_sweep()
        assert wake_env.calls.slept == ["box1"]
        assert wake_env.calls.swept == 1

    @pytest.mark.asyncio
    async def test_failed_wake_is_not_toured(self, wake_env, monkeypatch):
        async def failing_wake(rcfg):
            return False, "gateway unreachable"
        monkeypatch.setattr(poller, "wake_for_epg", failing_wake)
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = False
        await poller._nightly_full_sweep()
        assert wake_env.calls.primed == []

    @pytest.mark.asyncio
    async def test_receiver_powered_down_even_if_sweep_raises(self, wake_env, monkeypatch):
        async def boom(full=False):
            raise RuntimeError("sweep exploded")
        monkeypatch.setattr(poller, "_refresh_all_epg", boom)
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = False
        with pytest.raises(RuntimeError):
            await poller._nightly_full_sweep()
        assert wake_env.calls.slept == ["box1"]

    @pytest.mark.asyncio
    async def test_box_switched_on_mid_sweep_is_left_powered(self, wake_env, monkeypatch):
        """These boxes are used for live TV now: whoever switches one on during
        the sweep owns it, and cutting its mains would black out their TV."""
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = False

        async def sweep_then_user_turns_it_on(full=False):
            wake_env.calls.swept += 1
            FAKE["power"] = "on"   # someone picked up the remote

        monkeypatch.setattr(poller, "_refresh_all_epg", sweep_then_user_turns_it_on)
        await poller._nightly_full_sweep()
        assert wake_env.calls.slept == []   # mains left alone

    @pytest.mark.asyncio
    async def test_recording_box_keeps_its_power(self, wake_env, monkeypatch):
        """The zap tour cannot hurt a recording — enigma2 refuses to retune a
        busy tuner — but cutting the mains would truncate the file."""
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = False

        async def sweep_then_a_timer_fires(full=False):
            wake_env.calls.swept += 1
            FAKE["recording"] = True

        monkeypatch.setattr(poller, "_refresh_all_epg", sweep_then_a_timer_fires)
        await poller._nightly_full_sweep()
        assert wake_env.calls.slept == []

    @pytest.mark.asyncio
    async def test_woken_receiver_is_zap_toured_before_the_sweep(self, wake_env):
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = False
        await poller._nightly_full_sweep()
        assert wake_env.calls.primed == ["box1"]

    @pytest.mark.asyncio
    async def test_receiver_someone_is_watching_is_never_zapped(self, wake_env):
        """The tour retunes the box. A receiver that was already on is not ours
        to touch — it is only ever swept."""
        self._flag(wake_env.env, epg_wake=True)
        FAKE["online"] = True
        await poller._nightly_full_sweep()
        assert wake_env.calls.primed == []
        assert wake_env.calls.swept == 1


class TestOpportunisticHarvest:
    """The cheap path: harvest while the box already sits in standby. No boot
    means no HDMI-CEC pulse, so the TV stays off — which is the whole point."""

    @pytest.fixture
    def opp(self, env, monkeypatch):
        calls = SimpleNamespace(primed=[], swept=0, woken=[], abort_tour=False)

        async def fake_prime(rcfg):
            calls.primed.append(rcfg.name)
            return {"aborted": calls.abort_tour, "visited": 1}

        async def fake_sweep(full=False):
            calls.swept += 1

        async def fake_wake(rcfg):
            calls.woken.append(rcfg.name)
            return True, None

        monkeypatch.setattr(poller, "_prime_woken_receiver", fake_prime)
        monkeypatch.setattr(poller, "_refresh_all_epg", fake_sweep)
        monkeypatch.setattr(poller, "wake_for_epg", fake_wake)
        poller._last_tour_at.clear()
        FAKE.update({"power": "standby", "online": True, "recording": False})
        rcv = env.db.query(models.Receiver).one()
        rcv.epg_wake = True
        rcv.power_method = "intertechno"
        env.db.commit()
        return SimpleNamespace(env=env, calls=calls)

    @pytest.mark.asyncio
    async def test_standby_box_is_harvested_without_waking_it(self, opp):
        await poller._opportunistic_tour()
        assert opp.calls.primed == ["box1"]
        assert opp.calls.woken == []      # never powered anything

    @pytest.mark.asyncio
    async def test_powered_down_box_is_left_alone(self, opp):
        FAKE["online"] = False
        await poller._opportunistic_tour()
        assert opp.calls.primed == []

    @pytest.mark.asyncio
    async def test_box_in_use_is_left_alone(self, opp):
        FAKE["power"] = "on"
        await poller._opportunistic_tour()
        assert opp.calls.primed == []

    @pytest.mark.asyncio
    async def test_recording_box_is_left_alone(self, opp):
        FAKE["recording"] = True
        await poller._opportunistic_tour()
        assert opp.calls.primed == []

    @pytest.mark.asyncio
    async def test_cooldown_prevents_a_second_harvest(self, opp):
        await poller._opportunistic_tour()
        await poller._opportunistic_tour()
        assert opp.calls.primed == ["box1"]

    @pytest.mark.asyncio
    async def test_aborted_tour_does_not_earn_the_cooldown(self, opp):
        """Cut short by a viewer means a fraction of the transponders were
        harvested. Blocking the retry for six hours over that would push the box
        towards a forced night wake for nothing."""
        opp.calls.abort_tour = True
        await poller._opportunistic_tour()
        await poller._opportunistic_tour()
        assert opp.calls.primed == ["box1", "box1"]

    @pytest.mark.asyncio
    async def test_cooldown_expires(self, opp):
        await poller._opportunistic_tour()
        poller._last_tour_at["box1"] = utcnow() - timedelta(hours=99)
        await poller._opportunistic_tour()
        assert opp.calls.primed == ["box1", "box1"]


class TestNightWakeGate:
    """Waking the box at night boots it, and the boot switches the TV on. Only
    worth it when the data has actually decayed."""

    @pytest.fixture
    def gate(self, env, monkeypatch):
        calls = SimpleNamespace(woken=[], swept=0)

        async def fake_wake_all():
            calls.woken.append("wake")
            return []

        async def fake_sweep(full=False):
            calls.swept += 1

        monkeypatch.setattr(poller, "_wake_epg_receivers", fake_wake_all)
        monkeypatch.setattr(poller, "_refresh_all_epg", fake_sweep)
        monkeypatch.setattr(poller, "_visible_horizon", lambda: 5.0)
        return SimpleNamespace(calls=calls, monkeypatch=monkeypatch)

    @pytest.mark.asyncio
    async def test_covered_epg_means_no_wake(self, gate):
        gate.monkeypatch.setattr(poller, "_coverage", lambda: (1.0, 33, 33))
        await poller._nightly_full_sweep()
        assert gate.calls.woken == []
        assert gate.calls.swept == 1     # still sweeps whatever is reachable

    @pytest.mark.asyncio
    async def test_decayed_epg_earns_the_wake(self, gate):
        gate.monkeypatch.setattr(poller, "_coverage", lambda: (0.3, 10, 33))
        await poller._nightly_full_sweep()
        assert gate.calls.woken == ["wake"]

    @pytest.mark.asyncio
    async def test_unmeasurable_coverage_falls_back_to_waking(self, gate):
        """Cannot measure it — behave as before rather than silently stop."""
        gate.monkeypatch.setattr(poller, "_coverage", lambda: (None, 0, 0))
        await poller._nightly_full_sweep()
        assert gate.calls.woken == ["wake"]

    @pytest.mark.asyncio
    async def test_shallow_broadcasters_do_not_force_a_wake(self, gate):
        """The point of coverage over horizon: a median dragged down by stations
        that only ever transmit two days must not book the box a 03:30 boot
        while everything important is still covered. Coverage is set well clear
        of the threshold on purpose — this test is about horizon being ignored,
        not about where the threshold sits."""
        gate.monkeypatch.setattr(poller, "_coverage", lambda: (1.0, 33, 33))
        gate.monkeypatch.setattr(poller, "_visible_horizon", lambda: 1.9)
        await poller._nightly_full_sweep()
        assert gate.calls.woken == []
