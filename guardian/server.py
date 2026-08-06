"""
The approval API — the endpoints your phone talks to when Jarvis wants to
change itself.

Nothing here trusts the network. Every route requires the shared bearer token,
and the routes that actually change something additionally require a signature
that only your fingerprint could have produced.

Enrollment is the one genuinely delicate moment. Before any device is enrolled
there is no key to verify against, so for that one call we fall back to a code
printed on the terminal of the machine running Jarvis. That makes physical
access to your computer the root of trust, which is the right answer: someone
standing at your desk has already won, and someone who isn't can't enroll.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
from functools import wraps
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from . import auth, patcher, protected

log = logging.getLogger("jarvis.guardian.server")

bp = Blueprint("guardian", __name__, url_prefix="/guardian")

# One-time enrollment code, generated at startup and printed to the console.
_enrollment_code: str | None = None


def generate_enrollment_code() -> str:
    """Mint the code shown on the console at startup."""
    global _enrollment_code
    _enrollment_code = f"{secrets.randbelow(10**6):06d}"
    return _enrollment_code


def _config() -> dict[str, Any]:
    return current_app.config["JARVIS"]


def require_token(view):
    """Every route needs the shared token. No exceptions."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        presented = header[7:] if header.startswith("Bearer ") else None
        expected = _config()["server"]["auth_token"]

        if not auth.verify_bearer_token(presented, expected):
            log.warning("rejected request from %s: bad token", request.remote_addr)
            return jsonify({"error": "unauthorised"}), 401
        return view(*args, **kwargs)

    return wrapper


@bp.get("/health")
@require_token
def health():
    problems = protected.verify_integrity()
    return jsonify({
        "ok": not problems,
        "problems": problems,
        "enrolled_devices": len(auth.list_devices()),
        "awaiting_approval": len(
            patcher.list_patches(status=patcher.Status.AWAITING_APPROVAL.value)
        ),
        "head": patcher.current_commit()[:12],
        "branch": patcher.current_branch(),
        "clean": patcher.working_tree_clean(),
    })


@bp.post("/enroll")
@require_token
def enroll():
    """Register this phone's hardware-backed public key.

    Requires the one-time code from the server console. That code is single
    use — a successful enrollment burns it, so a second device cannot quietly
    join by replaying a code it overheard.
    """
    global _enrollment_code

    body = request.get_json(silent=True) or {}
    code = body.get("enrollment_code", "")
    device_id = body.get("device_id", "").strip()
    name = body.get("name", "").strip() or "unnamed device"
    public_key_pem = body.get("public_key_pem", "")

    if not _enrollment_code:
        return jsonify({"error": "enrollment is closed; restart Jarvis to reopen"}), 403

    if not secrets.compare_digest(str(code), _enrollment_code):
        log.warning("failed enrollment from %s: wrong code", request.remote_addr)
        return jsonify({"error": "wrong enrollment code"}), 403

    if not device_id or not public_key_pem:
        return jsonify({"error": "device_id and public_key_pem are required"}), 400

    try:
        device = auth.enroll_device(device_id, name, public_key_pem)
    except auth.AuthError as exc:
        return jsonify({"error": str(exc)}), 400

    # Burn the code. One enrollment per startup.
    _enrollment_code = None
    log.info("device enrolled: %s (%s)", device.name, device.device_id)

    return jsonify({"enrolled": True, "device_id": device.device_id})


@bp.get("/patches")
@require_token
def list_pending():
    """Patches that passed their tests and are waiting on you."""
    patcher.expire_stale(_config()["selfedit"]["approval_timeout_minutes"])

    pending = patcher.list_patches(status=patcher.Status.AWAITING_APPROVAL.value)
    return jsonify({
        "patches": [
            {
                "patch_id": p.patch_id,
                "summary": p.summary,
                "rationale": p.rationale,
                "files": p.files,
                "diff_lines": p.diff_lines,
                "created_at": p.created_at,
            }
            for p in pending
        ]
    })


@bp.get("/patch/<patch_id>")
@require_token
def patch_detail(patch_id: str):
    """The full diff. This is what you read before pressing your thumb down."""
    try:
        patch = patcher.load(patch_id)
    except patcher.PatchError as exc:
        return jsonify({"error": str(exc)}), 404

    return jsonify({
        "patch_id": patch.patch_id,
        "summary": patch.summary,
        "rationale": patch.rationale,
        "status": patch.status,
        "files": patch.files,
        "diff": patch.diff,
        "diff_lines": patch.diff_lines,
        "test_output": patch.test_output,
        "history": patch.history,
    })


@bp.post("/patch/<patch_id>/challenge")
@require_token
def challenge(patch_id: str):
    """Get a nonce to sign.

    The nonce is bound to this specific patch, so a signature collected for one
    change can never be replayed to approve a different one.
    """
    try:
        patch = patcher.load(patch_id)
    except patcher.PatchError as exc:
        return jsonify({"error": str(exc)}), 404

    if patch.status != patcher.Status.AWAITING_APPROVAL.value:
        return jsonify({"error": f"patch is {patch.status}"}), 409

    nonce = auth.issue_challenge(patch_id)
    return jsonify({
        "nonce": nonce,
        # Exactly what the phone must sign, so the two sides can never drift.
        "sign_this": f"{len(patch_id)}:{patch_id}|{len(nonce)}:{nonce}",
        "expires_in": auth.CHALLENGE_TTL_SECONDS,
    })


@bp.post("/patch/<patch_id>/approve")
@require_token
def approve(patch_id: str):
    """Verify the biometric signature, then apply the patch.

    If verification fails we stop here and nothing is touched. If it succeeds,
    patcher.apply_approved owns the rollback guarantee from that point on.
    """
    body = request.get_json(silent=True) or {}
    device_id = body.get("device_id", "")
    nonce = body.get("nonce", "")
    signature_b64 = body.get("signature", "")

    if not (device_id and nonce and signature_b64):
        return jsonify({"error": "device_id, nonce and signature are required"}), 400

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "signature is not valid base64"}), 400

    try:
        auth.verify_approval(device_id, patch_id, nonce, signature)
    except auth.AuthError as exc:
        log.warning("approval rejected for %s: %s", patch_id, exc)
        return jsonify({"error": str(exc)}), 403

    try:
        patch = patcher.load(patch_id)
        commit = patcher.apply_approved(patch, _config()["selfedit"]["test_command"])
    except patcher.PatchError as exc:
        return jsonify({"error": str(exc), "rolled_back": True}), 500

    return jsonify({"applied": True, "commit": commit[:12], "summary": patch.summary})


@bp.post("/patch/<patch_id>/reject")
@require_token
def reject(patch_id: str):
    """Decline a patch.

    No signature required — refusing a change can only ever reduce what Jarvis
    is allowed to do, so there is nothing here worth protecting against.
    """
    try:
        patch = patcher.load(patch_id)
    except patcher.PatchError as exc:
        return jsonify({"error": str(exc)}), 404

    reason = (request.get_json(silent=True) or {}).get("reason", "declined")
    patcher.reject(patch, reason)
    return jsonify({"rejected": True})
