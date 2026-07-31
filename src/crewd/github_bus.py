"""Typed public-bus (GitHub) transaction boundary for the crew dispatcher.

GOAL required outcomes #1/#2/#3/#5 ask that GitHub — issues, PRs, reviews,
comments — be the canonical public record for material crew coordination, and
that dispatcher authority never advance while bypassing that record. Prompt text
alone is insufficient; this module is the *typed contract and validation
boundary* through which the orchestrator gates authority and posts attributed
artifacts.

Design (a boundary, not a distributed transaction):

* **One seam.** All GitHub effects flow through the small :class:`GitHubClient`
  protocol. Production uses :class:`CliGitHubClient` (wrapping ``gh``); tests use
  :class:`FakeGitHubClient`. Nothing else in the codebase shells out to ``gh`` for
  bus operations.
* **Typed prerequisite validation.** :class:`PublicBus` answers, with an explicit
  typed :class:`PrereqOutcome`, whether the public record satisfies the invariant
  for each authority transition: a single open umbrella goal issue; an open linked
  ``crewd:task`` with observable ownership before Worker routing; a linked
  PR/acceptance artifact with an attributed readiness record before Verifier
  routing; and a closed final-acceptance issue + public summary before finish.
  Missing, closed, wrong-goal, wrong-repository, and unverified/model-provided
  references are *rejected* — distinctly from a transient GitHub *outage*.
* **Explicit failure routing.** A GitHub failure is classified
  (:class:`GitHubErrorKind`) and mapped to a recoverable action —
  ``wait`` (retryable: rate-limit/timeout/ambiguous/transient) or ``pause``
  (human: permission) — never silent internal-only success. A rejected
  prerequisite maps to ``reject`` (authority does not advance, no handoff
  consumed).
* **Idempotent posting.** Every crew write carries a first-line attribution and a
  stable ``crewd:correlation`` marker. :meth:`PublicBus.post` reserves intent,
  searches for the marker before writing (so a crash/ambiguous retry never
  double-posts), writes, then verifies the persisted URL. An unverified
  model-provided URL is never treated as proof.
"""
from __future__ import annotations

import enum
import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


# ── attribution ─────────────────────────────────────────────────────────
# One canonical parseable first line. Rendered with an ASCII arrow (matching the
# crew's posted comments); the parser also accepts the Unicode arrow used in the
# rendered template so either convention validates.
_ATTR_RENDER = "> **[crewd:{role} -> {target}]** {crew}"
_ATTR_RE = re.compile(
    r"^>\s*\*\*\[crewd:(?P<role>[a-z]+)\s*(?:->|\u2192)\s*(?P<target>[a-z]+)\]\*\*\s+(?P<crew>.+?)\s*$"
)
ROLES = ("lead", "worker", "verifier", "advisory")
TARGETS = ("lead", "worker", "verifier", "advisory", "all")


@dataclass(frozen=True)
class Attribution:
    """The mandatory first-line attribution for a crew-authored artifact."""

    role: str
    target: str
    crew: str

    def render(self) -> str:
        return _ATTR_RENDER.format(role=self.role, target=self.target, crew=self.crew)

    @classmethod
    def parse(cls, text: str) -> Optional["Attribution"]:
        """Parse the first non-empty line of ``text`` as an attribution."""
        for line in text.splitlines():
            if not line.strip():
                continue
            m = _ATTR_RE.match(line.strip())
            if not m:
                return None
            return cls(m.group("role"), m.group("target"), m.group("crew").strip())
        return None

    def validate(self, *, crew: Optional[str] = None) -> Optional[str]:
        """Return an error string if this attribution is malformed, else ``None``."""
        if self.role not in ROLES:
            return f"unknown role {self.role!r}"
        if self.target not in TARGETS:
            return f"unknown target {self.target!r}"
        if not self.crew.strip():
            return "empty crew name"
        if crew is not None and self.crew.strip() != crew:
            return f"crew name {self.crew!r} != expected {crew!r}"
        return None


def validate_attribution(body: str, *, crew: Optional[str] = None) -> Optional[str]:
    """Return an error string if ``body`` lacks a valid first-line attribution."""
    attr = Attribution.parse(body)
    if attr is None:
        return "missing or malformed first-line crewd attribution"
    return attr.validate(crew=crew)


# ── boundary data types ─────────────────────────────────────────────────
@dataclass(frozen=True)
class IssueRef:
    number: int
    title: str
    state: str                       # "open" | "closed"
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    body: str = ""
    url: str = ""


@dataclass(frozen=True)
class PullRef:
    number: int
    title: str
    state: str                       # "open" | "closed" | "merged"
    body: str = ""
    url: str = ""
    linked_issues: tuple[int, ...] = ()
    # Durable head-of-branch evidence (#65): the source branch, the mergeability
    # signal, and a summarised check-rollup state. These let the host recover a
    # role's real branch/PR/check evidence directly from GitHub when the role's
    # own structured handoff was lost (a duplicate/malformed ``submit_role_handoff``
    # returns no payload), so Lead routes on durable facts rather than a fabricated
    # readiness. Empty string means "not reported by this read".
    head_ref: str = ""               # source branch name (gh ``headRefName``)
    mergeable: str = ""              # gh ``mergeable``: MERGEABLE/CONFLICTING/UNKNOWN
    checks: str = ""                 # summarised rollup: passing/failing/pending/none


@dataclass(frozen=True)
class CommentRef:
    id: int
    url: str
    body: str = ""


class GitHubErrorKind(str, enum.Enum):
    PERMISSION = "permission"        # auth/permission → human pause
    RATE_LIMIT = "rate_limit"        # retryable wait
    TIMEOUT = "timeout"              # retryable wait
    AMBIGUOUS = "ambiguous"          # write may or may not have landed → reconcile
    NOT_FOUND = "not_found"          # a concrete reference is absent
    TRANSIENT = "transient"          # other retryable network error


class GitHubError(Exception):
    """A boundary-level GitHub failure classified for explicit routing."""

    def __init__(self, kind: GitHubErrorKind, detail: str = ""):
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.kind in (
            GitHubErrorKind.RATE_LIMIT,
            GitHubErrorKind.TIMEOUT,
            GitHubErrorKind.TRANSIENT,
            GitHubErrorKind.AMBIGUOUS,
        )


