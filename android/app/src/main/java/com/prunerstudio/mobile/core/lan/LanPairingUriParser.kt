package com.prunerstudio.mobile.core.lan

import java.net.URI
import java.net.URLDecoder
import okhttp3.HttpUrl

data class PairingUri(
    val host: String,
    val port: Int,
    val code: String,
    val fingerprint: String,
    val name: String,
    val spkiPin: String? = null,
)

object LanPairingUriParser {
    fun parse(text: String): PairingUri {
        val uri = URI(text)
        require(
            uri.scheme == "pruner-lan" &&
                uri.rawAuthority == "pair" &&
                uri.path.isEmpty() &&
                uri.fragment == null
        )
        val pairs =
            requireNotNull(uri.rawQuery).split('&').map {
                require('=' in it)
                URLDecoder.decode(it.substringBefore('='), "UTF-8") to
                    URLDecoder.decode(it.substringAfter('='), "UTF-8")
            }
        val q = pairs.toMap()
        require(q.size == pairs.size && q["v"] == "1" && q["tls"] == "1")
        val host = requireNotNull(q["host"])
        require(host.matches(Regex("[A-Za-z0-9.:\\[\\]-]+")))
        val canonical = HttpUrl.Builder().scheme("https").host(host).build().host
        require(
            ':' in canonical ||
                canonical.split('.').all {
                    it.matches(Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"))
                }
        )
        if (canonical.all { it.isDigit() || it == '.' })
            require(
                canonical.split('.').let {
                    it.size == 4 && it.all { s -> s.toIntOrNull() in 0..255 }
                }
            )
        val rawFp = requireNotNull(q["fp"]).trim()
        require(PinnedTlsFactory.isValidPin(rawFp))
        val normalizedFp =
            if (rawFp.startsWith("sha256/", ignoreCase = true)) rawFp
            else rawFp.lowercase()
        val spki =
            q["spki"]?.let { PinnedTlsFactory.normalizeSpkiPin(it) }
                ?: if (
                    normalizedFp.startsWith("spki-sha256:") ||
                        normalizedFp.startsWith("sha256/", ignoreCase = true)
                )
                    PinnedTlsFactory.normalizeSpkiPin(normalizedFp)
                else null
        return PairingUri(
            canonical,
            requireNotNull(q["port"]?.toIntOrNull()).also { require(it in 1024..65535) },
            requireNotNull(q["code"]).also { require(it.matches(Regex("[0-9]{6}"))) },
            normalizedFp,
            q["name"].orEmpty(),
            spkiPin = spki,
        )
    }
}
