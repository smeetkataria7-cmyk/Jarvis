"""
The self-edit pipeline: how Jarvis changes its own code without that being
a terrifying idea.

The flow, end to end:

    Jarvis writes a diff
      -> validated (size, protected paths, applies cleanly)
      -> queued, and the queue is on disk so a crash doesn't lose it
      -> applied to a scratch branch, never to your working tree
      -> tests run against the scratch branch
      -> only if green does your phone even buzz
      -> you read the diff and press your thumb to the sensor
      -> merged, and the commit records that you approved it
      -> if anything at all goes wrong, hard reset to the last good commit

Two principles hold this together.

The first is that every state change is a git commit, so "undo" is always
available and never depends on our own bookkeeping being correct.

The second is that tests run *before* you are asked, not after. Your attention
is the scarcest resource in this system. Spending it on patches that were
always going to fail is how a security gate degrades into a button you press
reflexively without reading — and a gate you press reflexively is not a gate.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Any

from . import protected

log = logging.getLogger("jarvis.guardian.patcher")

REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = REPO_ROOT / "guardian" / "queue"


class PatchError(Exception):
    """Raised when a patch is invalid or cannot be applied."""


class Status(str, Enum):
    PENDING_TESTS = "pending_tests"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED_TESTS = "failed_tests"
    FAILED_APPLY = "failed_apply"
    EXPIRED = "expired"


@dataclass
class Patch:
    patch_id: str
    summary: str          # plain English, for the phone screen
    rationale: str        # why Jarvis thinks this is worth doing
    diff: str             # unified diff
    files: list[str]
    created_at: float
    status: str = Status.PENDING_TESTS.value
    test_output: str = ""
    applied_commit: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def diff_lines(self) -> int:
        return sum(
            1 for line in self.diff.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )

    def note(self, event: str, detail: str = "") -> None:
        self.history.append({"at": time.time(), "event": event, "detail": detail})


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the repo. Never invokes a shell."""
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise PatchError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def current_commit() -> str:
    return _git("rev-parse", "HEAD").stdout.strip()


def current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def working_tree_clean() -> bool:
    return not _git("status", "--porcelain").stdout.strip()


# --------------------------------------------------------------------------
# Queue persistence
# --------------------------------------------------------------------------

def _patch_path(patch_id: str) -> Path:
    # patch_id is always a uuid4 we generated, but this is the only place a
    # caller-supplied string becomes a filesystem path, so validate anyway.
    if not all(c.isalnum() or c == "-" for c in patch_id):
        raise PatchError(f"invalid patch id: {patch_id!r}")
    return QUEUE_DIR / f"{patch_id}.json"


def _save(patch: Patch) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    path = _patch_path(patch.patch_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(patch), indent=2))
    tmp.replace(path)


def load(patch_id: str) -> Patch:
    path = _patch_path(patch_id)
    if not path.is_file():
        raise PatchError(f"no such patch: {patch_id}")
    return Patch(**json.loads(path.read_text()))


def list_patches(status: str | None = None) -> list[Patch]:
    if not QUEUE_DIR.is_dir():
        return []
    patches = []
    for path in QUEUE_DIR.glob("*.json"):
        try:
            patches.append(Patch(**json.loads(path.read_text())))
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("skipping unreadable patch %s: %s", path.name, exc)
    if status:
        patches = [p for p in patches if p.status == status]
    return sorted(patches, key=lambda p: p.created_at, reverse=True)


# --------------------------------------------------------------------------
# Proposing
# --------------------------------------------------------------------------

def _files_in_diff(diff: str) -> list[str]:
    """Extract touched paths from a unified diff.

    Reads the `+++ b/path` lines rather than the `diff --git` header, because
    the +++ line is what git apply actually writes to. If a crafted diff made
    those disagree, we want to be validating the one that matters.
    """
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                continue
            if path.startswith(("a/", "b/")):
                path = path[2:]
            files.append(path)
    return files


def propose(
    summary: str,
    rationale: str,
    diff: str,
    max_diff_lines: int = 400,
) -> Patch:
    """Validate and queue a proposed self-edit.

    Everything cheap and certain is checked here, before any git state moves.
    """
    if not diff.strip():
        raise PatchError("empty diff")
    if not summary.strip():
        raise PatchError("a patch must explain itself — summary is required")

    files = _files_in_diff(diff)
    if not files:
        raise PatchError("diff touches no files — is it a valid unified diff?")

    # The load-bearing check. Raises if the patch touches the Guardian, git
    # internals, config, or anything else Jarvis doesn't get a vote on.
    protected.assert_all_allowed(files)

    patch = Patch(
        patch_id=str(uuid.uuid4()),
        summary=summary.strip(),
        rationale=rationale.strip(),
        diff=diff,
        files=files,
        created_at=time.time(),
    )

    # A diff too large to read on a phone is a diff that gets approved
    # unread. Rejecting it is a feature.
    if patch.diff_lines > max_diff_lines:
        patch.status = Status.REJECTED.value
        patch.note("auto_rejected", f"{patch.diff_lines} lines > limit {max_diff_lines}")
        _save(patch)
        raise PatchError(
            f"patch is {patch.diff_lines} lines, limit is {max_diff_lines}. "
            f"Split it into smaller changes."
        )

    patch.note("proposed", f"{len(files)} file(s), {patch.diff_lines} lines")
    _save(patch)
    log.info("queued patch %s: %s", patch.patch_id, patch.summary)
    return patch


# --------------------------------------------------------------------------
# Testing on a scratch branch
# --------------------------------------------------------------------------