# ── boundary protocol ───────────────────────────────────────────────────
@runtime_checkable
class GitHubClient(Protocol):
    """The sole GitHub effect seam. Methods raise :class:`GitHubError` on failure."""

    def repo(self) -> str: ...
    def list_issues(self, *, label: Optional[str] = None, state: str = "open") -> list[IssueRef]: ...
    def get_issue(self, number: int) -> Optional[IssueRef]: ...
    def get_pull(self, number: int) -> Optional[PullRef]: ...
    def list_pulls(self, state: str = "open") -> list[PullRef]: ...
    def list_comments(self, *, target: str, number: int) -> list[CommentRef]: ...
    def find_comment(self, *, target: str, number: int, marker: str) -> Optional[CommentRef]: ...
    def create_comment(self, *, target: str, number: int, body: str) -> CommentRef: ...


# ── host-side durable evidence discovery (#65) ──────────────────────────
#
# When a dispatched role reaches a clean idle but its structured
# ``submit_role_handoff`` is lost — a duplicate or malformed submission makes the
# exactly-one capture return *no payload* — the role may still have produced real
# durable work on GitHub (a pushed branch, an opened/mergeable PR, green checks).
# The host recovers that evidence directly from the exact bound task's linked PR
# so Lead routes on durable facts instead of a fabricated readiness, preserving
# the exact task/PR binding and enabling an exact-bound Verifier review — without
# blindly repeating Worker. Discovery NEVER upgrades the routing class; it only
# enriches evidence.
def summarize_check_rollup(rollup: object) -> str:
    """Summarise a ``gh`` ``statusCheckRollup`` list into a stable state token.

    ``passing`` when every contexts's conclusion/state is a success, ``failing``
    when any is a failure/error/cancelled, ``pending`` when some are still
    running with none failing, ``none`` when there are no checks, and ``unknown``
    when the shape is unrecognised. Kept deterministic and defensive so a shape
    change in the CLI degrades to ``unknown`` rather than raising.
    """
    if rollup is None:
        return "unknown"
    if not isinstance(rollup, (list, tuple)):
        return "unknown"
    if len(rollup) == 0:
        return "none"
    ok = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    bad = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    saw_pending = False
    for c in rollup:
        if not isinstance(c, dict):
            return "unknown"
        # A check run carries ``conclusion`` (+ ``status``); a legacy commit
        # status carries ``state``. Only an explicit success conclusion/state
        # passes — a bare ``COMPLETED`` status with no conclusion is treated as
        # not-yet-green (pending) rather than optimistically passing.
        raw = c.get("conclusion") or c.get("state") or ""
        token = str(raw).upper()
        if token in bad:
            return "failing"
        if token in ok:
            continue
        saw_pending = True
    return "pending" if saw_pending else "passing"


@dataclass(frozen=True)
class RecoveredEvidence:
    """Durable branch/PR/check evidence the host recovered for a bound task.

    ``found`` is True when a linked PR (or at least a branch) was discovered. All
    fields are read from GitHub, never from the role's lost handoff payload, so a
    completion can never be fabricated from a botched handoff.
    """

    task_number: int
    pr_number: Optional[int] = None
    pr_state: Optional[str] = None
    branch: Optional[str] = None
    mergeable: Optional[str] = None
    checks: Optional[str] = None
    pr_url: str = ""

    @property
    def found(self) -> bool:
        return self.pr_number is not None or bool(self.branch)

    def render(self) -> str:
        if not self.found:
            return f"no linked PR/branch found for task #{self.task_number}"
        parts = [f"task #{self.task_number}"]
        if self.pr_number is not None:
            pr = f"PR #{self.pr_number}"
            if self.pr_state:
                pr += f" ({self.pr_state})"
            parts.append(pr)
        if self.branch:
            parts.append(f"branch {self.branch}")
        if self.mergeable:
            parts.append(f"mergeable={self.mergeable}")
        if self.checks:
            parts.append(f"checks={self.checks}")
        if self.pr_url:
            parts.append(self.pr_url)
        return "; ".join(parts)


class EvidenceDiscovery:
    """Recovers a bound task's durable branch/PR/check evidence from GitHub.

    Pure read-side and side-effect free: it resolves the PR that links the exact
    routed ``crewd:task`` (optionally constrained to a specific ``pr_number`` when
    one was already bound), then reads its full head/mergeable/check state. A
    transient GitHub failure yields ``None`` (the caller keeps the un-enriched
    uncertain terminal); a clean absence yields a ``RecoveredEvidence`` with
    ``found == False`` (no fabricated evidence).
    """

    def __init__(self, client: GitHubClient):
        self.client = client

    def discover(
        self, *, task_number: int, pr_number: Optional[int] = None
    ) -> Optional[RecoveredEvidence]:
        try:
            pulls = self.client.list_pulls(state="all")
        except GitHubError:
            return None
        linked = [p for p in pulls if task_number in p.linked_issues]
        if pr_number is not None:
            # A previously bound PR is authoritative: only accept that exact PR so
            # an unrelated PR that also mentions the issue can never divert binding.
            linked = [p for p in linked if p.number == pr_number]
        if not linked:
            return RecoveredEvidence(task_number=task_number)
        if pr_number is None and len({p.number for p in linked}) > 1:
            # Ambiguous: several PRs mention the task and none is authoritatively
            # bound. Fail closed rather than guess a binding — the caller keeps the
            # un-enriched uncertain terminal (safe; never fabricates or mis-binds).
            return RecoveredEvidence(task_number=task_number)
        pr = linked[0]
        # Re-read the chosen PR for full head/mergeable/check detail (list views
        # may omit it); tolerate a transient failure by falling back to the list
        # record we already have rather than discarding the discovery.
        full = None
        try:
            full = self.client.get_pull(pr.number)
        except GitHubError:
            full = None
        p = full or pr
        return RecoveredEvidence(
            task_number=task_number,
            pr_number=p.number,
            pr_state=p.state,
            branch=p.head_ref or None,
            mergeable=p.mergeable or None,
            checks=p.checks or None,
            pr_url=p.url,
        )
class RejectReason(str, enum.Enum):
    MISSING = "missing"
    MULTIPLE = "multiple"
    CLOSED = "closed"
    WRONG_GOAL = "wrong_goal"
    WRONG_REPO = "wrong_repo"
    UNVERIFIED = "unverified"
    NOT_READY = "not_ready"
    NO_ASSIGNMENT = "no_assignment"
    NO_OWNER = "no_owner"
    UNATTRIBUTED = "unattributed"

    @property
    def lead_correctable(self) -> bool:
        """Whether Lead can repair this rejection by editing the public record.

        Every rejection except :attr:`WRONG_REPO` is a correctable internal /
        public-record inconsistency (missing assignment, missing readiness,
        ambiguous census, stale/unrelated closure, wrong-goal label): Lead can
        post the assignment/readiness, close a duplicate, relabel, or reroute
        under a different intent. ``WRONG_REPO`` is a workspace/configuration
        error only an operator can fix, so it is *not* Lead-correctable (#64).
        """
        return self is not RejectReason.WRONG_REPO


