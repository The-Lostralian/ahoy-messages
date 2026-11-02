package org.fossify.messages.ahoy

import android.os.Bundle
import org.fossify.commons.extensions.getProperPrimaryColor
import org.fossify.commons.extensions.hideKeyboard
import org.fossify.commons.extensions.updateTextColors
import org.fossify.commons.extensions.value
import org.fossify.commons.extensions.viewBinding
import org.fossify.commons.helpers.NavigationIcon
import org.fossify.commons.helpers.ensureBackgroundThread
import org.fossify.messages.R
import org.fossify.messages.activities.SimpleActivity
import org.fossify.messages.databinding.ActivityAhoySettingsBinding

/** Server address, shared key and an on/off switch, with a test that the server accepts the key. */
class AhoySettingsActivity : SimpleActivity() {
    private val binding by viewBinding(ActivityAhoySettingsBinding::inflate)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(binding.root)
        setupEdgeToEdge(padBottomImeAndSystem = listOf(binding.ahoySettingsScroll))
        setupMaterialScrollListener(binding.ahoySettingsScroll, binding.ahoySettingsAppbar)

        val store = AhoyStore.get(this)
        binding.ahoyEnabled.isChecked = store.enabled
        binding.ahoyServerUrl.setText(store.serverUrl)
        binding.ahoyKey.setText(store.key)
        binding.ahoySaveAndTest.setOnClickListener { saveAndTest() }
    }

    override fun onResume() {
        super.onResume()
        setupTopAppBar(binding.ahoySettingsAppbar, NavigationIcon.Arrow)
        updateTextColors(binding.ahoySettingsScroll)
        binding.ahoySaveAndTest.setTextColor(getProperPrimaryColor())
    }

    override fun onPause() {
        super.onPause()
        save()
    }

    private fun save() {
        val store = AhoyStore.get(this)
        store.enabled = binding.ahoyEnabled.isChecked
        store.serverUrl = binding.ahoyServerUrl.value.ifBlank { AhoyStore.DEFAULT_SERVER_URL }
        store.key = binding.ahoyKey.value
    }

    private fun saveAndTest() {
        hideKeyboard()
        save()
        val store = AhoyStore.get(this)
        if (runCatching { AhoyClient.decodeKey(store.key) }.isFailure) {
            binding.ahoyTestResult.text = getString(R.string.ahoy_key_invalid)
            return
        }
        binding.ahoyTestResult.text = getString(R.string.ahoy_testing)
        ensureBackgroundThread {
            // "forget" on a number that can't exist: proves the key works without changing anything
            val message = try {
                store.client().forget("0000000000")
                getString(R.string.ahoy_test_ok)
            } catch (e: Exception) {
                getString(R.string.ahoy_failed, e.message ?: e.javaClass.simpleName)
            }
            runOnUiThread { binding.ahoyTestResult.text = message }
        }
    }
}
