package com.jarvis.assistant

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * The hands.
 *
 * Android's Accessibility API is what screen readers use, and it is the only
 * sanctioned way for an app to read the screen and act on other apps. That is
 * exactly why it is powerful enough to be Jarvis's hands, and exactly why the
 * permission has to be granted by hand in Settings and shows a persistent
 * warning. Both of those are correct. Do not try to talk anyone out of them.
 *
 * What this can do: read the view hierarchy, tap, type, scroll, go back and
 * home, and launch apps.
 *
 * What it cannot do, and no amount of cleverness will change:
 *
 *  - Speak on a live phone call. The telephony uplink is closed to apps,
 *    deliberately, so that malware cannot impersonate you to your bank. The
 *    speakerphone-plus-text-to-speech route in call_and_speak is a workaround
 *    that genuinely transmits, and genuinely sounds like a robot in a tunnel.
 *  - See inside apps that set FLAG_SECURE — banking apps, password managers.
 *    Their windows read back blank. That is the system protecting you from
 *    precisely the kind of access this service has.
 */
class JarvisAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "JarvisA11y"

        @Volatile
        var instance: JarvisAccessibilityService? = null
            private set

        fun isRunning(): Boolean = instance != null
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "accessibility service connected")
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    // Jarvis is driven by explicit commands from the server, not by reacting to
    // whatever happens on screen. Left empty on purpose: an assistant that
    // silently observed every event on the device would be a keylogger with
    // better manners.
    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    // ---------------------------------------------------------------------
    // Reading the screen
    // ---------------------------------------------------------------------

    sealed class Result {
        object Ok : Result()
        data class Text(val value: String) : Result()
        data class Failed(val reason: String) : Result()
    }

    /** Every visible piece of text on screen, top to bottom. */
    fun readScreen(): Result {
        val root = rootInActiveWindow
            ?: return Result.Failed("can't see the screen — is an app in the foreground?")

        val collected = mutableListOf<String>()
        walk(root) { node ->
            val text = node.text?.toString()?.trim()
            val description = node.contentDescription?.toString()?.trim()
            when {
                !text.isNullOrEmpty() -> collected += text
                !description.isNullOrEmpty() -> collected += description
            }
        }

        if (collected.isEmpty()) {
            // Usually FLAG_SECURE. Say so rather than reporting an empty screen,
            // which reads like a bug and sends people debugging the wrong thing.
            return Result.Failed(
                "nothing readable on screen — this app may block screen reading"
            )
        }

        return Result.Text(collected.distinct().joinToString("\n"))
    }

    private fun walk(node: AccessibilityNodeInfo?, visit: (AccessibilityNodeInfo) -> Unit) {
        if (node == null) return
        visit(node)
        for (i in 0 until node.childCount) {
            walk(node.getChild(i), visit)
        }
    }

    // ---------------------------------------------------------------------
    // Tapping
    // ---------------------------------------------------------------------

    /**
     * Tap the element whose text or description best matches [target].
     *
     * Matching is deliberately layered: exact first, then case-insensitive,
     * then contains. Looser matching finds more things and taps the wrong one
     * more often, so the tightest match that succeeds wins.
     */
    fun tap(target: String): Result {
        val root = rootInActiveWindow ?: return Result.Failed("can't see the screen")
        val needle = target.trim()
        if (needle.isEmpty()) return Result.Failed("nothing to tap")

        val candidates = mutableListOf<AccessibilityNodeInfo>()
        walk(root) { node ->
            val text = node.text?.toString()?.trim().orEmpty()
            val description = node.contentDescription?.toString()?.trim().orEmpty()
            if (text.isNotEmpty() || description.isNotEmpty()) candidates += node
        }

        val match =
            candidates.firstOrNull { it.matches(needle, exact = true, ignoreCase = false) }
                ?: candidates.firstOrNull { it.matches(needle, exact = true, ignoreCase = true) }
                ?: candidates.firstOrNull { it.matches(needle, exact = false, ignoreCase = true) }
                ?: return Result.Failed("couldn't find \"$needle\" on screen")

        // The node holding the text is very often a TextView inside a clickable
        // parent, so clicking the node itself does nothing at all. Walk up
        // until something will actually accept the click.
        var clickable: AccessibilityNodeInfo? = match
        var depth = 0
        while (clickable != null && !clickable.isClickable && depth < 6) {
            clickable = clickable.parent
            depth++
        }

        if (clickable != null && clickable.isClickable) {
            return if (clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                Result.Ok
            } else {
                Result.Failed("found \"$needle\" but the tap was refused")
            }
        }

        // Last resort: synthesise a touch at the element's centre. Works on
        // custom-drawn UIs that expose no clickable node at all.
        return tapAt(match)
    }

    private fun AccessibilityNodeInfo.matches(
        needle: String,
        exact: Boolean,
        ignoreCase: Boolean,
    ): Boolean {
        val fields = listOfNotNull(text?.toString(), contentDescription?.toString())
        return fields.any { field ->
            val value = field.trim()
            if (exact) value.equals(needle, ignoreCase) else value.contains(needle, ignoreCase)
        }
    }

    private fun tapAt(node: AccessibilityNodeInfo): Result {
        val bounds = Rect().also { node.getBoundsInScreen(it) }
        if (bounds.isEmpty) return Result.Failed("element has no position on screen")

        val path = Path().apply { moveTo(bounds.exactCenterX(), bounds.exactCenterY()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 50))
            .build()

        return if (dispatchGesture(gesture, null, null)) {
            Result.Ok
        } else {
            Result.Failed("gesture was refused")
        }
    }

    // ---------------------------------------------------------------------
    // Typing
    // ---------------------------------------------------------------------

    fun type(text: String): Result {
        val root = rootInActiveWindow ?: return Result.Failed("can't see the screen")

        var field = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)

        if (field == null) {
            // Nothing focused — fall back to the first editable field, and
            // focus it explicitly so the text lands somewhere predictable.
            walk(root) { node ->
                if (field == null && node.isEditable) field = node
            }
            field?.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        }

        val target = field ?: return Result.Failed("no text field to type into")

        val arguments = Bundle().apply {
            putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                text,
            )
        }

        return if (target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)) {
            Result.Ok
        } else {
            Result.Failed("the field refused the text")
        }
    }

    // ---------------------------------------------------------------------
    // Navigation
    // ---------------------------------------------------------------------

    fun goBack(): Result =
        if (performGlobalAction(GLOBAL_ACTION_BACK)) Result.Ok
        else Result.Failed("back was refused")

    fun goHome(): Result =
        if (performGlobalAction(GLOBAL_ACTION_HOME)) Result.Ok
        else Result.Failed("home was refused")

    fun scroll(forward: Boolean): Result {
        val root = rootInActiveWindow ?: return Result.Failed("can't see the screen")

        var scrollable: AccessibilityNodeInfo? = null
        walk(root) { node ->
            if (scrollable == null && node.isScrollable) scrollable = node
        }

        val target = scrollable ?: return Result.Failed("nothing scrollable on screen")
        val action =
            if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
            else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD

        return if (target.performAction(action)) Result.Ok else Result.Failed("scroll refused")
    }

    fun openApp(packageName: String): Result {
        val launch = packageManager.getLaunchIntentForPackage(packageName)
            ?: return Result.Failed("$packageName isn't installed")

        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            startActivity(launch)
            Result.Ok
        } catch (e: Exception) {
            Result.Failed("couldn't open $packageName: ${e.message}")
        }
    }
}
