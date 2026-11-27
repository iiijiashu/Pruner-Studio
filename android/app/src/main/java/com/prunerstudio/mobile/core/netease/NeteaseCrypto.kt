package com.prunerstudio.mobile.core.netease

import com.prunerstudio.mobile.core.model.*
import java.math.BigInteger
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.Locale
import javax.crypto.Cipher
import javax.crypto.spec.*
import kotlinx.serialization.json.*

object NeteaseCrypto {
    private val random = SecureRandom()
    @Volatile private var defaultDeviceId: String = ByteArray(16).also(random::nextBytes).hex()
    private val modulus =
        BigInteger(
            "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
            16,
        )

    fun setDefaultDeviceId(id: String) {
        if (id.isNotBlank()) defaultDeviceId = id.trim()
    }

    private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }

    private fun ecb(bytes: ByteArray, mode: Int) =
        Cipher.getInstance("AES/ECB/PKCS5Padding").run {
            init(mode, SecretKeySpec("e82ckenh8dichen8".toByteArray(), "AES"))
            doFinal(bytes)
        }

    fun eapiEncrypt(url: String, payloadJson: String): Map<String, String> {
        val path = if (url.startsWith('/')) url else "/$url"
        val json = wire.parseToJsonElement(payloadJson).jsonObject.toString()
        val digest =
            MessageDigest.getInstance("MD5")
                .digest("nobody${path}use${json}md5forencrypt".toByteArray())
                .hex()
        return mapOf(
            "params" to
                ecb(
                        "$path-36cd479b6b5-$json-36cd479b6b5-$digest".toByteArray(),
                        Cipher.ENCRYPT_MODE,
                    )
                    .hex()
                    .uppercase()
        )
    }

    fun eapiDecrypt(hexCiphertext: String): ByteArray {
        val hex = hexCiphertext.trim()
        require(hex.length % 2 == 0)
        return eapiDecrypt(hex.chunked(2).map { it.toInt(16).toByte() }.toByteArray())
    }

    fun eapiDecrypt(raw: ByteArray) = ecb(raw, Cipher.DECRYPT_MODE)

    private fun cookies(cookie: String?) =
        cookie
            .orEmpty()
            .split(';')
            .filter { '=' in it }
            .associate { it.substringBefore('=').trim() to it.substringAfter('=').trim() }
            .filterKeys { it.isNotEmpty() }

    fun buildEapiHeader(
        cookie: String? = null,
        deviceId: String? = null,
        requestId: String? = null,
        buildVer: String? = null,
    ): Map<String, String> {
        val pairs = cookies(cookie)
        val now = System.currentTimeMillis()
        return linkedMapOf(
                "osver" to "Microsoft-Windows-10-Professional-build-22631-64bit",
                "deviceId" to
                    (deviceId?.takeIf(String::isNotEmpty)
                        ?: pairs["deviceId"]?.takeIf(String::isNotEmpty)
                        ?: defaultDeviceId),
                "os" to "pc",
                "appver" to "3.1.3.203419",
                "versioncode" to "140",
                "mobilename" to "",
                "buildver" to (buildVer?.takeIf(String::isNotEmpty) ?: (now / 1000).toString()),
                "resolution" to "1920x1080",
                "__csrf" to pairs["__csrf"].orEmpty(),
                "channel" to "netease",
                "requestId" to
                    (requestId ?: "%d_%04d".format(Locale.ROOT, now, random.nextInt(10000))),
            )
            .apply {
                putAll(
                    pairs
                        .filter { it.key in setOf("MUSIC_U", "MUSIC_A") && it.value.isNotEmpty() }
                        .toSortedMap(reverseOrder())
                )
            }
    }

    fun buildEapiCookie(cookie: String?, header: Map<String, String> = buildEapiHeader(cookie)) =
        (listOf("os", "appver", "channel", "osver", "deviceId").associateWith {
                header[it] ?: buildEapiHeader()[it].orEmpty()
            } + cookies(cookie))
            .filterValues { it.isNotEmpty() }
            .entries
            .joinToString("; ") { "${it.key}=${it.value}" }

    fun buildPlayerUrlV1Payload(
        songIds: List<Long>,
        level: String = "jymaster",
        cookie: String? = null,
        header: Map<String, String> = buildEapiHeader(cookie),
    ) = buildJsonObject {
        val quality =
            AudioQualityLevel.entries.find { it.name == level.trim().lowercase() }
                ?: AudioQualityLevel.jymaster
        put("ids", songIds.joinToString(prefix = "[", postfix = "]"))
        put("level", quality.name)
        put(
            "encodeType",
            if (quality.ordinal <= AudioQualityLevel.lossless.ordinal) "flac" else "aac",
        )
        put("header", JsonObject(header.mapValues { JsonPrimitive(it.value) }).toString())
        if (quality == AudioQualityLevel.sky) put("immerseType", "c51")
    }

    private fun cbc(text: String, key: String) =
        Cipher.getInstance("AES/CBC/PKCS5Padding").run {
            init(
                Cipher.ENCRYPT_MODE,
                SecretKeySpec(key.toByteArray(), "AES"),
                IvParameterSpec("0102030405060708".toByteArray()),
            )
            Base64.getEncoder().encodeToString(doFinal(text.toByteArray()))
        }

    fun weapiEncrypt(payloadJson: String, secretKeyOverride: String? = null): Map<String, String> {
        val alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        val key = secretKeyOverride ?: (1..16).map { alphabet[random.nextInt(62)] }.joinToString("")
        require(key.toByteArray().size == 16)
        val params =
            cbc(
                cbc(wire.parseToJsonElement(payloadJson).jsonObject.toString(), "0CoJUm6Qyw8W8jud"),
                key,
            )
        return mapOf(
            "params" to params,
            "encSecKey" to
                BigInteger(1, key.reversed().toByteArray())
                    .modPow(BigInteger.valueOf(65537), modulus)
                    .toString(16)
                    .padStart(256, '0'),
        )
    }
}
