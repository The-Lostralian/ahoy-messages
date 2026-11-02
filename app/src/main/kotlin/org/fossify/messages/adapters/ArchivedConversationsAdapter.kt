package org.fossify.messages.adapters

import android.view.Menu
import org.fossify.commons.dialogs.ConfirmationDialog
import org.fossify.commons.extensions.notificationManager
import org.fossify.commons.helpers.ensureBackgroundThread
import org.fossify.commons.views.MyRecyclerView
import org.fossify.messages.R
import org.fossify.messages.activities.SimpleActivity
import org.fossify.messages.ahoy.AhoyStore
import org.fossify.messages.extensions.deleteConversation
import org.fossify.messages.extensions.updateConversationArchivedStatus
import org.fossify.messages.helpers.refreshConversations
import org.fossify.messages.models.Conversation

class ArchivedConversationsAdapter(
    activity: SimpleActivity, recyclerView: MyRecyclerView, onRefresh: () -> Unit, itemClick: (Any) -> Unit,
    private val filteredMode: Boolean = false,
) : BaseConversationsAdapter(activity, recyclerView, onRefresh, itemClick) {
    override fun getActionMenuId() = R.menu.cab_archived_conversations

    // Ahoy: on the Filtered screen, "unarchive" becomes "Not spam"
    override fun prepareActionMode(menu: Menu) {
        if (filteredMode) menu.findItem(R.id.cab_unarchive)?.title = activity.getString(R.string.ahoy_not_spam)
    }

    override fun actionItemPressed(id: Int) {
        if (selectedKeys.isEmpty()) {
            return
        }

        when (id) {
            R.id.cab_delete -> askConfirmDelete()
            R.id.cab_unarchive -> unarchiveConversation()
            R.id.cab_select_all -> selectAll()
        }
    }

    private fun askConfirmDelete() {
        val itemsCnt = selectedKeys.size
        val items = resources.getQuantityString(R.plurals.delete_conversations, itemsCnt, itemsCnt)

        val baseString = org.fossify.commons.R.string.deletion_confirmation
        val question = String.format(resources.getString(baseString), items)

        ConfirmationDialog(activity, question) {
            ensureBackgroundThread {
                deleteConversations()
            }
        }
    }

    private fun deleteConversations() {
        if (selectedKeys.isEmpty()) {
            return
        }

        val conversationsToRemove = currentList.filter { selectedKeys.contains(it.hashCode()) } as ArrayList<Conversation>
        conversationsToRemove.forEach {
            activity.deleteConversation(it.threadId)
            activity.notificationManager.cancel(it.threadId.hashCode())
        }

        removeConversationsFromList(conversationsToRemove)
    }

    private fun unarchiveConversation() {
        if (selectedKeys.isEmpty()) {
            return
        }

        ensureBackgroundThread {
            val conversationsToUnarchive = currentList.filter { selectedKeys.contains(it.hashCode()) } as ArrayList<Conversation>
            conversationsToUnarchive.forEach {
                if (filteredMode) {
                    AhoyStore.get(activity).setFiltered(it.threadId, false)
                } else {
                    activity.updateConversationArchivedStatus(it.threadId, false)
                }
            }

            removeConversationsFromList(conversationsToUnarchive)
        }
    }

    private fun removeConversationsFromList(removedConversations: List<Conversation>) {
        val newList = try {
            currentList.toMutableList().apply { removeAll(removedConversations) }
        } catch (ignored: Exception) {
            currentList.toMutableList()
        }

        activity.runOnUiThread {
            if (newList.none { selectedKeys.contains(it.hashCode()) }) {
                refreshConversations()
                finishActMode()
            } else {
                submitList(newList)
                if (newList.isEmpty()) {
                    refreshConversations()
                }
            }
        }
    }
}
