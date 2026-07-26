"""Gated, test-only support for the bounded live SDK smoke.

This module ships with crewd but is **inert in production**: nothing imports it
unless the ``CREWD_SMOKE_POLICY`` environment variable points at a policy file
(``_build_orchestrator`` in :mod:`crewd.commands` is the only loader, and only on
that gate). It exists so the live-smoke harness can drive the *real* CLI/run/
dispatcher/executor stack against a real Copilot runtime with bounded prompts,
**without** replacing the production prompt builders.

Design constraints (from review):

* The production ``_lead_prompt`` / ``_role_prompt`` **rendering is preserved** —
  the policy only appends a bounded instruction *suffix* to the already-rendered
  production string. So the structured handoff payload a real Lead turn receives
  is exactly the production one; a regression that dropped handoff fields would
  be caught, not masked.
* The Lead-prompt canary check is instrumented at the request seam (the finalized
  production prompt string, right before it is sent) — **no transcript is
  stored**. Only booleans/counts are recorded to the evidence file.
* The evidence file contains **only** non-secret canary tokens (which the harness
  itself generated), booleans, counts, and a decision kind — never prompts,
  arguments, credentials, or raw SDK payloads.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Canonical handoff field order the smoke plants canaries into. Each maps to a
# ``submit_role_handoff`` argument and a rendered line in the Lead prompt. ALL
# six are mandatory: the oracle fails if any is dropped from the worker policy,
# the durable row, the rendered production prompt, or the pass predicate.
CANARY_FIELDS = ("evidence", "changed", "remaining", "reason", "disagreement", "blocker")

# Maps a canary field name to the corresponding column on an exported handoff row.
HANDOFF_COLUMN = {
    "evidence": "evidence", "changed": "changed", "remaining": "remaining",
    "reason": "reason_returned", "disagreement": "disagreement", "blocker": "blocker",
}


def durable_row_matches(row: dict, canaries: dict) -> dict:
    """Which of the six canaries the durable handoff row reproduces exactly.

    Pure/importable so a deterministic test can assert that omitting any field
    (e.g. ``disagreement``/``blocker``) from the row fails the oracle.
    """
    return {f: (str(row.get(HANDOFF_COLUMN[f]) or "") == canaries[f]) for f in CANARY_FIELDS}


def prompt_render_counts(prompt: str, canaries: dict) -> dict:
    """Occurrence count of each of the six canaries in a rendered prompt string."""
    return {f: prompt.count(canaries[f]) for f in CANARY_FIELDS}


@dataclass
class SmokePromptPolicy:
    """Bounded prompt decorator + request-seam oracle for the live smoke."""

    evidence_file: Path
    canaries: dict  # field -> exact canary token (non-secret)
    nonce: str      # exact token Lead must echo iff it saw all six fields
    slow_worker: bool = False  # for the cancellation phase: keep the worker busy

    @classmethod
    def from_env(cls) -> Optional["SmokePromptPolicy"]:
        path = os.environ.get("CREWD_SMOKE_POLICY")
        if not path:
            return None
        data = json.loads(Path(path).read_text())
        return cls(
            evidence_file=Path(data["evidence_file"]),
            canaries={k: str(data["canaries"][k]) for k in CANARY_FIELDS},
            nonce=str(data["nonce"]),
            slow_worker=bool(data.get("slow_worker", False)),
        )

    # ── evidence file (read-modify-write; sanitized) ──
    def _load_evidence(self) -> dict:
        try:
            return json.loads(self.evidence_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_evidence(self, ev: dict) -> None:
        self.evidence_file.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_file.write_text(json.dumps(ev, indent=2) + "\n")

    # ── prompt decoration (append-only; production rendering preserved) ──
    def decorate_role(self, role: str, prompt: str) -> str:
        if role != "worker":
            # Only the worker participates in the bounded routing flow; any other
            # dispatched role is told to return immediately with no progress.
            return prompt + (
                "\n\n=== AUTOMATED SMOKE OVERRIDE (do NOT touch any repo/GitHub/"
                "files) ===\nIgnore the work instructions above. Call "
                "`submit_role_handoff` EXACTLY ONCE with outcome_class=\"no_progress\" "
                "and reason=\"smoke non-worker return\". Then finish."
            )
        c = self.canaries
        if self.slow_worker:
            # Cancellation phase: keep the worker demonstrably busy so the
            # orchestrator can request an in-flight abort before it reaches idle.
            return prompt + (
                "\n\n=== AUTOMATED SMOKE OVERRIDE (do NOT touch any repo/GitHub/"
                "files) ===\nIgnore the work instructions above. This is a "
                "cancellation smoke test. First, think step by step and privately "
                "enumerate the integers from 1 to 500 one at a time, spelling each "
                "in English words, before doing anything else. Take your time and "
                "do not rush. Only after finishing that enumeration, call "
                "`submit_role_handoff` once with outcome_class=\"no_progress\", "
                "reason=\"smoke slow return\"."
            )
        # Payload phase: worker MUST populate ALL SIX handoff fields, each with its
        # exact distinct canary token, so the oracle can prove every field travels
        # SDK-capture → SQLite → production Lead prompt. No field may be empty, and
        # each field must hold ONLY its single token (no prose, no merged tokens).
        payload_json = (
            "{"
            '"outcome_class": "completed", '
            f'"evidence": "{c["evidence"]}", '
            f'"changed": "{c["changed"]}", '
            f'"remaining": "{c["remaining"]}", '
            f'"reason": "{c["reason"]}", '
            f'"disagreement": "{c["disagreement"]}", '
            f'"blocker": "{c["blocker"]}"'
            "}"
        )
        return prompt + (
            "\n\n=== AUTOMATED SMOKE OVERRIDE (do NOT touch any repo/GitHub/files) ==="
            "\nIgnore the work instructions above. This is a mechanical relay test. "
            "Do NOT think, summarize, or write prose. Call the `submit_role_handoff` "
            "tool EXACTLY ONCE, using these EXACT arguments — this literal JSON "
            "object, with each value copied verbatim, character for character:\n"
            f"{payload_json}\n"
            "Every value is a distinct opaque code. All six string fields are "
            "required and must be non-empty, and each field must contain ONLY its "
            "own single code (never merge or copy a code between fields). Then "
            "finish. Do nothing else."
        )

    def decorate_lead(self, pending, prompt: str) -> str:
        ids = [h.id for h in pending]
        if not pending:
            return prompt + (
                "\n\n=== AUTOMATED SMOKE OVERRIDE (HIGHEST PRIORITY — overrides ALL "
                "grounding above) ===\nThis is an automated wiring test. No worker "
                "has run yet, so the goal is NOT done and you MUST NOT finish, "
                "pause, wait, or continue_lead — those are all forbidden on this "
                "turn.\nYour ONLY permitted action: call `submit_lead_decision` "
                "EXACTLY ONCE with kind=\"dispatch\", role=\"worker\", "
                "ack_handoff_ids=[], reason=\"smoke dispatch\". Then finish your "
                "turn. Do nothing else."
            )
        # Request-seam oracle (the real delivery proof). At the finalized
        # production Lead prompt string — right before it is sent to the SDK —
        # require EACH of the six canaries to appear EXACTLY ONCE. A regression
        # that dropped any field (e.g. disagreement/blocker) from `_lead_prompt`
        # would drop its count to 0 and fail. We read the production prompt only;
        # our appended suffix below deliberately contains none of the canaries.
        counts = prompt_render_counts(prompt, self.canaries)
        field_presence_count = sum(1 for f in CANARY_FIELDS if counts[f] >= 1)
        exact_once = all(counts[f] == 1 for f in CANARY_FIELDS)
        # One-time nonce the Lead must echo — the shared 8-char suffix embedded in
        # every rendered canary. Lead can only produce it by reading the rendered
        # payload, proving its SDK turn saw the delivered content.
        ev = self._load_evidence()
        ev["lead_prompt_field_presence_count"] = field_presence_count
        ev["lead_prompt_exact_once"] = exact_once
        ev["lead_prompt_render_counts"] = counts  # per-field ints (sanitized)
        ev["expected_nonce"] = self.nonce  # internal only; never copied to manifest
        self._save_evidence(ev)
        return prompt + (
            "\n\n=== AUTOMATED SMOKE OVERRIDE (HIGHEST PRIORITY — overrides ALL "
            "grounding above) ===\nThis is an automated wiring test. A worker "
            "handoff is already COMPLETE and the goal is considered DONE. You MUST "
            "terminate the run NOW.\n"
            "Inspect the pending handoff block rendered above. Every one of its "
            "fields (evidence, changed, remaining, reason, disagreement, blocker) "
            "contains a token ending in the SAME short code after its final hyphen "
            "(an 8-character alphanumeric code). Read that shared code.\n"
            "Your ONLY permitted action: call `submit_lead_decision` EXACTLY ONCE "
            "with kind=\"finish\". Do NOT choose dispatch, continue_lead, wait, or "
            "pause under any circumstances — those are all forbidden here.\n"
            f"Set ack_handoff_ids={ids} (you MUST acknowledge that exact handoff "
            "id). Set final_acceptance to EXACTLY that shared 8-character code and "
            "nothing else (copy it character for character). Then finish. Do "
            "nothing else."
        )

    # ── post-decision capture (content-echo oracle) ──
    def record_lead_decision(self, decision) -> None:
        kind = getattr(getattr(decision, "kind", None), "value", None) or str(
            getattr(decision, "kind", "")
        )
        acceptance = getattr(decision, "final_acceptance", None)
        ev = self._load_evidence()
        # Record the FIRST finish decision's echo match (the routing flow's
        # terminal). Never store the acceptance string itself — only the boolean.
        if "lead_decision_kind" not in ev:
            ev["lead_decision_kind"] = kind
        if kind == "finish" and "lead_decision_nonce_match" not in ev:
            expected = ev.get("expected_nonce")
            # The nonce is the shared 8-char code embedded in every rendered
            # canary. Its presence in `final_acceptance` proves the Lead SDK turn
            # read the delivered payload (tolerating benign surrounding text).
            acc = "" if acceptance is None else str(acceptance)
            ev["lead_decision_nonce_match"] = (
                bool(expected) and str(expected).strip() in acc)
        self._save_evidence(ev)
