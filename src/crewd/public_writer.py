"""Durable, attributed public-write journal + orchestrator publisher (issue #29).

GOAL required outcomes #2 (publish *every* material inter-role communication as an
attributed public GitHub artifact), #3 (mandatory attribution), and #5 (restart
safety across GitHub side effects) ask that each material role handoff / Lead
routing decision become a verified GitHub artifact — *exactly once* — and that a
crash between "intent" and "verified" never loses, duplicates, or silently drops
the write.

:class:`~crewd.github_bus.PublicBus` already makes a single post idempotent and
verified (attribution first line, correlation marker pre-check, ambiguous-write
reconcile, verified re-read). This module adds the *durable* layer around it that
``post`` alone lacks:

* **A journaled intent per correlation id.** Before a post is attempted the intent
  is reserved in a durable JSON journal (``state/public_writes/``), so a crash
  mid-write leaves an observable ``reserved`` record that :meth:`PublicWriter.reconcile`
  finishes on restart (the correlation marker makes the retry a no-op if it landed).
* **Verified-URL capture.** Only a *re-read* URL from ``PublicBus`` is stored; a
  model-provided URL is never trusted.
* **Explicit recoverable routing.** A GitHub outage leaves the intent ``reserved``
  and surfaces :class:`Route.WAIT`/``PAUSE`` — never a silent internal success.
* **Duplicate suppression.** A second publish for the same correlation id short-
  circuits to the already-verified artifact.

The orchestrator composes this to publish role handoffs at their terminal and to
enforce, at Lead ack time, that a *material* handoff is not consumed until its
public artifact is verified (see :mod:`crewd.orchestrator`).
"""
from __future__ import annotations

import enum
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .github_bus import (
    PostOutcome,
    PrereqOutcome,
    PublicBus,
    Route,
)
from .inbox import redact_secrets


# ── materiality ─────────────────────────────────────────────────────────
def _has_content(value: str) -> bool:
    """Whether a handoff field carries genuinely material content.

    An empty field, or an explicit no-op sentinel (``none``/``n/a``/``-``), is not
    material — so a Verifier approval's ``changed: none`` and a bare no-progress
    return do not, by themselves, force a public artifact.
    """
    v = (value or "").strip().lower()
    return bool(v) and v not in ("none", "n/a", "na", "-", "n.a.")


def is_material_handoff(
    outcome_class: str,
    *,
    evidence: str = "",
    changed: str = "",
    remaining: str = "",
    disagreement: str = "",
    blocker: str = "",
) -> bool:
    """Whether a role handoff must be published as a public artifact.

    Every ``completed`` and ``uncertain`` handoff is material. The single narrow
    exception (GOAL: "all-role material handoff enforcement with the narrow
    private ``no_progress`` exception") is a *genuinely empty* ``no_progress``
    return — a bare "nothing changed, try again" that carries no changed state, no
    material remaining work, no evidence, no disagreement, and no blocker. A
    ``no_progress`` that reports concrete evidence, a *changed* state, material
    *remaining* work, a disagreement, or a blocker IS material and must be
    published. The return ``reason`` alone does not make a no-progress material
    (every no-progress carries one), so it is intentionally excluded here.
    """
    oc = (outcome_class or "").lower()
    if oc == "no_progress":
        return any(_has_content(f) for f in (evidence, changed, remaining,
                                             disagreement, blocker))
    return True


# ── field/body rendering ────────────────────────────────────────────────
_MAX_FIELD = 1500


def _clip(text: str) -> str:
    text = redact_secrets((text or "").strip())
    if len(text) > _MAX_FIELD:
        return text[: _MAX_FIELD - 1].rstrip() + "\u2026"
    return text


