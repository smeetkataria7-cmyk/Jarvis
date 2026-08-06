package com.jarvis.assistant

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.provider.Settings as AndroidSettings
import android.view.inputmethod.EditorInfo
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.jarvis.assistant.databinding.ActivityMainBinding
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * The one screen.
 *
 * The conversation loop it runs is deliberately linear:
 *
 *     listen -> ask the server -> maybe confirm -> execute -> report back
 *
 * The last step is the one that is easy to leave out and shouldn't be. Without
 * it Jarvis knows what it attempted and never whether it worked, which means it
 * can never notice that tapping a particular app fails nine times in ten — and
 * that record is the only honest basis it has for proposing a fix to itself.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var settings: Settings
    private lateinit var voice: VoiceEngine
    private lateinit var executor: ActionExecutor

    private var client: JarvisClient? = null

    private val requestPermissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        val denied = granted.filterValues { !it }.keys
        if (denied.isNotEmpty()) {
            // Name what stops working rather than nagging. Refusing SMS
            // permission is a legitimate choice, and the assistant is still
            // useful without it.
            say("Some things won't work without those permissions. That's fine — ask anyway and I'll tell you.")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        settings = Settings(this)
        voice = VoiceEngine(this)
        executor = ActionExecutor(this, voice)

        lifecycleScope.launch { voice.initialise() }

        binding.micButton.setOnClickListener { startListening() }

        binding.textInput.setOnEditorActionListener { view, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                val text = view.text.toString().trim()
                if (text.isNotEmpty()) {
                    view.text = null
                    handle(text)
                }
                true
            } else {
                false
            }
        }

        if (settings.isPaired) {
            connect()
        } else {
            showPairingDialog()
        }
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
        if (settings.isPaired) checkForPendingPatches()
    }

    override fun onDestroy() {
        voice.shutdown()
        super.onDestroy()
    }

    // ---------------------------------------------------------------------
    // Pairing
    // ---------------------------------------------------------------------

    private fun showPairingDialog() {
        val view = layoutInflater.inflate(R.layout.dialog_pairing, null)
        val urlField = view.findViewById<android.widget.EditText>(R.id.serverUrl)
        val tokenField = view.findViewById<android.widget.EditText>(R.id.authToken)
        val codeField = view.findViewById<android.widget.EditText>(R.id.enrollmentCode)

        urlField.setText("http://192.168.1.")

        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.enrollment_title)
            .setMessage(R.string.enrollment_hint)
            .setView(view)
            .setCancelable(false)
            .setPositiveButton(R.string.pair) { _, _ ->
                pair(
                    url = urlField.text.toString().trim(),
                    token = tokenField.text.toString().trim(),
                    code = codeField.text.toString().trim(),
                )
            }
            .show()
    }

    private fun pair(url: String, token: String, code: String) {
        if (url.isBlank() || token.isBlank() || code.isBlank()) {
            showError("I need all three: address, token, and the 6-digit code.")
            showPairingDialog()
            return
        }

        lifecycleScope.launch {
            try {
                // Generate the hardware-backed key first. If this phone has no
                // secure element or no fingerprint enrolled, we want to fail
                // here rather than after registering a device that can never
                // approve anything.
                val publicKey = ApprovalKeystore.createKey()

                val candidate = JarvisClient(url, token)
                candidate.enroll(
                    enrollmentCode = code,
                    deviceId = settings.deviceId,
                    name = settings.deviceName,
                    publicKeyPem = publicKey,
                )

                settings.serverUrl = url
                settings.authToken = token
                client = candidate

                append("Jarvis", "Paired. Say something.")
                refreshStatus()
                requestNeededPermissions()
                promptForAccessibilityIfOff()

            } catch (e: Exception) {
                // Roll back the key so a retry isn't blocked by the
                // already-exists guard in createKey.
                runCatching { ApprovalKeystore.deleteKey() }
                showError(e.message ?: "Pairing failed.")
                showPairingDialog()
            }
        }
    }

    private fun connect() {
        client = JarvisClient(settings.serverUrl, settings.authToken)
        requestNeededPermissions()
    }

    private fun requestNeededPermissions() {
        val wanted = listOf(
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.READ_CONTACTS,
            Manifest.permission.SEND_SMS,
            Manifest.permission.CALL_PHONE,
        ).filter {
            ContextCompat.checkSelfPermission(this, it) !=
                android.content.pm.PackageManager.PERMISSION_GRANTED
        }

        if (wanted.isNotEmpty()) requestPermissions.launch(wanted.toTypedArray())
    }

    private fun promptForAccessibilityIfOff() {
        if (JarvisAccessibilityService.isRunning()) return

        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.enable_phone_control)
            .setMessage(R.string.enable_phone_control_body)
            .setPositiveButton(R.string.open_settings) { _, _ ->
                startActivity(Intent(AndroidSettings.ACTION_ACCESSIBILITY_SETTINGS))
            }
            .setNegativeButton(R.string.not_now, null)
            .show()
    }

    // ---------------------------------------------------------------------
    // The conversation loop
    // ---------------------------------------------------------------------

    private fun startListening() {
        if (client == null) {
            showPairingDialog()
            return
        }

        lifecycleScope.launch {
            binding.micButton.isEnabled = false
            binding.statusLine.text = getString(R.string.listening)

            when (val heard = voice.listen()) {
                is VoiceEngine.Heard.Text -> {
                    append("You", heard.value)
                    handle(heard.value)
                }
                is VoiceEngine.Heard.Silence -> {
                    refreshStatus()
                }
                is VoiceEngine.Heard.Failed -> {
                    say(heard.reason)
                    refreshStatus()
                }
            }

            binding.micButton.isEnabled = true
        }
    }

    private fun handle(utterance: String) {
        val active = client ?: return

        lifecycleScope.launch {
            binding.statusLine.text = getString(R.string.thinking)

            try {
                val reply = active.say(utterance)
                append("Jarvis", reply.speak)

                when {
                    // Something that reaches another person, or that the model
                    // wasn't sure about. Speak the confirmation, then ask.
                    reply.confirmToken != null -> {
                        say(reply.speak)
                        askToConfirm(reply.confirmToken, reply.speak)
                    }

                    reply.execute -> {
                        say(reply.speak)
                        run(reply.action, reply.params)
                    }

                    else -> say(reply.speak)
                }
            } catch (e: JarvisClient.JarvisError) {
                append("Jarvis", e.message.orEmpty())
                say(e.message.orEmpty())
            } finally {
                refreshStatus()
            }
        }
    }

    /**
     * Confirmation is a dialog, not a spoken yes/no.
     *
     * Voice confirmation would be the more elegant flow and is the wrong call
     * here: the recogniser mishearing is exactly the failure this step exists
     * to catch, so routing the confirmation back through it re-introduces the
     * risk it was meant to remove. A tap is unambiguous.
     */
    private fun askToConfirm(token: String, question: String) {
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.confirm_title)
            .setMessage(question)
            .setPositiveButton(R.string.yes) { _, _ -> resolveConfirmation(token, true) }
            .setNegativeButton(R.string.no) { _, _ -> resolveConfirmation(token, false) }
            .setCancelable(false)
            .show()
    }

    private fun resolveConfirmation(token: String, approved: Boolean) {
        val active = client ?: return

        lifecycleScope.launch {
            try {
                val reply = active.confirm(token, approved)
                append("Jarvis", reply.speak)
                say(reply.speak)
                if (reply.execute) run(reply.action, reply.params)
            } catch (e: JarvisClient.JarvisError) {
                say(e.message.orEmpty())
            }
        }
    }

    private suspend fun run(action: String, params: JSONObject) {
        val outcome = executor.execute(action, params)

        append("Jarvis", outcome.speak)
        say(outcome.speak)

        // Report back even when it worked. Success rates are only meaningful
        // if successes are recorded too.
        runCatching {
            client?.reportOutcome(
                action = action,
                outcome = if (outcome.ok) "ok" else "failed",
                detail = outcome.detail,
            )
        }
    }

    // ---------------------------------------------------------------------
    // Self-edit approvals
    // ---------------------------------------------------------------------

    private fun checkForPendingPatches() {
        val active = client ?: return

        lifecycleScope.launch {
            val pending = runCatching { active.pendingPatches() }.getOrNull().orEmpty()

            if (pending.isEmpty()) {
                binding.patchBanner.visibility = android.view.View.GONE
                return@launch
            }

            val patch = pending.first()
            binding.patchBanner.visibility = android.view.View.VISIBLE
            binding.patchSummary.text = patch.summary
            binding.reviewPatchButton.setOnClickListener { reviewPatch(patch) }
        }
    }

    /**
     * Show the diff, then offer the fingerprint — in that order, always.
     *
     * A prompt that appears before the diff trains you to approve first and
     * read afterwards, and at that point the gate has quietly become a
     * formality. The whole design is worth less than nothing if this ordering
     * ever flips, because it would still look secure.
     */
    private fun reviewPatch(patch: JarvisClient.PendingPatch) {
        val active = client ?: return

        lifecycleScope.launch {
            val diff = runCatching { active.patchDiff(patch.patchId) }
                .getOrElse {
                    showError("Couldn't load the change.")
                    return@launch
                }

            val body = buildString {
                appendLine(patch.summary)
                appendLine()
                if (patch.rationale.isNotBlank()) {
                    appendLine(patch.rationale)
                    appendLine()
                }
                appendLine("${patch.diffLines} lines changed:")
                appendLine()
                append(diff)
            }

            AlertDialog.Builder(this@MainActivity)
                .setTitle(R.string.approve_patch_title)
                .setMessage(body)
                .setPositiveButton(R.string.approve) { _, _ -> approvePatch(patch) }
                .setNegativeButton(R.string.reject) { _, _ ->
                    lifecycleScope.launch {
                        runCatching { active.reject(patch.patchId) }
                        checkForPendingPatches()
                    }
                }
                .setNeutralButton(R.string.later, null)
                .show()
        }
    }

    private fun approvePatch(patch: JarvisClient.PendingPatch) {
        val active = client ?: return

        lifecycleScope.launch {
            val approval = PatchApproval(this@MainActivity, active, settings.deviceId)

            when (val outcome = approval.approve(patch.patchId)) {
                is PatchApproval.Outcome.Applied -> {
                    append("Jarvis", "Change applied as ${outcome.commit}.")
                    say("Done. I've updated myself.")
                }
                is PatchApproval.Outcome.Cancelled -> Unit
                is PatchApproval.Outcome.Failed -> showError(outcome.reason)
            }

            checkForPendingPatches()
        }
    }

    // ---------------------------------------------------------------------
    // UI plumbing
    // ---------------------------------------------------------------------

    private fun append(speaker: String, text: String) {
        if (text.isBlank()) return
        binding.transcript.append("$speaker: $text\n\n")
        binding.transcriptScroll.post {
            binding.transcriptScroll.fullScroll(android.view.View.FOCUS_DOWN)
        }
    }

    private fun say(text: String) {
        if (text.isBlank()) return
        lifecycleScope.launch { voice.speakBlocking(text) }
    }

    private fun refreshStatus() {
        val control =
            if (JarvisAccessibilityService.isRunning()) R.string.control_on
            else R.string.control_off

        binding.statusLine.text = getString(
            R.string.status_line,
            if (settings.isPaired) settings.serverUrl else getString(R.string.not_paired),
            getString(control),
        )
    }

    private fun showError(message: String) {
        MaterialAlertDialogBuilder(this)
            .setMessage(message)
            .setPositiveButton(android.R.string.ok, null)
            .show()
    }
}