def run_tests(patch: Patch, test_command: str) -> bool:
    """Apply the patch to a scratch branch and run the test suite there.

    Your working tree is never touched. Whatever the outcome, we return to the
    branch we started on and delete the scratch branch.
    """
    if not working_tree_clean():
        raise PatchError(
            "working tree has uncommitted changes — commit or stash them "
            "before testing a self-edit"
        )

    origin_branch = current_branch()
    base_commit = current_commit()
    scratch = f"jarvis/patch-{patch.patch_id[:8]}"
    diff_file = QUEUE_DIR / f"{patch.patch_id}.diff"

    try:
        diff_file.write_text(patch.diff)
        _git("checkout", "-b", scratch)

        apply_result = _git("apply", "--index", str(diff_file), check=False)
        if apply_result.returncode != 0:
            patch.status = Status.FAILED_APPLY.value
            patch.test_output = apply_result.stderr.strip()
            patch.note("apply_failed", patch.test_output[:500])
            _save(patch)
            return False

        _git("commit", "-m", f"jarvis: {patch.summary}", "--no-verify")

        test = subprocess.run(
            test_command,
            cwd=REPO_ROOT,
            shell=True,  # test_command comes from your config.yaml, not from Jarvis
            capture_output=True,
            text=True,
            timeout=600,
        )
        # Tail, not head — pytest puts the failure summary at the bottom, and
        # this is what gets rendered on a phone screen.
        patch.test_output = (test.stdout + test.stderr)[-4000:]

        if test.returncode != 0:
            patch.status = Status.FAILED_TESTS.value
            patch.note("tests_failed", f"exit {test.returncode}")
            _save(patch)
            log.info("patch %s failed tests — not surfacing for approval", patch.patch_id)
            return False

        patch.status = Status.AWAITING_APPROVAL.value
        patch.note("tests_passed")
        _save(patch)
        return True

    except subprocess.TimeoutExpired:
        patch.status = Status.FAILED_TESTS.value
        patch.test_output = "test command timed out after 600s"
        patch.note("tests_timeout")
        _save(patch)
        return False

    finally:
        # Get back to a known state no matter what happened above. Leaving the
        # repo on a scratch branch would break the next proposal and, worse,
        # could leave untested code sitting in the working tree.
        _git("checkout", "--force", origin_branch, check=False)
        _git("reset", "--hard", base_commit, check=False)
        _git("branch", "-D", scratch, check=False)
        diff_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Applying, after you have approved
# --------------------------------------------------------------------------

def apply_approved(patch: Patch, test_command: str, verify: bool = True) -> str:
    """Apply an approved patch to the real branch. Returns the new commit sha.

    Called only after auth.verify_approval has succeeded. If anything fails at
    any point, the repo is hard-reset to where it started.
    """
    if patch.status != Status.AWAITING_APPROVAL.value:
        raise PatchError(
            f"patch {patch.patch_id} is {patch.status}, not awaiting approval"
        )

    if not working_tree_clean():
        raise PatchError("working tree is dirty — refusing to apply")

    # Re-check protected paths. The first check happened at propose time; the
    # diff has been sitting on disk since. Cheap to repeat, catastrophic to skip.
    protected.assert_all_allowed(patch.files)

    problems = protected.verify_integrity()
    if problems:
        raise PatchError("guardian integrity check failed: " + "; ".join(problems))

    rollback_to = current_commit()
    diff_file = QUEUE_DIR / f"{patch.patch_id}.diff"

    try:
        diff_file.write_text(patch.diff)

        result = _git("apply", "--index", str(diff_file), check=False)
        if result.returncode != 0:
            raise PatchError(f"patch no longer applies cleanly: {result.stderr.strip()}")

        message = (
            f"jarvis: {patch.summary}\n\n"
            f"{patch.rationale}\n\n"
            f"Patch-Id: {patch.patch_id}\n"
            f"Approved-By: biometric signature verified\n"
        )
        _git("commit", "-m", message, "--no-verify")
        new_commit = current_commit()

        # Belt and braces: tests already passed on the scratch branch, but the
        # base may have moved since. Verify on the branch we actually shipped to.
        if verify:
            test = subprocess.run(
                test_command, cwd=REPO_ROOT, shell=True,
                capture_output=True, text=True, timeout=600,
            )
            if test.returncode != 0:
                raise PatchError(
                    "tests failed after applying to the live branch — rolled back"
                )

        patch.status = Status.APPROVED.value
        patch.applied_commit = new_commit
        patch.note("applied", new_commit[:12])
        _save(patch)
        log.info("applied patch %s as %s", patch.patch_id, new_commit[:12])
        return new_commit

    except Exception as exc:
        _git("reset", "--hard", rollback_to, check=False)
        patch.status = Status.FAILED_APPLY.value
        patch.note("rolled_back", str(exc)[:500])
        _save(patch)
        log.error("patch %s rolled back to %s: %s", patch.patch_id, rollback_to[:12], exc)
        raise

    finally:
        diff_file.unlink(missing_ok=True)


def reject(patch: Patch, reason: str = "declined") -> None:
    patch.status = Status.REJECTED.value
    patch.note("rejected", reason)
    _save(patch)


def expire_stale(timeout_minutes: int) -> int:
    """Expire approval requests you never answered. Returns how many."""
    cutoff = time.time() - timeout_minutes * 60
    count = 0
    for patch in list_patches(status=Status.AWAITING_APPROVAL.value):
        if patch.created_at < cutoff:
            patch.status = Status.EXPIRED.value
            patch.note("expired", f"unanswered for {timeout_minutes}m")
            _save(patch)
            count += 1
    return count
