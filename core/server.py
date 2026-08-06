"""
The main API — what your phone talks to when you speak to Jarvis.

The core loop is deliberately small:

    you speak
      -> brain turns it into a structured intent
      -> phone.evaluate decides if it runs, or needs confirming first
      -> the phone is told what to do, and does it
      -> the outcome is recorded, so failures accumulate into evidence

The confirmation step is the interesting one. Actions that reach another human
are always confirmed, and the confirmation is spoken back in full — contact
name and message both — because the failure this catches is the model hearing
the wrong name, and you only catch that if you hear the name.
"""

from __future__ import annotations

import logging
import time
import uuid
from functools import wraps
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from core import phone
from core.brain import BrainError
from core.selfedit import SelfEditError, propose_improvement
from guardian import auth as guardian_auth, patcher

log = logging.getLogger("jarvis.server")

bp = Blueprint("core", __name__, url_prefix="/api")

SYSTEM_PROMPT = """\
You are Jarvis, a personal assistant running privately on your user's own \
computer, reachable only from their phone on their home network.

Be brief. You are usually being listened to rather than read, and long replies \
are tiring out loud. Two sentences is generous; one is often enough.

Never invent facts about the user. If you don't know something about them, say \
so plainly and ask.
"""

# Actions awaiting a yes/no. In memory only — a restart cancels anything
# outstanding, which is the safe direction: we have no idea what happened
# while we were down, so we should not send a text on the strength of a
# confirmation given before a crash.
_pending: dict[str, dict[str, Any]] = {}

PENDING_TTL_SECONDS = 120


def _config() -> dict[str, Any]:
    return current_app.config["JARVIS"]


def _brain():
    return current_app.config["BRAIN"]


def _memory():
    return current_app.config["MEMORY"]


