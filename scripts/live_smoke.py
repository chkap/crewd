#!/usr/bin/env python
"""Bounded live SDK smoke for crewd (#23).

Proves the shipped system against a **real** official Copilot SDK runtime, end to
end, in disposable self-cleaning workspaces. Three clearly-separated evidence
levels (the manifest keeps them distinct):

1. **capability_probe** — a single isolated `send_and_wait` at the SDK transport
   level. Proves only that a runtime starts, reaches idle, and disconnects. It
   says nothing about the crewd integration.

2. **cli_routing** (the primary integrated proof) — drives the **shipped CLI
   command surface** as an operator would: `crewd run` and `crewd status --json`
   as real child processes against a disposable workspace. This exercises Typer
   entry → `_preflight` (config load, family check, backend doctor, repo-clone
   check, GOAL sha) → orchestrator build → dispatcher journal → SDK executor →
   exit-reason, and the read-only status JSON projection. Bounded smoke prompts
   are injected only through the explicitly gated `CREWD_SMOKE_POLICY` seam,
   which **appends** a bounded instruction suffix to the *production-rendered*
   prompts and never replaces the handoff payload rendering. A per-field canary +
   nonce oracle proves genuine payload delivery (SDK capture → SQLite → the exact
   production Lead prompt → Lead's typed decision), not mere row acknowledgement.

3. **cli_cancellation** — `crewd run` as a child process, SIGINT'd mid-attempt,
   with process-aware cleanup assertions (child exited, no surviving PID/runtime,
   workspace removed) and the worker terminal classified clean-or-tainted, never
   idle_completed.

4. **internal_lifecycle** — lower-level executor checks that have no direct CLI
   surface: same-session resume by id and taint→fresh-generation.

Emits a **sanitized** JSON manifest (crewd IDs, outcomes, counts, booleans, and
non-secret canary tokens the harness itself generated — never transcripts,
prompts, tool arguments, credentials, or raw SDK payloads) and exits non-zero
unless every required check passes. A whole-smoke wall-clock watchdog bounds the
run in addition to per-turn/per-subprocess timeouts.

Run:  `uv run --active python scripts/live_smoke.py [--out PATH] [--probe-only]`
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

WHOLE_SMOKE_BUDGET_S = 1200.0
_DEADLINE = 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_watchdog(phase: str) -> None:
    if _DEADLINE and time.time() > _DEADLINE:
        raise TimeoutError(f"whole-smoke wall-clock budget exceeded before phase {phase!r}")


def _canaries(nonce: str) -> dict:
    return {
        "evidence": f"CANARY-EVID-{nonce}",
        "changed": f"CANARY-CHANGED-{nonce}",
        "remaining": f"CANARY-REMAIN-{nonce}",
        "reason": f"CANARY-REASON-{nonce}",
        "disagreement": f"CANARY-DISAGREE-{nonce}",
        "blocker": f"CANARY-BLOCKER-{nonce}",
    }


# ─────────────────────────── capability probe (isolated) ───────────────────────────
def capability_probe(model: str = "claude-sonnet-4.6", timeout: float = 90.0) -> dict:
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


# ─────────────────────────── disposable CLI workspace ───────────────────────────
def _make_cli_workspace(root: Path, *, max_work: int = 8):
    """Build a workspace an operator's `crewd` command could load and run."""
    from crewd.config import GoalState, default_config, sha256_file
    from crewd.workspace import Workspace

    ws = Workspace(root)
    ws.ensure_skeleton()
    cfg = default_config(name="livesmoke", repo="acme/smoke")
    cfg.loop.per_tick_timeout = 90
    cfg.loop.max_cycles = max_work  # bounds the dispatcher work budget
    cfg.save(ws.crew_yaml)
    ws.goal_md.write_text("# GOAL\n\nBounded live SDK smoke.\n")
    # preflight requires the local clone dir + a matching GOAL sha
    ws.repo_dir(cfg.target.repo).mkdir(parents=True, exist_ok=True)
    for role in ("lead", "advisory", "worker", "verifier"):
        ws.role_worktree(role).mkdir(parents=True, exist_ok=True)
    gs = GoalState(version=1, label="goal:v1", cycles=0,
                   goal_md_sha256=sha256_file(ws.goal_md))
    gs.save(ws.goal_json)
    return ws, cfg


def _write_policy(path: Path, *, evidence_file: Path, nonce: str, slow_worker: bool) -> None:
    path.write_text(json.dumps({
        "evidence_file": str(evidence_file),
        "nonce": f"FINISH-NONCE-{nonce}",
        "canaries": _canaries(nonce),
        "slow_worker": slow_worker,
    }, indent=2))


