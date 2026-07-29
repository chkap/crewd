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


# ── prerequisite validation results ─────────────────────────────────────
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


def route_for_error(err: GitHubError) -> PrereqOutcome:
    """Map a classified GitHub failure to an explicit recoverable route."""
    if err.kind is GitHubErrorKind.PERMISSION:
        return PrereqOutcome(Route.PAUSE, f"github permission error: {err.detail}",
                             error_kind=err.kind)
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

    def _find_lead_assignment(self, task_number: int) -> Optional[CommentRef]:
        try:
            comments = self.client.list_comments(target="issue", number=task_number)
        except GitHubError:
            return None
        for c in comments:
            attr = Attribution.parse(c.body)
            if attr and attr.role == "lead" and attr.target == "worker":
                if attr.validate(crew=self.crew) is None:
                    return c
        return None

    # -- verifier dispatch --
    def verify_verifier_dispatch(self, task_number: int) -> PrereqOutcome:
        """Linked PR (or acceptance issue) plus an attributed Worker readiness
        record before Verifier routing."""
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
        readiness = self._find_worker_readiness(task_number)
        if readiness is None:
            return PrereqOutcome.reject(
                RejectReason.NOT_READY,
                f"no attributed Worker readiness record on task #{task_number}",
            )
        return PrereqOutcome.proceed(
            f"PR #{linked[0].number} linked; readiness recorded",
            task=task_number,
            pr=linked[0].number,
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

    def evaluate(
        self, role: str, dsp: object, *, task_number: Optional[int] = None
    ) -> Optional[PrereqOutcome]:
        if role not in ("worker", "verifier"):
            return None
        # Prefer an explicit routed task; else fall back to the dispatch's
        # persisted binding (defense-in-depth for a dispatch resurrected from the
        # journal after a restart).
        bound = task_number
        if bound is None:
            bound = getattr(dsp, "task_number", None)
        task = self._resolve_task(bound)
        if not task.ok:
            return task
        number = task.refs["task"]
        if role == "worker":
            return self.bus.verify_worker_dispatch(number)
        return self.bus.verify_verifier_dispatch(number)

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
                             "number,title,state,body,url"])
        except GitHubError as e:
            if e.kind is GitHubErrorKind.NOT_FOUND:
                return None
            raise
        return self._pull_from_json(json.loads(out))

    def list_pulls(self, state: str = "open") -> list[PullRef]:
        out = self._run(["pr", "list", "--state", state, "--json",
                         "number,title,state,body,url", "--limit", "200"])
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
        )