# Suggested Lead actions surfaced with a typed correction, keyed by the failed
# predicate. These are advice for Lead's routing turn, not a fixed policy — Lead
# retains final authority (repair the record, reroute, wait, or escalate).
_CORRECTION_ACTIONS: dict = {
    RejectReason.NO_ASSIGNMENT: (
        "repair_public_record: assign the task or post a Lead→worker assignment",
        "reroute", "wait", "escalate",
    ),
    RejectReason.NOT_READY: (
        "repair_public_record: post the attributed Worker readiness record, "
        "or reroute under a verifier-only audit/acceptance intent",
        "reroute", "wait", "escalate",
    ),
    RejectReason.MISSING: (
        "repair_public_record: create/link the missing task/PR/summary artifact",
        "reroute", "wait", "escalate",
    ),
    RejectReason.MULTIPLE: (
        "repair_public_record: close the duplicate so exactly one open record remains",
        "reroute", "wait", "escalate",
    ),
    RejectReason.CLOSED: (
        "repair_public_record: reopen the task, or route a post-merge terminal intent",
        "reroute", "wait", "escalate",
    ),
    RejectReason.WRONG_GOAL: (
        "repair_public_record: relabel the task to the current goal epoch",
        "reroute", "escalate",
    ),
    RejectReason.UNVERIFIED: (
        "repair_public_record: link the merged PR that closed the task, or reopen it",
        "reroute", "wait", "escalate",
    ),
}


class Route(str, enum.Enum):
    """How the orchestrator must act on a prerequisite/post outcome."""

    PROCEED = "proceed"     # invariant satisfied; authority may advance
    REJECT = "reject"       # public record invalid; do NOT advance, no handoff consumed
    WAIT = "wait"           # transient GitHub failure; retry via a wake condition
    PAUSE = "pause"         # human-only blocker (permission); halt for operator



@dataclass(frozen=True)
class PrereqOutcome:
    route: Route
    detail: str
    reason: Optional[RejectReason] = None
    error_kind: Optional[GitHubErrorKind] = None
    refs: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.route is Route.PROCEED

    @staticmethod
    def proceed(detail: str, **refs) -> "PrereqOutcome":
        return PrereqOutcome(Route.PROCEED, detail, refs=refs)

    @staticmethod
    def reject(reason: RejectReason, detail: str) -> "PrereqOutcome":
        return PrereqOutcome(Route.REJECT, detail, reason=reason)


@dataclass(frozen=True)
class GateCorrection:
    """A typed, non-mutating gate correction returned to Lead (#64).

    Emitted when a dispatch/finish gate rejects a transition. It carries the
    exact binding, the failed predicate, the observed evidence, the allowed Lead
    actions, a retry classification, and a wake condition where applicable — so
    Lead can repair the public record or reroute with precise evidence rather
    than being handed an opaque human blocker. Producing a correction never
    mutates any public or durable state.

    ``retry_class`` separates a *correctable* internal / public-record
    inconsistency Lead can repair from a *transient* condition that self-heals on
    a wake, and an *operator* prerequisite only a human can clear.
    """

    repo: str
    goal: str
    role: str
    intent: str
    failed_predicate: str
    observed: str
    allowed_lead_actions: tuple
    retry_class: str
    task: Optional[int] = None
    pr: Optional[int] = None
    wake_condition: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({
            "repo": self.repo,
            "goal": self.goal,
            "role": self.role,
            "intent": self.intent,
            "task": self.task,
            "pr": self.pr,
            "failed_predicate": self.failed_predicate,
            "observed": self.observed,
            "allowed_lead_actions": list(self.allowed_lead_actions),
            "retry_class": self.retry_class,
            "wake_condition": self.wake_condition,
        }, sort_keys=True)

    def summary(self) -> str:
        """A compact single-line rendering for a Lead prompt / pause blocker."""
        task = f" task #{self.task}" if self.task is not None else ""
        pr = f" pr #{self.pr}" if self.pr is not None else ""
        return (
            f"gate correction [{self.retry_class}] {self.role}/{self.intent}"
            f"{task}{pr} on {self.repo}@{self.goal}: failed {self.failed_predicate} — "
            f"{self.observed}; allowed: {', '.join(self.allowed_lead_actions)}"
        )


def route_for_error(err: GitHubError) -> PrereqOutcome:
    """Map a classified GitHub failure to an explicit typed route (issue #49).

    * ``PERMISSION`` → :attr:`Route.PAUSE` — a genuine credential / policy denial
      that only an operator can clear.
    * ``NOT_FOUND`` → :attr:`Route.REJECT` — a deleted / missing target is a
      permanent invalid-target condition, NOT something a retry will ever fix, so
      it terminates explicitly instead of looping in WAIT forever.
    * rate-limit / timeout / transient / ambiguous → :attr:`Route.WAIT` — a
      recoverable condition that self-heals on a bounded retry/reconcile.
    """
    if err.kind is GitHubErrorKind.PERMISSION:
        return PrereqOutcome(Route.PAUSE, f"github permission error: {err.detail}",
                             error_kind=err.kind)
    if err.kind is GitHubErrorKind.NOT_FOUND:
        return PrereqOutcome(Route.REJECT, f"github target not found: {err.detail}",
                             reason=RejectReason.MISSING, error_kind=err.kind)
    # rate-limit / timeout / transient / ambiguous → retry via wait
    return PrereqOutcome(Route.WAIT, f"github {err.kind.value}: {err.detail}",
                         error_kind=err.kind)


@dataclass(frozen=True)
class PostOutcome:
    route: Route
    detail: str
    comment: Optional[CommentRef] = None
    error_kind: Optional[GitHubErrorKind] = None
    deduplicated: bool = False

    @property
    def ok(self) -> bool:
        return self.route is Route.PROCEED


def _has_label(issue: IssueRef, label: str) -> bool:
    return label in issue.labels


def correlation_marker(correlation_id: str) -> str:
    """Stable idempotency marker embedded (invisibly) in a posted body."""
    safe = re.sub(r"[^A-Za-z0-9:._-]+", "-", correlation_id)
    return f"<!-- crewd:correlation:{safe} -->"


