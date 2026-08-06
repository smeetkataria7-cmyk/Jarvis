package com.jarvis.assistant

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Talking to Jarvis on your computer.
 *
 * Every request carries the shared token. That token is the outer fence: it
 * keeps anything else on your WiFi from reaching the API at all. It is not
 * what authorises a code change — a stolen token still cannot forge the
 * signature that approving a patch requires.
 */
class JarvisClient(
    private val baseUrl: String,
    private val authToken: String,
) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        // Generous, because a local Ollama model on CPU genuinely takes this
        // long to answer and timing it out mid-thought is worse than waiting.
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    class JarvisError(message: String) : Exception(message)

    private val json = "application/json; charset=utf-8".toMediaType()

    private suspend fun request(
        path: String,
        body: JSONObject? = null,
    ): JSONObject = withContext(Dispatchers.IO) {
        val builder = Request.Builder()
            .url("${baseUrl.trimEnd('/')}$path")
            .header("Authorization", "Bearer $authToken")

        if (body != null) {
            builder.post(body.toString().toRequestBody(json))
        }

        try {
            http.newCall(builder.build()).execute().use { response ->
                val text = response.body?.string().orEmpty()

                if (text.isBlank()) {
                    throw JarvisError("Jarvis returned an empty response (HTTP ${response.code})")
                }

                val parsed = try {
                    JSONObject(text)
                } catch (e: Exception) {
                    throw JarvisError("Couldn't understand Jarvis's reply (HTTP ${response.code})")
                }

                if (!response.isSuccessful) {
                    // Surface the server's own message — it is written to be
                    // read by a person, and is far more useful than the code.
                    throw JarvisError(parsed.optString("error", "HTTP ${response.code}"))
                }

                parsed
            }
        } catch (e: IOException) {
            throw JarvisError(
                "Can't reach Jarvis at $baseUrl. Is your computer awake and on the same WiFi?"
            )
        }
    }

    // ---- conversation ----

    data class Reply(
        val action: String,
        val speak: String,
        val params: JSONObject,
        val execute: Boolean,
        val confirmToken: String?,
    )

    /** Send what the user said. Returns what to do about it. */
    suspend fun say(text: String): Reply {
        val response = request("/api/say", JSONObject().put("text", text))
        return Reply(
            action = response.optString("action", "none"),
            speak = response.optString("speak", ""),
            params = response.optJSONObject("params") ?: JSONObject(),
            execute = response.optBoolean("execute", false),
            confirmToken = response.optString("confirm_token").ifBlank { null },
        )
    }

    /** Answer a pending confirmation. */
    suspend fun confirm(token: String, approved: Boolean): Reply {
        val response = request(
            "/api/confirm",
            JSONObject().put("confirm_token", token).put("approved", approved),
        )
        return Reply(
            action = response.optString("action", "none"),
            speak = response.optString("speak", ""),
            params = response.optJSONObject("params") ?: JSONObject(),
            execute = response.optBoolean("execute", false),
            confirmToken = null,
        )
    }

    /**
     * Report whether an action actually worked.
     *
     * Easy to skip and important not to. Without it Jarvis knows what it tried
     * and never whether it landed, which means it can never notice that tapping
     * a particular app fails nine times in ten.
     */
    suspend fun reportOutcome(action: String, outcome: String, detail: String = "") {
        request(
            "/api/outcome",
            JSONObject()
                .put("action", action)
                .put("outcome", outcome)
                .put("detail", detail),
        )
    }

    // ---- enrollment ----

    suspend fun enroll(
        enrollmentCode: String,
        deviceId: String,
        name: String,
        publicKeyPem: String,
    ) {
        request(
            "/guardian/enroll",
            JSONObject()
                .put("enrollment_code", enrollmentCode)
                .put("device_id", deviceId)
                .put("name", name)
                .put("public_key_pem", publicKeyPem),
        )
    }

    // ---- self-edit approval ----

    data class PendingPatch(
        val patchId: String,
        val summary: String,
        val rationale: String,
        val diffLines: Int,
    )

    suspend fun pendingPatches(): List<PendingPatch> {
        val array = request("/guardian/patches").optJSONArray("patches") ?: return emptyList()
        return (0 until array.length()).map { i ->
            val item = array.getJSONObject(i)
            PendingPatch(
                patchId = item.getString("patch_id"),
                summary = item.getString("summary"),
                rationale = item.optString("rationale"),
                diffLines = item.optInt("diff_lines"),
            )
        }
    }

    /** The full diff — what you read before putting your thumb on the sensor. */
    suspend fun patchDiff(patchId: String): String =
        request("/guardian/patch/$patchId").optString("diff")

    /** Ask for a nonce to sign. Bound to this patch and usable once. */
    suspend fun challenge(patchId: String): String =
        request("/guardian/patch/$patchId/challenge").getString("nonce")

    suspend fun approve(
        patchId: String,
        deviceId: String,
        nonce: String,
        signatureBase64: String,
    ): String {
        val response = request(
            "/guardian/patch/$patchId/approve",
            JSONObject()
                .put("device_id", deviceId)
                .put("nonce", nonce)
                .put("signature", signatureBase64),
        )
        return response.optString("commit")
    }

    suspend fun reject(patchId: String, reason: String = "declined") {
        request("/guardian/patch/$patchId/reject", JSONObject().put("reason", reason))
    }
}
