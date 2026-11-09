package org.fossify.messages.ahoy

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import org.fossify.commons.extensions.getMyContactsCursor
import org.fossify.commons.helpers.ContactLookupResult
import org.fossify.commons.helpers.SimpleContactsHelper
import org.fossify.messages.extensions.getMessages
import org.fossify.messages.extensions.getNameFromAddress
import org.fossify.messages.extensions.getSmsDraft
import org.fossify.messages.extensions.getThreadParticipants
import org.fossify.messages.extensions.saveSmsDraft
import org.fossify.messages.helpers.refreshConversations
import org.greenrobot.eventbus.EventBus
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** What the phone does with sms-triage: sort incoming texts, fetch drafts, send conversations for review. */
object AhoyTriage {
    private const val TAG = "AhoyTriage"
    private const val THREAD_LINES = 20
    private const val FEEDBACK_MAX_AGE_MS = 12 * 60 * 60 * 1000L  // a reply much later is probably about something else

    /**
     * Sorts one incoming text. Returns true if it was filtered as spam/advertising, in which case the
     * caller skips the notification. Anything going wrong (no network, server down, no key) lets the
     * text through as normal: a missed filter is better than a missed customer. It is then sorted again
     * in the background once the server can be reached, which also asks for its draft.
     */
    fun onIncoming(context: Context, threadId: Long, address: String, body: String, sentAt: Long): Boolean {
        val store = AhoyStore.get(context)
        if (!store.isConfigured) return false
        return try {
            sort(context, threadId, address, body, sentAt, late = false)
        } catch (e: Exception) {
            Log.w(TAG, "sorting failed, letting the text through and retrying later: $e")
            if (e !is AhoyClient.Refused) LateSortWorker.enqueue(context, threadId, address, body, sentAt)
            false
        }
    }

    /** Asks the server about one text, files it, and asks for a draft if it's worth one. */
    fun sort(context: Context, threadId: Long, address: String, body: String, sentAt: Long, late: Boolean): Boolean {
        val store = AhoyStore.get(context)
        val (name, isContact) = sender(context, address)
        val result = store.client().incoming(address, if (isContact) name else "", isContact, body,
            recentThread(context, threadId), sentAt, late)
        val filtered = result.optBoolean("filtered")
        store.setFiltered(threadId, filtered)
        Log.i(TAG, "thread $threadId: ${result.optString("category")}${if (filtered) ", filtered" else ""}")
        if (!filtered && result.optBoolean("draft")) requestQuick(context, threadId)
        return filtered
    }

    /** Asks for one-tap replies to the newest text; the thread shows a spinner until they arrive. */
    fun requestQuick(context: Context, threadId: Long) {
        val store = AhoyStore.get(context)
        store.setQuickReplies(threadId, null)
        store.setQuickPending(threadId, true)
        EventBus.getDefault().post(DraftStateChanged(threadId))
        val request = OneTimeWorkRequestBuilder<QuickWorker>()
            .setInputData(workDataOf(DraftWorker.THREAD_ID to threadId))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork("quick-$threadId", ExistingWorkPolicy.REPLACE, request)
    }

    /** Queues a draft for a conversation; the thread shows a spinner until it arrives. */
    fun requestDraft(context: Context, threadId: Long) {
        AhoyStore.get(context).setDraftPending(threadId, true)
        EventBus.getDefault().post(DraftStateChanged(threadId))
        val request = OneTimeWorkRequestBuilder<DraftWorker>()
            .setInputData(workDataOf(DraftWorker.THREAD_ID to threadId))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork("draft-$threadId", ExistingWorkPolicy.REPLACE, request)
    }

    /** Sends a whole conversation for the server to learn context and writing style from. */
    fun review(context: Context, threadId: Long, deep: Boolean = false): JSONObject {
        val (address, name) = threadContact(context, threadId)
        val messages = context.getMessages(threadId, includeScheduledMessages = false, limit = 2000)
            .sortedBy { it.date }
            .filter { it.body.isNotBlank() }
            .map { ThreadLine(fromMe = !it.isReceivedMessage(), body = it.body) }
        return AhoyStore.get(context).client().review(address, name, messages, deep)
    }

    /** A message was sent in a one-to-one thread: if a draft was written for it, tell the server what was sent. */
    fun onSent(context: Context, threadId: Long, text: String) {
        val store = AhoyStore.get(context)
        if (store.quickReplies(threadId) != null) {
            store.setQuickReplies(threadId, null)  // answered: the buttons go
            EventBus.getDefault().post(DraftStateChanged(threadId))
        }
        if (!store.isConfigured || text.isBlank()) return
        val saved = store.takeFeedbackDraft(threadId, FEEDBACK_MAX_AGE_MS) ?: return
        FeedbackWorker.enqueue(context, threadId, saved.optString("draft"), saved.optString("context"), text)
    }

    fun forget(context: Context, threadId: Long) {
        val (address, _) = threadContact(context, threadId)
        AhoyStore.get(context).client().forget(address)
    }

    /** The other person's number and (if saved) name; only one-to-one conversations are supported. */
    fun threadContact(context: Context, threadId: Long): Pair<String, String> {
        val participants = context.getThreadParticipants(threadId, null)
        val contact = participants.singleOrNull()
            ?: throw IllegalStateException("Drafts and reviews only work in one-to-one conversations")
        val address = contact.phoneNumbers.firstOrNull()?.normalizedNumber?.ifBlank { null }
            ?: contact.phoneNumbers.first().value
        val (name, isContact) = sender(context, address)
        return address to if (isContact) name else ""
    }

