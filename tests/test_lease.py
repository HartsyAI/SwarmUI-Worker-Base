"""Lease lifecycle against fake SwarmUI process and API objects."""

from __future__ import annotations

import asyncio

import pytest

from swarmui_worker.config import WorkerConfig
from swarmui_worker.idle import REASON_EXITED, REASON_IDLE, REASON_UNRESPONSIVE
from swarmui_worker.models import build_shadow_tree
from swarmui_worker.supervisor import Supervisor


class FakeProcess:
    def __init__(self):
        self.running = True


class FakeApi:
    def __init__(self, responses, interrupt=None):
        self.responses = list(responses)
        self.calls: list[str] = []
        self.interrupt = interrupt or {"success": True}

    async def call(self, route, body=None):
        self.calls.append(route)
        if route == "InterruptAll":
            if isinstance(self.interrupt, Exception):
                raise self.interrupt
            return self.interrupt
        if route != "GetGlobalStatus":
            return {"success": True}
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


IDLE = {"status": {"live_gens": 0, "waiting_gens": 0, "loading_models": 0, "waiting_backends": 0}}
BUSY = {"status": {"live_gens": 1, "waiting_gens": 0, "loading_models": 0, "waiting_backends": 0}}


def make_supervisor(tmp_path, api, **env) -> Supervisor:
    config = WorkerConfig.from_env({"SWARMUI_DIR": str(tmp_path), "SWARMUI_POLL_INTERVAL": "0.5", **env})
    sup = Supervisor(config)
    sup.api = api
    sup.process = FakeProcess()
    return sup


def fast_clock(monkeypatch, step: float = 120.0):
    """Makes time.monotonic jump `step` seconds per call, and sleeps instant."""
    now = [0.0]

    def tick():
        now[0] += step
        return now[0]
    monkeypatch.setattr("swarmui_worker.supervisor.time.monotonic", tick)
    monkeypatch.setattr("swarmui_worker.idle.time.monotonic", tick)

    real_sleep = asyncio.sleep

    async def no_sleep(_):
        await real_sleep(0)
    monkeypatch.setattr("swarmui_worker.supervisor.asyncio.sleep", no_sleep)


def test_dead_api_ends_lease_as_unresponsive(tmp_path, monkeypatch):
    fast_clock(monkeypatch)
    sup = make_supervisor(tmp_path, FakeApi([BUSY, RuntimeError("connection refused")]))
    assert asyncio.run(sup.wait_for_release()) == REASON_UNRESPONSIVE


def test_error_reply_counts_as_failure(tmp_path, monkeypatch):
    fast_clock(monkeypatch)
    sup = make_supervisor(tmp_path, FakeApi([{"error": "broken", "error_id": "x"}]))
    assert asyncio.run(sup.wait_for_release()) == REASON_UNRESPONSIVE


def test_short_api_hiccup_does_not_end_a_busy_lease(tmp_path, monkeypatch):
    fast_clock(monkeypatch, step=1.0)
    api = FakeApi([BUSY, RuntimeError("timeout"), BUSY] + [IDLE] * 400)
    sup = make_supervisor(tmp_path, api, SWARMUI_IDLE_SECONDS="60")
    assert asyncio.run(sup.wait_for_release()) == REASON_IDLE


def test_exited_swarm_ends_lease(tmp_path):
    sup = make_supervisor(tmp_path, FakeApi([IDLE]))
    sup.process.running = False
    assert asyncio.run(sup.wait_for_release()) == REASON_EXITED


def test_end_lease_revokes_interrupts_then_wipes(tmp_path):
    api = FakeApi([IDLE])
    sup = make_supervisor(tmp_path, api)
    out = tmp_path / "Output" / "local"
    out.mkdir(parents=True)
    (out / "image.png").write_bytes(b"x")

    async def body():
        info = await sup.begin_lease()
        assert info.token
        with pytest.raises(RuntimeError):
            await sup.begin_lease()
        await sup.end_lease()
    asyncio.run(body())
    assert api.calls == ["InterruptAll"]
    assert not any((tmp_path / "Output").iterdir())
    assert sup.gateway._token is None


def test_non_finite_limits_fall_back_to_defaults(tmp_path):
    sup = make_supervisor(tmp_path, FakeApi([IDLE]), SWARMUI_MAX_LEASE_SECONDS="3600")
    assert sup.lease_limits(float("nan"), float("inf"), float("nan")) == (120.0, 600.0, 3600.0)


def test_shadow_root_may_not_overlap_models(tmp_path):
    models = tmp_path / "models"
    (models / "Lora").mkdir(parents=True)
    with pytest.raises(ValueError):
        build_shadow_tree(str(models), str(models))
    with pytest.raises(ValueError):
        build_shadow_tree(str(models), str(tmp_path))
    with pytest.raises(ValueError):
        build_shadow_tree(str(models), str(models / "Lora"))
    assert (models / "Lora").is_dir()


@pytest.mark.parametrize("interrupt", [RuntimeError("connection refused"), {"error": "boom", "error_id": "x"}])
def test_unconfirmed_interrupt_retires_the_worker(tmp_path, interrupt):
    sup = make_supervisor(tmp_path, FakeApi([IDLE], interrupt=interrupt))

    async def body():
        await sup.begin_lease()
        await sup.end_lease()
        assert sup.retired and sup.gateway._token is None
        with pytest.raises(RuntimeError, match="retired"):
            await sup.begin_lease()
        await asyncio.wait_for(sup.watch_process(), 2)
    asyncio.run(body())
