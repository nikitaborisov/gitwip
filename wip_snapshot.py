#!/usr/bin/env python3
"""
wip_snapshot.py — snapshot dirty working trees onto wip/<branch> and push.

Never touches HEAD, your checked-out branch, or your real index. It builds a
commit directly from a copy of the index (tracked files only — no untracked
files are ever added) and points refs/heads/wip/<current-branch> at it.

  clean repo          -> nothing (you committed properly; done)
  dirty repo          -> new commit on wip/<branch> with your working copy
  same as last time   -> nothing

Repos: every immediate subdirectory of ~/git (symlinks followed) with a .git.
Remote: the one whose URL matches WIP_REMOTE_PATTERN if any, else origin,
        else the only remote, else the first.

Review a branch's week:   git log --oneline --first-parent wip/main
Restore a lost snapshot:  git checkout wip/main -- .      (from main, clean tree)
Install:                  uv tool install .      (puts `wip-snapshot` on PATH)
Schedule (cron):          5,35 * * * * $HOME/.local/bin/wip-snapshot
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

GIT_ROOT = Path(os.environ.get("WIP_GIT_ROOT", "~/git")).expanduser()
REMOTE_PATTERN = re.compile(
    os.environ.get("WIP_REMOTE_PATTERN", r"github\.com[/:]nikitaborisov/"), re.I
)
BRANCH_PREFIX = os.environ.get("WIP_BRANCH_PREFIX", "wip/")
LOG = Path(os.environ.get("WIP_LOG", "~/.wip-snapshot.log")).expanduser()


def log(msg: str) -> None:
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%F %T} {msg}\n")


class Git:
    def __init__(self, repo: Path):
        self.repo = repo

    def run(self, *args, env=None, input=None, ok_codes=(0,)) -> str | None:
        """Run git; return stripped stdout, or None if exit code not in ok_codes."""
        full_env = dict(os.environ, **(env or {}))
        r = subprocess.run(
            ["git", *args], cwd=self.repo, env=full_env, input=input,
            capture_output=True, text=True,
        )
        if r.returncode not in ok_codes:
            return None
        return r.stdout.strip()

    def rev(self, ref: str) -> str | None:
        return self.run("rev-parse", "--verify", "-q", ref, ok_codes=(0, 1)) or None

    def is_ancestor(self, a: str, b: str) -> bool:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                           cwd=self.repo, capture_output=True)
        return r.returncode == 0

    def git_path(self, name: str) -> Path:
        p = Path(self.run("rev-parse", "--git-path", name) or "")
        return p if p.is_absolute() else self.repo / p


def find_repos(root: Path) -> list[Path]:
    root = root.resolve()  # follows the ~/git symlink
    if not root.is_dir():
        log(f"ROOT     {root} not found")
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())


def choose_remote(g: Git) -> str | None:
    names = (g.run("remote") or "").split()
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    for name in names:
        url = g.run("remote", "get-url", name) or ""
        if REMOTE_PATTERN.search(url):
            return name
    return "origin" if "origin" in names else names[0]


def snapshot(repo: Path) -> None:
    g = Git(repo)
    name = repo.name

    if g.run("rev-parse", "--is-inside-work-tree") != "true":
        log(f"SKIP     {name} (not a repo)")
        return
    if g.git_path("index.lock").exists():
        log(f"SKIP     {name} (index.lock present — git busy)")
        return

    # Tracked files only: untracked never counts as dirty and is never added.
    status = g.run("status", "--porcelain", "--untracked-files=no")
    if not status:
        return

    head = g.rev("HEAD")
    if not head:
        log(f"SKIP     {name} (no commits yet)")
        return
    branch = g.run("symbolic-ref", "-q", "--short", "HEAD", ok_codes=(0, 1)) or "detached"
    wip_ref = f"refs/heads/{BRANCH_PREFIX}{branch}"
    wip = g.rev(wip_ref)

    # Build the tree from a throwaway copy of the real index. `add -u` only
    # refreshes files the index already knows about.
    real_index = g.git_path("index")
    fd, tmp_index = tempfile.mkstemp(prefix="wip-index-")
    os.close(fd)
    try:
        if real_index.exists():
            shutil.copyfile(real_index, tmp_index)
        env = {"GIT_INDEX_FILE": tmp_index}
        if g.run("add", "-u", ".", env=env) is None:
            log(f"FAIL     {name} (add -u)")
            return
        tree = g.run("write-tree", env=env)
    finally:
        os.unlink(tmp_index)
    if not tree:
        log(f"FAIL     {name} (write-tree)")
        return

    if wip and g.rev(f"{wip}^{{tree}}") == tree:
        return

    # Parents: last snapshot on this wip branch, plus HEAD if it has moved on.
    parents = []
    if wip:
        parents += ["-p", wip]
        if not g.is_ancestor(head, wip):
            parents += ["-p", head]
    else:
        parents += ["-p", head]

    msg = f"wip: {datetime.now():%a %H:%M} on {branch}\n"
    commit = g.run("commit-tree", tree, *parents, input=msg)
    if not commit:
        log(f"FAIL     {name} (commit-tree)")
        return
    if g.run("update-ref", wip_ref, commit, *([wip] if wip else [])) is None:
        log(f"FAIL     {name} (update-ref)")
        return
    log(f"SNAP     {name} {commit[:8]} -> {BRANCH_PREFIX}{branch}")

    remote = choose_remote(g)
    if not remote:
        return
    if g.run("push", "-q", remote, f"{wip_ref}:{wip_ref}") is None:
        log(f"PUSHFAIL {name} -> {remote} ({BRANCH_PREFIX}{branch})")
    else:
        log(f"PUSH     {name} -> {remote} ({BRANCH_PREFIX}{branch})")


def main() -> int:
    repos = find_repos(GIT_ROOT)
    for repo in repos:
        try:
            snapshot(repo)
        except Exception as e:  # one bad repo shouldn't stop the rest
            log(f"ERROR    {repo.name}: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
