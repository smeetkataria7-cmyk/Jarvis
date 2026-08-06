"""
The protected list: paths Jarvis may never modify, no matter what it decides.

This is the load-bearing wall of the entire design.

Jarvis can rewrite its own brain, its skills, its memory handling — that is the
point. But if it could also rewrite the thing that asks your permission, then
the very first patch it could propose is "delete the part that asks
permission," and every approval after that would be theatre.

So the rule is simple and absolute: the approval machinery is not part of the
editable surface. Jarvis proposes; the Guardian decides; Jarvis gets no vote on
the Guardian.

If you ever find yourself wanting to relax something in here to unblock a
feature, that is the moment the design is being lost. Move the feature into
core/ instead.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Prefixes Jarvis may never touch. Matched against repo-relative POSIX paths.
PROTECTED_PREFIXES: tuple[str, ...] = (
    "guardian/",        # the approval machinery itself
    ".git/",            # history — the only real undo we have
    ".github/",         # CI, which is a second opinion on every patch
    "tests/guardian/",  # the tests that prove the gate still works
)

# Exact files Jarvis may never touch.
PROTECTED_FILES: frozenset[str] = frozenset({
    "config.yaml",          # your keys
    "config.example.yaml",  # the shape of your keys
    ".gitignore",           # what keeps secrets out of git
    "requirements.txt",     # dependency pinning is a supply-chain surface
    "run.py",               # the entrypoint that starts the Guardian
})

# File types Jarvis may never create or modify. A self-editing assistant has
# no business writing shell scripts or CI config — those execute outside the
# review path or with wider authority than the patch itself.
PROTECTED_SUFFIXES: tuple[str, ...] = (
    ".sh", ".bash", ".zsh",
    ".yml", ".yaml",
    ".service", ".plist",
    ".so", ".dll", ".dylib", ".exe", ".bin",
)


class ProtectedPathError(Exception):
    """Raised when a patch tries to touch something off-limits."""


def _normalise(path: str) -> str:
    """Reduce a path to a repo-relative POSIX string, or raise if it escapes.

    Everything downstream trusts this function's output, so it has to be the
    place where traversal tricks die. `../`, absolute paths, and symlinks that
    point outside the repo all get rejected here rather than being quietly
    normalised into something that looks safe.
    """
    # resolve() throws a surprising variety of things on hostile input: a null
    # byte raises ValueError, a symlink loop raises OSError, and a deep enough
    # chain raises RuntimeError. Every one of them has to land as
    # ProtectedPathError, because is_protected() catches that and only that —
    # anything escaping this function uncaught would propagate out of a
    # is_protected() call that the caller reasonably expects to just return a
    # bool, and a crashed validator is an unvalidated patch.
    try:
        candidate = Path(path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (REPO_ROOT / candidate).resolve()
    except (ValueError, OSError, RuntimeError) as exc:
        raise ProtectedPathError(f"path is not resolvable: {path!r} ({exc})") from exc

    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ProtectedPathError(
            f"path escapes the repository: {path!r}"
        ) from exc

    return relative.as_posix()


def is_protected(path: str) -> bool:
    """True if `path` is off-limits to Jarvis."""
    try:
        rel = _normalise(path)
    except ProtectedPathError:
        # Anything we can't resolve safely is treated as protected. Failing
        # closed is the only acceptable direction for this function.
        return True

    if rel in PROTECTED_FILES:
        return True

    if any(rel.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return True

    if rel.endswith(PROTECTED_SUFFIXES):
        return True

    # Dotfiles at the repo root configure tooling that runs with more
    # authority than Jarvis has. Off-limits.
    if "/" not in rel and rel.startswith("."):
        return True

    return False


def assert_all_allowed(paths: list[str]) -> None:
    """Raise ProtectedPathError if any path in `paths` is off-limits.

    Called before a patch is queued and again immediately before it is applied.
    Checking twice is deliberate: the file list is derived from a diff that
    lives on disk between those two moments.
    """
    blocked = sorted(p for p in paths if is_protected(p))
    if blocked:
        raise ProtectedPathError(
            "patch touches protected paths: " + ", ".join(blocked)
        )


def verify_integrity() -> list[str]:
    """Check that the Guardian's own files are present and non-empty.

    A patch cannot legitimately delete these, so if one goes missing something
    has gone wrong that no approval flow should be running on top of. Returns a
    list of human-readable problems; empty means healthy.
    """
    problems: list[str] = []
    required = ("protected.py", "auth.py", "patcher.py", "server.py")

    for name in required:
        target = REPO_ROOT / "guardian" / name
        if not target.is_file():
            problems.append(f"guardian/{name} is missing")
        elif target.stat().st_size == 0:
            problems.append(f"guardian/{name} is empty")

    return problems
