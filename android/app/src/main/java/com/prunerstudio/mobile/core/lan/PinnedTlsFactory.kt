package com.prunerstudio.mobile.core.lan

import java.net.InetAddress
import java.net.Socket
import java.security.MessageDigest
import java.security.cert.*
import java.util.Base64
import javax.net.ssl.*
import okhttp3.*

/**
 * TLS pin verifier supporting:
 * 1) SPKI public-key SHA-256 pinning (preferred — survives desktop SAN-based leaf cert rotation):
 *    - Hex format: `spki-sha256:<64-lowercase-hex>`
 *    - RFC 7469 Base64 format: `sha256/<44-char-base64>`
 *    Computed as `SHA-256(X509Certificate.publicKey.encoded)` (DER SubjectPublicKeyInfo).
 * 2) Backward-compatible leaf certificate DER SHA-256 pinning:
 *    - Bare `<64-hex>` or `cert-sha256:<64-hex>`
 *    Computed as `SHA-256(X509Certificate.encoded)`.
 *
 * When [spkiPin] is supplied alongside a legacy [fingerprint], SPKI verification takes precedence.
 */
class PinnedTlsFactory(fingerprint: String, spkiPin: String? = null) {
    private enum class PinMode {
        CERT_SHA256,
        SPKI_SHA256,
    }

    private data class ParsedPin(val mode: PinMode, val digest: ByteArray)

    private val parsed: ParsedPin =
        spkiPin?.takeIf { it.isNotBlank() }?.let { parsePin(it, defaultSpki = true) }
            ?: parsePin(fingerprint, defaultSpki = false)

    val isSpkiPinned: Boolean
        get() = parsed.mode == PinMode.SPKI_SHA256

    private fun matches(cert: Certificate?): Boolean {
        if (cert == null) return false
        val targetBytes =
            when (parsed.mode) {
                PinMode.SPKI_SHA256 -> cert.publicKey?.encoded ?: return false
                PinMode.CERT_SHA256 -> cert.encoded ?: return false
            }
        return MessageDigest.isEqual(
            parsed.digest,
            MessageDigest.getInstance("SHA-256").digest(targetBytes),
        )
    }

    val trustManager =
        object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

            override fun checkClientTrusted(chain: Array<X509Certificate>, type: String) {
                throw CertificateException("Client authentication unsupported")
            }

            override fun checkServerTrusted(chain: Array<X509Certificate>, type: String) {
                if (!matches(chain.firstOrNull())) {
                    val label =
                        if (parsed.mode == PinMode.SPKI_SHA256) "Desktop SPKI public-key pin mismatch"
                        else "Desktop certificate fingerprint mismatch"
                    throw CertificateException(label)
                }
            }
        }

    companion object {
        private val HEX_64 = Regex("[A-Fa-f0-9]{64}")

        private fun hexToBytes(hex: String): ByteArray {
            require(HEX_64.matches(hex)) { "Invalid SHA-256 hex pin" }
            return hex.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
        }

        private fun parsePin(raw: String, defaultSpki: Boolean): ParsedPin {
            val trimmed = raw.trim()
            return when {
                trimmed.startsWith("spki-sha256:", ignoreCase = true) ->
                    ParsedPin(PinMode.SPKI_SHA256, hexToBytes(trimmed.substringAfter(':')))
                trimmed.startsWith("sha256/", ignoreCase = true) -> {
                    val b64 = trimmed.substringAfter('/')
                    val decoded =
                        runCatching { Base64.getDecoder().decode(b64) }.getOrElse {
                            throw IllegalArgumentException("Invalid Base64 SPKI pin", it)
                        }
                    require(decoded.size == 32) { "SPKI SHA-256 digest must be 32 bytes" }
                    ParsedPin(PinMode.SPKI_SHA256, decoded)
                }
                trimmed.startsWith("cert-sha256:", ignoreCase = true) ->
                    ParsedPin(PinMode.CERT_SHA256, hexToBytes(trimmed.substringAfter(':')))
                HEX_64.matches(trimmed) ->
                    ParsedPin(
                        if (defaultSpki) PinMode.SPKI_SHA256 else PinMode.CERT_SHA256,
                        hexToBytes(trimmed),
                    )
                else -> throw IllegalArgumentException("Unsupported TLS pin format")
            }
        }

        fun normalizeSpkiPin(raw: String?): String? {
            val trimmed = raw?.trim().orEmpty()
            if (trimmed.isEmpty()) return null
            val parsed = parsePin(trimmed, defaultSpki = true)
            require(parsed.mode == PinMode.SPKI_SHA256)
            return "spki-sha256:" + parsed.digest.joinToString("") { "%02x".format(it) }
        }

        fun isValidPin(raw: String): Boolean = runCatching { parsePin(raw, false) }.isSuccess

        fun computeSpkiPin(cert: Certificate): String {
            val digest = MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded)
            return "spki-sha256:" + digest.joinToString("") { "%02x".format(it) }
        }

        fun computeSpkiBase64Pin(cert: Certificate): String {
            val digest = MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded)
            return "sha256/" + Base64.getEncoder().encodeToString(digest)
        }

        fun computeCertFingerprint(cert: Certificate): String {
            val digest = MessageDigest.getInstance("SHA-256").digest(cert.encoded)
            return digest.joinToString("") { "%02x".format(it) }
        }
    }
    private val delegate =
        SSLContext.getInstance("TLS")
            .apply { init(null, arrayOf(trustManager), null) }
            .socketFactory
    val socketFactory: SSLSocketFactory =
        object : SSLSocketFactory() {
            private fun secure(socket: Socket): Socket =
                (socket as SSLSocket).apply {
                    enabledProtocols =
                        supportedProtocols
                            .filter { it == "TLSv1.3" || it == "TLSv1.2" }
                            .toTypedArray()
                }

            override fun getDefaultCipherSuites(): Array<String> = delegate.defaultCipherSuites

            override fun getSupportedCipherSuites(): Array<String> = delegate.supportedCipherSuites

            override fun createSocket() = secure(delegate.createSocket())

            override fun createSocket(socket: Socket, host: String, port: Int, close: Boolean) =
                secure(delegate.createSocket(socket, host, port, close))

            override fun createSocket(host: String, port: Int) =
                secure(delegate.createSocket(host, port))

            override fun createSocket(host: InetAddress, port: Int) =
                secure(delegate.createSocket(host, port))

            override fun createSocket(host: String, port: Int, local: InetAddress, localPort: Int) =
                secure(delegate.createSocket(host, port, local, localPort))

            override fun createSocket(
                host: InetAddress,
                port: Int,
                local: InetAddress,
                localPort: Int,
            ) = secure(delegate.createSocket(host, port, local, localPort))
        }
    val hostnameVerifier = HostnameVerifier { _, session ->
        runCatching { matches(session.peerCertificates.firstOrNull()) }.getOrDefault(false)
    }

    fun clientBuilder() =
        OkHttpClient.Builder()
            .sslSocketFactory(socketFactory, trustManager)
            .hostnameVerifier(hostnameVerifier)
            .connectionSpecs(
                listOf(
                    ConnectionSpec.Builder(ConnectionSpec.MODERN_TLS)
                        .tlsVersions(TlsVersion.TLS_1_3, TlsVersion.TLS_1_2)
                        .build()
                )
            )
            .followRedirects(false)
            .followSslRedirects(false)
            .retryOnConnectionFailure(false)
}
