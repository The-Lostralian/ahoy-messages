package org.fossify.messages.ahoy

import android.content.res.ColorStateList
import android.view.Menu
import android.widget.Toast
import org.fossify.commons.dialogs.ConfirmationDialog
import org.fossify.commons.extensions.applyColorFilter
import org.fossify.commons.extensions.beVisibleIf
import org.fossify.commons.extensions.getProperPrimaryColor
import org.fossify.commons.extensions.getProperTextColor
import org.fossify.commons.extensions.toast
import org.fossify.commons.extensions.value
import org.fossify.commons.helpers.ensureBackgroundThread
import org.fossify.messages.R
import org.fossify.messages.activities.SimpleActivity
import org.fossify.messages.databinding.LayoutThreadSendMessageHolderBinding
import org.fossify.messages.extensions.deleteSmsDraft
import org.fossify.messages.extensions.getSmsDraft
import org.fossify.messages.helpers.refreshConversations

/**
 * Ahoy's part of a conversation screen: the draft bar above the reply box (spinner while a draft is
 * being written, then "suggested reply" with a clear button, or one-tap quick replies for the last
 * text) and the extra menu items. ThreadActivity
 * only calls into this, so Fossify's own code stays nearly untouched.
 */
class AhoyThreadUi(
    private val activity: SimpleActivity,
    private val holder: LayoutThreadSendMessageHolderBinding,
    private val threadId: () -> Long,
    private val onMenuChanged: () -> Unit,
    /** The newest message's text if it was received (not sent), so quick replies only show for it. */
    private val lastReceived: () -> String?,
) {
    private val store = AhoyStore.get(activity)

    init {
        holder.ahoyDraftClear.setOnClickListener { clearSuggestion() }
        holder.ahoyQuickPositive.setOnClickListener { sendQuick(holder.ahoyQuickPositive.text.toString()) }
        holder.ahoyQuickNegative.setOnClickListener { sendQuick(holder.ahoyQuickNegative.text.toString()) }
        holder.ahoyDraftIcon.applyColorFilter(activity.getProperTextColor())
        holder.ahoyDraftClear.applyColorFilter(activity.getProperTextColor())
        holder.ahoyDraftProgress.indeterminateTintList = ColorStateList.valueOf(activity.getProperPrimaryColor())
    }

    fun updateDraftBar() {
        val id = threadId()
        val typed = holder.threadTypeMessage.value
        val pending = store.isDraftPending(id)
        val suggestion = store.suggestedDraft(id)
        val showingSuggestion = !pending && suggestion != null && typed == suggestion
        // Quick replies only while the reply box is empty: a tap must never throw away typing
        val boxFree = typed.isEmpty() && !pending
        val quickPending = boxFree && store.isQuickPending(id)
        val quick = if (boxFree && !quickPending) currentQuick(id) else emptyList()
        val spinner = pending || quickPending
        val done = !spinner && (showingSuggestion || quick.isNotEmpty())

        holder.ahoyDraftHolder.beVisibleIf(spinner || done)
        holder.ahoyDraftProgress.beVisibleIf(spinner)
        holder.ahoyDraftIcon.beVisibleIf(done)
        holder.ahoyDraftClear.beVisibleIf(done)
        holder.ahoyDraftLabel.setText(when {
            pending -> R.string.ahoy_writing_draft
            quickPending -> R.string.ahoy_picking_quick
            quick.isNotEmpty() -> R.string.ahoy_quick_label
            else -> R.string.ahoy_suggested_draft
        })
        holder.ahoyQuickRow.beVisibleIf(!spinner && quick.isNotEmpty())
        holder.ahoyQuickPositive.text = quick.getOrNull(0).orEmpty()
        holder.ahoyQuickNegative.text = quick.getOrNull(1).orEmpty()
        holder.ahoyQuickNegative.beVisibleIf(quick.size > 1)
    }

    private fun currentQuick(id: Long): List<String> {
        val (replies, answers) = store.quickReplies(id) ?: return emptyList()
        return if (lastReceived() == answers) replies else emptyList()
    }

    /** One tap sends: the reply goes in the box and through the normal send button, so SIM choice etc. still apply. */
    private fun sendQuick(text: String) {
        if (text.isBlank() || holder.threadTypeMessage.value.isNotEmpty()) return
        holder.threadTypeMessage.setText(text)
        holder.threadSendMessage.performClick()
    }

    /** A draft finished (or started) for some thread; fill the reply box if it's this one and still empty. */
    fun onDraftStateChanged(event: DraftStateChanged) {
        val id = threadId()
        if (event.threadId != id) return
        ensureBackgroundThread {
            val draft = activity.getSmsDraft(id)
            activity.runOnUiThread {
                if (draft.isNotEmpty() && holder.threadTypeMessage.value.isEmpty()) {
                    holder.threadTypeMessage.setText(draft)
                    holder.threadTypeMessage.setSelection(draft.length)
                }
                updateDraftBar()
            }
        }
    }

    private fun clearSuggestion() {
        val id = threadId()
        if (holder.ahoyQuickRow.visibility == android.view.View.VISIBLE) {
            store.setQuickReplies(id, null)
            updateDraftBar()
            return
        }
        store.setSuggestedDraft(id, null)
        holder.threadTypeMessage.setText("")
        ensureBackgroundThread { activity.deleteSmsDraft(id) }
        updateDraftBar()
    }

    fun refreshMenu(menu: Menu, oneToOne: Boolean) {
        val usable = oneToOne && store.isConfigured
        val filtered = store.isFiltered(threadId())
        menu.findItem(R.id.ahoy_draft_reply)?.isVisible = usable
        menu.findItem(R.id.ahoy_send_for_review)?.isVisible = usable
        menu.findItem(R.id.ahoy_deep_review)?.isVisible = usable
        menu.findItem(R.id.ahoy_forget_review)?.isVisible = usable
        menu.findItem(R.id.ahoy_not_spam)?.isVisible = filtered
        menu.findItem(R.id.ahoy_move_to_filtered)?.isVisible = !filtered
    }

    fun handleMenuItem(itemId: Int): Boolean {
        val id = threadId()
        when (itemId) {
            R.id.ahoy_draft_reply -> {
                AhoyTriage.requestDraft(activity, id)
                updateDraftBar()
            }

            R.id.ahoy_send_for_review -> ConfirmationDialog(activity, activity.getString(R.string.ahoy_review_confirm)) {
                review(id, deep = false)
            }

            R.id.ahoy_deep_review -> ConfirmationDialog(activity, activity.getString(R.string.ahoy_deep_review_confirm)) {
                review(id, deep = true)
            }

            R.id.ahoy_forget_review -> ConfirmationDialog(activity, activity.getString(R.string.ahoy_forget_confirm)) {
                ensureBackgroundThread {
                    val result = runCatching { AhoyTriage.forget(activity, id) }
                    activity.runOnUiThread {
                        result.onSuccess { activity.toast(R.string.ahoy_forgotten) }.onFailure { showFailure(it) }
                    }
                }
            }

            R.id.ahoy_not_spam, R.id.ahoy_move_to_filtered -> {
                store.setFiltered(id, itemId == R.id.ahoy_move_to_filtered)
                refreshConversations()
                onMenuChanged()
            }

            else -> return false
        }
        return true
    }

    private fun review(id: Long, deep: Boolean) {
        activity.toast(R.string.ahoy_reviewing, Toast.LENGTH_LONG)
        ensureBackgroundThread {
            val result = runCatching { AhoyTriage.review(activity, id, deep) }
            activity.runOnUiThread {
                result.onSuccess { json ->
                    var message = activity.getString(R.string.ahoy_review_done, json.optString("summary"))
                    if (deep) {
                        val hours = json.optString("deep_hours")
                        message += "\n\n" + activity.getString(R.string.ahoy_deep_review_queued, json.optInt("deep_queued")) +
                            " " + if (hours.isBlank()) activity.getString(R.string.ahoy_deep_review_anytime)
                            else activity.getString(R.string.ahoy_deep_review_hours, hours)
                    }
                    ConfirmationDialog(
                        activity = activity,
                        message = message,
                        positive = org.fossify.commons.R.string.ok,
                        negative = 0
                    ) {}
                }.onFailure { showFailure(it) }
            }
        }
    }

    private fun showFailure(error: Throwable) {
        val message = if (error is IllegalStateException) {
            activity.getString(R.string.ahoy_one_to_one_only)
        } else {
            activity.getString(R.string.ahoy_failed, error.message ?: error.javaClass.simpleName)
        }
        activity.toast(message, Toast.LENGTH_LONG)
    }
}
