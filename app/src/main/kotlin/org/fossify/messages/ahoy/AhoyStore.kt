package org.fossify.messages.ahoy

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit

/**
 * Ahoy's own settings and state, kept apart from Fossify's config so upstream updates merge cleanly.
 *  - server address and shared key
 *  - conversations filtered as spam/advertising (hidden from the main list, shown under Filtered)
 *  - conversations with a draft on its way (the thread shows a spinner)
 *  - drafts the server wrote, so the thread can mark them as suggestions with a Clear button
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

        @Volatile
        private var instance: AhoyStore? = null

        fun get(context: Context): AhoyStore =
            instance ?: synchronized(this) {
                instance ?: AhoyStore(context.applicationContext).also { instance = it }
            }
    }
}
