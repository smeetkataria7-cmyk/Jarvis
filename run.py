#!/usr/bin/env python3
"""
Start Jarvis.

    python3 run.py

Everything is validated before the socket opens. A misconfiguration should
fail here with a sentence you can act on, rather than at 2am when you ask it
to set an alarm.
"""

from __future__ import annotations

import logging
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import yaml
from flask import Flask

from core.brain import BrainError, build_brain
from core.memory import Memory
from core.server import bp as core_bp
from guardian import protected
from guardian.server import bp as guardian_bp, generate_enrollment_code

CONFIG_PATH = REPO_ROOT / "config.yaml"


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        sys.exit(
            "No config.yaml found.\n\n"
            "  cp config.example.yaml config.yaml\n\n"
            "Then open it and fill in your API key and an auth_token."
        )

    try:
        config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except yaml.YAMLError as exc:
        sys.exit(f"config.yaml is not valid YAML:\n{exc}")

    token = config.get("server", {}).get("auth_token", "")
    if not token:
        suggestion = secrets.token_urlsafe(32)
        sys.exit(
            "server.auth_token is empty in config.yaml.\n\n"
            "Without it, anything on your WiFi could talk to Jarvis.\n"
            f"Here is one — paste it in:\n\n  {suggestion}\n"
        )

    if len(token) < 32:
        sys.exit("server.auth_token is too short — use at least 32 characters.")

    return config


def create_app(config: dict) -> Flask:
    app = Flask(__name__)
    app.config["JARVIS"] = config

    try:
        app.config["BRAIN"] = build_brain(config["brain"])
    except BrainError as exc:
        sys.exit(f"Could not set up the brain:\n  {exc}")

    app.config["MEMORY"] = Memory(REPO_ROOT / config["memory"]["path"])

    app.register_blueprint(core_bp)
    app.register_blueprint(guardian_bp)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # If the Guardian is damaged, refuse to start. Running the assistant
    # without a working approval gate is the one failure mode this whole
    # design exists to prevent.
    problems = protected.verify_integrity()
    if problems:
        sys.exit("Guardian integrity check failed:\n  " + "\n  ".join(problems))

    config = load_config()
    app = create_app(config)

    host = config["server"]["host"]
    port = config["server"]["port"]

    print()
    print("  JARVIS")
    print(f"  brain      {config['brain']['provider']}")
    print(f"  listening  http://{host}:{port}")
    print(f"  self-edit  {'on' if config['selfedit']['enabled'] else 'off'}")

    from guardian.auth import has_enrolled_device

    if not has_enrolled_device():
        code = generate_enrollment_code()
        print()
        print("  No phone enrolled yet. In the Jarvis app, enter:")
        print(f"      {code}")
        print("  This code works once, and only until you stop the server.")
    print()

    # debug=False deliberately. The Werkzeug debugger exposes an interactive
    # Python console to anyone who can reach a traceback, and this process is
    # listening on your LAN.
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
