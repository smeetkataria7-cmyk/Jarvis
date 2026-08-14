#!/usr/bin/env python3
"""A paced terminal demo of the Guardian refusing to edit itself.

Built to be screen-recorded. Everything here calls the real
`guardian.protected` code — nothing is faked or re-implemented for the
camera, because a demo that lies is worse than no demo.

    python3 demo_guardian.py           # paced, for recording
    python3 demo_guardian.py --fast    # no delays, for checking it works
    python3 demo_guardian.py --no-color

Lines are kept under ~44 characters so the output stays readable when the
recording is cropped to a 9:16 vertical frame.
"""

from __future__ import annotations

import argparse
import sys
import time

from guardian.protected import ProtectedPathError, assert_all_allowed

# A patch Jarvis is allowed to make: its own reasoning code.
ALLOWED_PATCH = [
    "core/brain.py",
    "core/memory.py",
]

# The patch this whole design exists to stop.
BLOCKED_PATCH = [
    "core/brain.py",
    "guardian/protected.py",
]

SPEED = 1.0
COLOR = True


class Ink:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    ON_RED = "\033[41m\033[97m"
    ON_GREEN = "\033[42m\033[30m"


def paint(text: str, *styles: str) -> str:
    if not COLOR:
        return text
    return "".join(styles) + text + Ink.RESET


def pause(seconds: float) -> None:
    time.sleep(seconds * SPEED)


def say(text: str = "", *styles: str, after: float = 0.35) -> None:
    print(paint(text, *styles) if styles else text)
    sys.stdout.flush()
    pause(after)


def type_out(text: str, *styles: str, after: float = 0.5) -> None:
    """Print one character at a time, so the camera has something to watch."""
    for char in text:
        sys.stdout.write(paint(char, *styles) if styles else char)
        sys.stdout.flush()
        pause(0.018)
    sys.stdout.write("\n")
    sys.stdout.flush()
    pause(after)


def rule() -> None:
    say("─" * 44, Ink.DIM, after=0.15)


def show_patch(title: str, paths: list[str]) -> None:
    say(title, Ink.BOLD, after=0.3)
    for path in paths:
        say(f"   modified  {path}", Ink.CYAN, after=0.22)
    say()


def run_check(paths: list[str]) -> bool:
    """Run the real Guardian check. Returns True if the patch is allowed."""
    type_out("$ guardian check --patch", Ink.DIM, after=0.5)
    try:
        assert_all_allowed(paths)
    except ProtectedPathError as exc:
        say()
        say("  PATCH REFUSED  ", Ink.ON_RED, Ink.BOLD, after=0.5)
        say()
        say(f"  {exc}", Ink.RED, after=0.6)
        return False

    say()
    say("  PATCH ALLOWED  ", Ink.ON_GREEN, Ink.BOLD, after=0.5)
    return True


def scene_one() -> None:
    say("Jarvis wants to change its own code.", Ink.BOLD, after=0.7)
    say()
    show_patch("Proposed patch #1", ALLOWED_PATCH)

    if run_check(ALLOWED_PATCH):
        say()
        say("  → tests pass on a scratch branch", Ink.DIM, after=0.4)
        say("  → your phone buzzes", Ink.DIM, after=0.4)
        say("  → nothing merges until you", Ink.DIM, after=0.1)
        say("    approve it with a fingerprint", Ink.DIM, after=0.9)


def scene_two() -> None:
    say("Now the interesting one.", Ink.BOLD, after=0.8)
    say()
    say("I asked it to remove the part", Ink.YELLOW, after=0.1)
    say("that asks for permission.", Ink.YELLOW, after=0.9)
    say()
    show_patch("Proposed patch #2", BLOCKED_PATCH)

    if not run_check(BLOCKED_PATCH):
        say()
        say("It cannot edit the thing that", Ink.BOLD, after=0.1)
        say("stops it. That file is outside", Ink.BOLD, after=0.1)
        say("everything it is allowed to touch.", Ink.BOLD, after=0.9)


def main() -> int:
    global SPEED, COLOR

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="skip the pacing — for checking the demo still runs",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colour",
    )
    args = parser.parse_args()

    SPEED = 0.0 if args.fast else 1.0
    COLOR = not args.no_color and sys.stdout.isatty()

    say()
    rule()
    scene_one()
    say()
    rule()
    say()
    scene_two()
    say()
    rule()
    say()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
