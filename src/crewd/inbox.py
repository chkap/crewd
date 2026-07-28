"""Host-owned operator inbox delivery.

The orchestrator — not model best effort — is responsible for delivering operator
inbox messages to a role attempt. This closes the ``crewd#29`` defect where the
dispatcher relied on each model to *read and clear its own inbox file*, which a
model could silently skip (losing an operator ``OVERRIDE``) or destroy without an
audit trail.

Contract (see GOAL required outcome #4):

* **read before prompting** — :meth:`InboxService.deliver` is called by the host
  while it constructs the next role/Lead prompt, so the payload is attached by the
  runtime rather than fetched by the model.
* **priority + ordering** — messages are ordered ``OVERRIDE`` → ``ADVICE`` →
  ``INFO`` and, within a priority, by arrival. An ``OVERRIDE`` therefore always
  reaches the top of the delivered block for the next applicable attempt.
* **bounded, delimited, redacted** — the rendered payload is wrapped in explicit
  ``OPERATOR INBOX`` markers, capped in size, and passed through
  :func:`redact_secrets` so tokens/credentials are not echoed into a prompt.
* **archive only after durable attachment** — delivery *stages* the live
  ``<role>.md`` under an attempt-scoped name; the host calls
  :meth:`acknowledge` only after the attempt terminalizes, moving it to a
  ``<role>.processed.*`` audit file. A crash between attach and ack therefore
  retains the message (it is re-folded into the next attempt of the *same* role),
  and a message is never consumed by the wrong role.
* **observable** — :meth:`counts` exposes pending/delivering/processed totals for
  diagnostics without leaking content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Priority ordering for delivery. Lower sort key = delivered first.
PRIORITY_ORDER = {"OVERRIDE": 0, "ADVICE": 1, "INFO": 2}
DEFAULT_PRIORITY = "INFO"

# Bounding: keep a delivered payload from ballooning a prompt while still carrying
# the operative content of each message.
MAX_PAYLOAD_CHARS = 6000
MAX_MESSAGE_CHARS = 1500

_BEGIN = "===== OPERATOR INBOX (host-delivered) ====="
_END = "===== END OPERATOR INBOX ====="

# Message header forms produced by the CLI:
#   cmd_inbox_append : "[OVERRIDE] <iso-ts> <text>"        (one line per message)
#   cmd_talk/new-goal: "## [operator @ <iso-ts>]\n<body>"  (block, "---" separated)
_LINE_RE = re.compile(r"^\[(OVERRIDE|ADVICE|INFO)\]\s+(\S+)?\s*(.*)$", re.IGNORECASE)
_BLOCK_HDR_RE = re.compile(r"^##\s*\[(?P<label>[^\]@]+?)(?:\s*@\s*(?P<ts>[^\]]+))?\]\s*$")


@dataclass(frozen=True)
class InboxMessage:
    priority: str
    sender: str
    timestamp: str
    body: str
    seq: int  # arrival order within the source file (ties broken by this)

    @property
    def sort_key(self) -> tuple[int, int]:
        return (PRIORITY_ORDER.get(self.priority, PRIORITY_ORDER[DEFAULT_PRIORITY]), self.seq)


# ── secret redaction ──────────────────────────────────────────────────
_REDACTIONS = [
    re.compile(r"gh[posu]_[A-Za-z0-9]{20,}"),                 # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),              # fine-grained PAT
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),              # Slack tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS access key id
    re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|authorization|bearer)\b\s*[:=]?\s*\S+"),
]
_PLACEHOLDER = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Best-effort redaction of common credential shapes in operator text."""
    out = text
    for rx in _REDACTIONS:
        out = rx.sub(_PLACEHOLDER, out)
    return out


# ── parsing ────────────────────────────────────────────────────────────
def parse_messages(raw: str) -> list[InboxMessage]:
    """Parse an inbox file body into ordered :class:`InboxMessage` records.

    Tolerant of both the single-line ``[PRIORITY] ts text`` form and the
    ``## [sender @ ts]`` block form, interleaved, with ``---`` separators.
    Malformed content is preserved as best-effort ``INFO`` text rather than
    dropped, so an operator message is never silently lost.
    """
    messages: list[InboxMessage] = []
    seq = 0
    lines = raw.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped == "---":
            i += 1
            continue

        line_m = _LINE_RE.match(stripped)
        block_m = _BLOCK_HDR_RE.match(stripped)

        if block_m:
            label = block_m.group("label").strip()
            ts = (block_m.group("ts") or "").strip()
            priority = label.upper() if label.upper() in PRIORITY_ORDER else DEFAULT_PRIORITY
            sender = label if priority == DEFAULT_PRIORITY else label.lower()
            body_lines: list[str] = []
            i += 1
            while i < n and lines[i].strip() != "---" and not _BLOCK_HDR_RE.match(lines[i].strip()):
                body_lines.append(lines[i])
                i += 1
            body = "\n".join(body_lines).strip()
            messages.append(InboxMessage(priority, sender or "operator", ts, body, seq))
            seq += 1
            continue

        if line_m:
            priority = line_m.group(1).upper()
            ts = line_m.group(2) or ""
            body = (line_m.group(3) or "").strip()
            messages.append(InboxMessage(priority, "operator", ts, body, seq))
            seq += 1
            i += 1
            continue

        # Unrecognized non-empty line: keep it as an INFO message.
        messages.append(InboxMessage(DEFAULT_PRIORITY, "operator", "", stripped, seq))
        seq += 1
        i += 1
    return messages


