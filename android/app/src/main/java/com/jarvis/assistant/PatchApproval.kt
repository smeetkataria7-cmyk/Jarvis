package com.jarvis.assistant

import androidx.biometric.BiometricPrompt
import androidx.fragment.app.FragmentActivity
import kotlinx.coroutines.suspendCancellableCoroutine
import java.util.concurrent.Executor
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * The approval flow, end to end.
 *
 * The ordering here is the whole point and is worth stating plainly: the diff
 * is fetched and shown *before* the fingerprint prompt appears. A prompt that
 * pops up first trains you to press your thumb down and read afterwards, and
 * at that point the gate has quietly become a formality. Read, then approve.
 */
class PatchApproval(
    private val activity: FragmentActivity,
    private val client: JarvisClient,
    private val deviceId: String,
) {

    sealed class Outcome {
        data class Applied(val commit: String) : Outcome()
        object Cancelled : Outcome()
        data class Failed(val reason: String) : Outcome()
    }

    private val executor: Executor
        get() = androidx.core.content.ContextCompat.getMainExecutor(activity)

    /**
     * Approve [patchId], assuming the user has already read the diff.
     *
     * Every failure path returns rather than throws, because the caller is a
     * UI that needs to say something useful either way.
     */
    suspend fun approve(patchId: String): Outcome {
        val nonce = try {
            client.challenge(patchId)
        } catch (e: JarvisClient.JarvisError) {
            return Outcome.Failed(e.message ?: "couldn't start approval")
        }

        // Primed but not yet usable: the Keystore will refuse to sign with it
        // until BiometricPrompt unlocks it. That refusal is enforced by the
        // secure element, not by this code.
        val signature = try {
            ApprovalKeystore.signatureForPrompt()
        } catch (e: Exception) {
            return Outcome.Failed(e.message ?: "no approval key on this device")
        }

        val result = try {
            promptForBiometric(signature)
        } catch (e: Exception) {
            return Outcome.Failed(e.message ?: "biometric check failed")
        } ?: return Outcome.Cancelled

        val signatureBase64 = try {
            ApprovalKeystore.finishSigning(result, patchId, nonce)
        } catch (e: Exception) {
            return Outcome.Failed(e.message ?: "couldn't sign the approval")
        }

        return try {
            val commit = client.approve(patchId, deviceId, nonce, signatureBase64)
            Outcome.Applied(commit)
        } catch (e: JarvisClient.JarvisError) {
            // The server rolls itself back on any failure during apply, so
            // there is nothing to undo from here.
            Outcome.Failed(e.message ?: "the server refused the approval")
        }
    }

    /**
     * Show the fingerprint prompt. Null means the user cancelled.
     */
    private suspend fun promptForBiometric(
        signature: java.security.Signature,
    ): BiometricPrompt.AuthenticationResult? = suspendCancellableCoroutine { continuation ->

        val callback = object : BiometricPrompt.AuthenticationCallback() {

            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                if (continuation.isActive) continuation.resume(result)
            }

            override fun onAuthenticationError(code: Int, message: CharSequence) {
                if (!continuation.isActive) return

                when (code) {
                    BiometricPrompt.ERROR_USER_CANCELED,
                    BiometricPrompt.ERROR_NEGATIVE_BUTTON,
                    BiometricPrompt.ERROR_CANCELED -> continuation.resume(null)

                    BiometricPrompt.ERROR_LOCKOUT,
                    BiometricPrompt.ERROR_LOCKOUT_PERMANENT ->
                        continuation.resumeWithException(
                            Exception("Too many failed attempts — unlock your phone and retry.")
                        )

                    else -> continuation.resumeWithException(Exception(message.toString()))
                }
            }

            // Deliberately not resuming: a single unrecognised finger is not a
            // failure, it is a smudge. The prompt stays up and lets them retry.
            override fun onAuthenticationFailed() = Unit
        }

        val prompt = BiometricPrompt(activity, executor, callback)

        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(activity.getString(R.string.biometric_prompt_title))
            .setSubtitle(activity.getString(R.string.approve_patch_subtitle))
            .setNegativeButtonText(activity.getString(R.string.biometric_prompt_negative))

            // Strong biometrics only. Device credential is excluded on
            // purpose — a PIN can be watched over your shoulder, and this
            // approves code running on your own machine.
            .setAllowedAuthenticators(androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_STRONG)
            .setConfirmationRequired(true)
            .build()

        prompt.authenticate(info, BiometricPrompt.CryptoObject(signature))

        continuation.invokeOnCancellation { prompt.cancelAuthentication() }
    }
}
