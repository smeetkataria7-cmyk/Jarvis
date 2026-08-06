"""
Proving that you — a human with your finger on the sensor — approved a patch.

The obvious design is for the phone to run its fingerprint check and then POST
{"approved": true}. That proves nothing. Any process on your WiFi could send
the same bytes, and so could Jarvis itself once it learned the endpoint.

So the approval is not a message, it is a signature.

At enrollment the phone generates a keypair inside the Android hardware
Keystore with setUserAuthenticationRequired(true). That flag means the private
key is physically unusable until a fingerprint or face unlock succeeds — the
secure element refuses to sign, and no amount of app-level code can talk it
round. The private key never leaves the phone and cannot be exported even by a
rooted OS.

The phone sends us only the public key. Later, to approve patch X, it signs a
challenge we issued. If the signature verifies, then a biometric check
happened, because there is no other way those bytes could exist.

That turns "trust the phone's word" into "trust the phone's hardware," which is
a much better thing to be trusting.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

log = logging.getLogger("jarvis.guardian.auth")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEVICE_STORE = REPO_ROOT / "data" / "devices.json"

# A challenge is single-use and short-lived. Long enough to read a diff on a
# phone screen and press your thumb to it; short enough that a captured
# signature is worthless by the time anyone could reuse it.
CHALLENGE_TTL_SECONDS = 300


class AuthError(Exception):
    """Raised when authentication or approval verification fails."""


# --------------------------------------------------------------------------
# Transport-level auth: the shared token every request must carry
# --------------------------------------------------------------------------

def verify_bearer_token(presented: str | None, expected: str) -> bool:
    """Constant-time check of the shared API token.

    This is the outer fence — it keeps anything that isn't your phone from
    reaching the API at all. It is emphatically *not* what authorises a patch;
    a stolen token still cannot forge a signature. Compared with compare_digest
    so a timing side channel can't be used to recover the token byte by byte.
    """
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


# --------------------------------------------------------------------------
# Device enrollment
# --------------------------------------------------------------------------

@dataclass
class Device:
    device_id: str
    name: str
    public_key_pem: str
    enrolled_at: float

    def load_public_key(self):
        return serialization.load_pem_public_key(self.public_key_pem.encode())


def _read_devices() -> dict[str, Device]:
    if not DEVICE_STORE.is_file():
        return {}
    try:
        raw = json.loads(DEVICE_STORE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthError(f"device store unreadable: {exc}") from exc
    return {k: Device(**v) for k, v in raw.items()}


def _write_devices(devices: dict[str, Device]) -> None:
    DEVICE_STORE.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: asdict(v) for k, v in devices.items()}
    # Write-then-rename so a crash mid-write can't leave us with a truncated
    # store and therefore no way to approve anything.
    tmp = DEVICE_STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(DEVICE_STORE)
    DEVICE_STORE.chmod(0o600)


def enroll_device(device_id: str, name: str, public_key_pem: str) -> Device:
    """Register a phone's public key.

    Deliberately refuses to overwrite an existing enrollment. Silent re-enroll
    would be the cleanest possible attack: swap in your own key and every
    future approval is yours to give. Replacing a device is a manual,
    physically-present act — delete the entry from data/devices.json yourself.
    """
    devices = _read_devices()

    if device_id in devices:
        raise AuthError(
            f"device {device_id!r} is already enrolled. To replace it, remove "
            f"its entry from {DEVICE_STORE} by hand."
        )

    # Validate the key before storing, so a malformed enrollment fails now
    # rather than at the moment you actually need to approve something.
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
    except (ValueError, TypeError) as exc:
        raise AuthError(f"not a valid PEM public key: {exc}") from exc

    if isinstance(key, ec.EllipticCurvePublicKey):
        if key.curve.name != "secp256r1":
            raise AuthError(f"unsupported curve {key.curve.name}; use P-256")
    elif isinstance(key, rsa.RSAPublicKey):
        if key.key_size < 2048:
            raise AuthError(f"RSA key too small ({key.key_size} bits); need 2048+")
    else:
        raise AuthError("public key must be EC P-256 or RSA-2048+")

    device = Device(
        device_id=device_id,
        name=name,
        public_key_pem=public_key_pem,
        enrolled_at=time.time(),
    )
    devices[device_id] = device
    _write_devices(devices)
    log.info("enrolled device %s (%s)", device_id, name)
    return device


def list_devices() -> list[Device]:
    return list(_read_devices().values())


def has_enrolled_device() -> bool:
    return bool(_read_devices())


# --------------------------------------------------------------------------
# Challenge / response
# --------------------------------------------------------------------------

# In-memory only. A restart invalidates every outstanding challenge, which is
# the correct behaviour — we have no idea what happened while we were down.
_challenges: dict[str, tuple[str, float]] = {}


def issue_challenge(patch_id: str) -> str:
    """Mint a single-use nonce binding an approval to one specific patch."""
    nonce = secrets.token_urlsafe(32)
    _challenges[nonce] = (patch_id, time.time())
    _expire_challenges()
    return nonce


def _expire_challenges() -> None:
    cutoff = time.time() - CHALLENGE_TTL_SECONDS
    for nonce in [n for n, (_, ts) in _challenges.items() if ts < cutoff]:
        del _challenges[nonce]


def verify_approval(
    device_id: str,
    patch_id: str,
    nonce: str,
    signature: bytes,
) -> None:
    """Verify that an enrolled device biometrically approved this exact patch.

    Returns None on success and raises AuthError on any failure. There is no
    boolean return by design — a caller that forgets to check a truthy result
    would fail open, and this is the one function in the system where failing
    open means the gate is gone.
    """
    _expire_challenges()

    issued = _challenges.get(nonce)
    if issued is None:
        raise AuthError("unknown or expired challenge")

    issued_patch_id, issued_at = issued

    # Burn the nonce before verifying, not after. If verification throws, the
    # nonce is still spent — otherwise a failed attempt would leave it live for
    # unlimited retries.
    del _challenges[nonce]

    if time.time() - issued_at > CHALLENGE_TTL_SECONDS:
        raise AuthError("challenge expired")

    # The nonce was issued for one patch. Approving diff A must never be
    # replayable as approval of diff B.
    if not hmac.compare_digest(issued_patch_id, patch_id):
        raise AuthError("challenge was issued for a different patch")

    devices = _read_devices()
    device = devices.get(device_id)
    if device is None:
        raise AuthError(f"device {device_id!r} is not enrolled")

    # Sign over both values, length-prefixed. Plain concatenation would let
    # ("ab", "c") and ("a", "bc") produce identical signing input.
    message = f"{len(patch_id)}:{patch_id}|{len(nonce)}:{nonce}".encode()

    public_key = device.load_public_key()
    try:
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        else:
            public_key.verify(
                signature,
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
    except InvalidSignature as exc:
        raise AuthError("signature does not verify") from exc

    log.info("approval verified: patch=%s device=%s", patch_id, device_id)