class PublicBus:
    """Typed prerequisite validation + idempotent attributed posting.

    ``goal_label`` is the current goal epoch (e.g. ``goal:v2``); ``umbrella_label``
    identifies the single umbrella GOAL issue for that epoch, and ``task_label``
    the linked work issues. ``expected_repo`` is the ``owner/name`` the workspace
    targets — every fact is checked against the *actual* repo the client reports,
    so a cross-repository reference is rejected rather than trusted.
    """

    def __init__(
        self,
        client: GitHubClient,
        *,
        crew: str,
        expected_repo: str,
        goal_label: str,
        umbrella_label: Optional[str] = None,
        task_label: str = "crewd:task",
    ):
        self.client = client
        self.crew = crew
        self.expected_repo = expected_repo
        self.goal_label = goal_label
        self.umbrella_label = umbrella_label or goal_label
        self.task_label = task_label

    # -- repository identity --
    def _check_repo(self) -> Optional[PrereqOutcome]:
        actual = self.client.repo()
        if actual != self.expected_repo:
            return PrereqOutcome.reject(
                RejectReason.WRONG_REPO,
                f"client repo {actual!r} != expected {self.expected_repo!r}",
            )
        return None

    # -- goal umbrella --
    def verify_goal_prerequisite(self) -> PrereqOutcome:
        """Exactly one open umbrella GOAL issue for the current goal label."""
        try:
            if (bad := self._check_repo()) is not None:
                return bad
            issues = self.client.list_issues(label=self.umbrella_label, state="open")
        except GitHubError as e:
            return route_for_error(e)
        umbrellas = [i for i in issues if i.title.upper().startswith("GOAL")]
        if not umbrellas:
            return PrereqOutcome.reject(
                RejectReason.MISSING,
                f"no open umbrella GOAL issue for {self.goal_label!r}",
            )
        if len(umbrellas) > 1:
            nums = [i.number for i in umbrellas]
            return PrereqOutcome.reject(
                RejectReason.MULTIPLE,
                f"multiple open umbrella GOAL issues for {self.goal_label!r}: {nums}",
            )
        return PrereqOutcome.proceed(
            f"umbrella #{umbrellas[0].number}", umbrella=umbrellas[0].number
        )

    # -- worker dispatch --
    def verify_worker_dispatch(self, task_number: int) -> PrereqOutcome:
        """Open linked ``crewd:task`` with observable ownership + a public Lead
        assignment record, under the current goal umbrella."""
        goal = self.verify_goal_prerequisite()
        if not goal.ok:
            return goal
        try:
            issue = self.client.get_issue(task_number)
        except GitHubError as e:
            return route_for_error(e)
        if issue is None:
            return PrereqOutcome.reject(
                RejectReason.MISSING, f"task #{task_number} not found"
            )
        if issue.state != "open":
            return PrereqOutcome.reject(
                RejectReason.CLOSED, f"task #{task_number} is {issue.state}"
            )
        if not _has_label(issue, self.task_label):
            return PrereqOutcome.reject(
                RejectReason.MISSING,
                f"task #{task_number} lacks {self.task_label!r} label",
            )
        if not _has_label(issue, self.goal_label):
            return PrereqOutcome.reject(
                RejectReason.WRONG_GOAL,
                f"task #{task_number} not labelled current goal {self.goal_label!r}",
            )
        # Observable ownership: an assignee OR a public Lead assignment comment.
        assignment = self._find_lead_assignment(task_number)
        if not issue.assignees and assignment is None:
            return PrereqOutcome.reject(
                RejectReason.NO_ASSIGNMENT,
                f"task #{task_number} has no assignee and no public Lead assignment",
            )
        return PrereqOutcome.proceed(
            f"task #{task_number} open, owned, goal-linked",
            umbrella=goal.refs.get("umbrella"),
            task=task_number,
            assignment=(assignment.url if assignment else None),
        )

    def _find_lead_assignment(
        self, task_number: int, targets: tuple = ("worker",)
    ) -> Optional[CommentRef]:
        try:
            comments = self.client.list_comments(target="issue", number=task_number)
        except GitHubError:
            return None
        for c in comments:
            attr = Attribution.parse(c.body)
            if attr and attr.role == "lead" and attr.target in targets:
                if attr.validate(crew=self.crew) is None:
                    return c
        return None

    # -- verifier-only audit / acceptance / release dispatch (#64) --
    def verify_verifier_audit(self, task_number: int) -> PrereqOutcome:
        """Intent-aware prerequisite for a Lead-assigned verifier-only task.

        A verifier-only audit, acceptance, or post-publication verification is
        routed by Lead directly to the Verifier — there is no Worker
        implementation to review — so it must NOT require a linked PR or a
        fictitious Worker readiness record (the #61 incident, where a generic
        Worker-readiness gate blocked direct Verifier dispatch). It still fully
        enforces pre-dispatch safety: exactly one open umbrella, and an open,
        goal-linked ``crewd:task`` with observable ownership (an assignee or a
        public Lead assignment to worker *or* verifier).
        """
        goal = self.verify_goal_prerequisite()
        if not goal.ok:
            return goal
        try:
            issue = self.client.get_issue(task_number)
        except GitHubError as e:
            return route_for_error(e)
        if issue is None:
            return PrereqOutcome.reject(
                RejectReason.MISSING, f"task #{task_number} not found"
            )
        if issue.state != "open":
            return PrereqOutcome.reject(
                RejectReason.CLOSED, f"task #{task_number} is {issue.state}"
            )
        if not _has_label(issue, self.task_label):
            return PrereqOutcome.reject(
                RejectReason.MISSING,
                f"task #{task_number} lacks {self.task_label!r} label",
            )
        if not _has_label(issue, self.goal_label):
            return PrereqOutcome.reject(
                RejectReason.WRONG_GOAL,
                f"task #{task_number} not labelled current goal {self.goal_label!r}",
            )
        assignment = self._find_lead_assignment(task_number, targets=("worker", "verifier"))
        if not issue.assignees and assignment is None:
            return PrereqOutcome.reject(
                RejectReason.NO_ASSIGNMENT,
                f"task #{task_number} has no assignee and no public Lead assignment",
            )
        return PrereqOutcome.proceed(
            f"task #{task_number} open, owned, goal-linked (verifier-only intent)",
            umbrella=goal.refs.get("umbrella"),
            task=task_number,
            assignment=(assignment.url if assignment else None),
        )

    # -- verifier dispatch --
    def verify_verifier_dispatch(
        self, task_number: int, *, pr_number: Optional[int] = None
    ) -> PrereqOutcome:
        """Linked PR (or acceptance issue) plus an attributed Worker readiness
        record before Verifier routing.

        ``pr_number`` pins the exact PR the Verifier must review — the durable
        binding established when the Worker dispatch resolved (or the host
        recovered) it (#47/#65). When pinned, only that exact linked-and-open PR
        is accepted so an unrelated PR that merely mentions the task can never
        divert the review; a pinned PR that is not an open linked PR fails closed
        (``MISSING``). When *not* pinned and several distinct PRs link the task,
        the selection is ambiguous and fails closed (``MULTIPLE``) rather than
        arbitrarily reviewing the first one.
        """
        worker = self.verify_worker_dispatch(task_number)
        if not worker.ok:
            return worker
        try:
            pulls = self.client.list_pulls(state="open")
        except GitHubError as e:
            return route_for_error(e)
        linked = [p for p in pulls if task_number in p.linked_issues]
        if not linked:
            return PrereqOutcome.reject(
                RejectReason.MISSING,
                f"no open PR linked to task #{task_number}",
            )
        if pr_number is not None:
            exact = [p for p in linked if p.number == pr_number]
            if not exact:
                return PrereqOutcome.reject(
                    RejectReason.MISSING,
                    f"bound PR #{pr_number} is not an open PR linked to task "
                    f"#{task_number} (found: {[p.number for p in linked]})",
                )
            chosen = exact[0]
        elif len({p.number for p in linked} ) > 1:
            nums = sorted({p.number for p in linked})
            return PrereqOutcome.reject(
                RejectReason.MULTIPLE,
                f"multiple open PRs linked to task #{task_number}: {nums} — "
                f"bind the exact PR to review rather than guessing",
            )
        else:
            chosen = linked[0]
        readiness = self._find_worker_readiness(task_number)
        if readiness is None:
            return PrereqOutcome.reject(
                RejectReason.NOT_READY,
                f"no attributed Worker readiness record on task #{task_number}",
            )
        return PrereqOutcome.proceed(
            f"PR #{chosen.number} linked; readiness recorded",
            task=task_number,
            pr=chosen.number,
            readiness=readiness.url,
        )

    def _find_worker_readiness(self, task_number: int) -> Optional[CommentRef]:
        try:
            comments = self.client.list_comments(target="issue", number=task_number)
        except GitHubError:
            return None
        for c in reversed(comments):
            attr = Attribution.parse(c.body)
            if attr and attr.role == "worker" and attr.target == "verifier":
                if attr.validate(crew=self.crew) is None:
                    return c
        return None

    # -- finish --
    def verify_finish(self, acceptance_issue: int) -> PrereqOutcome:
        """A closed final-acceptance issue plus a public goal summary."""
        try:
            if (bad := self._check_repo()) is not None:
                return bad
            issue = self.client.get_issue(acceptance_issue)
        except GitHubError as e:
            return route_for_error(e)
        if issue is None:
            return PrereqOutcome.reject(
                RejectReason.MISSING, f"acceptance issue #{acceptance_issue} not found"
            )
        if issue.state != "closed":
            return PrereqOutcome.reject(
                RejectReason.NOT_READY,
                f"acceptance issue #{acceptance_issue} is {issue.state}, not closed",
            )
        summary = self._find_goal_summary(acceptance_issue)
        if summary is None:
            return PrereqOutcome.reject(
                RejectReason.MISSING,
                f"no attributed public goal summary on issue #{acceptance_issue}",
            )
        return PrereqOutcome.proceed(
            f"acceptance #{acceptance_issue} closed with public summary",
            acceptance=acceptance_issue,
            summary=summary.url,
        )

    def _find_goal_summary(self, number: int) -> Optional[CommentRef]:
        try:
            comments = self.client.list_comments(target="issue", number=number)
        except GitHubError:
            return None
        for c in reversed(comments):
            attr = Attribution.parse(c.body)
            if attr and attr.role == "lead" and attr.target == "all":
                if attr.validate(crew=self.crew) is None:
                    return c
        return None

    # -- terminal-publication authorization (issue #49) --
    def authorize_terminal(
        self, *, task_number: int, pr_number: Optional[int] = None
    ) -> PrereqOutcome:
        """Authorize a *terminal* attributed write to a bound task issue.

        A terminal record (a Verifier/Lead handoff published at the end of an
        attempt) must target the exact routed task under the current
        repository/goal namespace. Unlike a dispatch prerequisite this tolerates a
        *closed* task — but only when the closure is provably caused by the
        verified linked merge, so an unrelated / stale closure is rejected rather
        than silently accepted:

        * wrong repository, missing task label, or wrong goal label →
          :attr:`Route.REJECT` (an invalid target that a retry can never fix).
        * task not found (deleted) → ``REJECT`` (invalid/deleted target).
        * task **open** → ``PROCEED`` (with the linked PR, if any, captured).
        * task **closed** with a merged PR that links it (matching ``pr_number``
          when one was persisted) → ``PROCEED`` (closed-but-proven; comment
          permitted without reopening).
        * task **closed** with no such merged linked PR → ``REJECT``
          (``UNVERIFIED``: unrelated/stale closure — do not post).

        A transient GitHub failure while reading the record surfaces as
        :attr:`Route.WAIT`/``PAUSE`` via :func:`route_for_error`, so a lookup
        outage self-heals instead of terminating.
        """
        if (bad := self._check_repo()) is not None:
            return bad
        try:
            issue = self.client.get_issue(task_number)
        except GitHubError as e:
            return route_for_error(e)
        if issue is None:
            return PrereqOutcome.reject(
                RejectReason.MISSING,
                f"terminal target task #{task_number} not found (deleted/missing)",
            )
        if not _has_label(issue, self.task_label):
            return PrereqOutcome.reject(
                RejectReason.WRONG_GOAL,
                f"terminal target #{task_number} lacks {self.task_label!r} label",
            )
        if not _has_label(issue, self.goal_label):
            return PrereqOutcome.reject(
                RejectReason.WRONG_GOAL,
                f"terminal target #{task_number} not labelled current goal "
                f"{self.goal_label!r}",
            )
        try:
            merged_pr = self._linked_merged_pr(task_number, pr_number)
        except GitHubError as e:
            return route_for_error(e)
        if issue.state == "open":
            return PrereqOutcome.proceed(
                f"terminal target #{task_number} open",
                task=task_number,
                pr=(merged_pr.number if merged_pr else None),
            )
        # Closed: require the verified linked merge as closure provenance.
        if merged_pr is None:
            want = f" (expected PR #{pr_number})" if pr_number else ""
            return PrereqOutcome.reject(
                RejectReason.UNVERIFIED,
                f"terminal target #{task_number} is closed with no merged linked "
                f"PR{want} — unrelated/stale closure, not a verified linked merge",
            )
        return PrereqOutcome.proceed(
            f"terminal target #{task_number} closed by merged PR #{merged_pr.number}",
            task=task_number,
            pr=merged_pr.number,
        )

    def _linked_merged_pr(
        self, task_number: int, pr_number: Optional[int]
    ) -> Optional[PullRef]:
        """The merged PR that links ``task_number`` (matching ``pr_number`` when a
        specific PR was persisted at Verifier routing), or ``None``."""
        pulls = self.client.list_pulls(state="all")
        for p in pulls:
            if p.state != "merged":
                continue
            if task_number not in p.linked_issues:
                continue
            if pr_number is not None and p.number != pr_number:
                continue
            return p
        return None

    def linked_pr(self, task_number: int) -> Optional[int]:
        """The open-or-merged PR number linking ``task_number`` at routing time.

        Captured into the durable lifecycle record so a later closed-target
        terminal write can prove the closure came from that exact merge."""
        try:
            pulls = self.client.list_pulls(state="all")
        except GitHubError:
            return None
        linked = [p for p in pulls if task_number in p.linked_issues]
        if not linked:
            return None
        # Prefer a merged link (closure provenance) then an open one.
        for p in linked:
            if p.state == "merged":
                return p.number
        return linked[0].number

    # -- typed reference resolution (public record → active identifiers) --
    #
    # The dispatcher journal is role-based and does not name a task/PR; the
    # canonical identifiers therefore come from the public record itself rather
    # than a hard-coded parameter. Ambiguity or absence resolves to a REJECT so a
    # dispatch is never routed against a stale/unknown reference.
    def resolve_active_task(self) -> PrereqOutcome:
        """Derive the single active ``crewd:task`` from the public record.

        The active task is the open ``crewd:task`` issue under the current goal
        umbrella that carries a public Lead worker-assignment record (attributed
        ``lead -> worker``) — i.e. the task the operator can see is active and
        why. Zero such tasks rejects (``NO_ASSIGNMENT``); more than one rejects
        (``MULTIPLE``) so an ambiguous record fails safe instead of guessing.
        """
        goal = self.verify_goal_prerequisite()
        if not goal.ok:
            return goal
        try:
            issues = self.client.list_issues(label=self.task_label, state="open")
        except GitHubError as e:
            return route_for_error(e)
        active = [
            i for i in issues
            if _has_label(i, self.goal_label)
            and self._find_lead_assignment(i.number) is not None
        ]
        if not active:
            return PrereqOutcome.reject(
                RejectReason.NO_ASSIGNMENT,
                f"no open {self.task_label!r} issue under {self.goal_label!r} "
                "with a public Lead assignment",
            )
        if len(active) > 1:
            nums = sorted(i.number for i in active)
            return PrereqOutcome.reject(
                RejectReason.MULTIPLE,
                f"multiple active {self.task_label!r} issues under "
                f"{self.goal_label!r}: {nums}",
            )
        n = active[0].number
        return PrereqOutcome.proceed(
            f"active task #{n}", task=n, umbrella=goal.refs.get("umbrella")
        )

    def resolve_acceptance_issue(self) -> PrereqOutcome:
        """Derive the final-acceptance issue: the umbrella GOAL issue for the
        current goal label, resolved across *all* states so ``verify_finish`` can
        require it closed. Absence/ambiguity rejects."""
        try:
            issues = self.client.list_issues(label=self.umbrella_label, state="all")
        except GitHubError as e:
            return route_for_error(e)
        umbrellas = [i for i in issues if i.title.upper().startswith("GOAL")]
        if not umbrellas:
            return PrereqOutcome.reject(
                RejectReason.MISSING,
                f"no umbrella GOAL issue for {self.goal_label!r}",
            )
        if len(umbrellas) > 1:
            nums = sorted(i.number for i in umbrellas)
            return PrereqOutcome.reject(
                RejectReason.MULTIPLE,
                f"multiple umbrella GOAL issues for {self.goal_label!r}: {nums}",
            )
        return PrereqOutcome.proceed(
            f"acceptance #{umbrellas[0].number}", acceptance=umbrellas[0].number
        )

    # -- idempotent attributed post --
    def post(
        self,
        *,
        role: str,
        target_role: str,
        body: str,
        target: str,
        number: int,
        correlation_id: str,
    ) -> PostOutcome:
        """Post an attributed comment idempotently.

        Reserve intent → search for the correlation marker (dedupe a prior /
        crashed / ambiguous write) → write with first-line attribution + marker →
        return the verified :class:`CommentRef`. A permission failure pauses; a
        retryable failure waits; an ambiguous write reconciles by re-scanning for
        the marker so a retry never double-posts.
        """
        attr = Attribution(role=role, target=target_role, crew=self.crew)
        err = attr.validate(crew=self.crew)
        if err:
            return PostOutcome(Route.REJECT, f"invalid attribution: {err}")
        marker = correlation_marker(correlation_id)

        # 1) Idempotency pre-check: has this correlation already landed?
        try:
            existing = self.client.find_comment(target=target, number=number, marker=marker)
        except GitHubError as e:
            return self._post_error(e)
        if existing is not None:
            return PostOutcome(Route.PROCEED, "already posted (marker found)",
                               comment=existing, deduplicated=True)

        # 2) Write with attribution first line + invisible marker.
        full = f"{attr.render()}\n\n{body.strip()}\n\n{marker}"
        try:
            created = self.client.create_comment(target=target, number=number, body=full)
        except GitHubError as e:
            if e.kind is GitHubErrorKind.AMBIGUOUS:
                # The write may have landed; reconcile by re-scanning for the marker.
                return self._reconcile_post(target, number, marker, e)
            return self._post_error(e)

        # 3) Verify the persisted artifact (never trust an unverified URL).
        return self._verify_post(target, number, marker, created)

    def _verify_post(self, target, number, marker, created) -> PostOutcome:
        try:
            confirmed = self.client.find_comment(target=target, number=number, marker=marker)
        except GitHubError as e:
            # The write likely succeeded but verification failed transiently;
            # treat as ambiguous → wait and reconcile on retry (marker dedupes).
            return PostOutcome(Route.WAIT, f"post unverified ({e.kind.value}); will reconcile",
                               comment=created, error_kind=e.kind)
        if confirmed is None:
            return PostOutcome(Route.WAIT, "post not confirmed on re-read; will retry")
        return PostOutcome(Route.PROCEED, "posted and verified", comment=confirmed)

    def _reconcile_post(self, target, number, marker, err: GitHubError) -> PostOutcome:
        try:
            found = self.client.find_comment(target=target, number=number, marker=marker)
        except GitHubError as e2:
            return PostOutcome(Route.WAIT, f"ambiguous post, reconcile failed ({e2.kind.value})",
                               error_kind=e2.kind)
        if found is not None:
            return PostOutcome(Route.PROCEED, "ambiguous write reconciled (marker found)",
                               comment=found, deduplicated=True)
        return PostOutcome(Route.WAIT, f"ambiguous write did not land ({err.detail}); retry",
                           error_kind=err.kind)

    @staticmethod
    def _post_error(err: GitHubError) -> PostOutcome:
        if err.kind is GitHubErrorKind.PERMISSION:
            return PostOutcome(Route.PAUSE, f"github permission error: {err.detail}",
                               error_kind=err.kind)
        if err.kind is GitHubErrorKind.NOT_FOUND:
            # A deleted / missing target can never be posted to: terminate as an
            # invalid target rather than retrying forever (issue #49).
            return PostOutcome(Route.REJECT, f"github target not found: {err.detail}",
                               error_kind=err.kind)
        return PostOutcome(Route.WAIT, f"github {err.kind.value}: {err.detail}",
                           error_kind=err.kind)


