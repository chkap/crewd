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
# ``submit_role_handoff`` argument and a rendered line in the Lead prompt.
CANARY_FIELDS = ("evidence", "changed", "remaining", "reason", "disagreement", "blocker")


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
        return prompt + (
            "\n\n=== AUTOMATED SMOKE OVERRIDE (do NOT touch any repo/GitHub/files) ==="
            "\nIgnore the work instructions above. This is a wiring smoke test. Your "
            "ONLY action: call the `submit_role_handoff` tool EXACTLY ONCE. Use "
            "outcome_class=\"completed\". For the `reason` field you MUST use this "
            f"exact token, character for character, nothing else: {c['reason']}\n"
            "Give a brief value for each of evidence, changed, and remaining "
            "(one short phrase each is fine). Leave disagreement and blocker empty. "
            "Then finish. Do nothing else."
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
        # Request-seam oracle (robust to model paraphrasing): verify that every
        # field the worker ACTUALLY populated in the durable handoff row is
        # rendered verbatim into the *production* Lead prompt. This proves
        # SDK-capture → SQLite → production-prompt-rendering with no dependence on
        # the model reproducing our canaries — a regression that dropped any field
        # from `_lead_prompt` would fail this. Uses the HandoffView values the
        # orchestrator loaded from the journal (not our injected text).
        h = pending[0]
        fields = {
            "evidence": h.evidence, "changed": h.changed, "remaining": h.remaining,
            "reason": h.reason_returned, "disagreement": h.disagreement,
            "blocker": h.blocker,
        }
        populated = {k: v for k, v in fields.items() if v and v.strip()}
        rendered = {k: (prompt.count(v) >= 1) for k, v in populated.items()}
        # The value Lead must echo — a short, distinctive code read FROM the
        # rendered handoff `reason` field (extracted from the ACTUAL stored value,
        # not injected here). Short codes are copied far more reliably by a real
        # model than long hyphenated tokens, while still being unforgeable: an
        # 8-char code appearing in `final_acceptance` could only have travelled
        # SDK-capture → SQLite → rendered production prompt → the Lead's SDK turn.
        reason_val = populated.get("reason", "")
        echo_target = reason_val.rsplit("-", 1)[-1] if reason_val else (
            next(iter(populated.values()), ""))
        canary_hits = sum(1 for k, tok in self.canaries.items() if populated.get(k) == tok)
        ev = self._load_evidence()
        ev["lead_prompt_populated_field_count"] = len(populated)
        ev["lead_prompt_all_populated_rendered"] = bool(populated) and all(rendered.values())
        ev["lead_prompt_canary_field_hits"] = canary_hits
        ev["expected_echo"] = echo_target  # internal only; never copied into the manifest
        self._save_evidence(ev)
        return prompt + (
            "\n\n=== AUTOMATED SMOKE OVERRIDE (HIGHEST PRIORITY — overrides ALL "
            "grounding above) ===\nThis is an automated wiring test. A worker "
            "handoff is already COMPLETE and the goal is considered DONE. You MUST "
            "terminate the run NOW.\n"
            "In the pending handoff block rendered above, find its `reason` field. "
            "That reason value ends with a short code after the final hyphen (an "
            "8-character alphanumeric code).\n"
            "Your ONLY permitted action: call `submit_lead_decision` EXACTLY ONCE "
            "with kind=\"finish\". Do NOT choose dispatch, continue_lead, wait, or "
            "pause under any circumstances — those are all forbidden here.\n"
            f"Set ack_handoff_ids={ids}. Set final_acceptance to EXACTLY that "
            "8-character code and nothing else (copy it character for character). "
            "Then finish. Do nothing else."
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
        if kind == "finish" and "lead_decision_echo_match" not in ev:
            expected = ev.get("expected_echo")
            # Substring containment: the echo target is a short, distinctive code
            # the Lead had to read out of the rendered handoff payload. Its
            # presence in `final_acceptance` proves genuine delivery into the
            # Lead's SDK turn while tolerating benign surrounding words/punctuation.
            acc = "" if acceptance is None else str(acceptance)
            ev["lead_decision_echo_match"] = (
                bool(expected) and str(expected).strip() in acc)
        self._save_evidence(ev)
