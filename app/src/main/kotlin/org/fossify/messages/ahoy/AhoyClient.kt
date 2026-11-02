package org.fossify.messages.ahoy

import android.util.Base64
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.SecureRandom
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Talks to sms-triage on the home server. Every body is AES-256-GCM encrypted with the shared key,
 * because the traffic goes through Cloudflare's proxy, which could otherwise read the texts.
 *
 * Protocol (must match sms.py / sms_client.py on the server): the request JSON gets "ts" (unix
 * seconds) and "rid" (random id), is encrypted with a fresh 12-byte nonce and the endpoint path as
 * associated data, and is posted as {"v":1,"n":base64(nonce),"c":base64(ciphertext+tag)}. The
 * response comes back in the same envelope with "<path>#<rid>" as associated data.
 */
class AhoyClient(private val baseUrl: String, keyBase64: String) {

    class NotConfigured : IOException("Server address or key not set")
    class Refused : IOException("The server refused the key (check it matches SMS_SHARED_KEY)")
    class ServerError(message: String) : IOException(message)

    private val key = SecretKeySpec(decodeKey(keyBase64), "AES")
    private val random = SecureRandom()

    /** [sentAt] identifies the text, so a retry of one the server already handled isn't alerted twice. */
    fun incoming(
        number: String, name: String, isContact: Boolean, body: String, thread: List<ThreadLine>, sentAt: Long,
        late: Boolean = false,
    ): JSONObject =
        call("/sms/incoming", JSONObject()
            .put("number", number).put("name", name).put("is_contact", isContact)
            .put("body", body).put("thread", ThreadLine.toJson(thread)).put("sent_at", sentAt).put("late", late),
            timeoutMs = if (late) 60_000 else 6_000, connectMs = if (late) 10_000 else 4_000)

    fun draft(number: String, name: String, thread: List<ThreadLine>): String =
        call("/sms/draft", JSONObject()
            .put("number", number).put("name", name).put("thread", ThreadLine.toJson(thread)), timeoutMs = 90_000)
            .optString("draft")

    fun review(number: String, name: String, messages: List<ThreadLine>): JSONObject =
        call("/sms/review", JSONObject()
            .put("number", number).put("name", name).put("messages", ThreadLine.toJson(messages)), timeoutMs = 150_000)

    fun forget(number: String): JSONObject =
        call("/sms/forget", JSONObject().put("number", number), timeoutMs = 20_000)

    private fun call(path: String, payload: JSONObject, timeoutMs: Int, connectMs: Int = 10_000): JSONObject {
        val rid = UUID.randomUUID().toString().replace("-", "")
        payload.put("ts", System.currentTimeMillis() / 1000).put("rid", rid)
        val body = seal(payload.toString().toByteArray(), path)

        val connection = URL(baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "POST"
            connection.connectTimeout = connectMs
            connection.readTimeout = timeoutMs
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            connection.outputStream.use { it.write(body) }

            val status = connection.responseCode
            if (status == 401) throw Refused()
            val stream = if (status < 400) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (text.isEmpty()) throw ServerError("Server answered HTTP $status")
            val result = JSONObject(String(unseal(text, "$path#$rid")))
            if (status >= 400) throw ServerError(result.optString("error", "Server answered HTTP $status"))
            return result
        } finally {
            connection.disconnect()
        }
    }

    private fun seal(plain: ByteArray, aad: String): ByteArray {
        val nonce = ByteArray(12).also { random.nextBytes(it) }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(128, nonce))
        cipher.updateAAD(aad.toByteArray())
        val sealed = cipher.doFinal(plain)
        return JSONObject()
            .put("v", 1)
            .put("n", Base64.encodeToString(nonce, Base64.NO_WRAP))
            .put("c", Base64.encodeToString(sealed, Base64.NO_WRAP))
            .toString().toByteArray()
    }

    private fun unseal(envelope: String, aad: String): ByteArray {
        val json = JSONObject(envelope)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, Base64.decode(json.getString("n"), Base64.NO_WRAP)))
        cipher.updateAAD(aad.toByteArray())
        return cipher.doFinal(Base64.decode(json.getString("c"), Base64.NO_WRAP))
    }

    companion object {
        fun decodeKey(keyBase64: String): ByteArray {
            val key = try {
                Base64.decode(keyBase64.trim(), Base64.NO_WRAP)
            } catch (_: IllegalArgumentException) {
                ByteArray(0)
            }
            if (key.size != 32) throw NotConfigured()
            return key
        }
    }
}

/** One message of a conversation, as the server sees it. */
data class ThreadLine(val fromMe: Boolean, val body: String) {
    companion object {
        fun toJson(lines: List<ThreadLine>) = org.json.JSONArray().apply {
            lines.forEach { put(JSONObject().put("from_me", it.fromMe).put("body", it.body)) }
        }
    }
}
