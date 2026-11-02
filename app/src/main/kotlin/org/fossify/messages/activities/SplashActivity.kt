package org.fossify.messages.activities

import android.content.Intent
import android.os.Bundle
import org.fossify.commons.activities.BaseSplashActivity
import org.fossify.commons.extensions.baseConfig
import org.fossify.commons.helpers.SIDELOADING_FALSE

class SplashActivity : BaseSplashActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        // Ahoy: Fossify's splash calls any build it didn't publish a fake app and stops there
        baseConfig.appSideloadingStatus = SIDELOADING_FALSE
        super.onCreate(savedInstanceState)
    }

    override fun initActivity() {
        startActivity(Intent(this, MainActivity::class.java))
        finish()
    }
}
