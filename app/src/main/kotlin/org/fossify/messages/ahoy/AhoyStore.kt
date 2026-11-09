package org.fossify.messages.ahoy

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit
import org.json.JSONArray
import org.json.JSONObject

/**
 * Ahoy's own settings and state, kept apart from Fossify's config so upstream updates merge cleanly.
 *  - server address and shared key
 *  - conversations filtered as spam/advertising (hidden from the main list, shown under Filtered)
 *  - conversations with a draft on its way (the thread shows a spinner)
 *  - drafts the server wrote, so the thread can mark them as suggestions with a Clear button
 *  - one-tap quick replies for the last text in a thread, and which text they answer
 */
class AhoyStore private constructor(context: Context) {

    private val prefs: SharedPreferences = context.getSharedPreferences("ahoy", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString(SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        set(value) = prefs.edit { putString(SERVER_URL, value.trim()) }

    var key: String
        get() = prefs.getString(KEY, "").orEmpty()
        set(value) = prefs.edit { putString(KEY, value.trim()) }

    var enabled: Boolean
        get() = prefs.getBoolean(ENABLED, true)
        set(value) = prefs.edit { putBoolean(ENABLED, value) }

    val isConfigured: Boolean
        get() = enabled && serverUrl.isNotBlank() && runCatching { AhoyClient.decodeKey(key) }.isSuccess

    fun client() = AhoyClient(serverUrl, key)

    fun filteredThreads(): Set<Long> = longSet(FILTERED)
    fun isFiltered(threadId: Long) = threadId in filteredThreads()
    fun setFiltered(threadId: Long, filtered: Boolean) = updateSet(FILTERED, threadId, filtered)

    fun isDraftPending(threadId: Long) = threadId in longSet(PENDING)
    fun setDraftPending(threadId: Long, pending: Boolean) = updateSet(PENDING, threadId, pending)

    fun suggestedDraft(threadId: Long): String? = prefs.getString("$SUGGESTED$threadId", null)
    fun setSuggestedDraft(threadId: Long, text: String?) = prefs.edit {
        if (text == null) remove("$SUGGESTED$threadId") else putString("$SUGGESTED$threadId", text)
    }

    fun isQuickPending(threadId: Long) = threadId in longSet(QUICK_PENDING)
    fun setQuickPending(threadId: Long, pending: Boolean) = updateSet(QUICK_PENDING, threadId, pending)

    /** Quick replies for a thread, and the text they answer: they're only shown while that's still the last message. */
    fun quickReplies(threadId: Long): Pair<List<String>, String>? {
        val json = prefs.getString("$QUICK$threadId", null)?.let { runCatching { JSONObject(it) }.getOrNull() } ?: return null
        val replies = json.optJSONArray("replies") ?: return null
        return (0 until replies.length()).map { replies.optString(it) } to json.optString("answers")
    }

    fun setQuickReplies(threadId: Long, replies: List<String>?, answers: String = "") = prefs.edit {
        if (replies.isNullOrEmpty()) remove("$QUICK$threadId")
        else putString("$QUICK$threadId", JSONObject().put("replies", JSONArray(replies)).put("answers", answers).toString())
    }

    /**
     * The last draft written for a thread, with the messages it answered, kept until the next message
     * is sent there (even if the suggestion was cleared), so the server can learn from what was sent.
     */
    fun setFeedbackDraft(threadId: Long, draft: String, context: String) = prefs.edit {
        putString("$FEEDBACK$threadId", JSONObject().put("draft", draft).put("context", context)
            .put("at", System.currentTimeMillis()).toString())
    }

    /** Returns and forgets the thread's last draft, if it was written in the last [maxAgeMs]. */
    @Synchronized
    fun takeFeedbackDraft(threadId: Long, maxAgeMs: Long): JSONObject? {
        val saved = prefs.getString("$FEEDBACK$threadId", null) ?: return null
        prefs.edit { remove("$FEEDBACK$threadId") }
        val json = runCatching { JSONObject(saved) }.getOrNull() ?: return null
        return json.takeIf { System.currentTimeMillis() - it.optLong("at") < maxAgeMs }
    }

    private fun longSet(name: String): Set<Long> =
        prefs.getStringSet(name, emptySet()).orEmpty().mapNotNull { it.toLongOrNull() }.toSet()

    @Synchronized
    private fun updateSet(name: String, threadId: Long, add: Boolean) {
        val current = prefs.getStringSet(name, emptySet()).orEmpty().toMutableSet()
        if (add) current.add(threadId.toString()) else current.remove(threadId.toString())
        prefs.edit(commit = true) { putStringSet(name, current) }
    }

    companion object {
        const val DEFAULT_SERVER_URL = ""
        private const val SERVER_URL = "server_url"
        private const val KEY = "key"
        private const val ENABLED = "enabled"
        private const val FILTERED = "filtered_threads"
        private const val PENDING = "pending_drafts"
        private const val SUGGESTED = "suggested_draft_"
        private const val FEEDBACK = "feedback_draft_"
        private const val QUICK_PENDING = "pending_quick"
        private const val QUICK = "quick_replies_"

        @Volatile
        private var instance: AhoyStore? = null

        fun get(context: Context): AhoyStore =
            instance ?: synchronized(this) {
                instance ?: AhoyStore(context.applicationContext).also { instance = it }
            }
    }
}