class PublicBusGate:
    """Adapts :class:`PublicBus` prerequisite checks to the orchestrator seam.

    The orchestrator consults :meth:`evaluate` before it reserves a Worker or
    Verifier attempt, and :meth:`evaluate_finish` before it applies a Lead
    ``finish`` decision. A non-``PROCEED`` outcome halts the transition *without*
    reserving the attempt, consuming any pending handoff, or terminalising the
    run, so authority never advances on an invalid or unverifiable public record.

    ``task_number`` may be supplied to pin a specific task (used by focused unit
    tests). In production the orchestrator passes the exact routed task —
    persisted with the dispatch — into :meth:`evaluate`, so the invariant is
    validated against the *bound* task rather than a later global re-census of
    the public record (issue #47). When neither an explicit task nor a pin is
    available the active task is derived from the public record via
    :meth:`PublicBus.resolve_active_task` as a legacy fallback.
    """

    def __init__(self, bus: PublicBus, *, task_number: Optional[int] = None):
        self.bus = bus
        self._fixed_task = task_number

    def _resolve_task(self, task_number: Optional[int] = None) -> PrereqOutcome:
        # Priority: the exact routed task carried by the dispatch/decision >
        # a unit-test pin > the legacy global census. Production always supplies
        # the routed task, so the ambiguous MULTIPLE census never gates a run.
        pinned = task_number if task_number is not None else self._fixed_task
        if pinned is not None:
            return PrereqOutcome.proceed(f"task #{pinned}", task=pinned)
        return self.bus.resolve_active_task()

    # Verifier intents that carry no Worker implementation to review, so the
    # linked-PR + Worker-readiness safeguards must NOT gate them (#64/#61).
    _AUDIT_INTENTS = frozenset({"verifier_audit", "acceptance", "release", "advisory"})

    @staticmethod
    def _intent_value(intent) -> str:
        """Normalise an intent (enum/str/None) to its lowercase string value.

        A missing/unknown intent conservatively reads as ``implementation`` — the
        strongest safeguard set — so a legacy/untyped dispatch never silently
        drops the Worker-readiness gate.
        """
        if intent is None:
            return "implementation"
        val = getattr(intent, "value", intent)
        val = str(val).strip().lower()
        known = {"implementation"} | PublicBusGate._AUDIT_INTENTS
        return val if val in known else "implementation"

    def evaluate(
        self, role: str, dsp: object, *, task_number: Optional[int] = None,
        intent: object = None,
    ) -> Optional[PrereqOutcome]:
        if role not in ("worker", "verifier"):
            return None
        # Prefer an explicit routed task; else fall back to the dispatch's
        # persisted binding (defense-in-depth for a dispatch resurrected from the
        # journal after a restart).
        bound = task_number
        if bound is None:
            bound = getattr(dsp, "task_number", None)
        if intent is None:
            intent = getattr(dsp, "intent", None)
        task = self._resolve_task(bound)
        if not task.ok:
            return task
        number = task.refs["task"]
        if role == "worker":
            # Worker is always an implementation actor; intent does not relax its
            # ownership prerequisite.
            return self.bus.verify_worker_dispatch(number)
        # Verifier: an implementation review keeps the linked-PR + Worker-readiness
        # safeguards; a Lead-assigned verifier-only intent uses the audit predicate.
        if self._intent_value(intent) in self._AUDIT_INTENTS:
            return self.bus.verify_verifier_audit(number)
        # Honour the exact PR durably bound to this dispatch (resolved when the
        # Worker dispatch landed, or host-recovered from the #64 lost-handoff
        # chain) so the Verifier reviews that exact PR rather than an arbitrary
        # linked one (#47/#65).
        pinned_pr = getattr(dsp, "pr_number", None)
        return self.bus.verify_verifier_dispatch(number, pr_number=pinned_pr)

    def build_correction(
        self, role: str, intent: object, outcome: "PrereqOutcome",
        *, task_number: Optional[int] = None,
    ) -> GateCorrection:
        """Build a typed :class:`GateCorrection` from a rejecting outcome (#64).

        Pure: reads the bus's known repo/goal identity plus the outcome's reason,
        detail, and refs. Never mutates any state.
        """
        reason = outcome.reason
        if outcome.route is Route.PAUSE:
            retry_class = "operator"
            actions = ("escalate",)
        elif outcome.route is Route.WAIT:
            retry_class = "transient"
            actions = ("wait", "reroute", "escalate")
        elif reason is not None and reason.lead_correctable:
            retry_class = "correctable"
            actions = _CORRECTION_ACTIONS.get(reason, ("reroute", "wait", "escalate"))
        else:
            retry_class = "operator"
            actions = ("escalate",)
        refs = outcome.refs or {}
        return GateCorrection(
            repo=self.bus.expected_repo,
            goal=self.bus.goal_label,
            role=role,
            intent=self._intent_value(intent),
            failed_predicate=(reason.value if reason is not None else outcome.route.value),
            observed=outcome.detail,
            allowed_lead_actions=tuple(actions),
            retry_class=retry_class,
            task=(task_number if task_number is not None else refs.get("task")),
            pr=refs.get("pr"),
            wake_condition=(outcome.detail if outcome.route is Route.WAIT else None),
        )

    def evaluate_finish(self) -> PrereqOutcome:
        """Verify the finish prerequisite (closed final-acceptance issue + public
        goal summary) before a Lead ``finish`` decision terminalises the run."""
        acceptance = self.bus.resolve_acceptance_issue()
        if not acceptance.ok:
            return acceptance
        return self.bus.verify_finish(acceptance.refs["acceptance"])