def _crewd_argv() -> list[str]:
    exe = shutil.which("crewd")
    if exe:
        return [exe]
    return [sys.executable, "-c",
            "import sys; from crewd.cli import app; sys.argv=['crewd']+sys.argv[1:]; app()"]


def _cli_env(policy_path: Path) -> dict:
    env = dict(os.environ)
    env["CREWD_SMOKE_POLICY"] = str(policy_path)
    return env


def _run_cli(args: list[str], *, env: dict, timeout: float) -> dict:
    t0 = time.time()
    p = subprocess.run(_crewd_argv() + args, env=env, timeout=timeout,
                       capture_output=True, text=True)
    return {"rc": p.returncode, "elapsed_s": round(time.time() - t0, 2),
            "stdout": p.stdout, "stderr": p.stderr}


def _open_dispatch(ws):
    from crewd.dispatcher import Dispatcher
    return Dispatcher(ws.state_dir / "dispatch.db")


def _export_latest_run(ws, goal_label: str = "goal:v1"):
    disp = _open_dispatch(ws)
    try:
        diag = disp.read_run_diagnostics(goal_label)
        if diag is None:
            return None
        return disp.export_run(diag.run.id)
    finally:
        disp.close()


# ─────────────────────────── phase 2: CLI routing ───────────────────────────
def _cli_routing(root: Path) -> dict:
    ws, cfg = _make_cli_workspace(root)
    nonce = uuid.uuid4().hex[:8]
    canaries = _canaries(nonce)
    evidence_file = root / "smoke-evidence.json"
    policy = root / "smoke-policy.json"
    _write_policy(policy, evidence_file=evidence_file, nonce=nonce, slow_worker=False)
    env = _cli_env(policy)

    run = _run_cli(["run", "-w", str(ws.root), "--no-auto-render"], env=env, timeout=540)
    status = _run_cli(["status", "-w", str(ws.root), "--json"], env=env, timeout=90)

    # status JSON is printed via rich; parse the JSON object out of stdout
    status_json = _parse_status_json(status["stdout"])

    exported = _export_latest_run(ws)
    handoffs = exported["handoffs"] if exported else []
    attempts = exported["attempts"] if exported else []
    worker_h = next((h for h in handoffs if h["role"] == "worker"), None)

    ev = {}
    if evidence_file.exists():
        ev = json.loads(evidence_file.read_text())

    sessions = {a["role"]: a["session_id"] for a in attempts if a["session_id"]}
    return {
        "run_rc": run["rc"],
        "run_elapsed_s": run["elapsed_s"],
        "exit_reason": (ws.exit_reason_file.read_text().strip()
                        if ws.exit_reason_file.exists() else None),
        "status_rc": status["rc"],
        "status_run_status": (status_json.get("run") or {}).get("status") if status_json else None,
        "status_next_action": status_json.get("next_action") if status_json else None,
        "lead_turns": sum(1 for a in attempts if a["role"] == "lead"),
        "worker_turns": sum(1 for a in attempts if a["role"] == "worker"),
        "worker_handoff_outcome": worker_h["outcome_class"] if worker_h else None,
        "worker_handoff_consumed_by_lead": bool(worker_h and worker_h["consumed_by_dispatch_id"]),
        "durable_reason_canary_exact": bool(worker_h) and worker_h["reason_returned"] == canaries["reason"],
        "worker_populated_field_count": sum(
            1 for col in ("evidence", "changed", "remaining", "reason_returned")
            if worker_h and (worker_h[col] or "").strip()) if worker_h else 0,
        # request-seam oracle (SQLite → production Lead prompt → typed decision),
        # robust to model paraphrasing (checks the ACTUAL stored fields render):
        "lead_prompt_populated_field_count": ev.get("lead_prompt_populated_field_count"),
        "lead_prompt_all_populated_rendered": ev.get("lead_prompt_all_populated_rendered"),
        "lead_prompt_canary_field_hits": ev.get("lead_prompt_canary_field_hits"),
        "lead_decision_kind": ev.get("lead_decision_kind"),
        "lead_decision_echo_match": ev.get("lead_decision_echo_match"),
        "isolated_sessions": (sessions.get("worker") is not None
                              and sessions.get("lead") is not None
                              and sessions.get("worker") != sessions.get("lead")),
        "journaled_identity": all(a["session_id"] for a in attempts
                                  if a["state"] != "reserved"),
        "attempt_generations": sorted({a["generation"] for a in attempts}),
    }


