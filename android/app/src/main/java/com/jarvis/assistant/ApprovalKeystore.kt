package com.jarvis.assistant

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.biometric.BiometricPrompt
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * The phone half of the approval gate.
 *
 * The whole design rests on one property: this app cannot approve a patch on
 * its own. Not "will not" — cannot. The signing key is generated inside the
 * hardware-backed Android Keystore with setUserAuthenticationRequired(true),
 * which means the secure element itself refuses to perform a signature until a
 * biometric check has succeeded. That refusal happens below the operating
 * system. Code running here, however compromised, cannot talk it round.
 *
 * So when the server verifies a signature, it isn't trusting this app's word
 * that a fingerprint happened. It is observing bytes that could not otherwise
 * exist.
 *
 * The private key never leaves the secure element and cannot be exported, even
 * on a rooted device.
 */
object ApprovalKeystore {

    private const val KEYSTORE = "AndroidKeyStore"
    private const val KEY_ALIAS = "jarvis_patch_approval"
    private const val SIGNATURE_ALGORITHM = "SHA256withECDSA"

    class KeystoreError(message: String, cause: Throwable? = null) :
        Exception(message, cause)

    private fun keystore(): KeyStore =
        KeyStore.getInstance(KEYSTORE).apply { load(null) }

    fun hasKey(): Boolean = try {
        keystore().containsAlias(KEY_ALIAS)
    } catch (e: Exception) {
        false
    }

    /**
     * Create the approval keypair. Called once, during enrollment.
     *
     * Returns the public key in PEM form, which is the only part that ever
     * leaves the phone.
     */
    fun createKey(): String {
        if (hasKey()) {
            throw KeystoreError(
                "An approval key already exists. Delete it before enrolling again — " +
                    "silently replacing it would hand approval rights to whoever asked."
            )
        }

        val generator = KeyPairGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_EC,
            KEYSTORE,
        )

        val spec = KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)

            // The load-bearing flag. Without it the key is usable by any code
            // in this process and the fingerprint prompt becomes decoration.
            .setUserAuthenticationRequired(true)

            // Authenticate per signature, with a strong biometric only.
            // Timeout 0 means "no validity window" — a fingerprint given for
            // one approval does not silently authorise the next one.
            .setUserAuthenticationParameters(
                0,
                KeyProperties.AUTH_BIOMETRIC_STRONG,
            )

            // If someone adds a new fingerprint to this phone, destroy the key.
            // Otherwise an attacker who can enrol their own finger — anyone who
            // knows the unlock PIN — inherits the ability to approve patches.
            // Losing the key is a recoverable annoyance; this is not.
            .setInvalidatedByBiometricEnrollment(true)

            .build()

        generator.initialize(spec)
        val keyPair = generator.generateKeyPair()

        return toPem(keyPair.public.encoded)
    }

    /** Wrap DER bytes into the PEM the server's cryptography library expects. */
    private fun toPem(der: ByteArray): String {
        val body = Base64.encodeToString(der, Base64.NO_WRAP)
            .chunked(64)
            .joinToString("\n")
        return "-----BEGIN PUBLIC KEY-----\n$body\n-----END PUBLIC KEY-----\n"
    }

    /**
     * Build a Signature object primed for the fingerprint prompt.
     *
     * This deliberately does not sign anything. It returns an uninitialised
     * operation to hand to BiometricPrompt, which only unlocks it if the
     * fingerprint check passes — which is what binds the biometric to this
     * specific signature rather than to a general "user was here recently".
     */
    fun signatureForPrompt(): Signature {
        val entry = keystore().getEntry(KEY_ALIAS, null) as? KeyStore.PrivateKeyEntry
            ?: throw KeystoreError("No approval key found — enrol this device first.")

        return Signature.getInstance(SIGNATURE_ALGORITHM).apply {
            initSign(entry.privateKey)
        }
    }

    /**
     * Exactly the bytes the server will verify.
     *
     * Length-prefixed and mirrored character for character in guardian/auth.py.
     * Plain concatenation would let ("ab","c") and ("a","bc") hash identically,
     * so one approval could satisfy two different patch/nonce pairs. If you
     * change this, change it on both sides in the same commit.
     */
    fun messageToSign(patchId: String, nonce: String): ByteArray =
        "${patchId.length}:$patchId|${nonce.length}:$nonce".toByteArray()

    /**
     * Complete a signature after the fingerprint prompt has unlocked it.
     *
     * The Signature must be the one BiometricPrompt handed back in its
     * CryptoObject. Passing any other instance means the biometric was never
     * bound to this operation, so it is refused rather than quietly signed.
     */
    fun finishSigning(
        result: BiometricPrompt.AuthenticationResult,
        patchId: String,
        nonce: String,
    ): String {
        val signature = result.cryptoObject?.signature
            ?: throw KeystoreError(
                "Biometric result carried no crypto object — refusing to sign."
            )

        signature.update(messageToSign(patchId, nonce))
        return Base64.encodeToString(signature.sign(), Base64.NO_WRAP)
    }

    /** Remove the key. The user must re-enrol afterwards. */
    fun deleteKey() {
        try {
            keystore().deleteEntry(KEY_ALIAS)
        } catch (e: Exception) {
            throw KeystoreError("Could not delete the approval key", e)
        }
    }
}
