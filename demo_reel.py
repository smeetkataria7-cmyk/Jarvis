#!/usr/bin/env python3
"""Produce the screen states the reel needs — all of them real.

Every scene here runs actual code in this repo. Nothing is mocked up for the
camera, because a faked demo is the one mistake a technical audience does not
forgive, and it would undercut the only thing that makes this account worth
following.

    python3 demo_reel.py list      # what each scene is for
    python3 demo_reel.py green     # clip 3  — the suite passing
    python3 demo_reel.py red       # clip 4  — the suite catching a real break
    python3 demo_reel.py boot      # clip 5  — Jarvis starting up
    python3 demo_reel.py crash     # clip 6  — a real traceback
    python3 demo_reel.py blame     # clip 8  — who actually wrote this
    python3 demo_reel.py history   # clip 12 — the commit log

Run each one full-screen with your terminal font at 20pt or larger.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent

# The line in core/phone.py that says anything reaching another human must be
# confirmed. The `red` scene inverts it, proving the tests catch it, then puts
# it straight back.
GUARD_FILE = REPO / "core" / "phone.py"
GUARD_LINE = "    if action.risk is Risk.OUTBOUND:"
GUARD_BROKEN = "    if action.risk is not Risk.OUTBOUND:"


class Ink:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


def paint(text: str, *styles: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "".join(styles) + text + Ink.RESET


def banner(text: str) -> None:
    print()
    print(paint("─" * 46, Ink.DIM))
    print(paint(text, Ink.BOLD))
    print(paint("─" * 46, Ink.DIM))
    print()
    sys.stdout.flush()


def run(cmd: list[str], **kw) -> int:
    """Run a command, letting its output go straight to the terminal."""
    print(paint("$ " + " ".join(cmd), Ink.DIM))
    print()
    sys.stdout.flush()
    return subprocess.call(cmd, cwd=REPO, **kw)


# --------------------------------------------------------------- scenes


def scene_green() -> int:
    """Clip 3 — the whole suite passing. Your face lit green."""
    banner("CLIP 3  ·  everything passes")
    return run([sys.executable, "-m", "pytest", "tests/", "-q"])


def scene_red() -> int:
    """Clip 4 — break the outbound rule for real, watch the tests catch it."""
    banner("CLIP 4  ·  one rule removed")

    source = GUARD_FILE.read_text()
    if GUARD_LINE not in source:
        print(paint(
            f"Could not find the guard line in {GUARD_FILE.name}.\n"
            "The file has changed since this script was written — "
            "nothing was modified.", Ink.YELLOW))
        return 1

    print(paint("  Removing the rule that says a message to", Ink.YELLOW))
    print(paint("  another person must always be confirmed.", Ink.YELLOW))
    print()
    time.sleep(1.2)

    original = source
    try:
        GUARD_FILE.write_text(source.replace(GUARD_LINE, GUARD_BROKEN, 1))
        code = run([sys.executable, "-m", "pytest", "tests/", "-q",
                    "--no-header", "-x"])
    finally:
        # Restore no matter what happened — including Ctrl-C. A demo script
        # that can leave the safety rule inverted on disk is worse than no
        # demo script.
        GUARD_FILE.write_text(original)

    if GUARD_FILE.read_text() != original:
        print(paint("\n  WARNING: could not restore core/phone.py — "
                    "run: git checkout core/phone.py", Ink.RED, Ink.BOLD))
        return 2

    print()
    print(paint("  Rule restored. core/phone.py is back to normal.", Ink.GREEN))
    return code


def scene_boot() -> int:
    """Clip 5 — Jarvis actually starting, with the enrollment code."""
    banner("CLIP 5  ·  it starts")

    if not (REPO / "config.yaml").is_file():
        print(paint("  No config.yaml yet. Make one first:\n", Ink.YELLOW))
        print("    cp config.example.yaml config.yaml")
        print("    # set brain.provider to ollama")
        print("    # set server.auth_token to any 32+ char string")
        return 1

    print(paint("$ python3 run.py", Ink.DIM))
    print()
    sys.stdout.flush()

    proc = subprocess.Popen(
        [sys.executable, "-u", "run.py"],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            # Stop before Flask's dev-server noise — the banner is the shot.
            if "Serving Flask app" in line:
                break
            print(line, end="")
            sys.stdout.flush()
        print()
        print(paint("  Listening on your LAN. Nothing is on the internet.",
                    Ink.CYAN))
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


def scene_crash() -> int:
    """Clip 6 — a real failure, not a mocked one."""
    banner("CLIP 6  ·  four minutes later")

    print(paint("  Asking the brain to think, with nothing", Ink.YELLOW))
    print(paint("  listening on the other end.", Ink.YELLOW))
    print()
    time.sleep(1.2)

    # Deliberately unhandled: the traceback IS the shot.
    code = subprocess.call(
        [sys.executable, "-c",
         "from core.brain import build_brain\n"
         "brain = build_brain({'provider': 'ollama',\n"
         "                     'ollama': {'model': 'llama3.1:8b',\n"
         "                                'host': 'http://127.0.0.1:11434'}})\n"
         "brain.think('are you there?', [])\n"],
        cwd=REPO,
    )
    print()
    print(paint("  This is what 3am actually looks like.", Ink.DIM))
    return 0 if code != 0 else 1


def scene_blame() -> int:
    """Clip 8 — who wrote the line. Read the name carefully."""
    banner("CLIP 8  ·  git blame")
    code = run(["git", "blame", "-L", "240,250", "--", "core/phone.py"])
    print()
    print(paint("  Check whose name is in that column.", Ink.DIM))
    return code


def scene_history() -> int:
    """Clip 12 — the log, and how much of it there now is to maintain."""
    banner("CLIP 12  ·  and now I maintain it")
    code = run(["git", "log", "--oneline", "--graph", "--decorate", "-15"])
    print()
    total = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.strip()
    print(paint(f"  {total} commits. All of it mine to keep working.", Ink.DIM))
    return code


SCENES = {
    "green": (scene_green, "clip 3", "The whole suite passing"),
    "red": (scene_red, "clip 4", "A real rule broken, caught by real tests"),
    "boot": (scene_boot, "clip 5", "Jarvis starting, with the enrollment code"),
    "crash": (scene_crash, "clip 6", "A genuine traceback"),
    "blame": (scene_blame, "clip 8", "Who actually wrote the line"),
    "history": (scene_history, "clip 12", "The commit log"),
}


def scene_list() -> int:
    banner("Scenes")
    for name, (_, clip, desc) in SCENES.items():
        print(f"  {paint(name.ljust(9), Ink.CYAN)}{clip.ljust(9)}{desc}")
    print()
    print(paint("  Full screen, terminal font 20pt or larger.", Ink.DIM))
    print(paint("  Record 8-10s of each — you only need 2s.", Ink.DIM))
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", nargs="?", default="list",
                        choices=["list", *SCENES])
    args = parser.parse_args()

    if args.scene == "list":
        return scene_list()

    os.environ.setdefault("PYTHONPATH", str(REPO))
    return SCENES[args.scene][0]()


if __name__ == "__main__":
    raise SystemExit(main())