def require_token(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        presented = header[7:] if header.startswith("Bearer ") else None
        if not guardian_auth.verify_bearer_token(
            presented, _config()["server"]["auth_token"]
        ):
            return jsonify({"error": "unauthorised"}), 401
        return view(*args, **kwargs)

    return wrapper


def _expire_pending() -> None:
    cutoff = time.time() - PENDING_TTL_SECONDS
    for key in [k for k, v in _pending.items() if v["created_at"] < cutoff]:
        del _pending[key]


@bp.get("/health")
@require_token
def health():
    return jsonify({
        "ok": True,
        "brain": _config()["brain"]["provider"],
        "facts_known": len(_memory().all_facts()),
        "pending_confirmations": len(_pending),
    })


@bp.post("/say")
@require_token
def say():
    """The main entry point. Send what the user said; get back what to do."""
    body = request.get_json(silent=True) or {}
    utterance = (body.get("text") or "").strip()

    if not utterance:
        return jsonify({"error": "text is required"}), 400

    _expire_pending()
    memory = _memory()
    memory.remember_turn("user", utterance)

    try:
        intent = _brain().intent(
            utterance=utterance,
            actions=phone.catalogue(),
            context=memory.context_block(),
        )
    except BrainError as exc:
        log.error("brain failed: %s", exc)
        return jsonify({
            "speak": "My brain isn't reachable right now.",
            "action": "none",
            "error": str(exc),
        }), 503

    decision = phone.evaluate(intent)

    # Refused outright — nothing to do but explain.
    if not decision.allowed:
        memory.remember_turn("assistant", decision.reason)
        return jsonify({"action": "none", "speak": decision.reason, "execute": False})

    # Pure conversation. No phone action involved.
    if decision.action_name == "answer":
        reply = intent["params"].get("text", "")
        memory.remember_turn("assistant", reply)
        return jsonify({"action": "answer", "speak": reply, "execute": False})

    if decision.needs_confirmation:
        token = str(uuid.uuid4())
        _pending[token] = {"intent": intent, "created_at": time.time()}
        question = phone.describe_for_confirmation(intent)
        memory.remember_turn("assistant", question)
        return jsonify({
            "action": decision.action_name,
            "speak": question,
            "execute": False,
            "confirm_token": token,
            "why": decision.reason,
            "expires_in": PENDING_TTL_SECONDS,
        })

    memory.remember_turn("assistant", intent.get("speak", ""))
    return jsonify({
        "action": decision.action_name,
        "params": intent["params"],
        "speak": intent.get("speak", ""),
        "execute": True,
    })


@bp.post("/confirm")
@require_token
def confirm():
    """Answer a pending confirmation with yes or no."""
    body = request.get_json(silent=True) or {}
    token = body.get("confirm_token", "")
    approved = bool(body.get("approved", False))

    _expire_pending()
    entry = _pending.pop(token, None)

    if entry is None:
        return jsonify({
            "speak": "That expired — say it again if you still want it.",
            "execute": False,
        }), 409

    intent = entry["intent"]

    if not approved:
        _memory().record_episode(
            intent["action"], intent.get("params", {}), "cancelled"
        )
        return jsonify({"speak": "Cancelled.", "execute": False})

    return jsonify({
        "action": intent["action"],
        "params": intent.get("params", {}),
        "speak": intent.get("speak", "Done."),
        "execute": True,
    })


@bp.post("/outcome")
@require_token
def outcome():
    """The phone reports back whether an action actually worked.

    Without this the system is blind: it would know what it tried and never
    whether it landed. These records are what any honest self-improvement has
    to be built on.
    """
    body = request.get_json(silent=True) or {}
    action = body.get("action", "")
    result = body.get("outcome", "")

    if not action or result not in ("ok", "failed", "cancelled"):
        return jsonify({"error": "action and outcome (ok|failed|cancelled) required"}), 400

    _memory().record_episode(
        action=action,
        params=body.get("params", {}),
        outcome=result,
        detail=body.get("detail", ""),
    )
    return jsonify({"recorded": True})


@bp.post("/chat")
@require_token
def chat():
    """Plain conversation with no action extraction."""
    body = request.get_json(silent=True) or {}
    message = (body.get("text") or "").strip()

    if not message:
        return jsonify({"error": "text is required"}), 400

    memory = _memory()
    memory.remember_turn("user", message)

    system = SYSTEM_PROMPT
    facts = memory.context_block()
    if facts:
        system += f"\n\nWhat you know about your user:\n{facts}"

    try:
        reply = _brain().chat(
            messages=memory.recent_turns(_config()["memory"]["context_turns"]),
            system=system,
        )
    except BrainError as exc:
        return jsonify({"error": str(exc)}), 503

    memory.remember_turn("assistant", reply)
    return jsonify({"reply": reply})


@bp.post("/remember")
@require_token
def remember():
    """Teach Jarvis a durable fact.

    Confidence is 1.0 because you said it outright — that must outrank
    anything the model merely inferred about the same key.
    """
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()
    value = (body.get("value") or "").strip()

    if not key or not value:
        return jsonify({"error": "key and value are required"}), 400

    _memory().learn(key, value, source="told", confidence=1.0)
    return jsonify({"learned": True, "key": key})


@bp.get("/facts")
@require_token
def facts():
    return jsonify({"facts": _memory().all_facts()})


@bp.get("/reliability")
@require_token
def reliability():
    """Which actions keep failing — the evidence behind self-edit proposals."""
    return jsonify({"actions": _memory().failure_rates()})


@bp.post("/selfedit/consider")
@require_token
def consider_self_edit():
    """Look at the failure log and, if warranted, draft a patch.

    Nothing is applied here. A successful call queues a proposal that still has
    to pass its tests on a scratch branch and then be approved with your
    fingerprint. "nothing worth changing" is the common and correct outcome.
    """
    config = _config()

    if not config["selfedit"]["enabled"]:
        return jsonify({"error": "self-editing is disabled in config.yaml"}), 403

    try:
        proposal = propose_improvement(_memory(), _brain())
    except (SelfEditError, BrainError) as exc:
        return jsonify({"error": str(exc)}), 503

    if proposal is None:
        return jsonify({"proposed": False, "reason": "nothing worth changing"})

    try:
        patch = patcher.propose(
            summary=proposal["summary"],
            rationale=proposal["rationale"],
            diff=proposal["diff"],
            max_diff_lines=config["selfedit"]["max_diff_lines"],
        )
    except patcher.PatchError as exc:
        # Includes the protected-path refusal. Worth surfacing rather than
        # swallowing: a model repeatedly trying to patch guardian/ is something
        # you would want to know about.
        return jsonify({"proposed": False, "reason": str(exc)}), 400

    if config["selfedit"]["run_tests"]:
        passed = patcher.run_tests(patch, config["selfedit"]["test_command"])
        if not passed:
            return jsonify({
                "proposed": False,
                "reason": f"patch failed its tests ({patch.status})",
                "patch_id": patch.patch_id,
            })

    return jsonify({
        "proposed": True,
        "patch_id": patch.patch_id,
        "summary": patch.summary,
        "diff_lines": patch.diff_lines,
        "awaiting": "your approval on the phone",
    })
