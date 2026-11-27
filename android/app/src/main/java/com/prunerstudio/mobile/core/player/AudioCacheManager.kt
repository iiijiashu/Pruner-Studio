package com.prunerstudio.mobile.core.player

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import androidx.core.net.toUri
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheWriter
import androidx.media3.datasource.cache.ContentMetadata
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import com.prunerstudio.mobile.core.lan.LanSyncClient
import com.prunerstudio.mobile.core.model.AudioQualityLevel
import com.prunerstudio.mobile.core.model.Track
import com.prunerstudio.mobile.core.sources.CompositeAudioSourceResolver
import java.io.File
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.resume
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext

data class OfflineCacheProgress(
    val active: Boolean = false,
    val playlistId: String? = null,
    val completed: Int = 0,
    val total: Int = 0,
    val currentTitle: String = "",
)

/**
 * Media3 cache for playback and offline downloads. Complete entries can be opened without
 * resolving a fresh URL. Incomplete transfers are isolated and discarded on close.
 */
@UnstableApi
class AudioCacheManager(
    private val context: Context,
    private val maxCacheBytes: Long = DEFAULT_CACHE_LIMIT_BYTES,
    private val activePairing: () -> LanSyncClient? = { null },
) {
    private val cacheDir: File = File(context.cacheDir, "pruner_audio_cache")
    @Volatile private var cacheInstance: SimpleCache? = null
    private val lock = Any()
    private val downloadLock = Mutex()

    private val _progress = MutableStateFlow(OfflineCacheProgress())
    val progress = _progress.asStateFlow()

    val cache: SimpleCache
        get() =
            cacheInstance
                ?: synchronized(lock) {
                    cacheInstance
                        ?: run {
                            cacheDir.mkdirs()
                            SimpleCache(
                                cacheDir,
                                LeastRecentlyUsedCacheEvictor(maxCacheBytes),
                                StandaloneDatabaseProvider(context),
                            )
                        }
                            .also { cacheInstance = it }
                }

    fun wrapDataSourceFactory(
        upstream: DataSource.Factory,
        lanProvider: () -> LanSyncClient?,
    ): DataSource.Factory {
        val cacheFactory by lazy {
            CacheDataSource.Factory()
                .setCache(cache)
                .setUpstreamDataSourceFactory(upstream)
                .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        }
        val diskOnlyFactory by lazy { CacheDataSource.Factory().setCache(cache) }
        return DataSource.Factory {
            object : DataSource {
                private var active: DataSource? = null
                private var desktop: LanSyncClient? = null
                private var attempt: String? = null
                private val listeners = mutableListOf<androidx.media3.datasource.TransferListener>()

                override fun addTransferListener(
                    transferListener: androidx.media3.datasource.TransferListener
                ) {
                    listeners += transferListener
                    active?.addTransferListener(transferListener)
                }

                override fun open(dataSpec: DataSpec): Long {
                    val scheme = dataSpec.uri.scheme.orEmpty()
                    val key = dataSpec.key
                    val namespace = AudioCacheKey.pairing(key)
                    if (
                        namespace != null || key?.startsWith("local:") == true ||
                        dataSpec.uri.encodedPath.orEmpty().startsWith("/api/lan/")
                    ) {
                        desktop = lanProvider()?.takeUnless { it.isClosed }
                            ?: throw IOException("Desktop pairing revoked")
                        if (namespace != null && AudioCacheKey.desktopNamespace(requireNotNull(desktop).config) != namespace) {
                            throw IOException("Desktop pairing changed")
                        }
                    }
                    var spec = dataSpec
                    val chosen = when {
                        scheme == "file" || scheme == "content" -> upstream.createDataSource()
                        key == null || !AudioCacheKey.isLogical(key) -> cacheFactory.createDataSource()
                        else -> {
                            val complete = completeEntry(key)
                            if (complete != null) {
                                spec = dataSpec.buildUpon().setKey(complete).build()
                                diskOnlyFactory.createDataSource()
                            } else {
                                // Each fresh transfer owns its byte ranges. A renewed resolver URL,
                                // fallback codec or changed server file can never fill an older hole.
                                val fresh = AudioCacheKey.attempt(key)
                                attempt = fresh
                                spec = dataSpec.buildUpon().setKey(fresh).build()
                                cacheFactory.createDataSource()
                            }
                        }
                    }
                    active = chosen
                    listeners.forEach(chosen::addTransferListener)
                    return chosen.open(spec)
                }

                override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
                    checkPairing()
                    val bytes = active?.read(buffer, offset, length) ?: C.RESULT_END_OF_INPUT
                    checkPairing()
                    return bytes
                }

                private fun checkPairing() {
                    val owner = desktop ?: return
                    if (owner.isClosed || lanProvider() !== owner) throw IOException("Desktop pairing revoked")
                }

                override fun getUri(): Uri? = active?.uri

                override fun getResponseHeaders(): Map<String, List<String>> =
                    active?.responseHeaders ?: emptyMap()

                override fun close() {
                    try {
                        active?.close()
                    } finally {
                        active = null
                        val owner = desktop
                        desktop = null
                        attempt?.let { key ->
                            if (owner?.isClosed == true || !hasCompleteSpans(key)) removeAttempt(key)
                        }
                        attempt = null
                    }
                }
            }
        }
    }

    fun isOnWifiOrUnmetered(): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return false
        val network = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(network) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED) ||
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
    }

    /**
     * Returns true ONLY when the entire content length for [trackKey] is present on disk.
     * Partial listen-while-caching spans do NOT falsely short-circuit offline playlist downloads.
     */
    fun isTrackCached(trackKey: String): Boolean = completeEntry(trackKey) != null

    private fun completeEntry(logical: String): String? = if (AudioCacheKey.isLogical(logical)) {
        cache.keys.firstOrNull { AudioCacheKey.isAttemptFor(it, logical) && hasCompleteSpans(it) }
    } else {
        logical.takeIf(::hasCompleteSpans)
    }

    private fun hasCompleteSpans(trackKey: String): Boolean =
        runCatching {
            val contentLength = ContentMetadata.getContentLength(cache.getContentMetadata(trackKey))
            if (contentLength <= 0L || contentLength == C.LENGTH_UNSET.toLong()) return@runCatching false
            cache.getCachedBytes(trackKey, 0L, contentLength) >= contentLength
        }.getOrDefault(false)

    fun cacheSizeBytes(): Long = runCatching { cache.cacheSpace }.getOrDefault(0L)

    fun evictLanEntries(namespace: String? = null) {
        cache.keys.filter {
            it.startsWith("local:") ||
                (AudioCacheKey.pairing(it)?.let { owner -> namespace == null || owner == namespace } == true)
        }.forEach(::removeAttempt)
    }

    private fun removeAttempt(key: String) {
        runCatching { cache.removeResource(key) }
    }

    fun clearAll() {
        runCatching {
            cache.keys.toList().forEach { cache.removeResource(it) }
        }
    }

    suspend fun cacheTracksOffline(
        playlistId: String?,
        tracks: List<Track>,
        quality: AudioQualityLevel,
        resolver: CompositeAudioSourceResolver,
        upstreamFactory: DataSource.Factory,
        allowCellular: Boolean = false,
        account: String = "offline",
    ): Pair<Int, Int> =
        withContext(Dispatchers.IO) {
            downloadLock.withLock {
                if (!allowCellular && !isOnWifiOrUnmetered()) {
                    throw IOException("??? Wi-Fi ?????? Wi-Fi ??????")
                }
                val cacheable = tracks.filter { it.providerFamily() != "device" }
                if (cacheable.isEmpty()) return@withLock 0 to 0
                val downloaded = ArrayList<String>(cacheable.size)
                _progress.value = OfflineCacheProgress(active = true, playlistId = playlistId, total = cacheable.size)
                try {
                    cacheable.forEachIndexed { index, track ->
                        coroutineContext.ensureActive()
                        if (!allowCellular && !isOnWifiOrUnmetered()) return@forEachIndexed
                        _progress.value = _progress.value.copy(completed = index, currentTitle = track.title)
                        val lan = if (requiresLanPairing(track)) {
                            activePairing()?.takeIf { it.config.accountId == account } ?: return@forEachIndexed
                        } else null
                        val scope = lan?.config?.let(AudioCacheKey::desktopNamespace)
                        if (track.desktop_scope != null && track.desktop_scope != scope) return@forEachIndexed
                        val logical = AudioCacheKey.logical(account, track, quality, scope)
                        if (isTrackCached(logical)) {
                            downloaded += logical
                            return@forEachIndexed
                        }
                        val attempt = AudioCacheKey.attempt(logical)
                        var ready = false
                        try {
                            val stream = resolver.resolveStream(track, quality)
                            if (!stream.cacheable || stream.requiresLanAuth != (lan != null)) return@forEachIndexed
                            val upstream = upstreamFactory.createDataSource()
                            val writerSource = CacheDataSource.Factory()
                                .setCache(cache)
                                .setUpstreamDataSourceFactory { upstream }
                                .createDataSource()
                            val uri = stream.uri.toUri()
                            val spec = DataSpec.Builder().setUri(uri).setKey(attempt)
                                .setCustomData(resolver.accessForStream(uri)).build()
                            val writer = CacheWriter(writerSource, spec, null, null)
                            runCancellableCacheTransfer(
                                transfer = writer::cache,
                                cancel = {
                                    writer.cancel()
                                    (upstream as? CancellableTransfer)?.cancelTransfer()
                                },
                            )
                            ready = (lan == null || activePairing() === lan && !lan.isClosed) && hasCompleteSpans(attempt)
                            if (ready) downloaded += logical
                        } catch (e: CancellationException) {
                            throw e
                        } catch (_: Exception) {
                            // Each track is independent. A failed transfer leaves no reusable spans.
                        } finally {
                            if (!ready) removeAttempt(attempt)
                        }
                    }
                    val cachedCount = downloaded.count { key ->
                        val scope = AudioCacheKey.pairing(key)
                        (scope == null || activePairing()?.config?.let(AudioCacheKey::desktopNamespace) == scope) &&
                            isTrackCached(key)
                    }
                    cachedCount to (cacheable.size - cachedCount)
                } finally {
                    _progress.value = OfflineCacheProgress()
                }
            }
        }

    fun release() {
        synchronized(lock) {
            runCatching { cacheInstance?.release() }
            cacheInstance = null
        }
    }

    companion object {
        const val DEFAULT_CACHE_LIMIT_BYTES = 512L * 1024L * 1024L
    }
}

internal suspend fun runCancellableCacheTransfer(transfer: () -> Unit, cancel: () -> Unit) =
    coroutineScope {
        suspendCancellableCoroutine { continuation ->
            continuation.invokeOnCancellation { cancel() }
            launch(Dispatchers.IO) {
                try {
                    transfer()
                    continuation.resume(Unit)
                } catch (exception: Exception) {
                    continuation.resumeWithException(exception)
                }
            }
        }
    }
