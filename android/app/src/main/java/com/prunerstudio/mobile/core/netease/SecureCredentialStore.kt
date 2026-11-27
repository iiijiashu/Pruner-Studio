package com.prunerstudio.mobile.core.netease

import android.content.Context
import android.security.keystore.*
import com.prunerstudio.mobile.core.model.*
import java.security.KeyStore
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.*
import javax.crypto.spec.GCMParameterSpec
import kotlinx.serialization.encodeToString

interface SecureCredentialStore {
    var cookie: String?
    var pairing: PairedServerConfig?

    var deviceId: String?
        get() = null
        set(_) {}

    fun readSecret(key: String): String? = null

    fun writeSecret(key: String, value: String?) {}

    fun getOrCreateDeviceId(): String =
        synchronized(this) {
            deviceId?.takeIf { it.matches(Regex("[a-f0-9]{32}")) }
                ?: ByteArray(16)
                    .also { SecureRandom().nextBytes(it) }
                    .joinToString("") { "%02x".format(it) }
                    .also { deviceId = it }
        }

    fun revoke(expected: PairedServerConfig) =
        synchronized(this) { if (pairing == expected) pairing = null }
}

class InMemoryCredentialStore : SecureCredentialStore {
    @get:Synchronized @set:Synchronized override var cookie: String? = null
    @get:Synchronized @set:Synchronized override var pairing: PairedServerConfig? = null
    @get:Synchronized @set:Synchronized override var deviceId: String? = null
    private val secrets = mutableMapOf<String, String>()

    @Synchronized override fun readSecret(key: String): String? = secrets[key]

    @Synchronized
    override fun writeSecret(key: String, value: String?) {
        if (value == null) secrets.remove(key) else secrets[key] = value
    }
}

class AndroidKeystoreCredentialStore(context: Context) : SecureCredentialStore {
    private val prefs by lazy { context.getSharedPreferences("credentials", Context.MODE_PRIVATE) }
    private val alias = "pruner.credentials.v1"

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        return store.getKey(alias, null) as? SecretKey
            ?: KeyGenerator.getInstance("AES", "AndroidKeyStore").run {
                init(
                    KeyGenParameterSpec.Builder(
                            alias,
                            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                        )
                        .setKeySize(256)
                        .setBlockModes("GCM")
                        .setEncryptionPaddings("NoPadding")
                        .build()
                )
                generateKey()
            }
    }

    @Synchronized
    private fun read(name: String): String? = runCatching {
        val blob = prefs.getString(name, null)?.let(Base64.getDecoder()::decode) ?: return@runCatching null
        require(blob.size >= 28)
        Cipher.getInstance("AES/GCM/NoPadding").run {
            init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, blob.copyOfRange(0, 12)))
            updateAAD(name.toByteArray())
            doFinal(blob, 12, blob.size - 12).decodeToString()
        }
    }.getOrNull()

    @Synchronized
    private fun write(name: String, value: String?) {
        val edit = prefs.edit()
        if (value == null) edit.remove(name)
        else
            Cipher.getInstance("AES/GCM/NoPadding").run {
                init(Cipher.ENCRYPT_MODE, key())
                updateAAD(name.toByteArray())
                edit.putString(
                    name,
                    Base64.getEncoder().encodeToString(iv + doFinal(value.toByteArray())),
                )
            }
        check(edit.commit())
    }

    override var cookie: String?
        get() = read("cookie")
        set(value) = write("cookie", value)

    override var pairing: PairedServerConfig?
        get() = read("pairing")?.let { runCatching { wire.decodeFromString<PairedServerConfig>(it) }.getOrNull() }
        set(value) = write("pairing", value?.let { wire.encodeToString(it) })

    override var deviceId: String?
        get() = read("device_id")
        set(value) = write("device_id", value)

    override fun readSecret(key: String): String? = read("secret:$key")

    override fun writeSecret(key: String, value: String?) = write("secret:$key", value)
}