def render_payload(messages: list[InboxMessage]) -> Optional[str]:
    """Render ordered messages into a bounded, delimited, redacted prompt block.

    Returns ``None`` when there is nothing operative to deliver.
    """
    ordered = sorted(messages, key=lambda m: m.sort_key)
    rendered: list[str] = []
    for m in ordered:
        body = redact_secrets(m.body).strip()
        if not body:
            continue
        if len(body) > MAX_MESSAGE_CHARS:
            body = body[:MAX_MESSAGE_CHARS] + " …[truncated]"
        head = f"[{m.priority}]"
        if m.sender and m.sender != "operator":
            head += f" {m.sender}"
        if m.timestamp:
            head += f" @ {m.timestamp}"
        rendered.append(f"{head}: {body}")
    if not rendered:
        return None

    counted = len(rendered)
    header = (
        f"{_BEGIN}\n"
        f"{counted} operator message(s), highest priority first. These are "
        f"authoritative operator input for THIS attempt; an [OVERRIDE] takes "
        f"precedence over GOAL.md and prior session memory. Act on them before "
        f"other work.\n"
    )
    body_text = "\n".join(rendered)
    payload = f"{header}{body_text}\n{_END}"
    if len(payload) > MAX_PAYLOAD_CHARS:
        keep = MAX_PAYLOAD_CHARS - len(header) - len(_END) - 32
        payload = f"{header}{body_text[:max(keep, 0)]}\n…[inbox truncated]\n{_END}"
    return payload


# ── service ─────────────────────────────────────────────────────────────
class InboxService:
    """Host-owned lifecycle for a workspace's per-role operator inbox files."""

    def __init__(self, inbox_dir: Path):
        self._dir = inbox_dir

    @classmethod
    def for_workspace(cls, ws) -> "InboxService":
        return cls(ws.state_dir / "inbox")

    # -- paths --
    def _live(self, role: str) -> Path:
        return self._dir / f"{role}.md"

    def _staging(self, role: str, attempt_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", attempt_id) or "attempt"
        return self._dir / f"{role}.delivering.{safe}.md"

    def _orphan_stagings(self, role: str, exclude: Path) -> list[Path]:
        return sorted(
            p for p in self._dir.glob(f"{role}.delivering.*.md") if p != exclude
        )

    # -- observability --
    def counts(self, role: str) -> dict:
        """Pending/delivering/processed counts for one role (no content)."""
        pending = 0
        live = self._live(role)
        if live.exists():
            pending = len(parse_messages(live.read_text(errors="replace")))
        delivering = len(list(self._dir.glob(f"{role}.delivering.*.md"))) if self._dir.exists() else 0
        processed = len(list(self._dir.glob(f"{role}.processed.*.md"))) if self._dir.exists() else 0
        return {"pending": pending, "delivering": delivering, "processed": processed}

    def has_pending(self, role: str) -> bool:
        live = self._live(role)
        if not live.exists():
            # A crash may have left a staged (attached-but-unacked) delivery.
            return bool(self._dir.exists() and list(self._dir.glob(f"{role}.delivering.*.md")))
        return bool(live.read_text(errors="replace").strip())

    # -- delivery lifecycle --
    def deliver(self, role: str, attempt_id: str) -> Optional[str]:
        """Stage this role's pending inbox for ``attempt_id`` and render it.

        Atomically moves the live inbox file (plus any orphaned staging from a
        crashed prior attempt of the same role) into an attempt-scoped staging
        file, then returns the rendered payload. Idempotent: a retry of the same
        attempt re-reads its own staging and re-delivers identical content.
        Returns ``None`` when there is nothing to deliver.
        """
        if not self._dir.exists():
            return None
        staging = self._staging(role, attempt_id)

        if not staging.exists():
            parts: list[str] = []
            absorbed: list[Path] = []
            for orphan in self._orphan_stagings(role, exclude=staging):
                parts.append(orphan.read_text(errors="replace"))
                absorbed.append(orphan)
            live = self._live(role)
            if live.exists():
                parts.append(live.read_text(errors="replace"))
            combined = "\n".join(p for p in parts if p.strip())
            if not combined.strip():
                # Nothing operative; tidy up empty/whitespace sources.
                for p in absorbed:
                    p.unlink(missing_ok=True)
                if live.exists() and not live.read_text(errors="replace").strip():
                    live.unlink(missing_ok=True)
                return None
            self._dir.mkdir(parents=True, exist_ok=True)
            staging.write_text(combined)
            for p in absorbed:
                p.unlink(missing_ok=True)
            live.unlink(missing_ok=True)

        return render_payload(parse_messages(staging.read_text(errors="replace")))

    def acknowledge(self, role: str, attempt_id: str) -> Optional[Path]:
        """Archive a delivered inbox after its attempt has terminalized.

        Moves the attempt's staging file to ``<role>.processed.<ts>.md``. Safe to
        call when nothing was delivered (returns ``None``). Only invoke once the
        attempt is durably terminal, so a crash before this point retains the
        message for redelivery to the next attempt of the same role.
        """
        staging = self._staging(role, attempt_id)
        if not staging.exists():
            return None
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        processed = self._dir / f"{role}.processed.{ts}.md"
        staging.replace(processed)
        return processed
