"""
Tests for biometric approval verification.

The property under test throughout: a valid signature can only exist if a
fingerprint unlocked a hardware-backed key. Everything here is an attempt to
produce an accepted approval without that having happened.
"""

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from guardian import auth


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the device store at a temp file and clear challenge state."""
    monkeypatch.setattr(auth, "DEVICE_STORE", tmp_path / "devices.json")
    auth._challenges.clear()
    yield
    auth._challenges.clear()


@pytest.fixture
def keypair():
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private, pem


@pytest.fixture
def enrolled(keypair):
    private, pem = keypair
    auth.enroll_device("phone-1", "My Pixel", pem)
    return private


def sign(private, patch_id: str, nonce: str) -> bytes:
    message = f"{len(patch_id)}:{patch_id}|{len(nonce)}:{nonce}".encode()
    return private.sign(message, ec.ECDSA(hashes.SHA256()))


class TestBearerToken:
    def test_matching_token_passes(self):
        assert auth.verify_bearer_token("secret", "secret")

    def test_wrong_token_fails(self):
        assert not auth.verify_bearer_token("wrong", "secret")

    def test_empty_values_fail(self):
        assert not auth.verify_bearer_token("", "secret")
        assert not auth.verify_bearer_token("secret", "")
        assert not auth.verify_bearer_token(None, "secret")


class TestEnrollment:
    def test_enrolls_a_valid_key(self, keypair):
        _, pem = keypair
        device = auth.enroll_device("phone-1", "My Pixel", pem)
        assert device.device_id == "phone-1"
        assert auth.has_enrolled_device()

    def test_rejects_silent_re_enrollment(self, keypair):
        # The cleanest attack available: swap in your own key and inherit
        # every future approval. Replacing a device must be a physical act.
        _, pem = keypair
        auth.enroll_device("phone-1", "My Pixel", pem)
        with pytest.raises(auth.AuthError, match="already enrolled"):
            auth.enroll_device("phone-1", "Attacker", pem)

    def test_rejects_malformed_key(self):
        with pytest.raises(auth.AuthError, match="valid PEM"):
            auth.enroll_device("phone-1", "Bad", "not a key")

    def test_rejects_weak_rsa(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        pem = weak.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        with pytest.raises(auth.AuthError, match="too small"):
            auth.enroll_device("phone-1", "Weak", pem)


class TestApproval:
    def test_valid_signature_is_accepted(self, enrolled):
        nonce = auth.issue_challenge("patch-abc")
        auth.verify_approval("phone-1", "patch-abc", nonce, sign(enrolled, "patch-abc", nonce))

    def test_signature_from_an_unenrolled_key_is_rejected(self, enrolled):
        attacker = ec.generate_private_key(ec.SECP256R1())
        nonce = auth.issue_challenge("patch-abc")
        with pytest.raises(auth.AuthError, match="does not verify"):
            auth.verify_approval(
                "phone-1", "patch-abc", nonce, sign(attacker, "patch-abc", nonce)
            )

    def test_signature_cannot_be_replayed_onto_another_patch(self, enrolled):
        # You approved a one-line typo fix. That approval must not carry over
        # to a patch that rewrites the brain.
        nonce = auth.issue_challenge("harmless-patch")
        signature = sign(enrolled, "harmless-patch", nonce)
        with pytest.raises(auth.AuthError, match="different patch"):
            auth.verify_approval("phone-1", "dangerous-patch", nonce, signature)

    def test_a_nonce_works_only_once(self, enrolled):
        nonce = auth.issue_challenge("patch-abc")
        signature = sign(enrolled, "patch-abc", nonce)
        auth.verify_approval("phone-1", "patch-abc", nonce, signature)
        with pytest.raises(auth.AuthError, match="unknown or expired"):
            auth.verify_approval("phone-1", "patch-abc", nonce, signature)

    def test_failed_attempt_still_burns_the_nonce(self, enrolled):
        # Otherwise a bad signature costs nothing and can be retried forever.
        attacker = ec.generate_private_key(ec.SECP256R1())
        nonce = auth.issue_challenge("patch-abc")
        with pytest.raises(auth.AuthError):
            auth.verify_approval(
                "phone-1", "patch-abc", nonce, sign(attacker, "patch-abc", nonce)
            )
        with pytest.raises(auth.AuthError, match="unknown or expired"):
            auth.verify_approval(
                "phone-1", "patch-abc", nonce, sign(enrolled, "patch-abc", nonce)
            )

    def test_unknown_nonce_is_rejected(self, enrolled):
        with pytest.raises(auth.AuthError, match="unknown or expired"):
            auth.verify_approval("phone-1", "patch-abc", "made-up", b"\x00")

    def test_expired_challenge_is_rejected(self, enrolled, monkeypatch):
        nonce = auth.issue_challenge("patch-abc")
        signature = sign(enrolled, "patch-abc", nonce)
        # Age the challenge past its TTL.
        patch_id, issued_at = auth._challenges[nonce]
        auth._challenges[nonce] = (patch_id, issued_at - auth.CHALLENGE_TTL_SECONDS - 1)
        with pytest.raises(auth.AuthError):
            auth.verify_approval("phone-1", "patch-abc", nonce, signature)

    def test_unenrolled_device_is_rejected(self, enrolled):
        nonce = auth.issue_challenge("patch-abc")
        with pytest.raises(auth.AuthError, match="not enrolled"):
            auth.verify_approval(
                "unknown-phone", "patch-abc", nonce, sign(enrolled, "patch-abc", nonce)
            )

    def test_garbage_signature_is_rejected(self, enrolled):
        nonce = auth.issue_challenge("patch-abc")
        with pytest.raises(auth.AuthError):
            auth.verify_approval(
                "phone-1", "patch-abc", nonce, base64.b64decode("AAAA")
            )

    def test_length_prefix_prevents_boundary_confusion(self, enrolled):
        # Without length prefixes, ("ab","c") and ("a","bc") would produce the
        # same signing input, so one approval could satisfy two different
        # patch/nonce pairs.
        nonce = auth.issue_challenge("ab")
        signature = sign(enrolled, "ab", nonce)
        auth._challenges["c" + nonce[1:]] = ("a", auth.time.time())
        with pytest.raises(auth.AuthError):
            auth.verify_approval("phone-1", "a", "c" + nonce[1:], signature)
