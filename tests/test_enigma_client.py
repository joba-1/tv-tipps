"""Tests for app/enigma/client.py — the online probe's stale-connection retry.

Receivers drop their open TCP connections whenever they enter or leave standby.
httpx then hands the next request one of those dead sockets and it fails
instantly, which the app reads as "receiver offline" — that is what made the
nightly EPG sweep skip a box it had just woken and confirmed reachable.
"""
from __future__ import annotations
import httpx
import pytest
import app.enigma.client as ec


class FakeHttp:
    """Stands in for the shared httpx client. `script` is one entry per call:
    an int status code, or an exception to raise after `delay` seconds."""

    def __init__(self, script, delay=0.0):
        self.script = list(script)
        self.delay = delay
        self.calls = 0

    async def head(self, url, timeout=None):
        self.calls += 1
        outcome = self.script.pop(0) if self.script else 200
        if isinstance(outcome, Exception):
            if self.delay:
                import time
                time.sleep(self.delay)  # sync sleep: keeps monotonic() honest
            raise outcome
        return httpx.Response(outcome, request=httpx.Request("HEAD", url))


@pytest.fixture
def fake_http(monkeypatch):
    def install(script, delay=0.0):
        fake = FakeHttp(script, delay)
        monkeypatch.setattr(ec, "_get_client", lambda: fake)
        return fake
    return install


class TestIsOnline:
    @pytest.mark.asyncio
    async def test_fast_failure_is_retried(self, fake_http):
        """Dropped keep-alive → instant error → retry on a fresh socket wins."""
        fake = fake_http([httpx.RemoteProtocolError("Server disconnected"), 200])
        assert await ec.EnigmaClient("10.0.0.1").is_online() is True
        assert fake.calls == 2

    @pytest.mark.asyncio
    async def test_slow_failure_is_not_retried(self, fake_http):
        """An unreachable box fails by timeout — don't pay that twice."""
        fake = fake_http([httpx.ConnectTimeout("timed out")],
                         delay=ec._STALE_CONN_SEC + 0.05)
        assert await ec.EnigmaClient("10.0.0.1").is_online() is False
        assert fake.calls == 1

    @pytest.mark.asyncio
    async def test_two_fast_failures_give_up(self, fake_http):
        fake = fake_http([httpx.ConnectError("refused"), httpx.ConnectError("refused")])
        assert await ec.EnigmaClient("10.0.0.1").is_online() is False
        assert fake.calls == 2

    @pytest.mark.asyncio
    async def test_server_error_is_offline_without_retry(self, fake_http):
        fake = fake_http([503])
        assert await ec.EnigmaClient("10.0.0.1").is_online() is False
        assert fake.calls == 1

    @pytest.mark.asyncio
    async def test_healthy_box_probes_once(self, fake_http):
        fake = fake_http([200])
        assert await ec.EnigmaClient("10.0.0.1").is_online() is True
        assert fake.calls == 1


class TestResetPool:
    @pytest.mark.asyncio
    async def test_closes_and_reopens(self, monkeypatch):
        closed = []

        class FakeClient:
            async def aclose(self):
                closed.append(True)

        monkeypatch.setattr(ec, "_client", FakeClient())
        await ec.reset_pool()
        assert closed == [True]
        assert ec._client is None
        # Next caller gets a working client again rather than None.
        assert isinstance(ec._get_client(), httpx.AsyncClient)
        await ec.aclose()

    @pytest.mark.asyncio
    async def test_noop_when_no_client_yet(self, monkeypatch):
        monkeypatch.setattr(ec, "_client", None)
        await ec.reset_pool()  # must not raise
        assert ec._client is None


class FakeHttpGet:
    """`script` is one outcome per GET: a dict payload, or an exception."""

    def __init__(self, script, delay=0.0):
        self.script = list(script)
        self.delay = delay
        self.calls = 0

    async def get(self, url, params=None, timeout=None):
        self.calls += 1
        outcome = self.script.pop(0) if self.script else {"result": True}
        if isinstance(outcome, Exception):
            if self.delay:
                import time
                time.sleep(self.delay)
            raise outcome
        return httpx.Response(200, json=outcome, request=httpx.Request("GET", url))


class TestGetRetry:
    """The first zap after a standby transition failed with
    RemoteProtocolError('illegal request line') — a dropped keep-alive, not an
    unreachable box — and cost the whole transponder."""

    @pytest.fixture
    def fake_get(self, monkeypatch):
        def install(script, delay=0.0):
            fake = FakeHttpGet(script, delay)
            monkeypatch.setattr(ec, "_get_client", lambda: fake)
            return fake
        return install

    @pytest.mark.asyncio
    async def test_zap_retries_a_dropped_connection(self, fake_get):
        fake = fake_get([httpx.RemoteProtocolError("illegal request line"),
                         {"result": True}])
        assert await ec.EnigmaClient("10.0.0.1").zap("1:0:19:1:AAA:1:C00000:0:0:0:") is True
        assert fake.calls == 2

    @pytest.mark.asyncio
    async def test_slow_failure_is_not_retried(self, fake_get):
        fake = fake_get([httpx.ConnectTimeout("timed out")],
                        delay=ec._STALE_CONN_SEC + 0.05)
        assert await ec.EnigmaClient("10.0.0.1").get_current() is None
        assert fake.calls == 1

    @pytest.mark.asyncio
    async def test_keypress_is_never_retried(self, fake_get):
        """A remote-control command the box did receive must not fire twice."""
        fake = fake_get([httpx.RemoteProtocolError("illegal request line"),
                         {"result": True}])
        assert await ec.EnigmaClient("10.0.0.1").send_key("power") is False
        assert fake.calls == 1

    @pytest.mark.asyncio
    async def test_timer_write_is_never_retried(self, fake_get):
        fake = fake_get([httpx.RemoteProtocolError("illegal request line"),
                         {"result": True}])
        assert await ec.EnigmaClient("10.0.0.1").add_timer(
            "1:0:19:1:AAA:1:C00000:0:0:0:", 100, 200, "Show") is None
        assert fake.calls == 1

    @pytest.mark.asyncio
    async def test_two_failures_give_up(self, fake_get):
        fake = fake_get([httpx.RemoteProtocolError("boom"),
                         httpx.RemoteProtocolError("boom")])
        assert await ec.EnigmaClient("10.0.0.1").get_current() is None
        assert fake.calls == 2
