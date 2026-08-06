package com.jarvis.assistant

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.camera2.CameraManager
import android.media.AudioManager
import android.net.Uri
import android.provider.AlarmClock
import android.provider.MediaStore
import android.telephony.SmsManager
import android.util.Log
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import org.json.JSONObject

/**
 * Where an intent becomes something that actually happens.
 *
 * The server decides *what* to do and whether to ask first; this decides *how*,
 * on this particular phone, with these particular apps installed.
 *
 * Two rules run through all of it. Prefer a real Android intent over driving
 * the UI through the accessibility service — intents are atomic and either work
 * or don't, while UI automation is a sequence of taps that can half-succeed and
 * leave a message sitting in a text box. And when something cannot be done,
 * say which thing and why, because "failed" tells the user nothing and teaches
 * Jarvis nothing.
 */
class ActionExecutor(
    private val context: Context,
    private val voice: VoiceEngine,
) {

    companion object {
        private const val TAG = "JarvisExec"
    }

    private val contacts = ContactResolver(context)

    data class Outcome(
        val ok: Boolean,
        val speak: String,
        val detail: String = "",
    )

    private fun failed(speak: String, detail: String = speak) =
        Outcome(ok = false, speak = speak, detail = detail)

    private val a11y: JarvisAccessibilityService?
        get() = JarvisAccessibilityService.instance

    private fun requireA11y(): JarvisAccessibilityService? = a11y

    private fun has(permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) ==
            PackageManager.PERMISSION_GRANTED

    suspend fun execute(action: String, params: JSONObject): Outcome {
        Log.i(TAG, "executing $action")

        return try {
            when (action) {
                "send_sms" -> sendSms(params)
                "place_call" -> placeCall(params)
                "call_and_speak" -> callAndSpeak(params)
                "whatsapp_message" -> whatsappMessage(params)
                "whatsapp_voice_note" -> whatsappVoiceNote(params)
                "open_app" -> openApp(params)
                "set_alarm" -> setAlarm(params)
                "set_timer" -> setTimer(params)
                "toggle_setting" -> toggleSetting(params)
                "navigate" -> navigate(params)
                "play_music" -> playMusic(params)
                "ui_tap" -> uiTap(params)
                "ui_type" -> uiType(params)
                "read_screen" -> readScreen()
                else -> failed("I don't know how to do $action.")
            }
        } catch (e: SecurityException) {
            failed("I'm missing a permission for that.", e.message.orEmpty())
        } catch (e: Exception) {
            Log.e(TAG, "$action threw", e)
            failed("That didn't work: ${e.message}", e.toString())
        }
    }

    // ---------------------------------------------------------------------
    // Reaching other people
    // ---------------------------------------------------------------------

    /**
     * Resolve a contact, or produce the outcome explaining why we won't guess.
     */
    private fun resolveOrExplain(name: String): Pair<ContactResolver.Contact?, Outcome?> =
        when (val resolution = contacts.resolve(name)) {
            is ContactResolver.Resolution.Found ->
                resolution.contact to null

            is ContactResolver.Resolution.Ambiguous -> {
                val names = resolution.candidates.joinToString(", ") { it.name }
                null to failed("I found several: $names. Which one?")
            }

            is ContactResolver.Resolution.NotFound ->
                null to failed("I couldn't find $name in your contacts.")

            is ContactResolver.Resolution.Denied ->
                null to failed(resolution.reason)
        }

    private fun sendSms(params: JSONObject): Outcome {
        val name = params.optString("contact")
        val message = params.optString("message")

        if (!has(Manifest.permission.SEND_SMS)) {
            return failed("I don't have permission to send texts.")
        }

        val (contact, problem) = resolveOrExplain(name)
        if (contact == null) return problem!!

        val sms = context.getSystemService(SmsManager::class.java)

        // Long messages have to be split; sendTextMessage silently truncates
        // past the single-part limit, which loses the end of what you said.
        val parts = sms.divideMessage(message)
        if (parts.size > 1) {
            sms.sendMultipartTextMessage(contact.number, null, parts, null, null)
        } else {
            sms.sendTextMessage(contact.number, null, message, null, null)
        }

        return Outcome(true, "Sent to ${contact.name}.")
    }

    private fun placeCall(params: JSONObject): Outcome {
        val name = params.optString("contact")

        if (!has(Manifest.permission.CALL_PHONE)) {
            return failed("I don't have permission to make calls.")
        }

        val (contact, problem) = resolveOrExplain(name)
        if (contact == null) return problem!!

        val intent = Intent(Intent.ACTION_CALL).apply {
            data = Uri.fromParts("tel", contact.number, null)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)

        return Outcome(true, "Calling ${contact.name}.")
    }

    /**
     * Dial, put it on speakerphone, and read the message into the microphone.
     *
     * This exists because it is the closest thing possible to "call mum and
     * tell her I got here safely", and it does genuinely transmit. It is also
     * unmistakably a workaround: Android closes the call audio uplink to apps
     * so malware cannot impersonate you to your bank, so the only route left is
     * to play sound out of the loudspeaker and let the microphone pick it up.
     *
     * The result is echoey and robotic, and the recipient will ask what is
     * wrong with your phone. Prefer send_sms unless the user insisted.
     */
    private suspend fun callAndSpeak(params: JSONObject): Outcome {
        val message = params.optString("message")
        val placed = placeCall(params)
        if (!placed.ok) return placed

        val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

        // No way to detect pickup without READ_CALL_LOG and a listener, so we
        // wait a fixed interval and accept that a slow answer clips the start.
        // Better than speaking over the ringtone.
        delay(6_000)

        @Suppress("DEPRECATION")
        audio.isSpeakerphoneOn = true
        delay(500)

        voice.speakBlocking(message)

        return Outcome(
            true,
            "Said it on speakerphone. It won't have sounded great.",
            detail = "call_and_speak used; audio quality is inherently poor",
        )
    }

    private suspend fun whatsappMessage(params: JSONObject): Outcome {
        val name = params.optString("contact")
        val message = params.optString("message")

        val (contact, problem) = resolveOrExplain(name)
        if (contact == null) return problem!!

        val number = contact.number.filter { it.isDigit() || it == '+' }

        // Opens the chat with the text pre-filled. WhatsApp deliberately gives
        // no way to send programmatically, so the tap is ours to make.
        val intent = Intent(Intent.ACTION_VIEW).apply {
            data = Uri.parse("https://wa.me/$number?text=${Uri.encode(message)}")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }

        try {
            context.startActivity(intent)
        } catch (e: Exception) {
            return failed("I couldn't open WhatsApp.")
        }

        delay(2_500)

        val service = requireA11y()
            ?: return failed(
                "WhatsApp is open with the message ready — tap send yourself. " +
                    "Enable Jarvis in Accessibility settings and I can do it."
            )

        return when (val result = service.tap("Send")) {
            is JarvisAccessibilityService.Result.Ok ->
                Outcome(true, "Sent to ${contact.name} on WhatsApp.")

            is JarvisAccessibilityService.Result.Failed ->
                // Never claim success here. The message is sitting in the box
                // unsent, and telling the user it went is the worst outcome.
                failed(
                    "The message is ready in WhatsApp but I couldn't tap send. " +
                        "Have a look.",
                    result.reason,
                )

            else -> Outcome(true, "Sent to ${contact.name} on WhatsApp.")
        }
    }

    private fun whatsappVoiceNote(params: JSONObject): Outcome {
        // Honest refusal. WhatsApp's record button needs a press-and-hold
        // gesture over a view with no accessibility node, and faking it via
        // dispatchGesture is unreliable enough that it would sometimes send a
        // half-second of silence to someone. A confident failure is worse than
        // a clear no.
        return failed(
            "I can't record a WhatsApp voice note reliably yet. Want me to send " +
                "it as a text instead?"
        )
    }

    // ---------------------------------------------------------------------
    // Device
    // ---------------------------------------------------------------------

    private fun openApp(params: JSONObject): Outcome {
        val query = params.optString("app").trim()
        val packageName = resolvePackage(query)
            ?: return failed("I couldn't find an app called $query.")

        val service = requireA11y()
        if (service != null) {
            return when (service.openApp(packageName)) {
                is JarvisAccessibilityService.Result.Ok -> Outcome(true, "Opening $query.")
                else -> failed("I couldn't open $query.")
            }
        }

        val launch = context.packageManager.getLaunchIntentForPackage(packageName)
            ?: return failed("I couldn't open $query.")
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(launch)
        return Outcome(true, "Opening $query.")
    }

    /** Match a spoken app name against installed apps' visible labels. */
    private fun resolvePackage(query: String): String? {
        val manager = context.packageManager
        val installed = manager.getInstalledApplications(PackageManager.GET_META_DATA)

        val labelled = installed.mapNotNull { info ->
            val label = manager.getApplicationLabel(info).toString()
            if (manager.getLaunchIntentForPackage(info.packageName) == null) null
            else label to info.packageName
        }

        return labelled.firstOrNull { it.first.equals(query, ignoreCase = true) }?.second
            ?: labelled.firstOrNull { it.first.startsWith(query, ignoreCase = true) }?.second
            ?: labelled.firstOrNull { it.first.contains(query, ignoreCase = true) }?.second
    }

    private fun setAlarm(params: JSONObject): Outcome {
        val time = params.optString("time")
        val label = params.optString("label").ifBlank { "Jarvis" }

        val (hour, minute) = parseTime(time)
            ?: return failed("I didn't understand the time \"$time\".")

        val intent = Intent(AlarmClock.ACTION_SET_ALARM).apply {
            putExtra(AlarmClock.EXTRA_HOUR, hour)
            putExtra(AlarmClock.EXTRA_MINUTES, minute)
            putExtra(AlarmClock.EXTRA_MESSAGE, label)
            // Skip the clock app's confirmation screen — the server already
            // confirmed this out loud, and a second prompt is just friction.
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)

        return Outcome(true, "Alarm set for %02d:%02d.".format(hour, minute))
    }

    /** Parse "8:30", "08:30", "0830", "8" into hour and minute. */
    private fun parseTime(raw: String): Pair<Int, Int>? {
        val cleaned = raw.trim().lowercase()
        val isPm = cleaned.contains("pm")
        val isAm = cleaned.contains("am")
        val digits = cleaned.filter { it.isDigit() || it == ':' }

        val (h, m) = when {
            digits.contains(':') -> {
                val parts = digits.split(':')
                (parts.getOrNull(0)?.toIntOrNull() ?: return null) to
                    (parts.getOrNull(1)?.toIntOrNull() ?: 0)
            }
            digits.length == 4 ->
                digits.take(2).toInt() to digits.drop(2).toInt()
            digits.isNotEmpty() ->
                (digits.toIntOrNull() ?: return null) to 0
            else -> return null
        }

        var hour = h
        if (isPm && hour < 12) hour += 12
        if (isAm && hour == 12) hour = 0

        if (hour !in 0..23 || m !in 0..59) return null
        return hour to m
    }

    private fun setTimer(params: JSONObject): Outcome {
        val seconds = params.optInt("seconds", -1)
        if (seconds <= 0) return failed("I need a length for the timer.")

        val intent = Intent(AlarmClock.ACTION_SET_TIMER).apply {
            putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            putExtra(AlarmClock.EXTRA_MESSAGE, params.optString("label", "Jarvis"))
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)

        return Outcome(true, "Timer started.")
    }

    private fun toggleSetting(params: JSONObject): Outcome {
        val setting = params.optString("setting").lowercase()
        val on = params.optString("state").lowercase() in setOf("on", "true", "enable")

        return when (setting) {
            "torch", "flashlight" -> {
                val cameras = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
                val id = cameras.cameraIdList.firstOrNull()
                    ?: return failed("This phone has no camera flash.")
                cameras.setTorchMode(id, on)
                Outcome(true, if (on) "Torch on." else "Torch off.")
            }

            // Since Android 10, apps cannot toggle WiFi, Bluetooth or airplane
            // mode directly — that was removed precisely so apps couldn't
            // change your connectivity behind your back. Opening the settings
            // panel is the whole of what remains.
            "wifi", "bluetooth", "airplane_mode", "do_not_disturb" -> {
                val panel = when (setting) {
                    "wifi" -> android.provider.Settings.Panel.ACTION_WIFI
                    "bluetooth" -> android.provider.Settings.ACTION_BLUETOOTH_SETTINGS
                    "airplane_mode" -> android.provider.Settings.ACTION_AIRPLANE_MODE_SETTINGS
                    else -> android.provider.Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS
                }
                context.startActivity(Intent(panel).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                Outcome(
                    true,
                    "Android won't let me flip that directly — I've opened the setting for you.",
                )
            }

            else -> failed("I don't know how to change $setting.")
        }
    }

    private fun navigate(params: JSONObject): Outcome {
        val destination = params.optString("destination")
        if (destination.isBlank()) return failed("Where to?")

        val intent = Intent(
            Intent.ACTION_VIEW,
            Uri.parse("google.navigation:q=${Uri.encode(destination)}"),
        ).apply { addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }

        return try {
            context.startActivity(intent)
            Outcome(true, "Navigating to $destination.")
        } catch (e: Exception) {
            failed("I couldn't start navigation — is Maps installed?")
        }
    }

    private fun playMusic(params: JSONObject): Outcome {
        val query = params.optString("query")

        val intent = Intent(MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH).apply {
            putExtra(MediaStore.EXTRA_MEDIA_FOCUS, "vnd.android.cursor.item/*")
            putExtra(android.app.SearchManager.QUERY, query)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }

        return try {
            context.startActivity(intent)
            Outcome(true, "Playing $query.")
        } catch (e: Exception) {
            failed("No music app would take that.")
        }
    }

    // ---------------------------------------------------------------------
    // Raw UI control
    // ---------------------------------------------------------------------

    private fun uiTap(params: JSONObject): Outcome {
        val service = requireA11y() ?: return accessibilityOff()
        return when (val result = service.tap(params.optString("target"))) {
            is JarvisAccessibilityService.Result.Ok -> Outcome(true, "Done.")
            is JarvisAccessibilityService.Result.Failed -> failed(result.reason)
            else -> Outcome(true, "Done.")
        }
    }

    private fun uiType(params: JSONObject): Outcome {
        val service = requireA11y() ?: return accessibilityOff()
        return when (val result = service.type(params.optString("text"))) {
            is JarvisAccessibilityService.Result.Ok -> Outcome(true, "Typed.")
            is JarvisAccessibilityService.Result.Failed -> failed(result.reason)
            else -> Outcome(true, "Typed.")
        }
    }

    private fun readScreen(): Outcome {
        val service = requireA11y() ?: return accessibilityOff()
        return when (val result = service.readScreen()) {
            is JarvisAccessibilityService.Result.Text -> Outcome(true, result.value)
            is JarvisAccessibilityService.Result.Failed -> failed(result.reason)
            else -> failed("Nothing to read.")
        }
    }

    private fun accessibilityOff() = failed(
        "Phone control is off. Turn on Jarvis in Settings, Accessibility."
    )
}
