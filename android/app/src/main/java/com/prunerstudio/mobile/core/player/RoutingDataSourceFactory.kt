package com.prunerstudio.mobile.core.player

import android.content.Context
import android.net.Uri
import androidx.media3.common.C
import androidx.media3.datasource.*
import androidx.media3.datasource.okhttp.OkHttpDataSource
import com.prunerstudio.mobile.core.lan.LanSyncClient
import com.prunerstudio.mobile.core.sources.StreamAccess
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import okhttp3.Call
import okhttp3.OkHttpClient

internal interface CancellableTransfer {
    fun cancelTransfer()
}

@androidx.media3.common.util.UnstableApi
class RoutingDataSourceFactory internal constructor(
    private val context: Context,
    private val pairing: () -> LanSyncClient?,
    private val streamAccess: ((Uri) -> StreamAccess?)? = null,
    private val allowExternalUri: ((Uri) -> Boolean)? = null,
    private val baseHttp: OkHttpClient =
        OkHttpClient.Builder().followRedirects(false).followSslRedirects(false).build(),
) : DataSource.Factory {
    private val local = DefaultDataSource.Factory(context)

    private val lanLocalStreamRegex = Regex("/api/lan/v1/local-library/stream/[0-9a-f]{24}")
    private val lanProxyStreamRegex =
        Regex("/api/lan/v1/(?:audio/)?stream/[A-Za-z0-9._-]{8,128}")

    override fun createDataSource(): DataSource =
        object : DataSource, CancellableTransfer {
            @Volatile private var selected: DataSource? = null
            private val cancelled = AtomicBoolean()
            private val activeCall = AtomicReference<Call>()

            override fun cancelTransfer() {
                cancelled.set(true)
                activeCall.get()?.cancel()
            }

            private fun track(call: Call): Call {
                activeCall.set(call)
                if (cancelled.get()) call.cancel()
                return call
            }

            private fun httpFactory(access: StreamAccess? = null): OkHttpDataSource.Factory {
                val client = access?.resolverPolicy?.client(baseHttp) ?: baseHttp
                return OkHttpDataSource.Factory(Call.Factory { request ->
                    val headers = access?.headersFor(request.url).orEmpty()
                    track(client.newCall(request.newBuilder().apply {
                        headers.forEach { (key, value) -> header(key, value) }
                    }.build()))
                })
            }
            private var activeLan: LanSyncClient? = null
            private var releaseLanStream: (() -> Unit)? = null
            private val lock = Any()
            private val listeners = mutableListOf<TransferListener>()

            private fun isLanRevoked(expected: LanSyncClient?): Boolean =
                expected != null && (expected.isClosed || pairing() !== expected)

            override fun addTransferListener(listener: TransferListener) {
                synchronized(lock) {
                    listeners += listener
                    selected?.addTransferListener(listener)
                }
            }

            override fun open(dataSpec: DataSpec): Long {
                if (cancelled.get()) throw IOException("Audio transfer cancelled")
                val uri = dataSpec.uri
                val access = dataSpec.customData as? StreamAccess ?: streamAccess?.invoke(uri)
                val factory =
                    when (uri.scheme?.lowercase()) {
                        "file",
                        "content" -> local
                        "https",
                        "http" ->
                            if (access != null) {
                                httpFactory(access)
                            } else if (
                                uri.scheme.equals("https", ignoreCase = true) &&
                                    uri.encodedPath.orEmpty().startsWith("/api/lan/")
                            ) {
                                val lan = pairing() ?: throw IOException("Desktop pairing required")
                                if (lan.isClosed) throw IOException("Desktop pairing revoked")
                                val path = uri.encodedPath.orEmpty()
                                val validPath =
                                    lanLocalStreamRegex.matches(path) ||
                                        lanProxyStreamRegex.matches(path)
                                if (
                                    uri.host != lan.base.host ||
                                        (if (uri.port == -1) 443 else uri.port) != lan.base.port ||
                                        !validPath
                                )
                                    throw IOException("Unrecognized desktop stream")
                                val streamCall = AtomicReference<Call>()
                                val untrack = lan.trackStream { exemptCall ->
                                    streamCall.get()?.takeUnless { it === exemptCall }?.cancel()
                                }
                                activeLan = lan
                                releaseLanStream = {
                                    untrack()
                                    streamCall.getAndSet(null)?.cancel()
                                }
                                val callFactory = Call.Factory { request ->
                                    track(lan.readClient.newCall(request)).also { call ->
                                        streamCall.set(call)
                                        if (isLanRevoked(lan)) call.cancel()
                                    }
                                }
                                OkHttpDataSource.Factory(callFactory)
                            } else if (
                                uri.scheme == "https" &&
                                    uri.host?.let {
                                        it.endsWith(".126.net") || it.endsWith(".music.163.com")
                                    } == true
                            ) {
                                httpFactory()
                            } else if (allowExternalUri?.invoke(uri) == true) {
                                httpFactory()
                            } else {
                                throw IOException("Unrecognized audio CDN")
                            }
                        else -> throw IOException("Unsupported audio scheme")
                    }
                try {
                    val delegate = factory.createDataSource()
                    synchronized(lock) {
                        selected = delegate
                        listeners.forEach(delegate::addTransferListener)
                    }
                    val length = delegate.open(dataSpec)
                    if (isLanRevoked(activeLan)) throw IOException("Desktop pairing revoked")
                    return length
                } catch (exc: Throwable) {
                    runCatching { close() }
                    throw exc
                }
            }

            override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
                val expected = activeLan
                if (isLanRevoked(expected)) {
                    close()
                    throw IOException("Desktop pairing revoked")
                }
                val delegate = selected ?: return C.RESULT_END_OF_INPUT
                val bytesRead =
                    try {
                        delegate.read(buffer, offset, length)
                    } catch (exc: IOException) {
                        if (isLanRevoked(expected)) {
                            buffer.fill(0, offset, offset + length)
                            runCatching { close() }
                            throw IOException("Desktop pairing revoked", exc)
                        }
                        throw exc
                    }
                if (isLanRevoked(expected)) {
                    if (bytesRead > 0) buffer.fill(0, offset, offset + bytesRead)
                    close()
                    throw IOException("Desktop pairing revoked")
                }
                return bytesRead
            }

            override fun getUri(): Uri? = selected?.uri

            override fun getResponseHeaders(): Map<String, List<String>> =
                selected?.responseHeaders ?: emptyMap()

            override fun close() {
                val delegate = synchronized(lock) { selected.also { selected = null } }
                activeLan = null
                val release = releaseLanStream
                releaseLanStream = null
                release?.invoke()
                delegate?.close()
                activeCall.set(null)
            }
        }
}