    fun recentThread(context: Context, threadId: Long): List<ThreadLine> =
        context.getMessages(threadId, includeScheduledMessages = false, limit = THREAD_LINES)
            .sortedBy { it.date }
            .filter { it.body.isNotBlank() }
            .takeLast(THREAD_LINES)
            .map { ThreadLine(fromMe = !it.isReceivedMessage(), body = it.body) }

    private fun sender(context: Context, address: String): Pair<String, Boolean> {
        val cursor = context.getMyContactsCursor(favoritesOnly = false, withPhoneNumbersOnly = true)
        val inContacts = SimpleContactsHelper(context).existsSync(address, cursor) != ContactLookupResult.NotFound
        val name = context.getMyContactsCursor(favoritesOnly = false, withPhoneNumbersOnly = true).use {
            context.getNameFromAddress(address, it)
        }
        return name to (inContacts || name != address)
    }
}

/** Sorts a text whose first sorting failed (no network, or the server was slow), once it can. */
class LateSortWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val data = inputData
        return try {
            val filtered = AhoyTriage.sort(applicationContext, data.getLong(THREAD_ID, 0L),
                data.getString(ADDRESS).orEmpty(), data.getString(BODY).orEmpty(), data.getLong(SENT_AT, 0L), late = true)
            if (filtered) refreshConversations()
            Result.success()
        } catch (e: Exception) {
            Log.w("LateSortWorker", "late sorting failed (attempt ${runAttemptCount + 1}): $e")
            if (runAttemptCount < 5 && e is java.io.IOException && e !is AhoyClient.Refused) Result.retry() else Result.failure()
        }
    }

    companion object {
        private const val THREAD_ID = "thread_id"
        private const val ADDRESS = "address"
        private const val BODY = "body"
        private const val SENT_AT = "sent_at"

        fun enqueue(context: Context, threadId: Long, address: String, body: String, sentAt: Long) {
            val request = OneTimeWorkRequestBuilder<LateSortWorker>()
                .setInputData(workDataOf(THREAD_ID to threadId, ADDRESS to address, BODY to body, SENT_AT to sentAt))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork("sort-$sentAt", ExistingWorkPolicy.KEEP, request)
        }
    }
}

/** Tells the server what was sent in place of a suggested draft, so later drafts can learn from it. */
class FeedbackWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        return try {
            val threadId = inputData.getLong(THREAD_ID, 0L)
            val (address, name) = AhoyTriage.threadContact(applicationContext, threadId)
            AhoyStore.get(applicationContext).client().feedback(address, name, JSONArray(inputData.getString(CONTEXT)),
                inputData.getString(DRAFT).orEmpty(), inputData.getString(SENT).orEmpty())
            Result.success()
        } catch (e: Exception) {
            Log.w("FeedbackWorker", "feedback failed (attempt ${runAttemptCount + 1}): $e")
            if (runAttemptCount < 5 && e is java.io.IOException && e !is AhoyClient.Refused) Result.retry() else Result.failure()
        }
    }

    companion object {
        private const val THREAD_ID = "thread_id"
        private const val DRAFT = "draft"
        private const val CONTEXT = "context"
        private const val SENT = "sent"

        fun enqueue(context: Context, threadId: Long, draft: String, threadContext: String, sent: String) {
            val request = OneTimeWorkRequestBuilder<FeedbackWorker>()
                .setInputData(workDataOf(THREAD_ID to threadId, DRAFT to draft, CONTEXT to threadContext, SENT to sent))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }
    }
}

/** Fetches quick replies for a thread's newest text. No retries: a late answer to an old text is no use. */
class QuickWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val threadId = inputData.getLong(DraftWorker.THREAD_ID, 0L)
        val store = AhoyStore.get(applicationContext)
        try {
            val (address, name) = AhoyTriage.threadContact(applicationContext, threadId)
            val thread = AhoyTriage.recentThread(applicationContext, threadId)
            val last = thread.lastOrNull()
            if (last != null && !last.fromMe) {
                store.setQuickReplies(threadId, store.client().quick(address, name, thread), last.body)
            }
        } catch (e: Exception) {
            Log.w("QuickWorker", "quick replies for thread $threadId failed: $e")
        }
        store.setQuickPending(threadId, false)
        refreshConversations()
        EventBus.getDefault().post(DraftStateChanged(threadId))
        return Result.success()
    }
}

/** Posted when a thread's draft starts or finishes, so an open thread can update its spinner. */
data class DraftStateChanged(val threadId: Long)

class DraftWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val threadId = inputData.getLong(THREAD_ID, 0L)
        val store = AhoyStore.get(applicationContext)
        return try {
            val (address, name) = AhoyTriage.threadContact(applicationContext, threadId)
            val thread = AhoyTriage.recentThread(applicationContext, threadId)
            val draft = store.client().draft(address, name, thread)
            // Never overwrite something the user has typed (an earlier suggestion is fine to replace)
            val existing = applicationContext.getSmsDraft(threadId)
            if (draft.isNotBlank() && (existing.isEmpty() || existing == store.suggestedDraft(threadId))) {
                applicationContext.saveSmsDraft(draft, threadId)
                store.setSuggestedDraft(threadId, draft)
                store.setFeedbackDraft(threadId, draft, ThreadLine.toJson(thread.takeLast(6)).toString())
            }
            finish(store, threadId)
            Result.success()
        } catch (e: Exception) {
            Log.w("DraftWorker", "draft for thread $threadId failed: $e")
            if (runAttemptCount < 2 && e is java.io.IOException && e !is AhoyClient.Refused) {
                Result.retry()
            } else {
                finish(store, threadId)
                Result.failure()
            }
        }
    }

    private fun finish(store: AhoyStore, threadId: Long) {
        store.setDraftPending(threadId, false)
        refreshConversations()
        EventBus.getDefault().post(DraftStateChanged(threadId))
    }

    companion object {
        const val THREAD_ID = "thread_id"
    }
}
