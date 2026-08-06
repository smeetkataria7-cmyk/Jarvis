"""
Where Jarvis proposes changes to its own code.

This is the half of self-editing that actually does something. The Guardian
decides whether a patch may be applied; this decides what to propose in the
first place, and the difference in character between the two matters. The
Guardian is paranoid by design. This is meant to be conservative by evidence.

The rule it works to: propose from failure logs, never from a hunch. Every
proposal has to point at real recorded episodes where something did not work.
An assistant that rewrites itself because a model thought the code looked
improvable is not learning, it is fidgeting — and each fidget spends a scarce
resource, which is your willingness to actually read the next diff.

So proposals are rare on purpose. If nothing is measurably broken, the correct
output of this module is nothing at all.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.selfedit")

REPO_ROOT = Path(__file__).resolve().parent.parent

# An action has to fail this often, this many times, before it is worth
# proposing a fix. Both thresholds exist to stop one bad afternoon — a flaky
# network, a phone left face down — from triggering a code change.
MIN_ATTEMPTS = 5
FAILURE_THRESHOLD = 0.4

PROPOSAL_SYSTEM = """\
You maintain the code of a personal assistant called Jarvis. You are being \
shown evidence that one of its actions keeps failing, along with the source \
file responsible.

Propose ONE small, focused fix as a unified diff.

Hard requirements:
- Output ONLY the diff. No prose before or after, no code fences.
- Use standard unified diff format with `--- a/path` and `+++ b/path` headers \
and correct @@ hunk headers.
- Change as little as possible. A reviewer is reading this on a phone screen.
- Only modify files under core/. Never touch guardian/, config, or tests/guardian/.
- If the evidence does not clearly point at a fixable bug, output exactly: \
NO_PATCH

That last option is not a failure. Most of the time there is nothing worth \
changing, and saying so is the correct answer.
"""


class SelfEditError(Exception):
    """Raised when a proposal cannot be produced or is unusable."""


def find_worst_action(memory) -> dict[str, Any] | None:
    """The action most worth fixing, or None if nothing qualifies.

    Returns at most one. Proposing several changes at once produces a diff too
    large to read, and a diff too large to read gets approved unread.
    """
    candidates = [
        entry for entry in memory.failure_rates(minimum_attempts=MIN_ATTEMPTS)
        if entry["rate"] >= FAILURE_THRESHOLD
    ]
    return candidates[0] if candidates else None


def gather_evidence(memory, action: str, limit: int = 10) -> str:
    """Recent failures for one action, as plain text for the prompt."""
    with memory._connect() as conn:
        rows = conn.execute(
            "SELECT params, outcome, detail, created_at FROM episodes "
            "WHERE action = ? AND outcome != 'ok' "
            "ORDER BY created_at DESC LIMIT ?",
            (action, limit),
        ).fetchall()

    if not rows:
        return ""

    lines = [f"Recent failures of {action}:"]
    for row in rows:
        detail = row["detail"] or "(no detail recorded)"
        lines.append(f"- params={row['params']} -> {row['outcome']}: {detail}")
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    """Pull the diff out of a code fence if the model used one.

    The trailing newline is restored deliberately. `git apply` treats a diff
    with no final newline as truncated and refuses it, so stripping whitespace
    off the end — the obvious thing to write — quietly breaks every patch that
    would otherwise have applied cleanly.
    """
    fenced = re.search(r"```(?:diff|patch)?\s*(.*?)```", text, re.DOTALL)
    body = fenced.group(1) if fenced else text

    body = body.strip()
    return body + "\n" if body else ""


def looks_like_unified_diff(text: str) -> bool:
    """Cheap structural check before handing anything to git apply.

    Not a validation — git is the real judge, and it runs on a scratch branch
    where a bad diff costs nothing. This just avoids queueing prose.
    """
    return (
        "--- " in text
        and "+++ " in text
        and "@@" in text
    )


def propose_improvement(memory, brain, source_file: str = "core/phone.py") -> dict[str, Any] | None:
    """Look for something worth fixing and draft a patch for it.

    Returns a dict ready for patcher.propose(), or None when there is nothing
    to do — which is the expected outcome most of the time.
    """
    worst = find_worst_action(memory)
    if worst is None:
        log.debug("nothing failing often enough to warrant a patch")
        return None

    action = worst["action"]
    log.info(
        "considering a fix for %s (%d/%d failed)",
        action, worst["failures"], worst["attempts"],
    )

    evidence = gather_evidence(memory, action)
    if not evidence:
        return None

    target = REPO_ROOT / source_file
    if not target.is_file():
        raise SelfEditError(f"{source_file} does not exist")

    prompt = (
        f"{evidence}\n\n"
        f"Failure rate: {worst['failures']}/{worst['attempts']} "
        f"({worst['rate']:.0%})\n\n"
        f"Current contents of {source_file}:\n\n"
        f"{target.read_text()}"
    )

    raw = brain.chat(
        messages=[{"role": "user", "content": prompt}],
        system=PROPOSAL_SYSTEM,
    )

    diff = _strip_fences(raw)

    if diff.strip() == "NO_PATCH" or not diff.strip():
        log.info("model saw no clear fix for %s — proposing nothing", action)
        return None

    if not looks_like_unified_diff(diff):
        # Do not attempt repair. A malformed diff means the model was not
        # really producing a patch, and coaxing prose into diff shape is how
        # you end up applying something nobody meant.
        log.warning("model returned something that isn't a diff; discarding")
        return None

    return {
        "summary": f"Fix {action}, failing {worst['rate']:.0%} of the time",
        "rationale": (
            f"{action} failed {worst['failures']} of {worst['attempts']} "
            f"recent attempts.\n\n{evidence}"
        ),
        "diff": diff,
    }
