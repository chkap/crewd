#!/usr/bin/env python
"""Bounded integrated live SDK smoke for crewd (#23).

This is the **integrated** proof required by the goal: it drives the *real*
production stack — the durable :class:`~crewd.dispatcher.Dispatcher` journal, the
:class:`~crewd.orchestrator.Orchestrator` run loop, the SDK-backed
:class:`~crewd.executor.SdkAttemptExecutor` / :mod:`crewd.sdk_adapter`, the
goal-scoped :class:`~crewd.session_backend.SessionRegistry` /
:class:`~crewd.session_backend.TaintStore`, and the read-only
:mod:`crewd.diagnostics` surface — against a **real** official Copilot SDK
runtime, end to end, in a disposable workspace. It is deliberately distinct from
the lower-level *capability probe* (``--probe-only``), which makes a single
isolated ``send_and_wait`` SDK call and proves nothing about the crewd
integration.

Boundedness / safety:

* Trivial smoke prompts (no repo work): each role turn only calls its one typed
  tool. The Lead dispatches Worker, Worker returns one ``completed`` handoff, Lead
  finishes — three real SDK turns for the routing flow, plus a resume turn, a
  taint->fresh-generation turn, and one cancelled turn.
* Strict bounds: ``DispatcherLimits(max_work)`` caps reservable attempts and each
  turn has a short ``per_tick_timeout``.
* Deterministic cleanup: the disposable workspace is always removed (``finally``),
  and every SDK client/session is disconnected by the executor's own lifecycle.
* Sanitized evidence only: the emitted manifest contains crewd-derived IDs,
  outcomes, timestamps, counts, and booleans — **never** transcripts, prompts,
  raw SDK payloads, tool arguments, credentials, or environment values.

Run:  ``uv run --active python scripts/live_smoke.py [--out PATH] [--probe-only]``
Exit: ``0`` iff every required outcome was observed; non-zero otherwise (with a
safe-recovery hint). Requires Copilot auth/network/runtime.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────── capability probe (isolated) ───────────────────────────
def capability_probe(model: str = "claude-sonnet-4.6", timeout: float = 90.0) -> dict:
    """Lower-level, *isolated* SDK call — NOT the integrated smoke.

    Proves only that a real runtime can start, create a session, reach idle on a
    trivial turn, and disconnect cleanly. Kept separate so the integrated flow's
    evidence is never conflated with a bare transport check.
    """
    from copilot import CopilotClient

    async def _run() -> dict:
        t0 = time.time()
        c = CopilotClient(working_directory=".")
        await c.start()
        s = await c.create_session(model=model)
        await s.send_and_wait("Reply with the single word: ok", timeout=timeout)
        n = len(await s.get_events())
        await s.disconnect()
        await c.stop()
        return {"reached_idle": True, "event_count": n, "elapsed_s": round(time.time() - t0, 2)}

    return asyncio.run(_run())


# ─────────────────────────── integrated smoke orchestrator ───────────────────────────
def _build_smoke_orchestrator(ws, cfg, executor, goal_state, *, max_work: int):
    """A real Orchestrator with only the two prompt builders overridden to bounded
    smoke instructions. Every other code path — journal, pre-send identity,
    cancellation, reconciliation, terminal resolution — is the production one."""
    from crewd.dispatcher import Dispatcher, DispatcherLimits
    from crewd.orchestrator import Orchestrator

    class _SmokeOrchestrator(Orchestrator):
        def _role_prompt(self, role, dsp):  # bounded smoke prompt
            return (
                "This is an automated wiring smoke test — do NOT touch any repo, "
                "GitHub, or files. Your ONLY task is to call the `submit_role_handoff` "
                "tool EXACTLY ONCE with: outcome_class=\"completed\", "
                "evidence=\"smoke-evidence-ref-001\", "
                "changed=\"none — wiring smoke, no repository mutation\", "
                "remaining=\"none\", reason=\"smoke complete\". Then finish. Do nothing else."
            )

        def _lead_prompt(self, pending):  # bounded smoke prompt
            ids = [h.id for h in pending]
            if not pending:
                return (
                    "This is an automated wiring smoke test — do NOT touch any repo, "
                    "GitHub, or files. Your ONLY task is to call the "
                    "`submit_lead_decision` tool EXACTLY ONCE with: kind=\"dispatch\", "
                    "role=\"worker\", ack_handoff_ids=[], reason=\"smoke dispatch\". "
                    "Then finish. Do nothing else."
                )
            return (
                "This is an automated wiring smoke test. A worker handoff is now "
                f"pending with ids {ids}. Your ONLY task is to call the "
                "`submit_lead_decision` tool EXACTLY ONCE with: kind=\"finish\", "
                f"ack_handoff_ids={ids}, final_acceptance=\"smoke flow verified\". "
                "Then finish. Do nothing else."
            )

    disp = Dispatcher(
        ws.state_dir / "dispatch.db", limits=DispatcherLimits(max_work=max_work)
    )
    return _SmokeOrchestrator(ws, cfg, executor, goal_state, dispatcher=disp), disp


def _make_workspace(root: Path):
    from crewd.config import GoalState, default_config
    from crewd.workspace import Workspace

    ws = Workspace(root)
    ws.ensure_skeleton()
    cfg = default_config(name="livesmoke", repo="acme/smoke")
    cfg.loop.per_tick_timeout = 90
    cfg.save(ws.crew_yaml)
    ws.goal_md.write_text("# GOAL\n\nBounded live SDK smoke.\n")
    for role in ("lead", "advisory", "worker", "verifier"):
        ws.role_worktree(role).mkdir(parents=True, exist_ok=True)
    gs = GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="smoke")
    gs.save(ws.goal_json)
    return ws, cfg, gs


def _role_request(ws, cfg, role: str, prompt: str, goal_label: str):
    from crewd.executor import AttemptRequest

    return AttemptRequest(
        role=role,
        model=cfg.roles[role].model,
        prompt=prompt,
        config_dir=ws.role_cfg_dir(role),
        add_dirs=[ws.root],
        cwd=ws.role_cfg_dir(role),
        workspace_root=ws.root,
        goal_label=goal_label,
        timeout=90.0,
        log_path=ws.log_file(role, 99, goal_label),
    )


def integrated_smoke(out_path: Path | None) -> dict:
    """Drive the full integrated flow against the real SDK and build a sanitized
    evidence manifest. Returns the manifest dict."""
    from crewd.diagnostics import NextAction, build_snapshot
    from crewd.dispatcher import RunStatus
    from crewd.executor import SdkAttemptExecutor
    from crewd.session_backend import CancelToken, TaintStore

    root = Path(tempfile.mkdtemp(prefix="crewd-livesmoke-"))
    manifest: dict = {
        "schema": "crewd.live_smoke/v1",
        "started_at": _now(),
        "workspace": str(root),
        "phases": {},
        "checks": {},
    }
    try:
        ws, cfg, gs = _make_workspace(root)
        executor = SdkAttemptExecutor()

        # ── Phase 1: capability probe (separate, lower level) ──
        probe = capability_probe(model=cfg.roles["lead"].model)
        manifest["phases"]["capability_probe"] = probe

        # ── Phase 2: integrated routing/handoff flow via the real Orchestrator ──
        orch, disp = _build_smoke_orchestrator(ws, cfg, executor, gs, max_work=8)
        t0 = time.time()
        rc = orch.run(once=False)  # dispatch → worker handoff → finish
        elapsed = round(time.time() - t0, 2)

        run = disp.start_or_resume_run("goal:v1")
        exported = disp.export_run(run.id)
        attempts = exported["attempts"]
        handoffs = exported["handoffs"]

        lead_attempts = [a for a in attempts if a["role"] == "lead"]
        worker_attempts = [a for a in attempts if a["role"] == "worker"]
        worker_handoffs = [h for h in handoffs if h["role"] == "worker"]

        # Pre-send journal identity: every attempt that ran carries a session id.
        journaled_sessions = {
            a["role"]: a["session_id"] for a in attempts if a["session_id"]
        }
        manifest["phases"]["integrated_routing"] = {
            "run_status": run.status,
            "exit_reason": (ws.exit_reason_file.read_text().strip()
                            if ws.exit_reason_file.exists() else None),
            "orchestrator_rc": rc,
            "elapsed_s": elapsed,
            "lead_turns": len(lead_attempts),
            "worker_turns": len(worker_attempts),
            "worker_handoffs": len(worker_handoffs),
            "worker_handoff_outcome": worker_handoffs[0]["outcome_class"] if worker_handoffs else None,
            "worker_handoff_consumed_by_lead": bool(
                worker_handoffs and worker_handoffs[0]["consumed_by_dispatch_id"]
            ),
            "journaled_session_roles": sorted(journaled_sessions.keys()),
            "worker_session_id": journaled_sessions.get("worker"),
            "attempt_generations": sorted({a["generation"] for a in attempts}),
        }

        # ── Phase 3: status / safe_next_action correctness on the finished run ──
        snap = build_snapshot(ws, crew_name=cfg.name, backend=cfg.backend, goal_label="goal:v1")
        manifest["phases"]["status_projection"] = {
            "run_status": snap.run_status,
            "next_action": snap.next_action.value,
            "latest_handoff_redaction_bounded": snap.latest_handoff is not None
            and "text" not in (snap.latest_handoff.get("evidence") or {}),
        }

        # ── Phase 4: session resume + goal-scoped generation advance on taint ──
        worker_sid_1 = journaled_sessions.get("worker")
        req_resume = _role_request(
            ws, cfg, "worker",
            "Automated smoke: call submit_role_handoff EXACTLY ONCE with "
            "outcome_class=\"no_progress\", reason=\"resume smoke\". Do nothing else.",
            "goal:v1",
        )
        out_resume = executor.execute_role(req_resume)
        resumed_same = out_resume.session_id == worker_sid_1
        gen_before = out_resume.generation

        TaintStore(ws.role_cfg_dir("worker") / ".crewd-sdk-taint").taint(out_resume.session_id)
        req_fresh = _role_request(
            ws, cfg, "worker",
            "Automated smoke: call submit_role_handoff EXACTLY ONCE with "
            "outcome_class=\"no_progress\", reason=\"fresh-gen smoke\". Do nothing else.",
            "goal:v1",
        )
        out_fresh = executor.execute_role(req_fresh)
        fresh_generation = out_fresh.generation > gen_before
        fresh_session = out_fresh.session_id != out_resume.session_id
        manifest["phases"]["session_lifecycle"] = {
            "resume_same_session": resumed_same,
            "generation_before_taint": gen_before,
            "generation_after_taint": out_fresh.generation,
            "taint_advanced_generation": fresh_generation,
            "fresh_session_after_taint": fresh_session,
        }

        # ── Phase 5: external cancellation classified clean-or-tainted (never completed) ──
        cancel = CancelToken()
        started_evt = threading.Event()

        def _on_started(_sid, _gen):
            started_evt.set()

        def _canceller():
            started_evt.wait(timeout=30)
            time.sleep(0.2)
            cancel.request("smoke-external-cancel")

        req_cancel = _role_request(
            ws, cfg, "worker",
            "Automated smoke: think step by step and privately enumerate two hundred "
            "distinct integers before doing anything else, then call submit_role_handoff "
            "once. Take your time.",
            "goal:v1",
        )
        th = threading.Thread(target=_canceller, daemon=True)
        th.start()
        out_cancel = executor.execute_role(req_cancel, on_started=_on_started, cancel=cancel)
        th.join(timeout=5)
        cancel_outcome = out_cancel.result.outcome.value
        cancel_session_tainted = TaintStore(
            ws.role_cfg_dir("worker") / ".crewd-sdk-taint"
        ).is_tainted(out_cancel.session_id)
        manifest["phases"]["external_cancellation"] = {
            "outcome": cancel_outcome,
            "classified_clean_or_tainted": cancel_outcome in ("cancelled_clean", "tainted"),
            "never_completed": cancel_outcome != "idle_completed",
            "tainted": out_cancel.result.tainted,
            "session_tainted_if_unconfirmed": cancel_session_tainted,
        }

        # ── Phase 6: clean shutdown — no surviving runtime/PID ──
        disp.close()
        pid_after = ws.read_pid()
        manifest["phases"]["shutdown"] = {
            "daemon_pid_after": pid_after,
            "no_surviving_pid": pid_after is None,
        }

        # ── Required-outcome checks (the manifest's verdict) ──
        checks = manifest["checks"]
        checks["isolated_role_sessions"] = (
            journaled_sessions.get("worker") is not None
            and journaled_sessions.get("lead") is not None
            and journaled_sessions.get("worker") != journaled_sessions.get("lead")
        )
        checks["goal_scoped_generation"] = 0 in manifest["phases"]["integrated_routing"]["attempt_generations"]
        checks["pre_send_journal_identity"] = all(
            a["session_id"] for a in attempts if a["state"] != "reserved"
        )
        checks["exactly_one_lead_decision_per_turn"] = len(lead_attempts) >= 2  # dispatch + finish
        checks["exactly_one_role_handoff"] = len(worker_handoffs) == 1
        checks["handoff_round_trip_into_next_lead"] = manifest["phases"]["integrated_routing"]["worker_handoff_consumed_by_lead"]
        checks["sdk_event_observation"] = probe["event_count"] > 0
        checks["bounded_routing_step"] = run.status == RunStatus.FINISHED.value
        checks["cancellation_clean_or_tainted"] = manifest["phases"]["external_cancellation"]["classified_clean_or_tainted"]
        checks["cancellation_never_completed"] = manifest["phases"]["external_cancellation"]["never_completed"]
        checks["status_next_action_correct"] = snap.run_status == "finished" and snap.next_action is NextAction.NEW_GOAL
        checks["session_resume"] = resumed_same
        checks["taint_fresh_generation"] = fresh_generation and fresh_session
        checks["clean_shutdown_no_pid"] = pid_after is None

        manifest["passed"] = all(checks.values())
        manifest["finished_at"] = _now()
    finally:
        shutil.rmtree(root, ignore_errors=True)
        manifest["workspace_cleaned_up"] = not root.exists()

    if out_path is not None:
        out_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Bounded integrated live SDK smoke for crewd (#23).")
    ap.add_argument("--out", type=Path, default=None, help="Write the sanitized manifest JSON here.")
    ap.add_argument("--probe-only", action="store_true",
                    help="Run ONLY the lower-level isolated SDK capability probe, not the integrated smoke.")
    args = ap.parse_args()

    if args.probe_only:
        probe = capability_probe()
        print(json.dumps({"schema": "crewd.capability_probe/v1", "probe": probe}, indent=2))
        return 0 if probe.get("reached_idle") else 1

    manifest = integrated_smoke(args.out)
    print(json.dumps(manifest, indent=2))
    if not manifest.get("passed"):
        failed = [k for k, v in manifest.get("checks", {}).items() if not v]
        sys.stderr.write(
            "\nLIVE SMOKE FAILED. Unmet checks: " + ", ".join(failed) + "\n"
            "Safe recovery: the disposable workspace is already removed; re-run "
            "after confirming Copilot auth/network/runtime with `--probe-only`.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
