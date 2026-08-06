package com.jarvis.assistant

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import kotlinx.coroutines.suspendCancellableCoroutine
import java.util.Locale
import java.util.UUID
import kotlin.coroutines.resume

/**
 * Ears and mouth.
 *
 * Both halves are wrapped as suspending functions because the underlying
 * Android APIs are callback-driven in a way that makes any real conversation
 * flow — listen, think, speak, listen again — collapse into nested callbacks
 * almost immediately.
 *
 * One detail that matters more than it looks: speaking and listening must never
 * overlap. The recogniser has no idea the microphone is hearing the phone's own
 * loudspeaker, so it will happily transcribe Jarvis's reply as your next
 * instruction and act on it. Hence speakBlocking, and hence every caller
 * awaiting it before listening again.
 */
class VoiceEngine(private val context: Context) {

    companion object {
        private const val TAG = "JarvisVoice"
    }

    private var tts: TextToSpeech? = null
    private var ttsReady = false

    // ---------------------------------------------------------------------
    // Speaking
    // ---------------------------------------------------------------------

    /** Warm up text-to-speech. Call once, early — it takes a moment. */
    suspend fun initialise(): Boolean = suspendCancellableCoroutine { continuation ->
        tts = TextToSpeech(context) { status ->
            ttsReady = status == TextToSpeech.SUCCESS

            if (ttsReady) {
                tts?.language = Locale.getDefault()
                // Slightly quicker than default. Assistant replies are short
                // and the stock rate sounds oddly ponderous read aloud.
                tts?.setSpeechRate(1.05f)
            } else {
                Log.e(TAG, "text-to-speech unavailable")
            }

            if (continuation.isActive) continuation.resume(ttsReady)
        }
    }

    /**
     * Speak, and don't return until the last word is out.
     *
     * The suspending part is the whole point: it is what keeps Jarvis from
     * hearing itself and treating its own reply as a command.
     */
    suspend fun speakBlocking(text: String): Unit = suspendCancellableCoroutine { continuation ->
        val engine = tts
        if (!ttsReady || engine == null || text.isBlank()) {
            if (continuation.isActive) continuation.resume(Unit)
            return@suspendCancellableCoroutine
        }

        val utteranceId = UUID.randomUUID().toString()

        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String?) = Unit

            override fun onDone(id: String?) {
                if (id == utteranceId && continuation.isActive) continuation.resume(Unit)
            }

            @Deprecated("required by the interface")
            override fun onError(id: String?) {
                // Resume rather than propagate. A failed utterance should not
                // wedge the conversation — losing one spoken line is far
                // better than the assistant going permanently silent.
                if (id == utteranceId && continuation.isActive) continuation.resume(Unit)
            }
        })

        engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, utteranceId)

        continuation.invokeOnCancellation { engine.stop() }
    }

    // ---------------------------------------------------------------------
    // Listening
    // ---------------------------------------------------------------------

    sealed class Heard {
        data class Text(val value: String) : Heard()
        object Silence : Heard()
        data class Failed(val reason: String) : Heard()
    }

    /**
     * Listen for one utterance and transcribe it.
     *
     * Recognition runs on-device where the phone supports it, which keeps your
     * voice off Google's servers. On phones without offline recognition this
     * silently falls back to the network — worth knowing, given the point of
     * this project is that it stays yours.
     */
    suspend fun listen(): Heard = suspendCancellableCoroutine { continuation ->
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            if (continuation.isActive) {
                continuation.resume(Heard.Failed("No speech recognition on this phone."))
            }
            return@suspendCancellableCoroutine
        }

        val recognizer = SpeechRecognizer.createSpeechRecognizer(context)

        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
            )
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault())
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            // A short trailing pause. People pause mid-sentence when thinking,
            // and cutting them off is more annoying than a beat of extra wait.
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1500)
        }

        // Guard against double-resume: the recogniser can deliver both an
        // error and a result in some failure modes.
        var settled = false
        fun settle(outcome: Heard) {
            if (settled) return
            settled = true
            recognizer.destroy()
            if (continuation.isActive) continuation.resume(outcome)
        }

        recognizer.setRecognitionListener(object : RecognitionListener {
            override fun onResults(results: Bundle?) {
                val text = results
                    ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                    ?.firstOrNull()
                    ?.trim()

                settle(if (text.isNullOrEmpty()) Heard.Silence else Heard.Text(text))
            }

            override fun onError(error: Int) {
                settle(
                    when (error) {
                        SpeechRecognizer.ERROR_NO_MATCH,
                        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> Heard.Silence

                        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ->
                            Heard.Failed("I don't have microphone permission.")

                        SpeechRecognizer.ERROR_NETWORK,
                        SpeechRecognizer.ERROR_NETWORK_TIMEOUT ->
                            Heard.Failed("Speech recognition needs a network on this phone.")

                        else -> Heard.Failed("I couldn't hear that (error $error).")
                    }
                )
            }

            override fun onReadyForSpeech(params: Bundle?) = Unit
            override fun onBeginningOfSpeech() = Unit
            override fun onRmsChanged(rms: Float) = Unit
            override fun onBufferReceived(buffer: ByteArray?) = Unit
            override fun onEndOfSpeech() = Unit
            override fun onPartialResults(partial: Bundle?) = Unit
            override fun onEvent(type: Int, params: Bundle?) = Unit
        })

        recognizer.startListening(intent)

        continuation.invokeOnCancellation {
            recognizer.cancel()
            recognizer.destroy()
        }
    }

    fun shutdown() {
        tts?.stop()
        tts?.shutdown()
        tts = null
        ttsReady = false
    }
}
