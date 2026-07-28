"""Deterministic in-memory fake for the crewd public-bus GitHub boundary.

Implements :class:`crewd.github_bus.GitHubClient` with programmable state and
per-call fault injection so the typed prerequisite/idempotency logic in
:class:`crewd.github_bus.PublicBus` can be exercised without a network: success,
permission / rate-limit / timeout / ambiguous results, crash-and-retry, and
duplicate-post suppression.
"""
from __future__ import annotations

from typing import Optional

from crewd.github_bus import (
    CommentRef,
    GitHubError,
    GitHubErrorKind,
    IssueRef,
    PullRef,
)


class FakeGitHubClient:
    def __init__(self, repo: str = "acme/widget"):
        self._repo = repo
        self.issues: dict[int, IssueRef] = {}
        self.pulls: dict[int, PullRef] = {}
        # comments keyed by (target, number) -> list[CommentRef]
        self.comments: dict[tuple[str, int], list[CommentRef]] = {}
        self._next_comment_id = 1000
        # Fault injection: map method name -> list of GitHubError to raise (popped
        # per call, so you can script "fail once then succeed" retry sequences).
        self.faults: dict[str, list[GitHubError]] = {}
        # When set, create_comment appends the comment to storage BUT still raises
        # (models an ambiguous write that actually landed).
        self.ambiguous_but_landed = False
        self.create_calls = 0

    # -- programming helpers --
    def add_issue(self, number: int, title: str, *, state: str = "open",
                  labels: tuple[str, ...] = (), assignees: tuple[str, ...] = (),
                  body: str = "") -> IssueRef:
        ref = IssueRef(number, title, state, labels, assignees, body,
                       url=f"https://github.com/{self._repo}/issues/{number}")
        self.issues[number] = ref
        return ref

    def add_pull(self, number: int, title: str, *, state: str = "open",
                 body: str = "", linked_issues: tuple[int, ...] = ()) -> PullRef:
        ref = PullRef(number, title, state, body,
                      url=f"https://github.com/{self._repo}/pull/{number}",
                      linked_issues=linked_issues)
        self.pulls[number] = ref
        return ref

    def add_comment(self, target: str, number: int, body: str) -> CommentRef:
        return self._store_comment(target, number, body)

    def fail_once(self, method: str, kind: GitHubErrorKind, detail: str = "x") -> None:
        self.faults.setdefault(method, []).append(GitHubError(kind, detail))

    # -- internal --
    def _maybe_fault(self, method: str) -> None:
        queue = self.faults.get(method)
        if queue:
            raise queue.pop(0)

    def _store_comment(self, target: str, number: int, body: str) -> CommentRef:
        cid = self._next_comment_id
        self._next_comment_id += 1
        ref = CommentRef(id=cid, url=f"https://github.com/{self._repo}/c/{cid}", body=body)
        self.comments.setdefault((target, number), []).append(ref)
        return ref

    # -- GitHubClient protocol --
    def repo(self) -> str:
        return self._repo

    def list_issues(self, *, label: Optional[str] = None, state: str = "open") -> list[IssueRef]:
        self._maybe_fault("list_issues")
        out = []
        for i in self.issues.values():
            if state and i.state != state:
                continue
            if label and label not in i.labels:
                continue
            out.append(i)
        return out

    def get_issue(self, number: int) -> Optional[IssueRef]:
        self._maybe_fault("get_issue")
        return self.issues.get(number)

    def get_pull(self, number: int) -> Optional[PullRef]:
        self._maybe_fault("get_pull")
        return self.pulls.get(number)

    def list_pulls(self, state: str = "open") -> list[PullRef]:
        self._maybe_fault("list_pulls")
        return [p for p in self.pulls.values() if not state or p.state == state]

    def list_comments(self, *, target: str, number: int) -> list[CommentRef]:
        self._maybe_fault("list_comments")
        return list(self.comments.get((target, number), []))

    def find_comment(self, *, target: str, number: int, marker: str) -> Optional[CommentRef]:
        self._maybe_fault("find_comment")
        for c in self.comments.get((target, number), []):
            if marker in c.body:
                return c
        return None

    def create_comment(self, *, target: str, number: int, body: str) -> CommentRef:
        self.create_calls += 1
        # If an ambiguous fault is queued and ambiguous_but_landed is set, store
        # first (the write landed) then raise so the boundary must reconcile.
        queue = self.faults.get("create_comment")
        if queue:
            err = queue[0]
            if err.kind is GitHubErrorKind.AMBIGUOUS and self.ambiguous_but_landed:
                self._store_comment(target, number, body)
            queue.pop(0)
            raise err
        return self._store_comment(target, number, body)
