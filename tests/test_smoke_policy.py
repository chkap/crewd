"""Deterministic guards for the live-smoke payload oracle (no SDK / network).

These run in the default suite. Their sole purpose is to make the all-six-field
requirement (Lead/Advisory) *structurally* enforced: each test fails if either
``disagreement`` or ``blocker`` is dropped from the worker policy, the durable-row
oracle, the production-prompt instrumentation, or the pass predicate. The gated
``SmokePromptPolicy`` itself stays inert in production (loaded only behind
``CREWD_SMOKE_POLICY``), so exercising it here has no runtime side effects.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crewd._smoke import (
    CANARY_FIELDS,
    SmokePromptPolicy,
    durable_row_matches,
    prompt_render_counts,
)

ALL_SIX = ("evidence", "changed", "remaining", "reason", "disagreement", "blocker")


def _canaries(nonce: str = "abc12345") -> dict:
    return {
        "evidence": f"MARKERA-QK7-{nonce}",
        "changed": f"MARKERB-VT2-{nonce}",
        "remaining": f"MARKERC-HZ9-{nonce}",
        "reason": f"MARKERD-LM4-{nonce}",
        "disagreement": f"MARKERE-PW6-{nonce}",
        "blocker": f"MARKERF-XB8-{nonce}",
    }


def _policy(tmp_path: Path, nonce: str = "abc12345") -> SmokePromptPolicy:
    return SmokePromptPolicy(
        evidence_file=tmp_path / "evidence.json",
        canaries=_canaries(nonce),
        nonce=nonce,
    )


def _row(canaries: dict) -> dict:
    """A durable handoff row (export_run shape) populated with every canary."""
    return {
        "evidence": canaries["evidence"],
        "changed": canaries["changed"],
        "remaining": canaries["remaining"],
        "reason_returned": canaries["reason"],
        "disagreement": canaries["disagreement"],
        "blocker": canaries["blocker"],
    }


def _load_harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_smoke.py"
    spec = importlib.util.spec_from_file_location("crewd_live_smoke", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_canonical_fields_include_disagreement_and_blocker():
    assert set(CANARY_FIELDS) == set(ALL_SIX)


def test_worker_policy_plants_all_six_canaries(tmp_path):
    canaries = _canaries()
    policy = _policy(tmp_path)
    out = policy.decorate_role("worker", "BASE PROMPT")
    for f in ALL_SIX:
        assert canaries[f] in out, f"worker policy dropped canary for {f}"
    # regression guard against the earlier "leave disagreement and blocker empty"
    assert "leave disagreement and blocker empty" not in out.lower()
    assert "non-empty" in out.lower()


def test_durable_row_oracle_requires_all_six():
    canaries = _canaries()
    full = durable_row_matches(_row(canaries), canaries)
    assert all(full.values())
    for missing in ("disagreement", "blocker"):
        row = _row(canaries)
        row[{"reason": "reason_returned"}.get(missing, missing)] = ""
        m = durable_row_matches(row, canaries)
        assert m[missing] is False
        assert not all(m.values()), f"oracle passed with {missing} dropped"


def test_prompt_render_counts_detects_missing_field():
    canaries = _canaries()
    rendered = "\n".join(canaries[f] for f in ALL_SIX)
    counts = prompt_render_counts(rendered, canaries)
    assert all(counts[f] == 1 for f in ALL_SIX)
    for missing in ("disagreement", "blocker"):
        partial = "\n".join(canaries[f] for f in ALL_SIX if f != missing)
        c = prompt_render_counts(partial, canaries)
        assert c[missing] == 0
        assert sum(1 for f in ALL_SIX if c[f] >= 1) == 5


def test_decorate_lead_seam_requires_six_exact_once(tmp_path):
    canaries = _canaries()
    policy = _policy(tmp_path)
    pending = [SimpleNamespace(id="h1")]
    production = "Pending handoff:\n" + "\n".join(canaries[f] for f in ALL_SIX)
    policy.decorate_lead(pending, production)
    ev = json.loads((tmp_path / "evidence.json").read_text())
    assert ev["lead_prompt_field_presence_count"] == 6
    assert ev["lead_prompt_exact_once"] is True

    # dropping blocker from the production prompt must fail the seam oracle
    policy2 = _policy(tmp_path / "b", "nonce9999")
    (tmp_path / "b").mkdir()
    canaries2 = _canaries("nonce9999")
    partial = "Pending handoff:\n" + "\n".join(
        canaries2[f] for f in ALL_SIX if f != "blocker")
    policy2.decorate_lead([SimpleNamespace(id="h1")], partial)
    ev2 = json.loads((tmp_path / "b" / "evidence.json").read_text())
    assert ev2["lead_prompt_field_presence_count"] == 5
    assert ev2["lead_prompt_exact_once"] is False


def test_decorate_lead_seam_rejects_duplicate_render(tmp_path):
    canaries = _canaries()
    policy = _policy(tmp_path)
    # blocker rendered twice → exact_once must be False even though all present
    dup = "\n".join(canaries[f] for f in ALL_SIX) + "\n" + canaries["blocker"]
    policy.decorate_lead([SimpleNamespace(id="h1")], dup)
    ev = json.loads((tmp_path / "evidence.json").read_text())
    assert ev["lead_prompt_field_presence_count"] == 6
    assert ev["lead_prompt_exact_once"] is False


def test_record_lead_decision_nonce_echo(tmp_path):
    policy = _policy(tmp_path, "zzz00000")
    # seed expected_nonce as decorate_lead would
    (tmp_path / "evidence.json").write_text(json.dumps({"expected_nonce": "zzz00000"}))
    policy.record_lead_decision(
        SimpleNamespace(kind=SimpleNamespace(value="finish"),
                        final_acceptance="the code is zzz00000"))
    ev = json.loads((tmp_path / "evidence.json").read_text())
    assert ev["lead_decision_nonce_match"] is True

    # a wrong echo must not match
    p2 = tmp_path / "c"
    p2.mkdir()
    policy2 = SmokePromptPolicy(evidence_file=p2 / "e.json",
                                canaries=_canaries(), nonce="zzz00000")
    (p2 / "e.json").write_text(json.dumps({"expected_nonce": "zzz00000"}))
    policy2.record_lead_decision(
        SimpleNamespace(kind=SimpleNamespace(value="finish"),
                        final_acceptance="something else"))
    ev2 = json.loads((p2 / "e.json").read_text())
    assert ev2["lead_decision_nonce_match"] is False


@pytest.mark.parametrize("drop_key,expected_false", [
    ("durable_field_match_count", "payload_worker_all_six_fields"),
    ("lead_prompt_field_presence_count", "payload_six_fields_rendered_exactly_once"),
])
def test_pass_predicate_requires_all_six(drop_key, expected_false):
    harness = _load_harness()
    full_rt = {
        "durable_field_match_count": 6,
        "durable_all_six_canaries_exact": True,
        "lead_prompt_field_presence_count": 6,
        "lead_prompt_exact_once": True,
        "lead_decision_nonce_match": True,
        "worker_handoff_consumed_by_lead": True,
    }
    assert all(harness.payload_checks(full_rt).values())
    # simulate a dropped field (count 5 instead of 6)
    degraded = dict(full_rt, **{drop_key: 5})
    checks = harness.payload_checks(degraded)
    assert checks[expected_false] is False


def test_pass_predicate_requires_consumption_and_nonce():
    harness = _load_harness()
    base = {
        "durable_field_match_count": 6, "durable_all_six_canaries_exact": True,
        "lead_prompt_field_presence_count": 6, "lead_prompt_exact_once": True,
        "lead_decision_nonce_match": True, "worker_handoff_consumed_by_lead": True,
    }
    assert harness.payload_checks(base)["payload_lead_echoed_nonce_and_consumed"] is True
    assert harness.payload_checks(
        dict(base, lead_decision_nonce_match=False)
    )["payload_lead_echoed_nonce_and_consumed"] is False
    assert harness.payload_checks(
        dict(base, worker_handoff_consumed_by_lead=False)
    )["payload_lead_echoed_nonce_and_consumed"] is False
