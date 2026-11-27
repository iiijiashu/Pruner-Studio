package com.prunerstudio.mobile.core.sources

import android.net.Uri
import android.os.Looper
import com.prunerstudio.mobile.core.awaitResponse
import com.prunerstudio.mobile.core.db.PrunerRepository
import com.prunerstudio.mobile.core.lan.LanException
import com.prunerstudio.mobile.core.lan.LanSyncClient
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.NeteaseApiClient
import com.prunerstudio.mobile.core.netease.SecureCredentialStore
import com.prunerstudio.mobile.core.player.AudioCacheKey
import java.io.IOException
import java.net.ConnectException
import java.net.InetAddress
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException
import kotlin.math.min
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody

data class ResolvedAudioStream(
    val uri: String,
    val headers: Map<String, String> = emptyMap(),
    val mimeType: String? = null,
    val providerLabel: String = "",
    val cacheable: Boolean = true,
    val requiresLanAuth: Boolean = false,
)

data class ExternalLyricsPayload(
    val lrc: String = "",
    val yrc: String = "",
    val tlyric: String = "",
    val romalrc: String = "",
    val source: String = "",
) {
    fun isNotEmpty(): Boolean = lrc.isNotBlank() || yrc.isNotBlank()
}

interface AudioSourceResolver {
    fun canResolve(track: Track): Boolean

    suspend fun resolveStream(
        track: Track,
        quality: AudioQualityLevel = AudioQualityLevel.jymaster,
    ): ResolvedAudioStream

    suspend fun search(query: String, limit: Int = 30): List<Track> = emptyList()
}

/**
 * Pure non-blocking URL validator for external self-hosted / resolver services.
 * Never performs synchronous DNS resolution so it is safe on any thread (including Compose UI).
 */
object ExternalUrlValidator {
    fun validateBaseUrl(
        raw: String,
        allowLanHttp: Boolean,
        allowDns: Boolean = true,
    ): HttpUrl {
        val parsed =
            raw.trim().toHttpUrlOrNull()
                ?: throw AudioResolutionException(
                    AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                    "服务地址格式无效",
                )
        if (parsed.scheme == "https") return parsed
        if (parsed.scheme == "http" && allowLanHttp && isPrivateOrLocalHost(parsed.host, allowDns)) {
            return parsed
        }
        throw AudioResolutionException(
            AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
            if (parsed.scheme == "http") "外部服务仅允许 HTTPS（或开启局域网 HTTP 开关用于内网地址）"
            else "不支持的服务协议: ${parsed.scheme}",
        )
    }

    fun isPrivateOrLocalHost(host: String, allowDns: Boolean = false): Boolean {
        if (isPrivateOrLocalHostLiteral(host)) return true
        if (!allowDns) return false
        val ip = runCatching { InetAddress.getByName(host) }.getOrNull() ?: return false
        return ip.isLoopbackAddress || ip.isSiteLocalAddress || ip.isLinkLocalAddress
    }

    fun isPrivateOrLocalHostLiteral(host: String): Boolean {
        val normalized = host.trim().lowercase().removePrefix("[").removeSuffix("]")
        if (normalized.isEmpty()) return false
        if (normalized == "localhost" || normalized.endsWith(".local") || normalized.endsWith(".lan")) {
            return true
        }
        // IPv6 loopback / ULA (fc00::/7) / link-local (fe80::/10)
        if (':' in normalized) {
            if (normalized == "::1" || normalized == "0:0:0:0:0:0:0:1") return true
            val firstGroup = normalized.substringBefore(':').toIntOrNull(16) ?: return false
            return (firstGroup and 0xfe00) == 0xfc00 || (firstGroup and 0xffc0) == 0xfe80
        }
        // IPv4 literal check without blocking DNS
        val parts = normalized.split('.')
        if (parts.size == 4) {
            val octets = parts.map { it.toIntOrNull() ?: return false }
            if (octets.any { it !in 0..255 }) return false
            val (a, b, _, _) = octets
            return a == 127 ||
                a == 10 ||
                (a == 172 && b in 16..31) ||
                (a == 192 && b == 168) ||
                (a == 169 && b == 254)
        }
        return false
    }
}

/**
 * Per-source exponential circuit breaker (30s -> 60s -> 120s -> 180s) matching
 * docs/HTTP_RESOLVER_V2.md Invariant #4 so degraded external endpoints never stall search/playback.
 */
internal class SourceCircuitBreaker(
    private val failureThreshold: Int = 3,
    private val baseCooldownMs: Long = 30_000L,
    private val maxCooldownMs: Long = 180_000L,
    private val nowMs: () -> Long = { System.currentTimeMillis() },
) {
    private data class Entry(
        var consecutiveFailures: Int = 0,
        var tripCount: Int = 0,
        var openUntilMs: Long = 0L,
    )

    private val entries = ConcurrentHashMap<String, Entry>()

    fun isOpen(sourceId: String): Boolean = remainingCooldownMs(sourceId) > 0L

    fun remainingCooldownMs(sourceId: String): Long {
        val entry = entries[sourceId] ?: return 0L
        synchronized(entry) {
            val left = entry.openUntilMs - nowMs()
            return if (left > 0L) left else 0L
        }
    }

    fun recordSuccess(sourceId: String) {
        val entry = entries[sourceId] ?: return
        synchronized(entry) {
            entry.consecutiveFailures = 0
            entry.tripCount = 0
            entry.openUntilMs = 0L
        }
    }

    fun recordFailure(sourceId: String): Long {
        val entry = entries.computeIfAbsent(sourceId) { Entry() }
        synchronized(entry) {
            entry.consecutiveFailures++
            if (entry.consecutiveFailures >= failureThreshold) {
                entry.tripCount = (entry.tripCount + 1).coerceAtMost(6)
                val cooldown = min(maxCooldownMs, baseCooldownMs * (1L shl (entry.tripCount - 1)))
                entry.openUntilMs = nowMs() + cooldown
                entry.consecutiveFailures = 0
                return cooldown
            }
            return 0L
        }
    }

    fun reset(sourceId: String) {
        entries.remove(sourceId)
    }
}

