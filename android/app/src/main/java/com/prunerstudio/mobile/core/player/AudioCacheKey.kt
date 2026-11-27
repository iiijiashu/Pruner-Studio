package com.prunerstudio.mobile.core.player

import com.prunerstudio.mobile.core.model.AudioQualityLevel
import com.prunerstudio.mobile.core.model.PairedServerConfig
import com.prunerstudio.mobile.core.model.Track
import com.prunerstudio.mobile.core.model.wire
import java.security.MessageDigest
import java.util.UUID
import kotlinx.serialization.encodeToString

internal object AudioCacheKey {
    private const val ATTEMPT = ":transfer:"
    private const val QUALITY = ":q:"

    fun desktopNamespace(pairing: PairedServerConfig): String = "lan:" + hash(
        listOf(pairing.spki_pin ?: pairing.fingerprint, pairing.deviceId, pairing.accountId, pairing.token)
    )

    fun logical(
        account: String,
        track: Track,
        quality: AudioQualityLevel,
        desktopNamespace: String? = null,
    ): String {
        val namespace = desktopNamespace ?: "audio:v2"
        val trackHash = hash(
            listOf(account, track.source_id.orEmpty(), track.effectiveProvider(), track.identity())
        )
        return "$namespace:$trackHash$QUALITY" + quality.name
    }

    fun quality(key: String?): AudioQualityLevel? {
        val name = key?.substringBefore(ATTEMPT)?.substringAfterLast(QUALITY, "")
        return AudioQualityLevel.entries.firstOrNull { it.name == name }
    }

    fun withQuality(key: String, quality: AudioQualityLevel): String =
        key.substringBeforeLast(QUALITY) + QUALITY + quality.name

    fun pairing(key: String?): String? =
        key?.takeIf { it.startsWith("lan:") }?.let { "lan:" + it.substringAfter(':').substringBefore(':') }

    fun isLogical(key: String) = quality(key) != null

    fun attempt(logical: String): String = logical + ATTEMPT + UUID.randomUUID()

    fun isAttemptFor(key: String, logical: String) = key.startsWith(logical + ATTEMPT)

    private fun hash(fields: List<String>): String =
        MessageDigest.getInstance("SHA-256").digest(wire.encodeToString(fields).toByteArray())
            .joinToString("") { "%02x".format(it) }
}
