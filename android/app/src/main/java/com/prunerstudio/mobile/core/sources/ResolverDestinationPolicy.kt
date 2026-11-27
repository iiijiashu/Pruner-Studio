package com.prunerstudio.mobile.core.sources

import java.io.IOException
import java.net.InetAddress
import java.net.Proxy
import java.net.UnknownHostException
import okhttp3.Dns
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient

internal fun HttpUrl.sameOrigin(other: HttpUrl): Boolean =
    scheme == other.scheme && host == other.host && port == other.port

internal class ResolverDestinationPolicy(private val loopbackOrigin: HttpUrl? = null) {
    fun validate(url: HttpUrl): HttpUrl {
        if (url.username.isNotEmpty() || url.password.isNotEmpty()) {
            throw IOException("Resolver URLs must not contain credentials")
        }
        if (url.isHttps) {
            if (isAddressLiteral(url.host) && !isGlobalAddress(InetAddress.getByName(url.host))) {
                throw IOException("Resolver HTTPS destination must be public")
            }
        } else if (loopbackOrigin?.sameOrigin(url) != true) {
            throw IOException("Resolver HTTP requires the explicitly configured loopback origin")
        }
        return url
    }

    fun dns(upstream: Dns): Dns = object : Dns {
        override fun lookup(hostname: String): List<InetAddress> {
            val addresses = upstream.lookup(hostname)
            val requireLoopback = hostname == loopbackOrigin?.host
            val disallowed = addresses.any {
                if (requireLoopback) !it.isLoopbackAddress else !isGlobalAddress(it)
            }
            if (addresses.isEmpty() || disallowed) {
                throw UnknownHostException("Resolver address outside permitted network scope")
            }
            return addresses
        }
    }

    fun client(base: OkHttpClient): OkHttpClient =
        base.newBuilder()
            .dns(dns(base.dns))
            .proxy(Proxy.NO_PROXY)
            .followRedirects(false)
            .followSslRedirects(false)
            .addInterceptor { chain ->
                validate(chain.request().url)
                chain.proceed(chain.request())
            }
            .build()

    companion object {
        private val loopbackHosts = setOf("127.0.0.1", "localhost", "::1")

        fun forConfig(raw: String, allowLoopbackHttp: Boolean): Pair<HttpUrl, ResolverDestinationPolicy> {
            val url = raw.trim().toHttpUrlOrNull() ?: throw IOException("Invalid Resolver URL")
            val policy = ResolverDestinationPolicy(
                url.takeIf { !it.isHttps && allowLoopbackHttp && it.host in loopbackHosts }
            )
            return policy.validate(url) to policy
        }

        private fun isAddressLiteral(host: String): Boolean =
            ':' in host || host.all { it in '0'..'9' || it == '.' }

        internal fun isGlobalAddress(address: InetAddress): Boolean {
            val bytes = address.address.map { it.toInt() and 0xff }
            if (bytes.size == 4) return isGlobalIpv4(bytes)
            if (bytes.size != 16) return false
            if (bytes.take(10).all { it == 0 } && bytes[10] == 255 && bytes[11] == 255) {
                return isGlobalIpv4(bytes.drop(12))
            }
            val (a, b, c, d) = bytes
            return a in 0x20..0x3f &&
                !(a == 0x20 && b == 0x01 && (c <= 1 || c == 0x0d && d == 0xb8)) &&
                !(a == 0x20 && b == 0x02) &&
                !(a == 0x3f && b == 0xff && c < 0x10)
        }

        private fun isGlobalIpv4(bytes: List<Int>): Boolean {
            val (a, b, c) = bytes
            return a in 1..223 && a != 10 && a != 127 &&
                !(a == 100 && b in 64..127) &&
                !(a == 169 && b == 254) &&
                !(a == 172 && b in 16..31) &&
                !(a == 192 && (b == 168 || b == 0 && c in setOf(0, 2) || b == 88 && c == 99)) &&
                !(a == 198 && (b in 18..19 || b == 51 && c == 100)) &&
                !(a == 203 && b == 0 && c == 113)
        }
    }
}

internal data class StreamAccess(
    val headers: Map<String, String>,
    val webDavOrigin: HttpUrl? = null,
    val resolverPolicy: ResolverDestinationPolicy? = null,
) {
    fun headersFor(url: HttpUrl): Map<String, String> {
        if (webDavOrigin?.sameOrigin(url) == false) throw IOException("WebDAV stream origin changed")
        resolverPolicy?.validate(url)
        return headers
    }
}