# ── CLI client (production) ──────────────────────────────────────────────
def classify_gh_stderr(stderr: str, returncode: int) -> GitHubErrorKind:
    """Best-effort classification of a ``gh`` failure into a routing kind."""
    s = (stderr or "").lower()
    if "rate limit" in s or "rate-limit" in s or "secondary rate" in s:
        return GitHubErrorKind.RATE_LIMIT
    if "timed out" in s or "timeout" in s or "deadline" in s:
        return GitHubErrorKind.TIMEOUT
    if "not found" in s or "404" in s:
        return GitHubErrorKind.NOT_FOUND
    # A *closed*-target comment rejection is an ordering/closure race, not a
    # credential failure: GitHub normally permits comments on a closed issue/PR,
    # so a terminal write racing a merge/auto-close self-heals (WAIT → reconcile)
    # rather than pausing for a human (issue #49). This is deliberately narrow —
    # only the closed-target wording. Persistent operator-actionable states
    # (locked / archived / read-only) are NOT matched here; they fall through to
    # the permission check below so they surface as a real operator blocker.
    if "closed issue" in s or "closed pull request" in s or "closed pr" in s:
        return GitHubErrorKind.TRANSIENT
    if (
        "permission" in s or "forbidden" in s or "403" in s
        or "401" in s or "authentication" in s or "must have admin" in s
        or "resource not accessible" in s
    ):
        return GitHubErrorKind.PERMISSION
    return GitHubErrorKind.TRANSIENT


