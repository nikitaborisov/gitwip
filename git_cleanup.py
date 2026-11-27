#!/usr/bin/env python3
"""
git_cleanup.py — report repos that need attention; optionally delete dead ones.

Goes through every immediate subdirectory of ~/git that has a .git and lists:

  unsynced   uncommitted changes (incl. untracked files), a stash, no remote,
             or commits on a local branch / HEAD that are on no remote
  stale      nothing has changed in more than --days (default 30)

"Last change" is the newest of: the latest commit on any local branch, the
latest HEAD reflog entry (commit, checkout, pull, clone...), and the mtime of
any uncommitted files.

  git-cleanup                 report only
  git-cleanup --delete        also delete repos that are synced AND stale

Before deleting, each candidate is re-checked after `git fetch --prune`, so a
branch that was deleted on the remote counts as unpushed. If the fetch fails
the repo is kept. Ignored files (build output, .env, ...) are not backed up
anywhere; their count is shown before you confirm.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = os.environ.get("WIP_GIT_ROOT", "~/git")


def git(repo: Path, *args: str) -> str | None:
    """Run git; return stdout (trailing newline stripped), or None on failure."""
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        # Don't let `status` rewrite the index: keeps us read-only.
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
    )
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


@dataclass
class Report:
    repo: Path
    reasons: list[str] = field(default_factory=list)  # why it is unsynced
    last_change: float = 0.0
    ignored: int = 0

    @property
    def synced(self) -> bool:
        return not self.reasons

    def age_days(self) -> float:
        return (time.time() - self.last_change) / 86400


def find_repos(root: Path) -> list[Path]:
    root = root.resolve()  # follows the ~/git symlink
    if not root.is_dir():
        sys.exit(f"{root}: not a directory")
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())


def parse_status(out: str) -> tuple[list[str], list[str], int]:
    """Split `status --porcelain -z --ignored` into (changed, untracked, #ignored)."""
    changed, untracked, ignored = [], [], 0
    tokens = iter(out.split("\0"))
    for tok in tokens:
        if len(tok) < 4:
            continue
        xy, path = tok[:2], tok[3:]
        if xy == "!!":
            ignored += 1
        elif xy == "??":
            untracked.append(path)
        else:
            changed.append(path)
            if "R" in xy or "C" in xy:
                next(tokens, None)  # skip the rename/copy source path
    return changed, untracked, ignored


def inspect(repo: Path) -> Report:
    rep = Report(repo)
    times = [0.0]

    if git(repo, "rev-parse", "--is-inside-work-tree") != "true":
        rep.reasons.append("not a usable work tree")
        rep.last_change = repo.stat().st_mtime
        return rep

    # --- uncommitted ---
    status = git(repo, "status", "--porcelain", "-z", "--ignored")
    if status is None:
        rep.reasons.append("git status failed")
    else:
        changed, untracked, rep.ignored = parse_status(status)
        parts = []
        if changed:
            parts.append(f"{len(changed)} modified")
        if untracked:
            parts.append(f"{len(untracked)} untracked")
        if parts:
            rep.reasons.append("uncommitted: " + ", ".join(parts))
        for path in changed + untracked:
            try:
                times.append((repo / path).lstat().st_mtime)
            except OSError:
                pass  # deleted file
    if git(repo, "rev-parse", "-q", "--verify", "refs/stash"):
        rep.reasons.append("stash")

    # --- unpushed ---
    has_head = git(repo, "rev-parse", "-q", "--verify", "HEAD") is not None
    if not has_head:
        rep.reasons.append("no commits")
    if not git(repo, "remote"):
        rep.reasons.append("no remote")
    elif has_head:
        ahead = []
        branches = (git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads") or "").split("\n")
        for ref, label in [(f"refs/heads/{b}", b) for b in branches if b] + [("HEAD", "HEAD")]:
            n = git(repo, "rev-list", "--count", ref, "--not", "--remotes")
            if n is None:
                ahead.append(f"{label} ?")
            elif n != "0":
                ahead.append(f"{label} +{n}")
        # HEAD only matters when detached; otherwise its branch already covers it.
        if git(repo, "symbolic-ref", "-q", "HEAD") is not None:
            ahead = [a for a in ahead if not a.startswith("HEAD ")]
        if ahead:
            rep.reasons.append("unpushed: " + ", ".join(ahead))

    if len((git(repo, "worktree", "list", "--porcelain") or "").split("\n\n")) > 1:
        rep.reasons.append("has linked worktrees")
    if not (repo / ".git").is_dir() or repo.is_symlink():
        rep.reasons.append("not a plain clone (symlink or .git file)")

    # --- last change ---
    newest = git(repo, "for-each-ref", "--sort=-committerdate", "--count=1",
                 "--format=%(committerdate:unix)", "refs/heads")
    if newest:
        times.append(float(newest))
    m = re.search(r"\{(\d+)\}", git(repo, "log", "-g", "-1", "--format=%gd", "--date=unix", "HEAD") or "")
    if m:
        times.append(float(m.group(1)))
    rep.last_change = max(times) or repo.stat().st_mtime
    return rep


def fmt_age(days: float) -> str:
    if days < 1:
        return "today"
    if days < 60:
        return f"{days:.0f}d ago"
    if days < 730:
        return f"{days / 30.4:.0f}mo ago"
    return f"{days / 365.25:.1f}y ago"


def print_group(title: str, reports: list[Report], detail) -> None:
    if not reports:
        return
    print(f"\n{title} ({len(reports)})")
    width = max(len(r.repo.name) for r in reports)
    for r in sorted(reports, key=lambda r: r.last_change):
        print(f"  {r.repo.name:<{width}}  {fmt_age(r.age_days()):>9}  {detail(r)}".rstrip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT, help="directory of repos (default: %(default)s)")
    ap.add_argument("--days", type=float, default=30, help="stale after this many days (default: %(default)s)")
    ap.add_argument("--delete", action="store_true", help="delete repos that are synced and stale")
    ap.add_argument("-y", "--yes", action="store_true", help="with --delete, don't ask for confirmation")
    args = ap.parse_args()

    reports = [inspect(r) for r in find_repos(Path(args.root).expanduser())]
    stale = lambda r: r.age_days() > args.days  # noqa: E731

    print_group("Unsynced", [r for r in reports if not r.synced],
                lambda r: ("STALE  " if stale(r) else "") + "; ".join(r.reasons))
    deletable = [r for r in reports if r.synced and stale(r)]
    print_group(f"Synced, unchanged for {args.days:g}+ days" + ("" if args.delete else " — removable with --delete"),
                deletable, lambda r: f"({r.ignored} ignored paths)" if r.ignored else "")
    ok = sum(1 for r in reports if r.synced and not stale(r))
    print(f"\n{len(reports)} repos: {ok} synced and active, "
          f"{sum(1 for r in reports if not r.synced)} unsynced, {len(deletable)} synced and stale")

    if not args.delete or not deletable:
        return 0

    # Remote-tracking refs may be out of date; verify against the real remotes.
    print("\nVerifying against remotes...")
    confirmed = []
    for r in deletable:
        if git(r.repo, "fetch", "--all", "--prune", "--quiet") is None:
            print(f"  keep {r.repo.name}: fetch failed")
            continue
        again = inspect(r.repo)
        if not again.synced or not stale(again):
            print(f"  keep {r.repo.name}: {'; '.join(again.reasons) or 'changed recently'}")
            continue
        confirmed.append(again)
    if not confirmed:
        print("Nothing to delete.")
        return 0

    print("\nWill delete:")
    for r in confirmed:
        print(f"  {r.repo}")
    if not args.yes:
        try:
            answer = input(f"Delete these {len(confirmed)} repos? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    for r in confirmed:
        shutil.rmtree(r.repo)
        print(f"  deleted {r.repo.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
