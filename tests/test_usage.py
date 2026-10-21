from dataclasses import asdict
from types import SimpleNamespace

import pytest

from crewd.executor import SdkAttemptExecutor
from crewd.sdk_adapter import SdkRoleRuntime
from crewd.session_backend import AttemptOutcome, AttemptResult, TaintStore, run_attempt
from crewd.usage import AttemptUsage, UsageSnapshot, snapshot_from_metrics
from test_session_backend import FakeOps


def snapshot(total):
    return UsageSnapshot(total_nano_aiu=total, premium_request_cost=0, models={})


def test_resumed_attempt_deltas_do_not_double_count():
    attempts = [
        AttemptUsage(before=snapshot(0), after=snapshot(100)),
        AttemptUsage(before=snapshot(100), after=snapshot(270)),
        AttemptUsage(before=snapshot(0), after=snapshot(30)),
    ]
    for usage in attempts:
        usage.reconcile()
        assert usage.complete
    assert sum(usage.delta_nano_aiu for usage in attempts) == 300


@pytest.mark.parametrize("before,after", [(None, 10), (0, None), (None, None)])
def test_missing_usage_is_not_zero(before, after):
    usage = AttemptUsage(before=snapshot(before), after=snapshot(after))
    usage.reconcile()
    assert usage.delta_nano_aiu is None
    assert not usage.complete


def test_decreasing_counter_is_explicit():
    usage = AttemptUsage(before=snapshot(100), after=snapshot(90))
    usage.reconcile()
    assert usage.delta_nano_aiu is None
    assert not usage.complete
    assert usage.errors


def test_unsettled_attempt_retains_partial_cost_without_claiming_complete():
    usage = AttemptUsage(before=snapshot(10), after=snapshot(30), settled=False)
    usage.reconcile()
    assert usage.delta_nano_aiu == 20
    assert not usage.complete


@pytest.mark.parametrize("total", [-1, float("nan"), float("inf"), 1.5, True])
def test_invalid_metric_is_rejected(total):
    with pytest.raises(ValueError):
        snapshot_from_metrics(SimpleNamespace(
            total_nano_aiu=total, total_premium_request_cost=0, model_metrics={},
        ))


def test_metric_snapshot_retains_per_model_token_and_cost_totals():
    model = SimpleNamespace(
        requests=SimpleNamespace(count=3),
        usage=SimpleNamespace(input_tokens=12, output_tokens=4,
                              cache_read_tokens=8, cache_write_tokens=2),
        total_nano_aiu=123,
    )
    result = snapshot_from_metrics(SimpleNamespace(
        total_nano_aiu=123, total_premium_request_cost=0, model_metrics={"model": model},
    ))
    assert result.models["model"].calls == 3
    assert result.models["model"].cache_read_tokens == 8
    assert asdict(result)["total_nano_aiu"] == 123


def runtime(tmp_path):
    return SdkRoleRuntime(
        session_id="usage-test", role="worker", model="model",
        config_dir=tmp_path, working_dir=tmp_path,
    )


def test_live_call_replay_is_deduplicated(tmp_path):
    rt = runtime(tmp_path)
    event = SimpleNamespace(
        type="assistant.usage", id="event",
        data=SimpleNamespace(api_call_id="call", model="model",
                             input_tokens=8, output_tokens=2,
                             cache_read_tokens=None, cache_write_tokens=None, cost=None),
    )
    rt._on_usage_event(event)
    rt._on_usage_event(event)
    assert len(rt._usage.calls) == 1
    assert rt._usage.calls[0]["premium_request_cost"] is None


@pytest.mark.parametrize("run_error,abort_confirmed", [(False, True), (True, True), (False, False)])
def test_accounting_is_captured_before_runtime_cleanup(tmp_path, run_error, abort_confirmed):
    from crewd.session_backend import RunSignal

    class MeteredOps(FakeOps):
        def get_usage(self, *, settled):
            assert "disconnect" not in self.calls and "force_stop" not in self.calls
            usage = AttemptUsage(before=snapshot(0), after=snapshot(22), settled=settled)
            usage.reconcile()
            return usage

    ops = MeteredOps(
        run_error=run_error, abort_confirmed=abort_confirmed,
        run_signal=RunSignal.IDLE if abort_confirmed else RunSignal.WAIT_TIMEOUT,
    )
    result = run_attempt(ops, TaintStore(tmp_path / "taint"), prompt="test", resume=False)
    assert result.usage.delta_nano_aiu == 22
    assert result.usage.complete == (not run_error and abort_confirmed)


def test_sidecar_write_failure_is_not_silently_ignored(tmp_path):
    request = SimpleNamespace(log_path=tmp_path / "missing" / "attempt.log", goal_label="goal:v1")
    result = AttemptResult("attempt", "session", "worker", AttemptOutcome.IDLE_COMPLETED, 1)
    with pytest.raises(OSError, match="Cannot persist attempt accounting"):
        SdkAttemptExecutor._write_sidecar(request, result, 0)
