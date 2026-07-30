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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .github_bus import (
    PostOutcome,
    PrereqOutcome,
    PublicBus,
    RejectReason,
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


class LifecyclePhase(str, enum.Enum):
    """Durable terminal-write lifecycle (issue #49).

    Models the full ordering a terminal Verifier/Lead record travels through, so a
    crash or an out-of-order merge/close is reconciled to the right next step
    rather than mis-reported as a human blocker:

    * ``verification_in_flight`` — intent reserved; the attributed artifact is
      being (re)published to the bound target.
    * ``verified`` — the artifact is confirmed by a re-read URL.
    * ``closure_observed`` — the exact linked PR was observed merged / the bound
      task observed closed by it (closure provenance recorded).
    * ``acknowledged`` — Lead consumed the handoff; the record is complete.

    ``invalid_target`` and ``blocked`` are terminal off-ramps: an invalid target
    (wrong repo/goal, deleted, unrelated closure) or a genuine human blocker.
    """

    IN_FLIGHT = "verification_in_flight"
    VERIFIED = "verified"
    CLOSURE_OBSERVED = "closure_observed"
    ACKNOWLEDGED = "acknowledged"
    INVALID_TARGET = "invalid_target"
    BLOCKED = "blocked"


# Route → the disposition an unverified terminal write takes (issue #49).
_RECOVERABLE = "recoverable"   # WAIT: retry/reconcile on a bounded backoff
_HUMAN = "human"               # PAUSE: genuine operator credential/policy blocker
_INVALID = "invalid_target"    # REJECT: wrong/deleted/unrelated target — terminal


def classify_route(route: Route) -> str:
    """Classify a non-PROCEED route into a durable disposition (issue #49)."""
    if route is Route.WAIT:
        return _RECOVERABLE
    if route is Route.PAUSE:
        return _HUMAN
    return _INVALID  # Route.REJECT


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
    attempts: int = 0           # publish attempts so far (reserve + each retry)
    last_route: str = ""        # last non-verified route (wait/pause/reject)
    # -- lifecycle / provenance (issue #49) --
    phase: str = LifecyclePhase.IN_FLIGHT.value
    repo: str = ""              # expected repo captured at reserve
    goal_label: str = ""        # current goal captured at reserve
    task_number: int = 0        # exact bound task (0 = none/legacy)
    pr_number: int = 0          # linked PR captured at Verifier routing (0 = none)
    closure_pr: int = 0         # PR observed to have merged/closed the task
    disposition: str = ""       # recoverable | human | invalid_target
    last_error: str = ""        # last classified failure detail
    next_retry_at: str = ""     # earliest time a WAIT retry may run (backoff)
    backoff_s: float = 0.0      # current backoff window in seconds

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "WriteIntent":
        data = json.loads(text)
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


def _now(clock: Optional[Callable[[], datetime]] = None) -> str:
    now = clock() if clock is not None else datetime.now(timezone.utc)
    return now.isoformat(timespec="seconds")


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe(correlation_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", correlation_id)


# Bounded exponential backoff for a recoverable (WAIT) terminal write (issue #49).
_BACKOFF_BASE_S = 2.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_CAP_S = 300.0
def _backoff_for(attempts: int) -> float:
    """The backoff window (seconds) after ``attempts`` failed retries, capped."""
    if attempts <= 0:
        return 0.0
    return min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (_BACKOFF_FACTOR ** (attempts - 1)))


class IntentStore:
    """Durable JSON journal of public-write intents (one file per correlation id).

    Mirrors :class:`~crewd.inbox.InboxService`'s durable-store discipline: each
    write is atomic (temp file + ``os.replace``), so a crash never leaves a
    half-written record, and the journal is the single source of truth for what
    has been reserved vs verified.
    """

    def __init__(self, directory: Path, *, clock: Optional[Callable[[], datetime]] = None):
        self._dir = Path(directory)
        self._clock = clock

    @classmethod
    def for_workspace(cls, ws, *, clock: Optional[Callable[[], datetime]] = None) -> "IntentStore":
        return cls(ws.state_dir / "public_writes", clock=clock)

    def _now(self) -> str:
        return _now(self._clock)

    def _now_dt(self) -> datetime:
        return self._clock() if self._clock is not None else datetime.now(timezone.utc)

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
        retry never regresses a verified write back to ``reserved``. Accumulated
        retry/lifecycle history (attempts, route, backoff, provenance) survives a
        re-reserve so bounded backoff and diagnostics persist across restarts.
        """
        existing = self.get(intent.correlation_id)
        if existing is not None and existing.state == WriteState.VERIFIED.value:
            return existing
        if existing is not None:
            intent.created_at = existing.created_at or self._now()
            intent.attempts = existing.attempts
            intent.last_route = existing.last_route
            intent.disposition = existing.disposition
            intent.last_error = existing.last_error
            intent.next_retry_at = existing.next_retry_at
            intent.backoff_s = existing.backoff_s
            intent.closure_pr = existing.closure_pr or intent.closure_pr
            # keep a previously-captured PR binding if the caller didn't supply one
            intent.pr_number = intent.pr_number or existing.pr_number
            # do not regress a richer phase back to in-flight
            if existing.phase not in (
                LifecyclePhase.IN_FLIGHT.value, WriteState.RESERVED.value, ""
            ):
                intent.phase = existing.phase
        else:
            intent.created_at = self._now()
        intent.state = WriteState.RESERVED.value
        if intent.phase in ("", WriteState.RESERVED.value):
            intent.phase = LifecyclePhase.IN_FLIGHT.value
        self._write_atomic(intent)
        return intent

    def mark_verified(self, correlation_id: str, url: str, detail: str = "") -> Optional[WriteIntent]:
        intent = self.get(correlation_id)
        if intent is None:
            return None
        intent.state = WriteState.VERIFIED.value
        intent.url = url
        intent.detail = detail
        intent.last_route = Route.PROCEED.value
        intent.disposition = ""
        intent.last_error = ""
        intent.next_retry_at = ""
        intent.backoff_s = 0.0
        intent.attempts += 1
        # Verified is the "verification published" milestone; do not downgrade a
        # later closure/ack phase if one was already observed.
        if intent.phase in (
            LifecyclePhase.IN_FLIGHT.value, WriteState.RESERVED.value, "",
            LifecyclePhase.BLOCKED.value,
        ):
            intent.phase = LifecyclePhase.VERIFIED.value
        intent.verified_at = self._now()
        self._write_atomic(intent)
        return intent

    def record_detail(
        self, correlation_id: str, detail: str, route: str = "",
        *, disposition: str = "",
    ) -> None:
        """Record a non-verifying outcome and schedule a bounded backoff.

        Counts the attempt, remembers the route/disposition and last error, and —
        for a recoverable (WAIT) route — computes the next-retry time from a
        bounded exponential backoff so the daemon/next run retries autonomously
        without hammering the boundary. A genuine human blocker or invalid target
        records no retry window (issue #49)."""
        intent = self.get(correlation_id)
        if intent is None:
            return
        intent.detail = detail
        intent.last_error = detail
        if route:
            intent.last_route = route
        if not disposition and route:
            disposition = classify_route(Route(route))
        if disposition:
            intent.disposition = disposition
        intent.attempts += 1
        if disposition == _RECOVERABLE:
            # A transient condition never becomes a human prerequisite merely
            # because it lasted a long time. Bound request pressure with capped
            # backoff while retaining the typed WAIT disposition indefinitely.
            intent.backoff_s = _backoff_for(intent.attempts - 1)
            nxt = self._now_dt() + timedelta(seconds=intent.backoff_s)
            intent.next_retry_at = nxt.isoformat(timespec="seconds")
        else:
            intent.next_retry_at = ""
            intent.backoff_s = 0.0
            intent.phase = (
                LifecyclePhase.INVALID_TARGET.value if disposition == _INVALID
                else LifecyclePhase.BLOCKED.value if disposition == _HUMAN
                else intent.phase
            )
        self._write_atomic(intent)

    def mark_closure_observed(self, correlation_id: str, closure_pr: int = 0) -> Optional[WriteIntent]:
        """Record that the bound task was observed closed by its linked merge."""
        intent = self.get(correlation_id)
        if intent is None:
            return None
        intent.closure_pr = closure_pr or intent.closure_pr
        if intent.phase in (
            LifecyclePhase.VERIFIED.value, LifecyclePhase.IN_FLIGHT.value,
            WriteState.RESERVED.value, "",
        ):
            intent.phase = LifecyclePhase.CLOSURE_OBSERVED.value
        self._write_atomic(intent)
        return intent

    def mark_acknowledged(self, correlation_id: str) -> Optional[WriteIntent]:
        """Record that Lead consumed the handoff — the terminal record is done."""
        intent = self.get(correlation_id)
        if intent is None:
            return None
        intent.phase = LifecyclePhase.ACKNOWLEDGED.value
        self._write_atomic(intent)
        return intent

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

    def list_due_pending(self) -> list[WriteIntent]:
        """Pending recoverable intents whose backoff window has elapsed.

        A pending write is *due* when it has no scheduled ``next_retry_at`` (never
        attempted, or a fresh reservation) or its backoff time has passed. A write
        parked as a human blocker or an invalid target is never due — it will not
        self-heal and must not be retried in a loop (issue #49)."""
        now = self._now_dt()
        due = []
        for i in self.list_pending():
            if i.disposition in (_HUMAN, _INVALID):
                continue
            nxt = _parse_ts(i.next_retry_at)
            if nxt is None or nxt <= now:
                due.append(i)
        return due

    def list_blocked(self) -> list[WriteIntent]:
        """Pending intents that need a human / are invalid targets (not self-healing)."""
        return [i for i in self.list_pending() if i.disposition in (_HUMAN, _INVALID)]

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
        task_number: Optional[int] = None,
        pr_number: Optional[int] = None,
        authorize: bool = False,
    ) -> PublishOutcome:
        """Durably publish one attributed artifact, exactly once.

        Reserve the intent (capturing the exact repo/goal/task/PR provenance) →
        (for a terminal write) authorize the bound namespace + closure provenance
        → delegate to the idempotent :meth:`PublicBus.post` → on ``PROCEED``
        capture the verified URL and mark the intent verified; on ``WAIT`` schedule
        a bounded backoff retry; on ``PAUSE`` park as a human blocker; on
        ``REJECT`` park as an invalid target — never a silent success (issue #49).
        """
        existing = self.store.get(correlation_id)
        if existing is not None and existing.state == WriteState.VERIFIED.value:
            return PublishOutcome(Route.PROCEED, "already verified (duplicate suppressed)",
                                  url=existing.url, correlation_id=correlation_id,
                                  deduplicated=True)

        self.store.reserve(WriteIntent(
            correlation_id=correlation_id, role=role, target_role=target_role,
            target=target, number=number, body=body,
            repo=self.bus.expected_repo, goal_label=self.bus.goal_label,
            task_number=int(task_number or 0), pr_number=int(pr_number or 0),
        ))

        if authorize and task_number is not None:
            auth = self.bus.authorize_terminal(
                task_number=task_number,
                pr_number=(pr_number if pr_number else None),
            )
            if not auth.ok:
                disp = classify_route(auth.route)
                self.store.record_detail(
                    correlation_id, f"authorization: {auth.detail}",
                    route=auth.route.value, disposition=disp,
                )
                return PublishOutcome(auth.route, auth.detail, correlation_id=correlation_id)
            # Capture the observed closure PR as provenance for diagnostics.
            observed_pr = auth.refs.get("pr")
            if observed_pr:
                self.store.mark_closure_observed(correlation_id, int(observed_pr))

        post: PostOutcome = self.bus.post(
            role=role, target_role=target_role, body=body,
            target=target, number=number, correlation_id=correlation_id,
        )
        if post.route is Route.PROCEED and post.comment is not None:
            self.store.mark_verified(correlation_id, post.comment.url, post.detail)
            return PublishOutcome(Route.PROCEED, post.detail, url=post.comment.url,
                                  correlation_id=correlation_id,
                                  deduplicated=post.deduplicated)
        # Not verified: keep the reserved intent, record the typed disposition, and
        # schedule a bounded backoff for a recoverable route.
        self.store.record_detail(
            correlation_id, post.detail, route=post.route.value,
            disposition=classify_route(post.route),
        )
        return PublishOutcome(post.route, post.detail, correlation_id=correlation_id)

    # -- restart / autonomous reconciliation --
    def reconcile(self, *, force: bool = False) -> list[PublishOutcome]:
        """Finish every *due* still-pending intent (idempotent via the marker).

        Only intents whose bounded backoff window has elapsed are retried, and a
        write parked as a human blocker or invalid target is skipped (it will not
        self-heal). Re-publishing through :meth:`PublicBus.post` is a no-op if the
        write already landed (the correlation marker is found), and a terminal
        write re-runs authorization so a since-merged closure is now allowed and a
        since-deleted target terminates — so a crash between reserve and verify,
        or a closed-target ordering race, is repaired without a double-post
        (issue #49)."""
        results = []
        pending = (
            [
                i for i in self.store.list_pending()
                if i.disposition not in (_HUMAN, _INVALID)
            ]
            if force else self.store.list_due_pending()
        )
        for intent in pending:
            results.append(self.publish(
                role=intent.role, target_role=intent.target_role,
                target=intent.target, number=intent.number,
                correlation_id=intent.correlation_id, body=intent.body,
                task_number=(intent.task_number or None),
                pr_number=(intent.pr_number or None),
                authorize=bool(intent.task_number),
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

    def acknowledge(self, correlation_id: str) -> None:
        """Record that Lead consumed the handoff (lifecycle → acknowledged)."""
        self.store.mark_acknowledged(correlation_id)

    def has_operator_block(self) -> bool:
        """Whether any pending write genuinely needs an operator (not self-healing)."""
        return bool(self.store.list_blocked())

    def has_recoverable_pending(self) -> bool:
        """Whether any pending write is still recoverable (WAIT/reconcile)."""
        return any(
            i.disposition not in (_HUMAN, _INVALID)
            for i in self.store.list_pending()
        )

    def counts(self) -> dict:
        return self.store.counts()

    # -- high-level: publish a material role handoff to the active task --
    def publish_role_handoff(
        self,
        *,
        handoff_id: str,
        role: str,
        outcome_class: str,
        task_number: Optional[int] = None,
        pr_number: Optional[int] = None,
        evidence: str = "",
        changed: str = "",
        remaining: str = "",
        reason: str = "",
        disagreement: str = "",
        blocker: str = "",
    ) -> PublishOutcome:
        """Durably publish a role's material handoff to the *routed* task.

        ``task_number`` is the exact task bound to this handoff's dispatch
        (issue #47); when supplied the artifact is posted there without a global
        re-census of the public record, so a second queued/assigned task can
        never divert the handoff. When omitted (legacy/defensive callers) the
        active task is resolved from the record as a fallback.

        The write is *authorized* against the bound namespace and closure
        provenance before it lands (issue #49): an open task proceeds; a closed
        task proceeds only when a merged linked PR closed it (idempotent terminal
        publication onto a legitimately-closed target); a wrong-repo/goal or
        deleted/unrelated-closed target is rejected as an invalid target rather
        than blocking a human forever. ``pr_number`` is the linked PR carried as
        closure provenance.

        The correlation id is the durable dispatcher ``handoff_id`` so the artifact
        is stable across crash/retry and the orchestrator can later prove the
        handoff was published before it is consumed.
        """
        number, resolved = self._resolve_target(task_number)
        if number is None:
            # Target could not be resolved: surface the recoverable route (a
            # REJECT record maps to WAIT for the writer, since the artifact still
            # needs to be posted once the routed task is known/fixed) — never a
            # mispublish to an unrelated task.
            route = resolved.route if resolved.route in (Route.WAIT, Route.PAUSE) else Route.WAIT
            return PublishOutcome(route, f"cannot resolve routed task: {resolved.detail}",
                                  correlation_id=handoff_id)
        target_role = _HANDOFF_TARGET.get(role, "lead")
        if role == "verifier" and pr_number is None:
            # Capture the exact linked PR in the durable intent before terminal
            # publication. It remains authoritative if merge auto-closes the task
            # before a restart/reconciliation pass.
            pr_number = self.bus.linked_pr(number)
        body = render_role_handoff_body(
            role=role, target_role=target_role, outcome_class=outcome_class,
            evidence=evidence, changed=changed, remaining=remaining, reason=reason,
            disagreement=disagreement, blocker=blocker,
        )
        return self.publish(
            role=role, target_role=target_role, target="issue", number=number,
            correlation_id=handoff_id, body=body,
            task_number=number, pr_number=pr_number, authorize=True,
        )

    # -- high-level: publish a Lead routing decision (best-effort observability) --
    def publish_lead_decision(
        self,
        *,
        decision_id: str,
        kind: str,
        target_role: str = "",
        reason: str = "",
        task_number: Optional[int] = None,
    ) -> PublishOutcome:
        """Durably publish a Lead routing decision to the routed task issue.

        ``task_number`` (issue #47) is the exact task the decision routes to;
        when supplied the decision is posted there rather than re-resolved from
        the public record.
        """
        number, resolved = self._resolve_target(task_number)
        if number is None:
            return PublishOutcome(Route.WAIT, f"cannot resolve routed task: {resolved.detail}",
                                  correlation_id=decision_id)
        body = render_lead_decision_body(kind=kind, target_role=target_role, reason=reason)
        return self.publish(
            role="lead", target_role=(target_role or "all"), target="issue",
            number=number, correlation_id=decision_id, body=body,
        )

    def _resolve_target(
        self, task_number: Optional[int]
    ) -> tuple[Optional[int], PrereqOutcome]:
        """Resolve the task a write targets: the bound routed task if supplied,
        else the legacy global census. Production always supplies the binding."""
        if task_number is not None:
            return task_number, PrereqOutcome.proceed(f"routed task #{task_number}",
                                                      task=task_number)
        return self.resolve_task_number()
