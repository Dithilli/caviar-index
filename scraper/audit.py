#!/usr/bin/env python3
"""
Audit-trail integrity checker for caviar-index.

The repo's credibility rests on invariants that git alone does not enforce.
This script proves they hold — over the entire commit history, not just the
latest state — so a challenge to the data is answered with a command anyone
can run, not an assurance.

  A1  Append-only logs. Every committed version of each file in APPEND_ONLY
      must be a byte-for-byte prefix of the version that follows it, and the
      last committed version a prefix of what is on disk now.
  A2  Observation ids are unique, strictly sequential, gap-free.
  A3  Snapshots are write-once. No commit in history modifies or deletes a
      file under snapshots/, and none is modified or deleted in the worktree.
  A4  Every snapshot_path referenced by a row exists.
  A5  No duplicate (observed_at, listing_url) pair — a run writes at most one
      row per listing.

Usage: python scraper/audit.py        (needs a full clone, not a shallow one,
                                       for the history checks to mean anything)

Exit 0: all invariants hold. Exit 1: violations, printed one per line.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

APPEND_ONLY = [
    "data/price-log.csv",
    "data/corrections.csv",
    "data/discovery-log.csv",
    "anchors/pre-registration.sha256",
]

violations: list[str] = []


def fail(msg: str) -> None:
    violations.append(msg)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True)


def git_show(ref: str, path: str) -> bytes | None:
    """File content at ref, or None if it did not exist there."""
    cp = git("show", f"{ref}:{path}")
    return cp.stdout if cp.returncode == 0 else None


# ----------------------------------------------------------------------------
# A1 — append-only over full history
# ----------------------------------------------------------------------------

def audit_append_only(path: str) -> None:
    cp = git("rev-list", "--reverse", "HEAD", "--", path)
    commits = cp.stdout.decode().split()
    prev = b""
    prev_commit = "(empty)"
    for commit in commits:
        cur = git_show(commit, path)
        if cur is None:
            if prev:
                fail(f"A1 {path}: deleted at commit {commit[:12]}")
                prev, prev_commit = b"", commit[:12]
            continue
        if not cur.startswith(prev):
            fail(f"A1 {path}: commit {commit[:12]} is not an append to "
                 f"{prev_commit} — history was rewritten or a row was edited")
        prev, prev_commit = cur, commit[:12]

    disk = ROOT / path
    if prev and not disk.exists():
        fail(f"A1 {path}: committed at {prev_commit} but missing from worktree")
    elif disk.exists():
        now = disk.read_bytes()
        if not now.startswith(prev):
            fail(f"A1 {path}: worktree file is not an append to {prev_commit}")


# ----------------------------------------------------------------------------
# A3 — snapshots are write-once
# ----------------------------------------------------------------------------

def audit_snapshots_write_once() -> None:
    cp = git("log", "--diff-filter=MD", "--name-only", "--format=%H",
             "--", "snapshots/")
    touched = [l for l in cp.stdout.decode().splitlines() if l.strip()]
    if touched:
        fail("A3 snapshots modified or deleted in history: "
             + ", ".join(touched[:10]))
    cp = git("diff", "--name-status", "--diff-filter=MD", "HEAD",
             "--", "snapshots/")
    dirty = cp.stdout.decode().strip()
    if dirty:
        fail(f"A3 snapshots modified or deleted in worktree: {dirty}")


# ----------------------------------------------------------------------------
# A2 / A4 / A5 — current state of the price log
# ----------------------------------------------------------------------------

def audit_price_log_rows() -> None:
    path = ROOT / "data" / "price-log.csv"
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    last = 0
    seen_pairs: set[tuple[str, str]] = set()
    for i, row in enumerate(rows, start=2):
        oid = (row.get("observation_id") or "").strip()
        if not oid.startswith("OBS-"):
            fail(f"A2 line {i}: malformed observation_id {oid!r}")
            continue
        try:
            n = int(oid[4:])
        except ValueError:
            fail(f"A2 line {i}: malformed observation_id {oid!r}")
            continue
        if n != last + 1:
            fail(f"A2 line {i}: {oid} follows OBS-{last:06d} — "
                 f"ids must be sequential and gap-free")
        last = n

        pair = (row.get("observed_at", ""), row.get("listing_url", ""))
        if pair in seen_pairs:
            fail(f"A5 line {i}: duplicate observation for {pair}")
        seen_pairs.add(pair)

        snap = (row.get("snapshot_path") or "").strip()
        if snap and not (ROOT / snap).exists():
            fail(f"A4 line {i}: referenced snapshot missing: {snap}")


# ----------------------------------------------------------------------------

def main() -> int:
    cp = git("rev-parse", "--is-shallow-repository")
    if cp.stdout.decode().strip() == "true":
        print("WARNING: shallow clone — history checks (A1, A3) see only "
              "part of the chain.", file=sys.stderr)

    for path in APPEND_ONLY:
        audit_append_only(path)
    audit_snapshots_write_once()
    audit_price_log_rows()

    if violations:
        print(f"AUDIT FAILED — {len(violations)} violation(s):")
        for v in violations:
            print(f"  {v}")
        return 1
    print("Audit passed: append-only, write-once, sequential, complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