class CliGitHubClient:
    """Production :class:`GitHubClient` wrapping the ``gh`` CLI.

    Thin and side-effecting: the routing/idempotency logic lives in
    :class:`PublicBus`, so this layer only translates a ``gh`` invocation into a
    typed record or a classified :class:`GitHubError`.
    """

    def __init__(self, repo: str, *, timeout: float = 30.0):
        self._repo = repo
        self._timeout = timeout

    def _run(self, args: list[str]) -> str:
        try:
            r = subprocess.run(
                ["gh", *args, "--repo", self._repo],
                capture_output=True, text=True, timeout=self._timeout,
            )
        except FileNotFoundError:
            raise GitHubError(GitHubErrorKind.PERMISSION, "gh CLI not found on PATH")
        except subprocess.TimeoutExpired:
            raise GitHubError(GitHubErrorKind.TIMEOUT, "gh timed out")
        if r.returncode != 0:
            raise GitHubError(classify_gh_stderr(r.stderr, r.returncode), r.stderr.strip())
        return r.stdout

    def repo(self) -> str:
        return self._repo

    def list_issues(self, *, label: Optional[str] = None, state: str = "open") -> list[IssueRef]:
        args = ["issue", "list", "--state", state, "--json",
                "number,title,state,labels,assignees,url", "--limit", "200"]
        if label:
            args += ["--label", label]
        out = self._run(args)
        return [self._issue_from_json(d) for d in json.loads(out or "[]")]

    def get_issue(self, number: int) -> Optional[IssueRef]:
        try:
            out = self._run(["issue", "view", str(number), "--json",
                             "number,title,state,labels,assignees,body,url"])
        except GitHubError as e:
            if e.kind is GitHubErrorKind.NOT_FOUND:
                return None
            raise
        return self._issue_from_json(json.loads(out))

    def get_pull(self, number: int) -> Optional[PullRef]:
        try:
            out = self._run(["pr", "view", str(number), "--json",
                             "number,title,state,body,url,headRefName,"
                             "mergeable,statusCheckRollup"])
        except GitHubError as e:
            if e.kind is GitHubErrorKind.NOT_FOUND:
                return None
            raise
        return self._pull_from_json(json.loads(out))

    def list_pulls(self, state: str = "open") -> list[PullRef]:
        out = self._run(["pr", "list", "--state", state, "--json",
                         "number,title,state,body,url,headRefName,"
                         "mergeable,statusCheckRollup", "--limit", "200"])
        return [self._pull_from_json(d) for d in json.loads(out or "[]")]

    def list_comments(self, *, target: str, number: int) -> list[CommentRef]:
        kind = "issue" if target == "issue" else "pr"
        out = self._run([kind, "view", str(number), "--json", "comments"])
        data = json.loads(out or "{}")
        result = []
        for c in data.get("comments", []):
            result.append(CommentRef(
                id=int(c.get("id", 0)) if str(c.get("id", "0")).isdigit() else 0,
                url=c.get("url", ""),
                body=c.get("body", ""),
            ))
        return result

    def find_comment(self, *, target: str, number: int, marker: str) -> Optional[CommentRef]:
        for c in self.list_comments(target=target, number=number):
            if marker in c.body:
                return c
        return None

    def create_comment(self, *, target: str, number: int, body: str) -> CommentRef:
        kind = "issue" if target == "issue" else "pr"
        out = self._run([kind, "comment", str(number), "--body", body])
        return CommentRef(id=0, url=out.strip(), body=body)

    @staticmethod
    def _issue_from_json(d: dict) -> IssueRef:
        return IssueRef(
            number=d["number"],
            title=d.get("title", ""),
            state=str(d.get("state", "open")).lower(),
            labels=tuple(l.get("name", "") for l in d.get("labels", [])),
            assignees=tuple(a.get("login", "") for a in d.get("assignees", [])),
            body=d.get("body", ""),
            url=d.get("url", ""),
        )

    @staticmethod
    def _pull_from_json(d: dict) -> PullRef:
        body = d.get("body", "") or ""
        linked = tuple(int(n) for n in re.findall(r"#(\d+)", body))
        return PullRef(
            number=d["number"],
            title=d.get("title", ""),
            state=str(d.get("state", "open")).lower(),
            body=body,
            url=d.get("url", ""),
            linked_issues=linked,
            head_ref=d.get("headRefName", "") or "",
            mergeable=str(d.get("mergeable", "") or ""),
            checks=summarize_check_rollup(d.get("statusCheckRollup")),
        )
