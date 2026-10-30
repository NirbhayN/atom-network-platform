# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Per-round workspace checkpoints + revert (gap G6 — turn-level rewind).

Claude Code snapshots files every turn so a bad round is one click to undo; here
a bad code round meant manual git archaeology across clones. This module gives the
operator the same lever:

* :func:`snapshot` — before each code round, capture every selected repo's FULL
  worktree (tracked + untracked) as a commit object reachable only from
  ``refs/atom/ckpt/<run>/<label>``. The working branch, index and worktree are
  left byte-identical (stage → write-tree → commit-tree → update-ref → unstage),
  and ``refs/atom/*`` is never pushed, so checkpoints are invisible to the MR.
* :func:`revert` — restore a repo set to a checkpoint: worktree content back to
  the snapshot and files created AFTER it (untracked) deleted — scoped to paths
  inside the repo, never a blanket ``git clean``.

Fail-open: a checkpoint that cannot be taken logs and returns partial/empty —
the round proceeds (losing rewind for that round, never the round itself).
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.agents import workspace_local
from app.agents.platform_adapter import adapter

logger = logging.getLogger("app.agentic")

_IDENT = {"GIT_AUTHOR_NAME": "atom-checkpoint", "GIT_AUTHOR_EMAIL": "noreply@atom.local",
          "GIT_COMMITTER_NAME": "atom-checkpoint", "GIT_COMMITTER_EMAIL": "noreply@atom.local"}


def _git(rd: Path, *argv: str):
    return adapter.run_command(rd, ["git", *argv], env_overrides=dict(_IDENT))


def ref_name(run_id: str, label: str) -> str:
    return f"refs/atom/ckpt/{run_id}/{label}"


def snapshot(run_id: str, workspace_run_id: str, repo_ids: list[str],
             label: str) -> dict[str, str]:
    """Capture every repo's worktree. Returns {repo_id: commit_sha} (partial on error)."""
    out: dict[str, str] = {}
    for rid in repo_ids or []:
        try:
            rd = workspace_local.repo_dir(workspace_run_id or run_id, rid)
            if not (rd / ".git").exists():
                continue
            if _git(rd, "add", "-A").exit_code != 0:
                continue
            tree = _git(rd, "write-tree")
            head = _git(rd, "rev-parse", "HEAD")
            if tree.exit_code != 0:
                _git(rd, "reset", "-q")
                continue
            args = ["commit-tree", tree.stdout.strip(), "-m", f"atom checkpoint {label}"]
            if head.exit_code == 0:
                args[2:2] = ["-p", head.stdout.strip()]
            sha = _git(rd, *args)
            _git(rd, "reset", "-q")               # leave the index exactly as found
            if sha.exit_code != 0:
                continue
            commit = sha.stdout.strip()
            if _git(rd, "update-ref", ref_name(run_id, label), commit).exit_code == 0:
                out[rid] = commit
        except Exception as e:  # noqa: BLE001 — losing a checkpoint must not lose the round
            logger.warning("checkpoint: snapshot failed run=%s repo=%s: %s", run_id, rid, e)
    return out


def revert(run_id: str, workspace_run_id: str, ckpt: dict[str, str]) -> dict:
    """Restore each repo in ``ckpt`` ({repo_id: sha}) to its snapshot.

    Returns {reverted: [...], failed: [{repo_id, reason}]}. Per-repo fail-open —
    one broken repo must not abort the others."""
    reverted, failed = [], []
    for rid, sha in (ckpt or {}).items():
        try:
            rd = workspace_local.repo_dir(workspace_run_id or run_id, rid).resolve()
            snap = _git(rd, "ls-tree", "-r", "--name-only", sha)
            if snap.exit_code != 0:
                failed.append({"repo_id": rid, "reason": f"checkpoint {sha[:12]} unreadable"})
                continue
            snap_files = set(snap.stdout.splitlines())
            # Files created AFTER the snapshot are untracked (the agent's tools never
            # stage); delete exactly those — path-contained, never a blanket clean.
            extras = _git(rd, "ls-files", "--others", "--exclude-standard")
            removed = 0
            for rel in (extras.stdout.splitlines() if extras.exit_code == 0 else []):
                rel = rel.strip()
                if not rel or rel in snap_files:
                    continue
                target = (rd / rel).resolve()
                if rd not in target.parents:
                    continue                       # refuse anything escaping the repo
                try:
                    target.unlink()
                    removed += 1
                except OSError:
                    pass
            res = _git(rd, "restore", "--source", sha, "--worktree", "--", ":/")
            if res.exit_code != 0:
                failed.append({"repo_id": rid, "reason": (res.stderr or "restore failed")[:200]})
                continue
            reverted.append({"repo_id": rid, "sha": sha, "extras_removed": removed})
        except Exception as e:  # noqa: BLE001
            failed.append({"repo_id": rid, "reason": f"{type(e).__name__}: {e}"})
    return {"reverted": reverted, "failed": failed}