def render_role_handoff_body(
    *,
    role: str,
    target_role: str,
    outcome_class: str,
    evidence: str = "",
    changed: str = "",
    remaining: str = "",
    reason: str = "",
    disagreement: str = "",
    blocker: str = "",
    context: str = "",
) -> str:
    """Render the *content* of a role handoff artifact (no attribution line).

    :class:`PublicBus.post` prepends the mandatory first-line attribution and the
    correlation marker, so this body is only the structured, redacted, bounded
    account. ``changed`` may legitimately be ``none`` (e.g. a Verifier approval).
    """
    lines = [f"**Role handoff:** {role} \u2192 {target_role}",
             f"**Outcome:** {(outcome_class or '').lower()}"]
    if context.strip():
        lines.append(f"**Context:** {_clip(context)}")
    for label, value in (
        ("Evidence", evidence),
        ("Changed", changed),
        ("Remaining", remaining),
        ("Reason", reason),
        ("Disagreement", disagreement),
        ("Blocker", blocker),
    ):
        if value and value.strip():
            lines.append(f"**{label}:** {_clip(value)}")
    return "\n\n".join(lines)


def render_lead_decision_body(
    *,
    kind: str,
    target_role: str = "",
    reason: str = "",
    context: str = "",
) -> str:
    """Render the content of a Lead routing-decision artifact (no attribution)."""
    lines = [f"**Lead decision:** {(kind or '').lower()}"]
    if target_role.strip():
        lines.append(f"**Routed to:** {target_role}")
    if context.strip():
        lines.append(f"**Context:** {_clip(context)}")
    if reason and reason.strip():
        lines.append(f"**Rationale:** {_clip(reason)}")
    return "\n\n".join(lines)


# ── durable intent journal ──────────────────────────────────────────────
class WriteState(str, enum.Enum):
    RESERVED = "reserved"   # intent journaled; post not yet verified
    VERIFIED = "verified"   # post confirmed by a re-read URL


