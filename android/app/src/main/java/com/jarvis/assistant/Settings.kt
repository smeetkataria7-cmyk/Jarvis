package com.jarvis.assistant

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.util.UUID

/**
 * Where the phone remembers how to reach Jarvis.
 *
 * Encrypted rather than plain SharedPreferences because the auth token is
 * stored here. That token is not the thing that authorises code changes — a
 * signature is — but it does grant full conversational access, which includes
 * sending texts as you. Not worth leaving in cleartext on disk.
 */
class Settings(context: Context) {

    private val prefs = EncryptedSharedPreferences.create(
        context,
        "jarvis_settings",
        MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var serverUrl: String
        get() = prefs.getString("server_url", "").orEmpty()
        set(value) = prefs.edit().putString("server_url", value.trim().trimEnd('/')).apply()

    var authToken: String
        get() = prefs.getString("auth_token", "").orEmpty()
        set(value) = prefs.edit().putString("auth_token", value.trim()).apply()

    /**
     * A stable identifier for this phone, generated once on first run.
     *
     * Deliberately random rather than derived from a hardware ID: ANDROID_ID
     * and friends are either unavailable, resettable, or shared across apps,
     * and none of those make for a good primary key on the server side.
     */
    val deviceId: String
        get() {
            prefs.getString("device_id", null)?.let { return it }
            val fresh = UUID.randomUUID().toString()
            prefs.edit().putString("device_id", fresh).apply()
            return fresh
        }

    var deviceName: String
        get() = prefs.getString("device_name", android.os.Build.MODEL).orEmpty()
        set(value) = prefs.edit().putString("device_name", value).apply()

    val isPaired: Boolean
        get() = serverUrl.isNotBlank() && authToken.isNotBlank() && ApprovalKeystore.hasKey()

    fun clear() = prefs.edit().clear().apply()
}
