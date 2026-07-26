#!/usr/bin/env python3
"""Reproduction script for docs/retrospective-orchestration.md.

Deterministic and self-contained: regenerates the census counts (§1),
the keyword-heuristic idle table (§4), the exception census (§5), and the
Stage-C hand-validation sample (§2/§4) exactly.

The two crew histories are read-only. Point --root at the parent directory
that contains `fin-crew/` and `legal-crew/` (default: /home/azureuser/crews).

Reproducibility contract:
  * Population ordering is CANONICAL: role logs are collected with a fixed
    crew order (fin-crew, legal-crew), a fixed role order (lead, advisory,
    worker, verifier), goals sorted by numeric version, and cycles sorted by
    integer cycle number. This ordering does NOT depend on filesystem glob
    order, so the seeded sample below is regenerable on any machine.
  * Stage-C selection: random.Random(1729).sample(POPULATION, 16) over the
    canonical ordering. The exact 16 paths are printed under "STAGE-C SAMPLE".
"""
from __future__ import annotations
import argparse, os, re, random
from pathlib import Path

ROLES = ("lead", "advisory", "worker", "verifier")
CREWS = ("fin-crew", "legal-crew")

# --- Stage-A keyword rubric (heuristic; lower bounds, not semantic rates) ---
KEYWORDS = {
    "timeout": re.compile(r"\btimed? ?out\b|rc=124|exit code 124", re.I),
    "signal": re.compile(r"\bSIGINT\b|\bSIGTERM\b|\bSIGKILL\b", re.I),
    "cancel": re.compile(r"\bcancel", re.I),
    "paused": re.compile(r"\bPAUSED\b|human blocker|state/PAUSED", re.I),
    "stopped": re.compile(r"\bSTOPPED\b", re.I),
    "stale": re.compile(r"\bstale\b", re.I),
    "resume": re.compile(r"--continue|--resume|\bresum", re.I),
    "no_pr": re.compile(r"no (open )?PR|no pull request", re.I),
    "waiting": re.compile(r"\bwaiting\b|do nothing this tick|nothing to do|no action", re.I),
    "no_progress": re.compile(r"no progress|no meaningful work|no new|\bidle\b|no change", re.I),
    "merge": re.compile(r"\bmerg(e|ed|ing)\b", re.I),
    "pr_ready": re.compile(r"PR ready|opened PR|created PR|pr create", re.I),
    "error": re.compile(r"\berror\b|\bexception\b|traceback", re.I),
    "crash": re.compile(r"crash|killed|OOM", re.I),
}

# --- Stage-A classifier: deterministic mapping matches -> category ---
# ACT = evidence of an artifact/routing change; IDLE = explicit no-op/waiting.
ACT = re.compile(
    r"PR ready|opened? (a )?PR|created? (a )?PR|pr create|assigned #|assigning #|"
    r"merged? (PR )?#|approv|requested changes|pushed|committed|opened issue|"
    r"created issue #", re.I)
IDLE = re.compile(
    r"no action this tick|do nothing this tick|nothing to do this tick|\bidle\b|"
    r"no meaningful work|no new .*(work|task|assignment)|no open .*(PR|pull request)|"
    r"no open `?crewd:task`?|no `?@\w+`? mention|waiting on|"
    r"remains? (open|queued|blocked)|no progress|no new activity|standing down", re.I)

def classify(text: str) -> str:
    a, i = bool(ACT.search(text)), bool(IDLE.search(text))
    if a and not i:
        return "productive"
    if a and i:
        return "mixed"
    if i:
        return "idle"
    return "unclear"

def canonical_population(root: Path) -> list[tuple[str, str, str, int, Path]]:
    """Return (crew, role, goal, cycle, path) in a filesystem-independent order."""
    pop = []
    for crew in CREWS:
        base = root / crew / "state" / "logs"
        if not base.is_dir():
            continue
        goals = sorted(
            (d for d in os.listdir(base) if d.startswith("goal-v")),
            key=lambda x: int(x.split("v")[1]),
        )
        for goal in goals:
            for role in ROLES:
                d = base / goal / role
                if not d.is_dir():
                    continue
                logs = sorted(
                    (p for p in d.iterdir() if p.suffix == ".log"),
                    key=lambda p: int(p.stem),
                )
                for p in logs:
                    pop.append((crew, role, goal, int(p.stem), p))
    return pop

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/azureuser/crews")
    ap.add_argument("--seed", type=int, default=1729)
    ap.add_argument("--n", type=int, default=16)
    args = ap.parse_args()
    root = Path(args.root)

    pop = canonical_population(root)
    print(f"POPULATION SIZE (role logs): {len(pop)}")

    # §1 census + §4 table + §5 exceptions
    for crew in CREWS:
        for role in ROLES:
            sub = [r for r in pop if r[0] == crew and r[1] == role]
            if not sub:
                continue
            cats = {"idle": 0, "productive": 0, "mixed": 0, "unclear": 0}
            kw = {k: 0 for k in KEYWORDS}
            for _, _, _, _, path in sub:
                t = path.read_text(errors="replace")
                cats[classify(t)] += 1
                for k, rx in KEYWORDS.items():
                    if rx.search(t):
                        kw[k] += 1
            n = len(sub)
            cat_s = " ".join(f"{k}={v}({v/n*100:.0f}%)" for k, v in cats.items())
            print(f"[{crew}/{role}] n={n}  {cat_s}")
            excerpt = {k: kw[k] for k in ("timeout","signal","cancel","crash","error","paused","stale") if kw[k]}
            if excerpt:
                print(f"    exceptions: " + " ".join(f"{k}={v}" for k, v in excerpt.items()))

    # Stage-C deterministic sample
    rng = random.Random(args.seed)
    sample = rng.sample(pop, args.n)
    print(f"\nSTAGE-C SAMPLE (seed={args.seed}, n={args.n}, over canonical ordering):")
    per_stratum: dict[tuple[str, str], int] = {}
    for crew, role, goal, cycle, path in sample:
        t = path.read_text(errors="replace")
        c = classify(t)
        per_stratum[(crew, role)] = per_stratum.get((crew, role), 0) + 1
        rel = path.relative_to(root)
        print(f"  {c:10s} {rel}")
    print("\nSTAGE-C observations per (crew,role) stratum:")
    for k in sorted(per_stratum):
        print(f"  {k[0]}/{k[1]}: {per_stratum[k]}")

if __name__ == "__main__":
    main()