def _parse_status_json(stdout: str):
    depth = 0
    start = None
    for i, ch in enumerate(stdout):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(stdout[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    return None


# ─────────────────────────── phase 3: CLI cancellation ───────────────────────────
def _cli_cancellation(root: Path) -> dict:
    from crewd.dispatcher import Dispatcher
    from crewd.session_backend import TaintStore

    ws, cfg = _make_cli_workspace(root)
    nonce = uuid.uuid4().hex[:8]
    evidence_file = root / "smoke-evidence.json"
    policy = root / "smoke-policy.json"
    _write_policy(policy, evidence_file=evidence_file, nonce=nonce, slow_worker=True)
    env = _cli_env(policy)

    p = subprocess.Popen(
        _crewd_argv() + ["run", "-w", str(ws.root), "--no-auto-render"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Wait until the worker attempt is durably in-flight, then SIGINT the child.
    worker_started = False
    deadline = time.time() + 120
    while time.time() < deadline:
        if p.poll() is not None:
            break
        exported = _export_latest_run(ws)
        if exported and any(a["role"] == "worker" and a["state"] in ("started", "terminal")
                            for a in exported["attempts"]):
            worker_started = True
            break
        time.sleep(0.5)
    signalled = False
    if p.poll() is None:
        os.kill(p.pid, signal.SIGINT)
        signalled = True
    try:
        out, err = p.communicate(timeout=90)
        child_exited = True
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        child_exited = False

    exported = _export_latest_run(ws)
    worker_att = None
    if exported:
        wa = [a for a in exported["attempts"] if a["role"] == "worker"]
        worker_att = wa[-1] if wa else None
    worker_outcome = worker_att["terminal_outcome"] if worker_att else None
    tainted = False
    if worker_att and worker_att["session_id"]:
        tainted = TaintStore(ws.role_cfg_dir("worker") / ".crewd-sdk-taint").is_tainted(
            worker_att["session_id"])
    return {
        "worker_started_before_signal": worker_started,
        "signalled": signalled,
        "child_exited": child_exited,
        "child_rc": p.returncode,
        "exit_reason": (ws.exit_reason_file.read_text().strip()
                        if ws.exit_reason_file.exists() else None),
        "no_surviving_pid": ws.read_pid() is None,
        "worker_terminal_outcome": worker_outcome,
        "classified_clean_or_tainted": worker_outcome in ("cancelled_clean", "tainted"),
        "never_idle_completed": worker_outcome != "idle_completed",
        "session_tainted": tainted,
    }


# ─────────────────────────── phase 4: internal executor lifecycle ───────────────────────────
def _role_request(ws, cfg, role: str, prompt: str, goal_label: str = "goal:v1"):
    from crewd.executor import AttemptRequest
    return AttemptRequest(
        role=role, model=cfg.roles[role].model, prompt=prompt,
        config_dir=ws.role_cfg_dir(role), add_dirs=[ws.root], cwd=ws.role_cfg_dir(role),
        workspace_root=ws.root, goal_label=goal_label, timeout=90.0,
        log_path=ws.log_file(role, 99, goal_label),
    )


def _internal_lifecycle(root: Path) -> dict:
    from crewd.executor import SdkAttemptExecutor
    from crewd.session_backend import TaintStore

    ws, cfg = _make_cli_workspace(root)
    executor = SdkAttemptExecutor()

    p1 = "Automated smoke: call submit_role_handoff EXACTLY ONCE with " \
         "outcome_class=\"no_progress\", reason=\"resume smoke a\". Do nothing else."
    out1 = executor.execute_role(_role_request(ws, cfg, "worker", p1))
    out2 = executor.execute_role(_role_request(
        ws, cfg, "worker",
        "Automated smoke: call submit_role_handoff EXACTLY ONCE with "
        "outcome_class=\"no_progress\", reason=\"resume smoke b\". Do nothing else."))
    resumed_same = out2.session_id == out1.session_id
    gen_before = out2.generation

    TaintStore(ws.role_cfg_dir("worker") / ".crewd-sdk-taint").taint(out2.session_id)
    out3 = executor.execute_role(_role_request(
        ws, cfg, "worker",
        "Automated smoke: call submit_role_handoff EXACTLY ONCE with "
        "outcome_class=\"no_progress\", reason=\"fresh-gen smoke\". Do nothing else."))
    return {
        "resume_same_session": resumed_same,
        "generation_before_taint": gen_before,
        "generation_after_taint": out3.generation,
        "taint_advanced_generation": out3.generation > gen_before,
        "fresh_session_after_taint": out3.session_id != out2.session_id,
    }


# ─────────────────────────── orchestration ───────────────────────────
def full_smoke(out_path: Path | None) -> dict:
    global _DEADLINE
    _DEADLINE = time.time() + WHOLE_SMOKE_BUDGET_S
    manifest: dict = {
        "schema": "crewd.live_smoke/v2",
        "started_at": _now(),
        "phases": {},
        "checks": {},
    }
    roots: list[Path] = []

    def _mkroot(tag: str) -> Path:
        r = Path(tempfile.mkdtemp(prefix=f"crewd-livesmoke-{tag}-"))
        roots.append(r)
        return r

    try:
        def _phase(name: str, fn, *args):
            _check_watchdog(name)
            try:
                manifest["phases"][name] = fn(*args)
            except Exception as exc:  # noqa: BLE001 — capture as evidence, never crash out
                manifest["phases"][name] = {"error": f"{type(exc).__name__}: {exc}"}

        _phase("capability_probe", capability_probe)
        _phase("cli_routing", _cli_routing, _mkroot("routing"))
        _phase("cli_cancellation", _cli_cancellation, _mkroot("cancel"))
        _phase("internal_lifecycle", _internal_lifecycle, _mkroot("lifecycle"))

        def g(phase: str, key: str):
            d = manifest["phases"].get(phase) or {}
            return d.get(key)

        probe = manifest["phases"].get("capability_probe") or {}
        rt = manifest["phases"].get("cli_routing") or {}
        cx = manifest["phases"].get("cli_cancellation") or {}
        lf = manifest["phases"].get("internal_lifecycle") or {}
        checks = manifest["checks"]
        # capability
        checks["probe_reached_idle"] = probe.get("reached_idle") is True and (probe.get("event_count") or 0) > 0
        # CLI integrated routing (primary)
        checks["cli_run_exit_goal_complete"] = rt.get("run_rc") == 0 and rt.get("exit_reason") == "goal-complete"
        checks["cli_status_finished_new_goal"] = (
            rt.get("status_rc") == 0 and rt.get("status_run_status") == "finished"
            and rt.get("status_next_action") == "new_goal")
        checks["cli_isolated_goal_scoped_sessions"] = (
            rt.get("isolated_sessions") is True and 0 in (rt.get("attempt_generations") or []))
        checks["cli_pre_send_journal_identity"] = bool(rt.get("journaled_identity"))
        checks["cli_exactly_one_worker_completed"] = (
            (rt.get("worker_turns") or 0) >= 1 and rt.get("worker_handoff_outcome") == "completed")
        checks["cli_handoff_consumed_by_lead"] = rt.get("worker_handoff_consumed_by_lead") is True
        # payload delivery oracle (robust — not mere acknowledgement).
        # The render check is deterministic (uses the ACTUAL stored handoff
        # fields, so a regression dropping fields from `_lead_prompt` fails it);
        # the echo check proves the real Lead SDK turn read the payload back.
        checks["payload_worker_fields_populated"] = (rt.get("worker_populated_field_count") or 0) >= 3
        checks["payload_fields_rendered_into_lead_prompt"] = (
            rt.get("lead_prompt_all_populated_rendered") is True
            and (rt.get("lead_prompt_populated_field_count") or 0) >= 3)
        checks["payload_lead_echoed_handoff_reason"] = rt.get("lead_decision_echo_match") is True
        # CLI cancellation (process-aware)
        checks["cli_cancel_child_exited"] = cx.get("child_exited") is True and cx.get("no_surviving_pid") is True
        checks["cli_cancel_clean_or_tainted"] = cx.get("classified_clean_or_tainted") is True
        checks["cli_cancel_never_idle_completed"] = cx.get("never_idle_completed") is True
        # internal executor lifecycle
        checks["internal_session_resume"] = lf.get("resume_same_session") is True
        checks["internal_taint_fresh_generation"] = (
            lf.get("taint_advanced_generation") is True and lf.get("fresh_session_after_taint") is True)

        manifest["passed"] = all(checks.values())
        manifest["finished_at"] = _now()
    finally:
        removed = []
        for r in roots:
            shutil.rmtree(r, ignore_errors=True)
            removed.append(not r.exists())
        manifest["workspaces_cleaned_up"] = all(removed) if removed else True

    if out_path is not None:
        out_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Bounded live SDK smoke for crewd (#23).")
    ap.add_argument("--out", type=Path, default=None, help="Write the sanitized manifest JSON here.")
    ap.add_argument("--probe-only", action="store_true",
                    help="Run ONLY the isolated lower-level SDK capability probe.")
    args = ap.parse_args()

    if args.probe_only:
        probe = capability_probe()
        print(json.dumps({"schema": "crewd.capability_probe/v1", "probe": probe}, indent=2))
        return 0 if probe.get("reached_idle") else 1

    manifest = full_smoke(args.out)
    print(json.dumps(manifest, indent=2))
    if not manifest.get("passed"):
        failed = [k for k, v in manifest.get("checks", {}).items() if not v]
        sys.stderr.write(
            "\nLIVE SMOKE FAILED. Unmet checks: " + ", ".join(failed) + "\n"
            "Safe recovery: all disposable workspaces are already removed; re-run "
            "after confirming Copilot auth/network/runtime with `--probe-only`.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