class CompositeAudioSourceResolver(
    private val repository: PrunerRepository,
    private val credentials: SecureCredentialStore,
    private val netease: NeteaseApiClient,
    private val lanProvider: () -> LanSyncClient?,
    private val httpClient: OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .build(),
    nowMs: () -> Long = { System.currentTimeMillis() },
    private val cachedPairing: (() -> PairedServerConfig?)? = null,
) : AudioSourceResolver {
    private val random = SecureRandom()
    private val circuitBreaker = SourceCircuitBreaker(nowMs = nowMs)
    private val streamRegistryLock = Any()
    private val authorizedExternalStreams =
        object : LinkedHashMap<String, StreamAccess>(64, 0.75f, true) {
            override fun removeEldestEntry(
                eldest: MutableMap.MutableEntry<String, StreamAccess>?
            ): Boolean = size > MAX_AUTHORIZED_STREAMS
        }

    @Volatile private var cachedSources: List<ExternalSourceConfig>? = null
    @Volatile private var cachedAllowedOrigins: List<HttpUrl> = emptyList()
    private val _sourcesRevision = MutableStateFlow(0L)
    val sourcesRevision: StateFlow<Long> = _sourcesRevision.asStateFlow()

    fun secretKey(sourceId: String) = "external_source:$sourceId"

    private fun isMainThread(): Boolean =
        runCatching { Looper.myLooper() == Looper.getMainLooper() }.getOrDefault(false)

    private fun updateSourceCache(items: List<ExternalSourceConfig>): List<ExternalSourceConfig> {
        cachedSources = items
        cachedAllowedOrigins =
            items.mapNotNull { cfg ->
                if (!cfg.enabled) null
                else
                    runCatching {
                        ExternalUrlValidator.validateBaseUrl(cfg.baseUrl, cfg.allowLanHttp, allowDns = true)
                    }.getOrNull()
            }
        _sourcesRevision.value = _sourcesRevision.value + 1L
        return items
    }

    fun warmCache() {
        updateSourceCache(repository.externalSources())
    }

    fun listSources(): List<ExternalSourceConfig> = updateSourceCache(repository.externalSources())

    fun enabledSources(): List<ExternalSourceConfig> =
        (cachedSources ?: listSources()).filter { it.enabled }

    private fun enabledSourcesForCaller(): List<ExternalSourceConfig> =
        cachedSources?.filter { it.enabled }
            ?: if (isMainThread()) emptyList() else listSources().filter { it.enabled }

    fun isSourceCircuitOpen(sourceId: String): Boolean = circuitBreaker.isOpen(sourceId)

    fun saveSource(config: ExternalSourceConfig, secret: String?) {
        if (config.kind == ExternalSourceKind.RESOLVER_V2) {
            require(validResolverSourceId(config.id))
            ResolverDestinationPolicy.forConfig(config.baseUrl, config.allowLanHttp)
        } else {
            ExternalUrlValidator.validateBaseUrl(config.baseUrl, config.allowLanHttp)
        }
        repository.saveExternalSource(config)
        if (secret != null) {
            credentials.writeSecret(secretKey(config.id), secret.ifBlank { null })
        }
        circuitBreaker.reset(config.id)
        updateSourceCache(repository.externalSources())
    }

    fun deleteSource(sourceId: String) {
        repository.deleteExternalSource(sourceId)
        credentials.writeSecret(secretKey(sourceId), null)
        circuitBreaker.reset(sourceId)
        updateSourceCache(repository.externalSources())
    }

    internal fun accessForStream(uri: Uri): StreamAccess? =
        synchronized(streamRegistryLock) { authorizedExternalStreams[uri.toString()] }

    fun headersForStream(uri: Uri): Map<String, String>? =
        accessForStream(uri)?.headers?.takeIf { it.isNotEmpty() }

    fun isAuthorizedExternalStream(uri: Uri): Boolean {
        val key = uri.toString()
        synchronized(streamRegistryLock) {
            if (authorizedExternalStreams.containsKey(key)) return true
        }
        val http = key.toHttpUrlOrNull() ?: return false
        return enabledSourcesForCaller().filter { it.kind != ExternalSourceKind.RESOLVER_V2 }.any { cfg ->
            val base =
                runCatching { ExternalUrlValidator.validateBaseUrl(cfg.baseUrl, cfg.allowLanHttp) }
                    .getOrNull() ?: return@any false
            base.scheme == http.scheme && base.host == http.host && base.port == http.port
        }
    }

    /** Resolves relative `/api/lan/v1/cover?...` paths against the active paired desktop base URL. */
    fun resolveLanCoverUrl(raw: String): String? {
        if (!raw.startsWith("/api/lan/v1/")) return null
        val base = coverLanBase() ?: return null
        val pathPart = raw.substringBefore('?')
        val queryPart = raw.substringAfter('?', "")
        return runCatching {
            base.newBuilder()
                .encodedPath(pathPart)
                .apply { if (queryPart.isNotEmpty()) encodedQuery(queryPart) }
                .build()
                .toString()
        }.getOrNull()
    }

    /**
     * Thread-safe and Compose-UI-safe cover origin validator: reads only the in-memory cached
     * external sources and never performs SQLite queries or blocking DNS on the caller thread.
     */
    fun isAllowedCoverUrl(url: HttpUrl): Boolean {
        if (
            url.isHttps &&
                (url.host.endsWith(".126.net") || url.host.endsWith(".music.163.com")) &&
                url.port == 443
        ) {
            return true
        }
        val lanBase = coverLanBase()
        if (lanBase?.sameOrigin(url) == true) {
            return url.encodedPath == "/api/lan/v1/cover"
        }
        val origins = cachedAllowedOrigins
        return origins.any { base ->
            base.scheme == url.scheme && base.host == url.host && base.port == url.port
        }
    }

    private fun coverLanBase(): HttpUrl? {
        val snapshot = cachedPairing ?: return lanProvider()?.takeUnless { it.isClosed }?.base
        val pairing = snapshot() ?: return null
        return HttpUrl.Builder().scheme("https").host(pairing.host).port(pairing.port).build()
    }

    override fun canResolve(track: Track): Boolean {
        val provider = track.providerFamily()
        val lanReady = lanProvider()?.isClosed == false
        if (track.source_id == LAN_DESKTOP_SOURCE_ID && provider != "netease") {
            return lanReady
        }
        val availableSources = enabledSourcesForCaller()
        return when (provider) {
            "device" -> !track.stream_url.isNullOrBlank()
            "local" -> !(track.file_id ?: track.external_id).isNullOrBlank() && lanReady
            "netease" -> (track.song_id ?: track.id) != null
            "subsonic", "opensubsonic", "webdav", "resolver", "resolver_v2" ->
                resolveSourceConfig(track, sourcesOverride = availableSources) != null ||
                    !track.stream_url.isNullOrBlank() ||
                    lanReady
            else ->
                !track.stream_url.isNullOrBlank() ||
                    lanReady ||
                    availableSources.any { it.kind == ExternalSourceKind.RESOLVER_V2 }
        }
    }

    override suspend fun resolveStream(
        track: Track,
        quality: AudioQualityLevel,
    ): ResolvedAudioStream =
        withContext(Dispatchers.IO) {
            when (val provider = track.providerFamily()) {
                "device" -> {
                    val uri =
                        track.stream_url?.takeIf {
                            it.startsWith("file:", ignoreCase = true) ||
                                it.startsWith("content:", ignoreCase = true)
                        } ?: throw AudioResolutionException(AudioErrorCategory.DEVICE_FILE_ERROR)
                    ResolvedAudioStream(uri = uri, providerLabel = "本机音频", cacheable = false)
                }
                "local" -> {
                    val lan =
                        lanProvider()?.takeUnless { it.isClosed }
                            ?: throw AudioResolutionException(AudioErrorCategory.LAN_UNPAIRED)
                    val id =
                        (track.file_id ?: track.external_id)?.takeIf {
                            it.matches(Regex("[a-f0-9]{24}"))
                        } ?: throw AudioResolutionException(AudioErrorCategory.UNKNOWN, "无效的桌面文件标识")
                    val url = lan.base.resolve("local-library/stream/$id").toString()
                    ResolvedAudioStream(
                        uri = url,
                        providerLabel = "桌面无损",
                        cacheable = true,
                        requiresLanAuth = true,
                    )
                }
                "netease" -> {
                    if (track.source_id == LAN_DESKTOP_SOURCE_ID) {
                        return@withContext requireNotNull(resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = true))
                    }
                    val id =
                        track.song_id ?: track.id
                            ?: throw AudioResolutionException(AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE)
                    val url = netease.resolveAudioUrlOrThrow(id, quality).toString()
                    ResolvedAudioStream(uri = url, providerLabel = quality.displayLabel(), cacheable = true)
                }
                "subsonic", "opensubsonic" -> {
                    if (track.source_id == LAN_DESKTOP_SOURCE_ID) {
                        resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = true)!!
                    } else {
                        val cfg = resolveSourceConfig(track, ExternalSourceKind.OPENSUBSONIC)
                        if (cfg == null) {
                            resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = false)
                                ?: throw AudioResolutionException(
                                    AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                                    "未找到对应的 OpenSubsonic 音源配置",
                                )
                        } else {
                            val songId =
                                track.external_id
                                    ?: track.identity().substringAfterLast(':').takeIf(String::isNotBlank)
                                    ?: throw AudioResolutionException(AudioErrorCategory.EXTERNAL_SOURCE_ERROR)
                            val streamUrl = buildSubsonicUrl(cfg, "stream.view", mapOf("id" to songId))
                            registerExternalStream(streamUrl, StreamAccess(emptyMap()))
                            ResolvedAudioStream(
                                uri = streamUrl,
                                providerLabel = cfg.name.ifBlank { "OpenSubsonic" },
                                cacheable = true,
                            )
                        }
                    }
                }
                "webdav" -> {
                    if (track.source_id == LAN_DESKTOP_SOURCE_ID) {
                        resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = true)!!
                    } else {
                        val cfg = resolveSourceConfig(track, ExternalSourceKind.WEBDAV)
                        if (cfg == null) {
                            resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = false)
                                ?: throw AudioResolutionException(
                                    AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                                    "未找到对应的 WebDAV 音源配置",
                                )
                        } else {
                            val base = ExternalUrlValidator.validateBaseUrl(cfg.baseUrl, cfg.allowLanHttp)
                            val streamUrl =
                                track.stream_url?.takeIf { it.isNotBlank() }
                                    ?: base.resolve(track.external_id.orEmpty().trimStart('/'))?.toString()
                                    ?: throw AudioResolutionException(AudioErrorCategory.EXTERNAL_SOURCE_ERROR)
                            val url = streamUrl.toHttpUrlOrNull()
                            if (url == null || !base.sameOrigin(url)) {
                                throw IOException("WebDAV stream origin changed")
                            }
                            val headers = buildBasicAuthHeader(cfg)
                            registerExternalStream(url.toString(), StreamAccess(headers, webDavOrigin = base))
                            ResolvedAudioStream(
                                uri = url.toString(),
                                headers = headers,
                                providerLabel = cfg.name.ifBlank { "WebDAV" },
                                cacheable = true,
                            )
                        }
                    }
                }
                "resolver", "resolver_v2" -> {
                    if (track.source_id == LAN_DESKTOP_SOURCE_ID) {
                        resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = true)!!
                    } else {
                        val cfg = resolveSourceConfig(track, ExternalSourceKind.RESOLVER_V2)
                        if (cfg == null) {
                            resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = false)
                                ?: throw AudioResolutionException(
                                    AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                                    "未找到对应的 HTTP Resolver v2 配置",
                                )
                        } else {
                            resolveViaHttpResolverV2(cfg, track, quality)
                        }
                    }
                }
                else -> {
                    // Priority 1: direct paired LAN proxy stream URL (supports both /audio/stream/ and legacy /stream/)
                    val direct = track.stream_url?.trim().orEmpty()
                    val lan = lanProvider()?.takeUnless { it.isClosed }
                    if (lan != null && isLanProxyStreamPath(direct)) {
                        val url = lan.base.newBuilder().encodedPath(direct).build().toString()
                        ResolvedAudioStream(
                            uri = url,
                            providerLabel = "桌面代理",
                            cacheable = true,
                            requiresLanAuth = true,
                        )
                    } else if (track.source_id == LAN_DESKTOP_SOURCE_ID) {
                        // Desktop-originated tracks strictly resolve through paired desktop proxy
                        resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = true)!!
                    } else {
                        // Priority 2: paired desktop multi-channel resolver proxy (/api/lan/v1/audio/resolve)
                        resolveViaPairedDesktopProxy(track, quality, throwOnLanFailure = false)
                            ?: run {
                                // Priority 3: configured Android HTTP Resolver v2
                                val resolverCfg =
                                    enabledSources().firstOrNull {
                                        it.kind == ExternalSourceKind.RESOLVER_V2 &&
                                            !circuitBreaker.isOpen(it.id)
                                    }
                                        ?: throw AudioResolutionException(
                                            AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                                            "不支持的音源类型: $provider",
                                        )
                                resolveViaHttpResolverV2(resolverCfg, track, quality)
                            }
                    }
                }
            }
        }

    private suspend fun resolveViaPairedDesktopProxy(
        track: Track,
        quality: AudioQualityLevel,
        throwOnLanFailure: Boolean = false,
    ): ResolvedAudioStream? {
        val lan = lanProvider()?.takeUnless { it.isClosed }
        if (lan == null) {
            if (throwOnLanFailure) {
                throw AudioResolutionException(AudioErrorCategory.LAN_UNPAIRED)
            }
            return null
        }
        if (track.desktop_scope != null &&
            track.desktop_scope != AudioCacheKey.desktopNamespace(lan.config)
        ) {
            throw AudioResolutionException(AudioErrorCategory.LAN_UNPAIRED)
        }
        val resp =
            try {
                lan.resolveProxyAudio(track, quality)
            } catch (e: CancellationException) {
                throw e
            } catch (e: LanException) {
                if (!throwOnLanFailure) return null
                when (e.status) {
                    401 -> throw AudioResolutionException(AudioErrorCategory.LAN_UNPAIRED, cause = e)
                    404 ->
                        throw AudioResolutionException(
                            AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE,
                            "桌面端暂未找到该曲目的可播放音源",
                            e,
                        )
                    else ->
                        throw AudioResolutionException(
                            AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                            "桌面代理音源解析失败 (${e.code})",
                            e,
                        )
                }
            } catch (e: Exception) {
                if (!throwOnLanFailure) return null
                if (
                    e is ConnectException ||
                        e is SocketTimeoutException ||
                        e is UnknownHostException ||
                        e is SSLException
                ) {
                    throw AudioResolutionException(AudioErrorCategory.LAN_UNREACHABLE, cause = e)
                }
                throw AudioResolutionException(
                    AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                    "桌面代理音源不可用，请确认桌面端连接正常",
                    e,
                )
            }
        val relPath = resp["stream_url"]?.jsonPrimitive?.contentOrNull?.trim().orEmpty()
        val isAllowedPath =
            isLanProxyStreamPath(relPath) || relPath.startsWith("/api/lan/v1/local-library/stream/")
        if (!isAllowedPath) {
            if (throwOnLanFailure) {
                throw AudioResolutionException(
                    AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE,
                    "桌面端未返回可用的代理音频流",
                )
            }
            return null
        }
        val url = lan.base.newBuilder().encodedPath(relPath).build().toString()
        val format = resp["format"]?.jsonPrimitive?.contentOrNull.orEmpty()
        val label =
            if (format.isNotBlank()) "桌面代理 · ${format.uppercase()}" else "桌面代理"
        return ResolvedAudioStream(
            uri = url,
            providerLabel = label,
            cacheable = true,
            requiresLanAuth = true,
        )
    }

    override suspend fun search(query: String, limit: Int): List<Track> =
        withContext(Dispatchers.IO) {
            val q = query.trim()
            if (q.isEmpty()) return@withContext emptyList()
            coroutineScope {
                val localDeferreds =
                    enabledSources()
                        .filterNot { circuitBreaker.isOpen(it.id) }
                        .map { cfg ->
                            async {
                                try {
                                    val hits =
                                        when (cfg.kind) {
                                            ExternalSourceKind.OPENSUBSONIC -> searchSubsonic(cfg, q, limit)
                                            ExternalSourceKind.RESOLVER_V2 -> searchResolverV2(cfg, q, limit)
                                            ExternalSourceKind.WEBDAV -> searchWebDav(cfg, q, limit)
                                        }
                                    circuitBreaker.recordSuccess(cfg.id)
                                    hits
                                } catch (e: CancellationException) {
                                    throw e
                                } catch (_: Exception) {
                                    circuitBreaker.recordFailure(cfg.id)
                                    emptyList()
                                }
                            }
                        }
                val desktopDeferred =
                    lanProvider()?.takeUnless { it.isClosed }?.let { lan ->
                        async {
                            try {
                                lan.searchMulti(q, limit = limit, offset = 0).tracks.map {
                                    normalizePairedDesktopTrack(it, AudioCacheKey.desktopNamespace(lan.config))
                                }
                            } catch (e: CancellationException) {
                                throw e
                            } catch (_: Exception) {
                                emptyList()
                            }
                        }
                    }
                val results = mutableListOf<Track>()
                localDeferreds.forEach { results.addAll(it.await()) }
                desktopDeferred?.let { results.addAll(it.await()) }
                results.distinctBy { it.identity() }
            }
        }

    private fun normalizePairedDesktopTrack(track: Track, desktopScope: String): Track {
        val provider = track.effectiveProvider()
        val artistText = track.artist.ifBlank { track.artists.joinToString(" / ") }
        val normArtists =
            track.artists.ifEmpty {
                artistText
                    .split('/', '、', ';', '；')
                    .map(String::trim)
                    .filter(String::isNotEmpty)
            }
        val isNetease = provider.equals("netease", ignoreCase = true)
        val fallbackNeteaseId =
            if (isNetease) {
                track.external_id?.toLongOrNull()?.takeIf { it > 0L }
                    ?: track.track_key?.substringAfter("netease:", "")?.toLongOrNull()?.takeIf { it > 0L }
            } else null
        val normId =
            if (isNetease) {
                track.id?.takeIf { it > 0L } ?: track.song_id?.takeIf { it > 0L } ?: fallbackNeteaseId
            } else track.id
        val normSongId =
            if (isNetease) {
                track.song_id?.takeIf { it > 0L } ?: track.id?.takeIf { it > 0L } ?: fallbackNeteaseId
            } else track.song_id
        return track.copy(
            id = normId,
            song_id = normSongId,
            artist = artistText,
            artists = normArtists,
            source_id = LAN_DESKTOP_SOURCE_ID,
            desktop_scope = desktopScope,
        )
    }

    suspend fun fetchExternalLyrics(track: Track): ExternalLyricsPayload? =
        withContext(Dispatchers.IO) {
            if (track.source_id == LAN_DESKTOP_SOURCE_ID) {
                return@withContext fetchPairedDesktopLyrics(track)
            }
            when (track.providerFamily()) {
                "subsonic", "opensubsonic" -> fetchSubsonicLyrics(track)
                "resolver", "resolver_v2" -> fetchResolverV2Lyrics(track)
                "local" -> fetchPairedDesktopLyrics(track)
                else ->
                    fetchPairedDesktopLyrics(track)
                        ?: fetchResolverV2Lyrics(track)
            }
        }

    private suspend fun fetchSubsonicLyrics(track: Track): ExternalLyricsPayload? {
        val cfg = resolveSourceConfig(track, ExternalSourceKind.OPENSUBSONIC) ?: return null
        val songId = track.external_id ?: return null
        val url = buildSubsonicUrl(cfg, "getLyricsBySongId.view", mapOf("id" to songId))
        return runCatching {
            val req = Request.Builder().url(url).get().build()
            httpClient.newCall(req).awaitResponse { resp ->
                if (!resp.isSuccessful) return@awaitResponse null
                val body = resp.body?.string().orEmpty()
                val root =
                    wire.parseToJsonElement(body).jsonObject["subsonic-response"]?.jsonObject
                        ?: return@awaitResponse null
                val list = root["lyricsList"]?.jsonObject?.get("structuredLyrics")?.jsonArray
                val lines = list?.firstOrNull()?.jsonObject?.get("line")?.jsonArray
                if (!lines.isNullOrEmpty()) {
                    val lrc =
                        lines
                            .mapNotNull { el ->
                                val obj = el.jsonObject
                                val value =
                                    obj["value"]?.jsonPrimitive?.contentOrNull
                                        ?: return@mapNotNull null
                                val start = obj["start"]?.jsonPrimitive?.longOrNull ?: 0L
                                val min = start / 60000
                                val sec = (start % 60000) / 1000
                                val ms = start % 1000
                                String.format(
                                    java.util.Locale.ROOT,
                                    "[%02d:%02d.%03d]%s",
                                    min,
                                    sec,
                                    ms,
                                    value,
                                )
                            }
                            .joinToString("\n")
                    ExternalLyricsPayload(lrc = lrc, source = "subsonic").takeIf { it.isNotEmpty() }
                } else null
            }
        }.getOrNull()
    }

    private suspend fun fetchResolverV2Lyrics(track: Track): ExternalLyricsPayload? {
        val cfg = resolveSourceConfig(track, ExternalSourceKind.RESOLVER_V2) ?: return null
        if (circuitBreaker.isOpen(cfg.id)) return null
        val (base, policy) = runCatching {
            ResolverDestinationPolicy.forConfig(cfg.baseUrl, cfg.allowLanHttp)
        }.getOrNull() ?: return null
        val resolverHttp = policy.client(httpClient)
        val secret = credentials.readSecret(secretKey(cfg.id))
        val artist = track.artist.ifBlank { track.artists.joinToString(" / ") }
        val lyricUrl = resolverEndpoint(base, "lyric")
        val payload =
            buildJsonObject {
                put("id", track.external_id ?: track.identity())
                put("title", track.title)
                put("artist", artist)
            }
        return runCatching {
            val postReq =
                Request.Builder()
                    .url(lyricUrl)
                    .applyResolverAuth(secret)
                    .post(payload.toString().toRequestBody(JSON_MEDIA_TYPE))
                    .build()
            val postResp =
                resolverHttp.newCall(postReq).awaitResponse { resp ->
                    when {
                        resp.isSuccessful -> parseResolverLyricsJson(resp.body?.string().orEmpty())
                        resp.code == 404 || resp.code == 405 -> null to true
                        else -> null to false
                    }
                }
            if (postResp.first != null) return@runCatching postResp.first
            if (!postResp.second) return@runCatching null
            val getUrl =
                lyricUrl
                    .newBuilder()
                    .addQueryParameter("id", track.external_id ?: track.identity())
                    .addQueryParameter("title", track.title)
                    .addQueryParameter("artist", artist)
                    .build()
            val getReq = Request.Builder().url(getUrl).applyResolverAuth(secret).get().build()
            resolverHttp.newCall(getReq).awaitResponse { resp ->
                if (!resp.isSuccessful) return@awaitResponse null
                parseResolverLyricsJson(resp.body?.string().orEmpty()).first
            }
        }.getOrNull()
    }

    private fun parseResolverLyricsJson(raw: String): Pair<ExternalLyricsPayload?, Boolean> {
        val obj = runCatching { wire.parseToJsonElement(raw).jsonObject }.getOrNull() ?: return null to false
        val lrc = obj["lrc"]?.jsonPrimitive?.contentOrNull.orEmpty()
        val yrc = obj["yrc"]?.jsonPrimitive?.contentOrNull.orEmpty()
        val tlyric = obj["tlyric"]?.jsonPrimitive?.contentOrNull.orEmpty()
        val romalrc = obj["romalrc"]?.jsonPrimitive?.contentOrNull.orEmpty()
        val payload = ExternalLyricsPayload(lrc, yrc, tlyric, romalrc, source = "resolver_v2")
        return payload.takeIf { it.isNotEmpty() } to false
    }

    private suspend fun fetchPairedDesktopLyrics(track: Track): ExternalLyricsPayload? {
        val lan = lanProvider()?.takeUnless { it.isClosed } ?: return null
        return try {
            val json = lan.fetchProxyLyrics(track)
            fun textField(key: String): String {
                val el = json[key] ?: return ""
                return when (el) {
                    is JsonPrimitive -> el.contentOrNull.orEmpty()
                    is JsonObject -> el["lyric"]?.jsonPrimitive?.contentOrNull.orEmpty()
                    else -> ""
                }
            }
            ExternalLyricsPayload(
                lrc = textField("lrc").ifBlank { textField("lyrics") },
                yrc = textField("yrc"),
                tlyric = textField("tlyric").ifBlank { textField("tlyrics") },
                romalrc = textField("romalrc"),
                source = "lan_desktop",
            ).takeIf { it.isNotEmpty() }
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            null
        }
    }

    suspend fun testSource(config: ExternalSourceConfig, secretOverride: String? = null): String =
        withContext(Dispatchers.IO) {
            val (base, sourceHttp) =
                if (config.kind == ExternalSourceKind.RESOLVER_V2) {
                    val (url, policy) = ResolverDestinationPolicy.forConfig(config.baseUrl, config.allowLanHttp)
                    url to policy.client(httpClient)
                } else {
                    ExternalUrlValidator.validateBaseUrl(config.baseUrl, config.allowLanHttp) to httpClient
                }
            val secret = secretOverride ?: credentials.readSecret(secretKey(config.id))
            when (config.kind) {
                ExternalSourceKind.OPENSUBSONIC -> {
                    val url = buildSubsonicUrl(config, "ping.view", emptyMap(), secretOverride = secret)
                    sourceHttp.newCall(Request.Builder().url(url).get().build()).awaitResponse { resp ->
                        if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                        val obj =
                            wire.parseToJsonElement(resp.body?.string().orEmpty()).jsonObject["subsonic-response"]
                                ?.jsonObject
                        val status = obj?.get("status")?.jsonPrimitive?.contentOrNull
                        if (status != "ok") {
                            val errMsg =
                                obj?.get("error")?.jsonObject?.get("message")?.jsonPrimitive?.contentOrNull
                                    ?: "鉴权失败"
                            throw AudioResolutionException(AudioErrorCategory.EXTERNAL_SOURCE_ERROR, errMsg)
                        }
                        val version = obj["version"]?.jsonPrimitive?.contentOrNull.orEmpty()
                        "OpenSubsonic 连通正常 ($version)"
                    }
                }
                ExternalSourceKind.RESOLVER_V2 -> {
                    val manifestUrl = resolverEndpoint(base, "manifest")
                    val req = Request.Builder().url(manifestUrl).applyResolverAuth(secret).get().build()
                    val manifestResult =
                        sourceHttp.newCall(req).awaitResponse { resp ->
                            when {
                                resp.isSuccessful -> {
                                    val body = resp.body?.string().orEmpty()
                                    val obj = runCatching { wire.parseToJsonElement(body).jsonObject }.getOrNull()
                                    val name = obj?.get("name")?.jsonPrimitive?.contentOrNull.orEmpty()
                                    val ver = obj?.get("version")?.jsonPrimitive?.contentOrNull.orEmpty()
                                    val suffix = listOf(name, ver).filter(String::isNotBlank).joinToString(" ")
                                    if (suffix.isNotBlank()) "HTTP Resolver v2 连通正常 ($suffix)"
                                    else "HTTP Resolver v2 连通正常"
                                }
                                resp.code == 404 -> null
                                else -> throw IOException("HTTP ${resp.code}")
                            }
                        }
                    if (manifestResult != null) {
                        circuitBreaker.recordSuccess(config.id)
                        manifestResult
                    } else {
                        // Legacy fallback probe for gateways exposing /v2/health
                        val healthUrl = base.newBuilder().addPathSegment("v2").addPathSegment("health").build()
                        val fallbackReq =
                            Request.Builder().url(healthUrl).applyResolverAuth(secret).get().build()
                        sourceHttp.newCall(fallbackReq).awaitResponse { resp ->
                            if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                            circuitBreaker.recordSuccess(config.id)
                            "HTTP Resolver v2 连通正常"
                        }
                    }
                }
                ExternalSourceKind.WEBDAV -> {
                    val headers = buildBasicAuthHeader(config, secretOverride = secret)
                    val req =
                        Request.Builder()
                            .url(base)
                            .method("PROPFIND", "".toRequestBody("application/xml".toMediaType()))
                            .header("Depth", "0")
                            .apply { headers.forEach { (k, v) -> header(k, v) } }
                            .build()
                    sourceHttp.newCall(req).awaitResponse { resp ->
                        if (resp.code !in 200..299) throw IOException("WebDAV HTTP ${resp.code}")
                        "WebDAV 目录连通正常"
                    }
                }
            }
        }

    private suspend fun resolveViaHttpResolverV2(
        cfg: ExternalSourceConfig,
        track: Track,
        quality: AudioQualityLevel,
    ): ResolvedAudioStream {
        if (circuitBreaker.isOpen(cfg.id)) {
            val sec = ((circuitBreaker.remainingCooldownMs(cfg.id) + 999L) / 1000L).coerceAtLeast(1L)
            throw AudioResolutionException(
                AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                "${cfg.name} 连续失败已熔断，请 ${sec} 秒后重试",
            )
        }
        val (base, policy) = ResolverDestinationPolicy.forConfig(cfg.baseUrl, cfg.allowLanHttp)
        val resolverHttp = policy.client(httpClient)
        val secret = credentials.readSecret(secretKey(cfg.id))
        val endpoint = resolverEndpoint(base, "resolve")
        val artistList =
            track.artists.ifEmpty {
                track.artist.split('/', '、', ',').map(String::trim).filter(String::isNotEmpty)
            }
        val artistStr = track.artist.ifBlank { artistList.joinToString(" / ") }
        val trackId = track.external_id ?: (track.song_id ?: track.id)?.toString() ?: track.identity()
        val payload =
            buildJsonObject {
                put("id", trackId)
                put("external_id", trackId)
                put("track_key", track.identity())
                put("title", track.title)
                put("artists", JsonArray(artistList.map { JsonPrimitive(it) }))
                put("artist", artistStr)
                put("album", track.album)
                put("duration_ms", track.duration_ms)
                put("quality", quality.name)
            }
        val req =
            Request.Builder()
                .url(endpoint)
                .applyResolverAuth(secret)
                .post(payload.toString().toRequestBody(JSON_MEDIA_TYPE))
                .build()
        return try {
            val resolved =
                resolverHttp.newCall(req).awaitResponse { resp ->
                    if (!resp.isSuccessful) {
                        throw AudioResolutionException(
                            AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                            "Resolver v2 返回 HTTP ${resp.code}",
                        )
                    }
                    val body = wire.parseToJsonElement(resp.body?.string().orEmpty()).jsonObject
                    // Primary spec format (docs/HTTP_RESOLVER_V2.md): { "candidates": [{ "url": ..., "headers": ... }] }
                    val candidateObj =
                        body["candidates"]
                            ?.jsonArray
                            ?.mapNotNull { runCatching { it.jsonObject }.getOrNull() }
                            ?.firstOrNull { !it["url"]?.jsonPrimitive?.contentOrNull.isNullOrBlank() }
                    val selected = candidateObj ?: body
                    val streamUrl =
                        selected["url"]?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotBlank)
                            ?: throw AudioResolutionException(
                                AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE,
                                "Resolver v2 未返回可用音频地址",
                            )
                    val url = policy.validate(streamUrl.toHttpUrlOrNull() ?: throw IOException("Invalid Resolver stream URL"))
                    val headers =
                        selected["headers"]?.jsonObject?.mapNotNull { (k, v) ->
                            v.jsonPrimitive.contentOrNull?.let { k to it }
                        }?.toMap().orEmpty()
                    val format = selected["format"]?.jsonPrimitive?.contentOrNull.orEmpty()
                    val mime =
                        selected["mime_type"]?.jsonPrimitive?.contentOrNull
                            ?: formatToMimeType(format)
                    val qLabel =
                        selected["quality_label"]?.jsonPrimitive?.contentOrNull
                            ?: format.takeIf(String::isNotBlank)?.uppercase()
                            ?: cfg.name.ifBlank { "Resolver v2" }
                    registerExternalStream(url.toString(), StreamAccess(headers, resolverPolicy = policy))
                    ResolvedAudioStream(
                        uri = url.toString(),
                        headers = headers,
                        mimeType = mime,
                        providerLabel = qLabel,
                        cacheable = true,
                    )
                }
            circuitBreaker.recordSuccess(cfg.id)
            resolved
        } catch (e: AudioResolutionException) {
            if (e.category == AudioErrorCategory.EXTERNAL_SOURCE_ERROR) {
                circuitBreaker.recordFailure(cfg.id)
            }
            throw e
        } catch (e: IOException) {
            circuitBreaker.recordFailure(cfg.id)
            throw AudioResolutionException(
                AudioErrorCategory.EXTERNAL_SOURCE_ERROR,
                "Resolver v2 连接失败，请检查地址与网络",
                e,
            )
        }
    }

    private suspend fun searchResolverV2(
        cfg: ExternalSourceConfig,
        query: String,
        limit: Int,
    ): List<Track> {
        val (base, policy) = ResolverDestinationPolicy.forConfig(cfg.baseUrl, cfg.allowLanHttp)
        val resolverHttp = policy.client(httpClient)
        val secret = credentials.readSecret(secretKey(cfg.id))
        val searchUrl = resolverEndpoint(base, "search")
        val body =
            buildJsonObject {
                put("query", query)
                put("limit", limit)
                put("offset", 0)
            }
        val postReq =
            Request.Builder()
                .url(searchUrl)
                .applyResolverAuth(secret)
                .post(body.toString().toRequestBody(JSON_MEDIA_TYPE))
                .build()
        val (postTracks, shouldFallbackGet) =
            resolverHttp.newCall(postReq).awaitResponse { resp ->
                when {
                    resp.isSuccessful -> parseResolverSearchTracks(cfg, resp.body?.string().orEmpty()) to false
                    resp.code == 404 || resp.code == 405 -> emptyList<Track>() to true
                    else -> throw IOException("Resolver v2 search HTTP ${resp.code}")
                }
            }
        if (!shouldFallbackGet) return postTracks
        val getUrl =
            searchUrl
                .newBuilder()
                .addQueryParameter("query", query)
                .addQueryParameter("q", query)
                .addQueryParameter("limit", limit.toString())
                .build()
        val getReq = Request.Builder().url(getUrl).applyResolverAuth(secret).get().build()
        return resolverHttp.newCall(getReq).awaitResponse { resp ->
            if (!resp.isSuccessful) throw IOException("Resolver v2 search GET HTTP ${resp.code}")
            parseResolverSearchTracks(cfg, resp.body?.string().orEmpty())
        }
    }

    private fun parseResolverSearchTracks(cfg: ExternalSourceConfig, rawBody: String): List<Track> {
        val root = wire.parseToJsonElement(rawBody).jsonObject
        val items =
            (root["tracks"] ?: root["candidates"] ?: root["items"] ?: root["data"])?.jsonArray.orEmpty()
        return items.mapNotNull { el ->
            val obj = runCatching { el.jsonObject }.getOrNull() ?: return@mapNotNull null
            val title = obj["title"]?.jsonPrimitive?.contentOrNull.orEmpty()
            val extId =
                obj["external_id"]?.jsonPrimitive?.contentOrNull
                    ?: obj["id"]?.jsonPrimitive?.contentOrNull
                    ?: return@mapNotNull null
            val artistsArray =
                obj["artists"]?.jsonArray?.mapNotNull { it.jsonPrimitive.contentOrNull }.orEmpty()
            val artistStr =
                obj["artist"]?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotBlank)
                    ?: artistsArray.joinToString(" / ")
            if (extId.isBlank()) return@mapNotNull null
            val provider = "resolver_v2:${cfg.id.lowercase()}"
            Track(
                provider = provider,
                source_id = cfg.id,
                external_id = extId,
                track_key = "$provider:$extId",
                title = title,
                artists = artistsArray,
                artist = artistStr,
                album = obj["album"]?.jsonPrimitive?.contentOrNull.orEmpty(),
                duration_ms = obj["duration_ms"]?.jsonPrimitive?.longOrNull ?: 0L,
                cover_url = obj["cover_url"]?.jsonPrimitive?.contentOrNull.orEmpty(),
                stream_url = obj["url"]?.jsonPrimitive?.contentOrNull,
            )
        }
    }

    private suspend fun searchSubsonic(
        cfg: ExternalSourceConfig,
        query: String,
        limit: Int,
    ): List<Track> {
        val url =
            buildSubsonicUrl(
                cfg,
                "search3.view",
                mapOf(
                    "query" to query,
                    "artistCount" to "0",
                    "albumCount" to "0",
                    "songCount" to limit.toString(),
                ),
            )
        return httpClient.newCall(Request.Builder().url(url).get().build()).awaitResponse { resp ->
            if (!resp.isSuccessful) throw IOException("OpenSubsonic HTTP ${resp.code}")
            val root =
                wire.parseToJsonElement(resp.body?.string().orEmpty()).jsonObject["subsonic-response"]
                    ?.jsonObject ?: return@awaitResponse emptyList()
            val songs = root["searchResult3"]?.jsonObject?.get("song")?.jsonArray.orEmpty()
            songs.mapNotNull { el ->
                val obj = el.jsonObject
                val id = obj["id"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null
                val coverId = obj["coverArt"]?.jsonPrimitive?.contentOrNull
                val coverUrl =
                    coverId?.let {
                        buildSubsonicUrl(cfg, "getCoverArt.view", mapOf("id" to it, "size" to "384"))
                    }.orEmpty()
                val durationSec = obj["duration"]?.jsonPrimitive?.longOrNull ?: 0L
                Track(
                    provider = "subsonic",
                    source_id = cfg.id,
                    external_id = id,
                    track_key = "subsonic:${cfg.id}:$id",
                    title = obj["title"]?.jsonPrimitive?.contentOrNull.orEmpty(),
                    artist = obj["artist"]?.jsonPrimitive?.contentOrNull.orEmpty(),
                    album = obj["album"]?.jsonPrimitive?.contentOrNull.orEmpty(),
                    duration_ms = durationSec * 1000L,
                    cover_url = coverUrl,
                )
            }
        }
    }

    private suspend fun searchWebDav(
        cfg: ExternalSourceConfig,
        query: String,
        limit: Int,
    ): List<Track> {
        val base = ExternalUrlValidator.validateBaseUrl(cfg.baseUrl, cfg.allowLanHttp)
        val headers = buildBasicAuthHeader(cfg)
        val req =
            Request.Builder()
                .url(base)
                .method("PROPFIND", "".toRequestBody("application/xml".toMediaType()))
                .header("Depth", "1")
                .apply { headers.forEach { (k, v) -> header(k, v) } }
                .build()
        return httpClient.newCall(req).awaitResponse { resp ->
            if (resp.code !in 200..299) throw IOException("WebDAV HTTP ${resp.code}")
            val xml = resp.body?.string().orEmpty()
            val hrefRegex =
                Regex(
                    """<(?:[A-Za-z0-9_-]+:)?href>([^<]+)</(?:[A-Za-z0-9_-]+:)?href>""",
                    RegexOption.IGNORE_CASE,
                )
            val audioExt = setOf("flac", "mp3", "m4a", "aac", "ogg", "opus", "wav", "alac")
            hrefRegex
                .findAll(xml)
                .map { it.groupValues[1].trim() }
                .filter { href ->
                    val ext = href.substringAfterLast('.', "").lowercase()
                    ext in audioExt
                }
                .mapNotNull { href ->
                    val resolved = base.resolve(href)?.takeIf { base.sameOrigin(it) } ?: return@mapNotNull null
                    val decodedName =
                        java.net.URLDecoder.decode(
                            resolved.pathSegments.lastOrNull().orEmpty(),
                            "UTF-8",
                        )
                    val title = decodedName.substringBeforeLast('.')
                    if (!title.contains(query, ignoreCase = true)) return@mapNotNull null
                    val idDigest =
                        MessageDigest.getInstance("SHA-256")
                            .digest(resolved.toString().toByteArray())
                            .take(12)
                            .joinToString("") { "%02x".format(it) }
                    Track(
                        provider = "webdav",
                        source_id = cfg.id,
                        external_id = href,
                        track_key = "webdav:${cfg.id}:$idDigest",
                        title = title,
                        artist = cfg.name.ifBlank { "WebDAV" },
                        stream_url = resolved.toString(),
                    )
                }
                .take(limit)
                .toList()
        }
    }

    private fun resolveSourceConfig(
        track: Track,
        expectedKind: ExternalSourceKind? = null,
        sourcesOverride: List<ExternalSourceConfig>? = null,
    ): ExternalSourceConfig? {
        val sources = sourcesOverride ?: enabledSources()
        val matching = sources.filter { expectedKind == null || it.kind == expectedKind }
        val idHint = track.source_id?.takeUnless { it == LAN_DESKTOP_SOURCE_ID }
            ?: track.effectiveProvider().substringAfter(':', "").ifBlank {
                track.track_key?.split(':', limit = 3)?.takeIf { it.size == 3 }?.get(1).orEmpty()
            }
        return if (idHint.isNullOrBlank()) matching.firstOrNull()
        else matching.firstOrNull { it.id.equals(idHint, ignoreCase = true) }
    }

    private fun resolverEndpoint(base: HttpUrl, segment: String): HttpUrl {
        val last = base.pathSegments.lastOrNull { it.isNotEmpty() }
        if (last.equals(segment, ignoreCase = true)) return base
        return base.newBuilder().addPathSegment(segment).build()
    }

    private fun Request.Builder.applyResolverAuth(secret: String?): Request.Builder {
        if (!secret.isNullOrBlank()) {
            val cleaned = secret.trim()
            header("X-Resolver-Token", cleaned)
            header("X-API-Key", cleaned)
            header("Authorization", "Bearer $cleaned")
        }
        return this
    }

    private fun formatToMimeType(format: String): String? =
        when (format.lowercase()) {
            "flac" -> "audio/flac"
            "mp3" -> "audio/mpeg"
            "m4a", "aac", "alac" -> "audio/mp4"
            "ogg" -> "audio/ogg"
            "opus" -> "audio/opus"
            "wav" -> "audio/wav"
            else -> null
        }

    private fun buildSubsonicUrl(
        cfg: ExternalSourceConfig,
        endpoint: String,
        params: Map<String, String>,
        secretOverride: String? = null,
    ): String {
        val base = ExternalUrlValidator.validateBaseUrl(cfg.baseUrl, cfg.allowLanHttp)
        val password = secretOverride ?: credentials.readSecret(secretKey(cfg.id)).orEmpty()
        val salt =
            ByteArray(6).also(random::nextBytes).joinToString("") { "%02x".format(it) }
        val token =
            MessageDigest.getInstance("MD5")
                .digest((password + salt).toByteArray())
                .joinToString("") { "%02x".format(it) }
        return base
            .newBuilder()
            .addPathSegment("rest")
            .addPathSegment(endpoint)
            .addQueryParameter("u", cfg.username)
            .addQueryParameter("t", token)
            .addQueryParameter("s", salt)
            .addQueryParameter("v", "1.16.1")
            .addQueryParameter("c", "PrunerStudio")
            .addQueryParameter("f", "json")
            .apply { params.forEach { (k, v) -> addQueryParameter(k, v) } }
            .build()
            .toString()
    }

    private fun buildBasicAuthHeader(
        cfg: ExternalSourceConfig,
        secretOverride: String? = null,
    ): Map<String, String> {
        val password = secretOverride ?: credentials.readSecret(secretKey(cfg.id)).orEmpty()
        if (cfg.username.isBlank() && password.isBlank()) return emptyMap()
        val encoded =
            Base64.getEncoder().encodeToString("${cfg.username}:$password".toByteArray(Charsets.UTF_8))
        return mapOf("Authorization" to "Basic $encoded")
    }

    private fun registerExternalStream(url: String, access: StreamAccess) {
        synchronized(streamRegistryLock) {
            authorizedExternalStreams[url] = access
        }
    }

    companion object {
        const val LAN_DESKTOP_SOURCE_ID = "lan_desktop"
        private const val MAX_AUTHORIZED_STREAMS = 512
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()

        fun isLanProxyStreamPath(path: String): Boolean =
            path.startsWith("/api/lan/v1/audio/stream/") || path.startsWith("/api/lan/v1/stream/")
    }
}