@dataclass
class WriteIntent:
    """One durable public-write intent, keyed by a stable correlation id."""

    correlation_id: str
    role: str
    target_role: str
    target: str                 # "issue" | "pull"
    number: int
    body: str
    state: str = WriteState.RESERVED.value
    url: str = ""
    created_at: str = ""
    verified_at: str = ""
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "WriteIntent":
        data = json.loads(text)
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(correlation_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", correlation_id)


class IntentStore:
    """Durable JSON journal of public-write intents (one file per correlation id).

    Mirrors :class:`~crewd.inbox.InboxService`'s durable-store discipline: each
    write is atomic (temp file + ``os.replace``), so a crash never leaves a
    half-written record, and the journal is the single source of truth for what
    has been reserved vs verified.
    """

    def __init__(self, directory: Path):
        self._dir = Path(directory)

    @classmethod
    def for_workspace(cls, ws) -> "IntentStore":
        return cls(ws.state_dir / "public_writes")

    def _path(self, correlation_id: str) -> Path:
        return self._dir / f"{_safe(correlation_id)}.json"

    def _write_atomic(self, intent: WriteIntent) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(intent.correlation_id)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(intent.to_json(), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, correlation_id: str) -> Optional[WriteIntent]:
        path = self._path(correlation_id)
        if not path.exists():
            return None
        try:
            return WriteIntent.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def reserve(self, intent: WriteIntent) -> WriteIntent:
        """Reserve an intent, preserving any prior (already-verified) record.

        Reservation is idempotent: if a verified record already exists for this
        correlation id it is returned unchanged (duplicate suppression), so a
        retry never regresses a verified write back to ``reserved``.
        """
        existing = self.get(intent.correlation_id)
        if existing is not None and existing.state == WriteState.VERIFIED.value:
            return existing
        if existing is not None:
            # keep original created_at; refresh the pending body/detail
            intent.created_at = existing.created_at or _now()
        else:
            intent.created_at = _now()
        intent.state = WriteState.RESERVED.value
        self._write_atomic(intent)
        return intent

    def mark_verified(self, correlation_id: str, url: str, detail: str = "") -> Optional[WriteIntent]:
        intent = self.get(correlation_id)
        if intent is None:
            return None
        intent.state = WriteState.VERIFIED.value
        intent.url = url
        intent.detail = detail
        intent.verified_at = _now()
        self._write_atomic(intent)
        return intent

    def record_detail(self, correlation_id: str, detail: str) -> None:
        """Record a non-verifying outcome detail (e.g. a WAIT/PAUSE reason)."""
        intent = self.get(correlation_id)
        if intent is None:
            return
        intent.detail = detail
        self._write_atomic(intent)

    def _all(self) -> list[WriteIntent]:
        if not self._dir.exists():
            return []
        out = []
        for p in sorted(self._dir.glob("*.json")):
            try:
                out.append(WriteIntent.from_json(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def list_pending(self) -> list[WriteIntent]:
        return [i for i in self._all() if i.state != WriteState.VERIFIED.value]

    def list_verified(self) -> list[WriteIntent]:
        return [i for i in self._all() if i.state == WriteState.VERIFIED.value]

    def counts(self) -> dict:
        pending = verified = 0
        for i in self._all():
            if i.state == WriteState.VERIFIED.value:
                verified += 1
            else:
                pending += 1
        return {"pending": pending, "verified": verified}


# ── publisher ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PublishOutcome:
    """Result of a durable publish attempt."""

    route: Route
    detail: str
    url: str = ""
    correlation_id: str = ""
    deduplicated: bool = False

    @property
    def ok(self) -> bool:
        return self.route is Route.PROCEED

    @property
    def verified(self) -> bool:
        return self.ok and bool(self.url)


# Attribution target for each role's *material* handoff. The Worker's readiness is
# addressed to the Verifier (and satisfies the Verifier-dispatch prerequisite);
# the Verifier reports back to Lead; an Advisory observation is addressed to all.
_HANDOFF_TARGET = {
    "worker": "verifier",
    "verifier": "lead",
    "advisory": "all",
    "lead": "all",
}


class PublicWriter:
    """Durable, attributed publisher composing :class:`PublicBus` + :class:`IntentStore`.

    The writer is the single production seam for *outbound* crew writes. It never
    fabricates success: a GitHub outage leaves a durable ``reserved`` intent and a
    :class:`Route.WAIT`/``PAUSE`` outcome for the caller to route on, and
    :meth:`reconcile` finishes those intents on restart.
    """

    def __init__(self, bus: PublicBus, store: IntentStore):
        self.bus = bus
        self.store = store

    # -- target resolution (never hard-code an identifier) --
    def resolve_task_number(self) -> tuple[Optional[int], PrereqOutcome]:
        outcome = self.bus.resolve_active_task()
        if not outcome.ok:
            return None, outcome
        return int(outcome.refs.get("task")), outcome

    # -- core publish --
    def publish(
        self,
        *,
        role: str,
        target_role: str,
        target: str,
        number: int,
        correlation_id: str,
        body: str,
    ) -> PublishOutcome:
        """Durably publish one attributed artifact, exactly once.

        Reserve the intent → delegate to the idempotent :meth:`PublicBus.post`
        (marker pre-check, write, verify, ambiguous-reconcile) → on ``PROCEED``
        capture the verified URL and mark the intent verified; on ``WAIT``/``PAUSE``
        leave it reserved for :meth:`reconcile`; on ``REJECT`` record the reason.
        """
        existing = self.store.get(correlation_id)
        if existing is not None and existing.state == WriteState.VERIFIED.value:
            return PublishOutcome(Route.PROCEED, "already verified (duplicate suppressed)",
                                  url=existing.url, correlation_id=correlation_id,
                                  deduplicated=True)

        self.store.reserve(WriteIntent(
            correlation_id=correlation_id, role=role, target_role=target_role,
            target=target, number=number, body=body,
        ))

        post: PostOutcome = self.bus.post(
            role=role, target_role=target_role, body=body,
            target=target, number=number, correlation_id=correlation_id,
        )
        if post.route is Route.PROCEED and post.comment is not None:
            self.store.mark_verified(correlation_id, post.comment.url, post.detail)
            return PublishOutcome(Route.PROCEED, post.detail, url=post.comment.url,
                                  correlation_id=correlation_id,
                                  deduplicated=post.deduplicated)
        # Not verified: keep the reserved intent for reconciliation and surface the
        # explicit recoverable route (REJECT for a bad attribution; WAIT/PAUSE for
        # a GitHub failure) — never a silent success.
        self.store.record_detail(correlation_id, post.detail)
        return PublishOutcome(post.route, post.detail, correlation_id=correlation_id)

    # -- restart reconciliation --
    def reconcile(self) -> list[PublishOutcome]:
        """Finish every still-``reserved`` intent (idempotent via the marker).

        Called on run start. Re-posting through :meth:`PublicBus.post` is a no-op
        if the write already landed (the correlation marker is found), so a crash
        between reserve and verify is repaired without a double-post.
        """
        results = []
        for intent in self.store.list_pending():
            results.append(self.publish(
                role=intent.role, target_role=intent.target_role,
                target=intent.target, number=intent.number,
                correlation_id=intent.correlation_id, body=intent.body,
            ))
        return results

    # -- queries --
    def is_verified(self, correlation_id: str) -> bool:
        intent = self.store.get(correlation_id)
        return intent is not None and intent.state == WriteState.VERIFIED.value

    def verified_url(self, correlation_id: str) -> Optional[str]:
        intent = self.store.get(correlation_id)
        if intent is not None and intent.state == WriteState.VERIFIED.value:
            return intent.url
        return None

    def counts(self) -> dict:
        return self.store.counts()

    # -- high-level: publish a material role handoff to the active task --
    def publish_role_handoff(
        self,
        *,
        handoff_id: str,
        role: str,
        outcome_class: str,
        evidence: str = "",
        changed: str = "",
        remaining: str = "",
        reason: str = "",
        disagreement: str = "",
        blocker: str = "",
    ) -> PublishOutcome:
        """Resolve the active task and durably publish a role's material handoff.

        The correlation id is the durable dispatcher ``handoff_id`` so the artifact
        is stable across crash/retry and the orchestrator can later prove the
        handoff was published before it is consumed.
        """
        number, resolved = self.resolve_task_number()
        if number is None:
            # Target could not be resolved from the public record: surface the
            # recoverable route (a REJECT record maps to WAIT for the writer, since
            # the artifact still needs to be posted once the record is fixed).
            route = resolved.route if resolved.route in (Route.WAIT, Route.PAUSE) else Route.WAIT
            return PublishOutcome(route, f"cannot resolve active task: {resolved.detail}",
                                  correlation_id=handoff_id)
        target_role = _HANDOFF_TARGET.get(role, "lead")
        body = render_role_handoff_body(
            role=role, target_role=target_role, outcome_class=outcome_class,
            evidence=evidence, changed=changed, remaining=remaining, reason=reason,
            disagreement=disagreement, blocker=blocker,
        )
        return self.publish(
            role=role, target_role=target_role, target="issue", number=number,
            correlation_id=handoff_id, body=body,
        )

    # -- high-level: publish a Lead routing decision (best-effort observability) --
    def publish_lead_decision(
        self,
        *,
        decision_id: str,
        kind: str,
        target_role: str = "",
        reason: str = "",
    ) -> PublishOutcome:
        """Durably publish a Lead routing decision to the active task issue."""
        number, resolved = self.resolve_task_number()
        if number is None:
            return PublishOutcome(Route.WAIT, f"cannot resolve active task: {resolved.detail}",
                                  correlation_id=decision_id)
        body = render_lead_decision_body(kind=kind, target_role=target_role, reason=reason)
        return self.publish(
            role="lead", target_role=(target_role or "all"), target="issue",
            number=number, correlation_id=decision_id, body=body,
        )
