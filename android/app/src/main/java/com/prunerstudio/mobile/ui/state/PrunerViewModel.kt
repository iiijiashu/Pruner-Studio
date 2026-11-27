package com.prunerstudio.mobile.ui.state

import android.app.Application
import android.net.Uri
import android.os.SystemClock
import androidx.compose.runtime.*
import androidx.compose.ui.graphics.Color
import androidx.core.content.edit
import androidx.core.net.toUri
import androidx.lifecycle.*
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import com.prunerstudio.mobile.core.engine.*
import com.prunerstudio.mobile.core.lan.*
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.player.*
import com.prunerstudio.mobile.core.sources.CompositeAudioSourceResolver
import com.prunerstudio.mobile.ui.theme.*
import java.io.File
import java.util.UUID
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.*
import kotlinx.serialization.json.*

enum class Tab(val label: String) {
    DISCOVER("发现"),
    LIBRARY("音乐库"),
    HISTORY("历史"),
    DECK("清理"),
    SETTINGS("设置"),
}

data class Desktop(val host: String, val port: Int, val fingerprint: String, val name: String)

enum class DeckOrder(val label: String) {
    RANDOM("随机抽选"),
    ORIGINAL("歌单原序"),
}

internal fun orderDeckTracks(
    tracks: List<Track>,
    order: DeckOrder,
    seed: Long,
): List<Track> {
    if (tracks.size <= 1) return tracks.toList()
    return when (order) {
        DeckOrder.ORIGINAL -> tracks.toList()
        DeckOrder.RANDOM -> {
            val rng = kotlin.random.Random(seed)
            val (stubs, valid) = tracks.partition { it.isStubTrack() }
            valid.shuffled(rng) + stubs.shuffled(rng)
        }
    }
}

internal fun buildSmartRadioQueue(
    history: List<Track>,
    library: List<Track>,
    daily: List<Track>,
    exploreRatio: Float,
    seed: Long = kotlin.random.Random.nextLong(),
): List<Track> {
    val ratio = sanitizeExploreRatio(exploreRatio)
    val historyMap =
        history
            .mapNotNull { h -> (h.id ?: h.song_id)?.takeIf { it > 0L }?.let { it to h } }
            .toMap()
    val familiar =
        (history + library.filter { it.play_count > 0 }).distinctBy { it.identity() }
    val familiarIds = familiar.mapTo(HashSet()) { it.identity() }
    val novel =
        (daily + library)
            .filter { it.identity() !in familiarIds }
            .distinctBy { it.identity() }
    if (familiar.isEmpty() && novel.isEmpty()) return emptyList()
    if (ratio <= 0.0001f) {
        val pool = familiar.ifEmpty { novel }
        return BalancedShuffleEngine.shuffle(pool, historyMap, seed = seed)
    }
    if (ratio >= 0.9999f) {
        val pool = novel.ifEmpty { familiar }
        return BalancedShuffleEngine.shuffle(pool, historyMap, seed = seed)
    }
    val shuffledFamiliar = BalancedShuffleEngine.shuffle(familiar, historyMap, seed = seed)
    val shuffledNovel = BalancedShuffleEngine.shuffle(novel, historyMap, seed = seed xor 0x517cc1b727220a95L)
    val totalTarget = (familiar.size + novel.size).coerceAtMost(60)
    val wantNovel = kotlin.math.round(totalTarget * ratio).toInt().coerceIn(0, shuffledNovel.size)
    val wantFamiliar = (totalTarget - wantNovel).coerceIn(0, shuffledFamiliar.size)
    val combined = shuffledFamiliar.take(wantFamiliar) + shuffledNovel.take(wantNovel)
    return BalancedShuffleEngine.shuffle(combined, historyMap, seed = seed + 31L)
}

data class Deck(
    val playlist: PlaylistSummary? = null,
    val tracks: List<Track> = emptyList(),
    val decisions: List<Boolean> = emptyList(),
) {
    val current
        get() = tracks.getOrNull(decisions.size)

    val staged
        get() = tracks.take(decisions.size).filterIndexed { i, _ -> !decisions[i] }
}

fun LanLocalFile.track() =
    Track(
        provider = "local",
        file_id = file_id,
        external_id = file_id,
        title = title,
        artist = artist,
        album = album,
        duration_ms = duration_ms,
        cover_url = cover_url,
    )

fun Track.matches(q: String) =
    listOf(title, artist, album, artists.joinToString()).any { it.contains(q, true) }

@androidx.annotation.OptIn(UnstableApi::class)
class PrunerViewModel internal constructor(application: Application, private val core: PrunerAppContainer) : AndroidViewModel(application) {
    constructor(application: Application) : this(application, PrunerAppContainer.get(application))
    private var lanState by mutableStateOf(LanSessionState())
    val pairingReady get() = lanState.initialized
    val pairingClaimInFlight get() = claimJob?.isActive == true || lanState.pairingClaimInFlight
    private var claimJob by mutableStateOf<Job?>(null)
    private val prefs by lazy { application.getSharedPreferences("pruner_ui_prefs", 0) }

    private suspend fun <T> io(block: suspend () -> T): T = withContext(Dispatchers.IO) { block() }

    private val notices = Channel<String>(Channel.BUFFERED)
    val messages = notices.receiveAsFlow()

    fun notice(text: String) {
        notices.trySend(text)
    }

    fun runTask(block: suspend () -> Unit) =
        viewModelScope.launch {
            try {
                block()
            } catch (e: CancellationException) {
                throw e
            } catch (e: AudioResolutionException) {
                notice(e.message?.takeIf(String::isNotBlank) ?: e.category.userMessage)
            } catch (e: LanException) {
                notice(
                    if (e.status == 401) "桌面配对已失效，请重新配对"
                    else "桌面服务请求失败 (${e.code})"
                )
            } catch (e: javax.net.ssl.SSLHandshakeException) {
                notice("桌面证书校验失败，请重新配对")
            } catch (e: SecurityException) {
                notice("缺少所需系统权限，请在设置中授权后重试")
            } catch (e: Exception) {
                notice("操作未完成，请检查连接或权限后重试")
            }
        }

    var notificationRequested = false
    var tab by mutableStateOf(Tab.DISCOVER)
    var librarySection by mutableIntStateOf(0)
    var historyQuery by mutableStateOf("")
    var theme by mutableStateOf(PrunerThemeId.parchment)
    var accent by mutableStateOf(AccentPreset.sage)
    var dynamicCoverAccent by mutableStateOf(false)
    var extractedCoverColor by mutableStateOf<Color?>(null)
    var showLyricsTranslation by mutableStateOf(true)
    var showLyricsRomaji by mutableStateOf(false)
    var quality by mutableStateOf(AudioQualityLevel.jymaster)
    var scale by mutableFloatStateOf(1f)
    var exploreRatio by mutableFloatStateOf(SMART_RADIO_EXPLORE_DEFAULT)
        private set
    var deckOrder by mutableStateOf(DeckOrder.RANDOM)
        private set
    private var deckSeed: Long = System.nanoTime()
    private var deckSourceTracks: List<Track> = emptyList()
    var user by mutableStateOf("")
    private var uid: Long? = null
    private var account = "offline"
    private var onlinePlaylists = emptyList<PlaylistSummary>()
    var playlists by mutableStateOf(emptyList<PlaylistSummary>())
    var history by mutableStateOf(emptyList<Track>())
    var device by mutableStateOf(emptyList<Track>())
    var cached by mutableStateOf(emptyList<Track>())
    var files by mutableStateOf(emptyList<Track>())
    var daily by mutableStateOf(emptyList<Track>())
    var rules by mutableStateOf(emptyList<PruningRule>())
    var notInterestedKeys by mutableStateOf(emptySet<String>())
    var selected by mutableStateOf<PlaylistSummary?>(null)
    var tracks by mutableStateOf(emptyList<Track>())
    var deck by mutableStateOf(Deck())
    var removing by mutableStateOf(false)
    var removeProgress by mutableStateOf("")
    var isRefreshing by mutableStateOf(false)
    var isLoadingPlaylist by mutableStateOf(false)
    var isScanning by mutableStateOf(false)
    var isSearching by mutableStateOf(false)
    var previewTrackKey by mutableStateOf<String?>(null)
    var isPreviewLoading by mutableStateOf(false)

    // Decoupled structural playback state vs high-frequency playback position stream
    private var _state by mutableStateOf(PlaybackSessionSnapshot())
    private val _playbackProgress = MutableStateFlow(PlaybackProgress())
    val playbackProgress: StateFlow<PlaybackProgress> = _playbackProgress.asStateFlow()

    /**
     * Live playback position in seconds sourced from the decoupled [playbackProgress] stream
     * (projected by elapsed time when actively playing) so play-mode switches, queue edits,
     * and desktop handoff never regress to stale 0.0s snapshot timestamps.
     */
    fun currentPositionSec(): Double {
        val prog = _playbackProgress.value
        val baseMs =
            if (prog.positionMs > 0L || prog.updatedAtElapsedMs > 0L) {
                val projected =
                    if (prog.isPlaying && prog.updatedAtElapsedMs > 0L) {
                        val delta = (SystemClock.elapsedRealtime() - prog.updatedAtElapsedMs).coerceAtLeast(0L)
                        prog.positionMs + (delta * prog.playbackRate.coerceIn(0.5f, 2f)).toLong()
                    } else {
                        prog.positionMs
                    }
                if (prog.durationMs > 0L) projected.coerceIn(0L, prog.durationMs)
                else projected.coerceAtLeast(0L)
            } else {
                (_state.position_sec.coerceAtLeast(0.0) * 1000.0).toLong()
            }
        return baseMs / 1000.0
    }

    var state: PlaybackSessionSnapshot
        get() = _state
        set(value) {
            _state = value
            val durMs =
                (value.duration_sec.takeIf { it.isFinite() && it > 0.0 }
                    ?: value.current_track?.durationSeconds() ?: 0.0) * 1000.0
            _playbackProgress.value =
                PlaybackProgress(
                    positionMs = (value.position_sec.coerceAtLeast(0.0) * 1000.0).toLong(),
                    durationMs = durMs.toLong().coerceAtLeast(0L),
                    isPlaying = value.is_playing,
                    playbackRate = value.playback_rate.toFloat().coerceIn(0.5f, 2f),
                    updatedAtElapsedMs = SystemClock.elapsedRealtime(),
                )
        }

    var isBuffering by mutableStateOf(false)
    var playbackError by mutableStateOf<String?>(null)
    var lyrics by mutableStateOf(ParsedLyrics(emptyList()))
    var offset by mutableLongStateOf(0)
    var liked by mutableStateOf(false)
    var eq by mutableStateOf(List(10) { 0f })
    val eqStateFlow: StateFlow<EqState> get() = core.playback.eqState
    val cacheProgressFlow: StateFlow<OfflineCacheProgress> get() = core.audioCache.progress
    var cacheSizeBytes by mutableLongStateOf(0L)
    var sleep by mutableIntStateOf(0)
    val desktop get() = lanState.pairing?.let { Desktop(it.host, it.port, it.fingerprint, it.name) }
    var hosts by mutableStateOf(emptyList<Announcement>())
    var pair by mutableStateOf<PairingUri?>(null)
        private set
    var pairError by mutableStateOf("")
    val live get() = if (lanState.sseConnected) "已连接" else lanState.lastError ?: "未连接"
    val incoming get() = lanState.pendingHandoff
    fun dismissHandoff() = core.lanSession.dismissHandoff()
    var qr by mutableStateOf<String?>(null)
    var qrStatus by mutableStateOf("")
    var seconds by mutableIntStateOf(0)
    var onlineResults by mutableStateOf(emptyList<Track>())
    var externalResults by mutableStateOf(emptyList<Track>())
    var searchHistory by mutableStateOf(emptyList<String>())
    var externalSources by mutableStateOf(emptyList<ExternalSourceConfig>())
    val hasMultiSourceSearch
        get() = externalSources.isNotEmpty() || desktop != null || externalResults.isNotEmpty()
    val lanResults get() = lanState.searchResults.map { it.track() }
    val more get() = lanState.lanBrowseHasMore
    val browse get() = lanState.lanBrowseTracks.map { it.track() }
    private var searchJob: Job? = null
    private var searchSeq: Long = 0L
    private var playlistJob: Job? = null
    private var lyricJob: Job? = null
    private var cacheJob: Job? = null

    init {
        core.playback.onNotificationLikeToggle = { like() }
        runTask {
            io {
                    core.lanSession
                    prefs.all
                }
                .let { p ->
                    theme = PrunerThemeId.entries.find { it.name == p["theme"] } ?: theme
                    accent = AccentPreset.entries.find { it.name == p["accent"] } ?: accent
                    quality = AudioQualityLevel.entries.find { it.name == p["quality"] } ?: quality
                    scale = (p["scale"] as? Float) ?: 1f
                    exploreRatio =
                        (p["smart_radio_explore_ratio"] as? Float)?.let(::sanitizeExploreRatio)
                            ?: exploreRatio
                    dynamicCoverAccent = (p["dynamic_cover_accent"] as? Boolean) ?: false
                    showLyricsTranslation = (p["lyrics_show_translation"] as? Boolean) ?: true
                    showLyricsRomaji = (p["lyrics_show_romaji"] as? Boolean) ?: false
                }
            core.lanSession.ensureConnected()
            val saved = io { prefs.getString("lastAccount", null) }
            lanState = core.lanSession.state.value
            account = lanState.pairing?.accountId ?: saved ?: "offline"
            runTask {
                core.lanSession.state.collect { next ->
                    val old = lanState
                    lanState = next
                    if (next.pairing != old.pairing) {
                        stopPreview()
                        cacheJob?.cancel()
                        searchJob?.cancel()
                        externalResults =
                            externalResults.filterNot {
                                it.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID
                            }
                    }
                    if (next.pairing != null && next.pairing != old.pairing) {
                        if (uid?.toString() != next.pairing.accountId) {
                            uid = null
                            user = ""
                        }
                        switchAccount(next.pairing.accountId)
                    }
                    if (next.lastSyncAt != null && next.lastSyncAt != old.lastSyncAt) {
                        readCache()
                        if (state.current_track == null) safeRestore(account)
                    }
                    if (next.lastError != null && next.lastError != old.lastError) notice(next.lastError)
                }
            }
            readCache()
            refreshCacheStats()
            safeRestore(account)
            if (io { core.credentials.cookie != null }) {
                try {
                    loadAccount()
                } catch (e: CancellationException) {
                    throw e
                } catch (_: java.io.IOException) {
                    // Offline startup with cached library should stay quiet.
                }
            }
        }
        runTask {
            val player = io { core.playback }
            player.state.collect { next ->
                queueEntryIds = next.queueEntryIds
                val prev = _state
                val changed =
                    prev.current_track?.identity() != next.snapshot.current_track?.identity()
                val structuralChanged =
                    changed ||
                        prev.is_playing != next.snapshot.is_playing ||
                        prev.queue_index != next.snapshot.queue_index ||
                        prev.play_mode != next.snapshot.play_mode ||
                        prev.playback_rate != next.snapshot.playback_rate ||
                        prev.duration_sec != next.snapshot.duration_sec ||
                        prev.queue.size != next.snapshot.queue.size ||
                        prev.queue != next.snapshot.queue
                if (structuralChanged) {
                    _state = next.snapshot
                }
                isBuffering = next.isBuffering
                if (next.error == null && (next.snapshot.is_playing || next.isBuffering)) {
                    playbackError = null
                }
                if (changed) {
                    extractedCoverColor = null
                    loadLyrics(next.snapshot.current_track)
                }
                if (next.error != null && next.errorToken > lastErrorToken) {
                    lastErrorToken = next.errorToken
                    val msg = next.errorMessage ?: "音频暂时无法播放，请稍后重试"
                    playbackError = msg
                    notice(msg)
                }
            }
        }
        runTask {
            val player = io { core.playback }
            player.progress.collect { prog ->
                _playbackProgress.value = prog
            }
        }
    }

    var queueEntryIds by mutableStateOf(emptyList<String>())
        private set

    private var lastErrorToken: Long = 0L

    private suspend fun safeRestore(targetAccount: String) {
        try {
            core.playback.restore(targetAccount)
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            // Do not block startup hydration or account loading on stale session restore failure.
        }
    }

    fun preferences(
        themeId: PrunerThemeId = theme,
        preset: AccentPreset = accent,
        level: AudioQualityLevel = quality,
        font: Float = scale,
        coverAccent: Boolean = dynamicCoverAccent,
    ) {
        theme = themeId
        accent = preset
        val qualityChanged = quality != level
        quality = level
        scale = font
        dynamicCoverAccent = coverAccent
        runTask {
            io {
                prefs.edit {
                    putString("theme", themeId.name)
                    putString("accent", preset.name)
                    putString("quality", level.name)
                    putFloat("scale", font)
                    putBoolean("dynamic_cover_accent", coverAccent)
                }
            }
            if (qualityChanged && quality == level) core.playback.refreshQuality(level)
        }
    }

    fun updateLyricsDisplay(translation: Boolean = showLyricsTranslation, romaji: Boolean = showLyricsRomaji) {
        showLyricsTranslation = translation
        showLyricsRomaji = romaji
        runTask {
            io {
                prefs.edit {
                    putBoolean("lyrics_show_translation", translation)
                    putBoolean("lyrics_show_romaji", romaji)
                }
            }
        }
    }

    private suspend fun readCache() {
        val a = account
        val snapshot = io {
            with(core.repository) {
                Catalog(
                    history(a),
                    deviceTracks(a),
                    localLibrary(a).map { it.track() },
                    playlists(a),
                    rules(a),
                    cachedTracks(a),
                    feedback(a).filter { it.action == "not_interested" }.mapTo(HashSet()) { it.track_key },
                    searchHistory(a),
                    core.sources.listSources(),
                )
            }
        }
        if (a != account) return
        cached = snapshot.cached
        history = snapshot.history
        device = snapshot.device
        files = snapshot.files
        playlists = (onlinePlaylists + snapshot.playlists).distinctBy { it.id.content }
        rules = snapshot.rules
        notInterestedKeys = snapshot.notInterested
        searchHistory = snapshot.searchHistory
        externalSources = snapshot.externalSources
    }

    private data class Catalog(
        val history: List<Track>,
        val device: List<Track>,
        val files: List<Track>,
        val playlists: List<PlaylistSummary>,
        val rules: List<PruningRule>,
        val cached: List<Track>,
        val notInterested: Set<String>,
        val searchHistory: List<String>,
        val externalSources: List<ExternalSourceConfig>,
    )

    fun refresh() = runTask { readCache() }

    fun pullToRefresh() = runTask {
        if (isRefreshing) return@runTask
        isRefreshing = true
        try {
            readCache()
            if (uid != null) {
                runCatching { dailyRecommendations().join() }
            }
            if (desktop != null) {
                sync()
            }
            refreshCacheStats()
        } finally {
            isRefreshing = false
        }
    }

    private suspend fun switchAccount(a: String) {
        if (a == account) return
        core.auth.invalidateWrites()
        stopPreview()
        playlistJob?.cancel()
        searchJob?.cancel()
        if (state.queue.isNotEmpty()) core.playback.setQueue(account, emptyList())
        selected = null
        tracks = emptyList()
        deck = Deck()
        deckSourceTracks = emptyList()
        daily = emptyList()
        onlineResults = emptyList()
        externalResults = emptyList()
        onlinePlaylists = emptyList()
        account = a
        readCache()
        safeRestore(a)
    }

    private suspend fun loadAccount() {
        val profile = io { core.netease.fetchAccount()["profile"] as? JsonObject } ?: return
        val id = profile.getValue("userId").jsonPrimitive.long
        val paired = io { core.lan()?.config?.accountId }
        if (paired != null && paired != id.toString()) {
            notice("桌面与网易云账号不同，请先解除配对")
            return
        }
        switchAccount(id.toString())
        uid = id
        user = profile["nickname"]?.jsonPrimitive?.content ?: id.toString()
        io { prefs.edit { putString("lastAccount", id.toString()) } }
        val list = io {
            buildList {
                do {
                    val page = core.netease.fetchUserPlaylists(id, 100, size)
                    val rows = page["playlist"]?.jsonArray.orEmpty()
                    rows.forEach { value ->
                        val p = value.jsonObject
                        add(
                            PlaylistSummary(
                                p.getValue("id").jsonPrimitive,
                                p.getValue("name").jsonPrimitive.content,
                                if (
                                    p["creator"]
                                        ?.jsonObject
                                        ?.get("userId")
                                        ?.jsonPrimitive
                                        ?.longOrNull != id
                                )
                                    "subscribed"
                                else if (p["specialType"]?.jsonPrimitive?.intOrNull == 5) "liked"
                                else "self_created",
                                p["trackCount"]?.jsonPrimitive?.intOrNull ?: 0,
                                cover_url = p["coverImgUrl"]?.jsonPrimitive?.content.orEmpty(),
                            )
                        )
                    }
                } while (rows.size == 100 && page["more"]?.jsonPrimitive?.booleanOrNull == true)
            }
        }
        if (uid == id) {
            onlinePlaylists = list
            playlists = list
            io { core.repository.cacheSummaries(id.toString(), list) }
        }
        val recommendations = io {
            core.netease
                .fetchDailyRecommend()["data"]
                ?.jsonObject
                ?.get("dailySongs")
                ?.jsonArray
                .orEmpty()
                .map { core.netease.parseTrack(it.jsonObject) }
        }
        if (uid == id) daily = recommendations
    }

    fun login() = runTask {
        qr = null
        qrStatus = "正在生成二维码"
        core.auth.start(viewModelScope) { token ->
            try {
                val key = io { core.netease.createQrKey() }
                qr = "https://music.163.com/login?codekey=" + Uri.encode(key)
                val end = android.os.SystemClock.elapsedRealtime() + 120000
                while (android.os.SystemClock.elapsedRealtime() < end) {
                    seconds = ((end - android.os.SystemClock.elapsedRealtime()) / 1000).toInt()
                    val poll = core.netease.checkQrStatus(key)
                    when (poll.code) {
                        800 -> break
                        801 -> qrStatus = "等待扫码"
                        802 -> qrStatus = "请在手机上确认"
                        803 -> {
                            if (!core.auth.commit(token, requireNotNull(poll.candidateCookie))) return@start
                            qr = null
                            qrStatus = ""
                            loadAccount()
                            return@start
                        }
                    }
                    delay(2000)
                }
                qr = null
                qrStatus = "二维码已过期"
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                qr = null
                qrStatus = "登录失败，请重试"
            }
        }
    }

    fun cancelLogin() = runTask {
        core.auth.cancel()
        qr = null
        qrStatus = ""
    }

    fun logout() = runTask {
        core.auth.cancel(logout = true)
        qr = null
        qrStatus = ""
        uid = null
        user = ""
        onlinePlaylists = emptyList()
        daily = emptyList()
        readCache()
    }

    fun openPlaylist(p: PlaylistSummary, forDeck: Boolean = false) {
        playlistJob?.cancel()
        playlistJob = runTask {
            isLoadingPlaylist = true
            try {
                val a = account
                val cached = io { core.repository.playlistTracks(a, p.id.content) }
                if (!forDeck && a == account) {
                    selected = p
                    tracks = cached
                }
                val list =
                    if (uid != null && p.id.longOrNull != null)
                        try {
                            io {
                                core.netease.fetchPlaylistTracks(p.id.long).also {
                                    core.repository.cachePlaylist(a, p, it)
                                }
                            }
                        } catch (e: java.io.IOException) {
                            notice("使用本地歌单缓存")
                            cached
                        }
                    else cached
                if (a != account) return@runTask
                if (forDeck) {
                    startDeck(p, list)
                    tab = Tab.DECK
                } else {
                    selected = p
                    tracks = list
                }
            } finally {
                isLoadingPlaylist = false
            }
        }
    }

    fun scan() = runTask {
        isScanning = true
        try {
            val a = account
            val found = io { core.scanner.scan(a) }
            readCache()
            notice("已扫描 ${found.size} 首本机音频")
        } finally {
            isScanning = false
        }
    }

    fun search(q: String) {
        searchJob?.cancel()
        val seq = ++searchSeq
        onlineResults = emptyList()
        externalResults = emptyList()
        core.lanSession.search(q)
        if (q.isBlank()) {
            isSearching = false
            return
        }
        isSearching = true
        searchJob = runTask {
            try {
                delay(250)
                coroutineScope {
                    launch {
                        val neteaseHits =
                            io {
                                try {
                                    core.netease
                                        .searchSongs(q)["result"]
                                        ?.jsonObject
                                        ?.get("songs")
                                        ?.jsonArray
                                        .orEmpty()
                                        .map { core.netease.parseTrack(it.jsonObject) }
                                } catch (e: CancellationException) {
                                    throw e
                                } catch (_: Exception) {
                                    null
                                }
                            }
                        if (searchSeq != seq) return@launch
                        if (neteaseHits == null) notice("网易云在线搜索暂不可用")
                        else onlineResults = neteaseHits
                    }
                    launch {
                        val extHits =
                            io {
                                try {
                                    core.sources.search(q, 30)
                                } catch (e: CancellationException) {
                                    throw e
                                } catch (_: Exception) {
                                    emptyList()
                                }
                            }
                        if (searchSeq != seq) return@launch
                        externalResults = extHits
                    }
                }
            } finally {
                if (searchSeq == seq) {
                    isSearching = false
                }
            }
        }
    }

    fun recordSearchQuery(q: String) = runTask {
        val clean = q.trim()
        if (clean.isEmpty()) return@runTask
        val a = account
        io { core.repository.recordSearchQuery(a, clean) }
        searchHistory = io { core.repository.searchHistory(a) }
    }

    fun deleteSearchHistory(q: String) = runTask {
        val a = account
        io { core.repository.deleteSearchQuery(a, q) }
        searchHistory = io { core.repository.searchHistory(a) }
    }

    fun clearSearchHistory() = runTask {
        val a = account
        io { core.repository.clearSearchHistory(a) }
        searchHistory = emptyList()
    }

    fun browseLan(q: String, append: Boolean = false) = core.lanSession.browse(q, append)

    fun updateExploreRatio(ratio: Float) {
        val clean = sanitizeExploreRatio(ratio)
        exploreRatio = clean
        runTask {
            io { prefs.edit { putFloat("smart_radio_explore_ratio", clean) } }
        }
    }

    fun playSmartRadio() = runTask {
        val library = (device + files + cached).distinctBy { it.identity() }
        val queue = buildSmartRadioQueue(history, library, daily, exploreRatio)
        if (queue.isEmpty()) {
            notice("扫描本机音频或同步曲库后即可开启心动电台")
            return@runTask
        }
        play(queue, index = 0, mode = PlayMode.smart_radio)
    }

    fun startDeck(
        playlist: PlaylistSummary?,
        sourceTracks: List<Track>,
        order: DeckOrder = deckOrder,
        seed: Long = System.nanoTime(),
    ) {
        stopPreview()
        deckOrder = order
        deckSeed = seed
        deckSourceTracks = sourceTracks.toList()
        deck = Deck(playlist, orderDeckTracks(deckSourceTracks, order, seed))
    }

    fun updateDeckOrder(order: DeckOrder) {
        if (removing) return
        deckOrder = order
        val currentDeck = deck
        if (currentDeck.tracks.isEmpty()) return
        val decidedCount = currentDeck.decisions.size
        val decided = currentDeck.tracks.take(decidedCount)
        val remaining = currentDeck.tracks.drop(decidedCount)
        if (remaining.size <= 1) return
        val reordered =
            when (order) {
                DeckOrder.RANDOM -> orderDeckTracks(remaining, DeckOrder.RANDOM, deckSeed)
                DeckOrder.ORIGINAL -> {
                    val originalIndex =
                        deckSourceTracks.withIndex().associate { (idx, tr) -> tr.identity() to idx }
                    if (originalIndex.isEmpty()) remaining
                    else remaining.sortedBy { originalIndex[it.identity()] ?: Int.MAX_VALUE }
                }
            }
        stopPreview()
        deck = currentDeck.copy(tracks = decided + reordered)
    }

    fun skipToAnotherDeckTrack() {
        if (removing) return
        val currentDeck = deck
        val decidedCount = currentDeck.decisions.size
        val remaining = currentDeck.tracks.drop(decidedCount)
        if (remaining.size <= 1) return
        stopPreview()
        val decided = currentDeck.tracks.take(decidedCount)
        val rotated = remaining.drop(1) + remaining.first()
        deck = currentDeck.copy(tracks = decided + rotated)
    }

    fun decide(keep: Boolean) {
        if (!removing && deck.current != null) deck = deck.copy(decisions = deck.decisions + keep)
    }

    fun undo() {
        if (!removing && deck.decisions.isNotEmpty())
            deck = deck.copy(decisions = deck.decisions.dropLast(1))
    }

    fun confirmReview(review: Deck) {
        if (review !== deck || removing || review.staged.isEmpty()) return
        removing = true
        removeProgress = ""
        runTask {
            try {
                val a = account
                val p = review.playlist
                var partialFailureMessage: String? = null
                var failedIds = emptySet<Long>()
                if (p != null && canRemove(review)) {
                    val session = core.auth.captureWriteSession(a)
                        ?: throw java.io.IOException("NetEase login required")
                    if (account != a || !canRemove(review)) return@runTask
                    val likedPlaylist = onlinePlaylists.first { it.id == p.id }.kind == "liked"
                    val refreshed = io {
                        val currentOwner =
                            core.netease
                                .fetchAccount(session)["profile"]
                                ?.jsonObject
                                ?.get("userId")
                                ?.jsonPrimitive
                                ?.content
                        require(session.accountId == currentOwner)
                        val before = core.netease.fetchPlaylistTracks(p.id.long, strict = true, session = session)
                        val stagedIds =
                            review.staged.map { requireNotNull(it.song_id ?: it.id) }.toSet()
                        val remaining =
                            before.mapNotNull { it.song_id ?: it.id }.filter(stagedIds::contains)
                        var circuitTripped = false
                        var succeededIds = emptySet<Long>()
                        if (likedPlaylist) {
                            val batch =
                                core.netease.unlikeTracksBatch(remaining, session) { done, total ->
                                    removeProgress = "$done / $total"
                                }
                            succeededIds = batch.succeededIds.toSet()
                            circuitTripped = batch.circuitTripped
                            if (batch.failedIds.isNotEmpty()) {
                                failedIds = batch.failedIds.toSet()
                                partialFailureMessage =
                                    batch.errorMessage
                                        ?: "已移除 ${batch.succeededIds.size} 首，其余 ${batch.failedIds.size} 首因限流暂停"
                            }
                        } else if (remaining.isNotEmpty()) {
                            val batch =
                                core.netease.changePlaylistTracksBatch(
                                    id = p.id.long,
                                    tracks = remaining,
                                    add = false,
                                    session = session,
                                ) { done, total ->
                                    removeProgress = "$done / $total"
                                }
                            succeededIds = batch.succeededIds.toSet()
                            circuitTripped = batch.circuitTripped
                            if (batch.failedIds.isNotEmpty()) {
                                failedIds = batch.failedIds.toSet()
                                partialFailureMessage =
                                    batch.errorMessage
                                        ?: "已移除 ${batch.succeededIds.size} 首，其余 ${batch.failedIds.size} 首因限流暂停"
                            }
                        }
                        val after =
                            if (circuitTripped || runCatching { session.requireCurrent() }.isFailure) {
                                // Do not hammer NetEase with a verification fetch while circuit breaker is open.
                                before.filterNot { (it.song_id ?: it.id) in succeededIds }
                            } else {
                                core.netease.fetchPlaylistTracks(p.id.long, strict = true, session = session).also { verified ->
                                    val expectedRemoved = stagedIds - failedIds
                                    check(verified.none { (it.song_id ?: it.id) in expectedRemoved })
                                }
                            }
                        core.repository.cachePlaylist(a, p.copy(track_count = after.size), after)
                        after
                    }
                    if (account == a && selected == p) tracks = refreshed
                }
                if (deck === review) {
                    val remaining = review.tracks.drop(review.decisions.size)
                    val retryStaged =
                        if (failedIds.isEmpty()) emptyList()
                        else review.staged.filter { (it.song_id ?: it.id) in failedIds }
                    val nextTracks = retryStaged + remaining
                    val nextIds = nextTracks.mapTo(HashSet()) { it.identity() }
                    deckSourceTracks = deckSourceTracks.filter { it.identity() in nextIds }
                    deck = Deck(p, nextTracks)
                }
                notice(partialFailureMessage ?: "已处理本次清单")
            } finally {
                removing = false
                removeProgress = ""
            }
        }
    }

    fun canRemove(d: Deck) =
        uid?.toString() == account &&
            onlinePlaylists.any {
                it.id == d.playlist?.id && it.kind in setOf("self_created", "liked")
            }

    fun dailyRecommendations() = runTask {
        val a = account
        val songs = io {
            core.netease
                .fetchDailyRecommend()["data"]
                ?.jsonObject
                ?.get("dailySongs")
                ?.jsonArray
                .orEmpty()
                .map { core.netease.parseTrack(it.jsonObject) }
        }
        if (account == a && uid != null) daily = songs
    }

    fun jump(i: Int) = runTask {
        stopPreview()
        core.playback.jump(i)
    }

    fun mode(mode: PlayMode) {
        val s = state
        if (s.queue.isEmpty() || s.queue_index !in s.queue.indices) return
        val livePosSec = currentPositionSec()
        if (mode == PlayMode.shuffle && s.current_track != null)
            runTask {
                val others = s.queue.toMutableList().apply { removeAt(s.queue_index) }
                val shuffled = io {
                    val historyMap =
                        core.repository
                            .history(account)
                            .mapNotNull { h ->
                                (h.id ?: h.song_id)?.takeIf { it > 0L }?.let { it to h }
                            }
                            .toMap()
                    BalancedShuffleEngine.shuffle(others, historyMap)
                }
                editQueue(listOf(s.current_track) + shuffled, 0, livePosSec, mode)
            }
        else editQueue(s.queue, s.queue_index, livePosSec, mode)
    }

    private var previewPlayer: androidx.media3.exoplayer.ExoPlayer? = null
    private var previewJob: Job? = null

    fun stopPreview() {
        previewJob?.cancel()
        previewPlayer?.release()
        previewPlayer = null
        previewTrackKey = null
        isPreviewLoading = false
    }

    fun preview(t: Track) {
        val targetKey = t.identity()
        if (previewTrackKey == targetKey) {
            stopPreview()
            return
        }
        stopPreview()
        previewTrackKey = targetKey
        isPreviewLoading = true
        previewJob = runTask {
            try {
                val ctx = getApplication<Application>()
                val resolved = io { core.sources.resolveStream(t, quality) }
                if (state.is_playing || isBuffering) core.playback.pause()
                previewPlayer =
                    androidx.media3.exoplayer.ExoPlayer.Builder(ctx)
                        .setMediaSourceFactory(
                            androidx.media3.exoplayer.source.DefaultMediaSourceFactory(
                                core.createRoutingDataSourceFactory()
                            )
                        )
                        .build()
                        .apply {
                            setAudioAttributes(
                                AudioAttributes.Builder()
                                    .setUsage(C.USAGE_MEDIA)
                                    .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                                    .build(),
                                true,
                            )
                            setHandleAudioBecomingNoisy(true)
                            addListener(
                                object : androidx.media3.common.Player.Listener {
                                    override fun onPlayerError(
                                        error: androidx.media3.common.PlaybackException
                                    ) {
                                        notice(classifyPlaybackError(error, t).second)
                                        stopPreview()
                                    }

                                    override fun onPlaybackStateChanged(playbackState: Int) {
                                        if (playbackState == androidx.media3.common.Player.STATE_READY) {
                                            isPreviewLoading = false
                                        } else if (playbackState == androidx.media3.common.Player.STATE_ENDED) {
                                            stopPreview()
                                        }
                                    }
                                }
                            )
                            setMediaItem(androidx.media3.common.MediaItem.fromUri(resolved.uri))
                            prepare()
                            play()
                        }
                delay(30000)
                stopPreview()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                stopPreview()
                throw e
            }
        }
    }

    override fun onCleared() {
        cacheJob?.cancel()
        stopPreview()
        core.playback.onNotificationLikeToggle = null
        super.onCleared()
    }

    fun play(list: List<Track>, index: Int = 0, mode: PlayMode = PlayMode.order) = runTask {
        stopPreview()
        if (list.isEmpty()) return@runTask
        val pairedAccountId = lanState.pairing?.accountId
        val filtered = filterPlayableQueue(list, index, account, pairedAccountId)
        if (filtered.tracks.isEmpty()) {
            if (filtered.skippedLocalCount > 0) {
                notice(AudioErrorCategory.LAN_UNPAIRED.userMessage)
            } else {
                notice(AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE.userMessage)
            }
            return@runTask
        }
        if (filtered.skippedLocalCount > 0 && list.size > 1) {
            notice("已跳过 ${filtered.skippedLocalCount} 首未连接桌面的曲目")
        }
        playbackError = null
        core.playback.setQueue(account, filtered.tracks, mode, filtered.index)
    }

    fun playPreservingQueue(track: Track) = runTask {
        stopPreview()
        val pairedAccountId = lanState.pairing?.accountId
        if (!isTrackPlayable(track, account, pairedAccountId)) {
            notice(
                if (
                    track.effectiveProvider() == "local" ||
                        track.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID
                ) {
                    AudioErrorCategory.LAN_UNPAIRED.userMessage
                } else {
                    AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE.userMessage
                }
            )
            return@runTask
        }
        playbackError = null
        if (state.queue.size > 1) {
            core.playback.playInsertingNext(account, track, state.play_mode)
        } else {
            core.playback.setQueue(account, listOf(track), state.play_mode, 0)
        }
    }

    fun appendToQueue(t: Track) = runTask {
        val pairedAccountId = lanState.pairing?.accountId
        if (!isTrackPlayable(t, account, pairedAccountId)) {
            notice(
                if (requiresLanPairing(t)) AudioErrorCategory.LAN_UNPAIRED.userMessage
                else AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE.userMessage
            )
            return@runTask
        }
        core.playback.appendToQueue(account, t, state.play_mode)
        notice("已加入播放队列末尾")
    }

    fun clearQueue() = runTask {
        core.playback.clearQueue()
        notice("已清空播放队列")
    }

    fun markNotInterested(t: Track) = runTask {
        val a = account
        val key = t.identity()
        io {
            core.repository.recordFeedback(
                a,
                FeedbackItem(
                    action = "not_interested",
                    track_key = key,
                    song_id = t.song_id ?: t.id ?: 0L,
                    context = "discover",
                    client_event_id = UUID.randomUUID().toString(),
                ),
            )
        }
        notInterestedKeys = notInterestedKeys + key
        daily = daily.filterNot { it.identity() == key }
        notice("已减少类似推荐")
    }

    fun refreshCacheStats() = runTask {
        cacheSizeBytes = io { core.audioCache.cacheSizeBytes() }
    }

    fun cachePlaylistOffline(
        playlist: PlaylistSummary,
        songs: List<Track>,
        allowCellular: Boolean = false,
    ) {
        if (cacheJob?.isActive == true) {
            notice("正在离线缓存曲目，请稍候")
            return
        }
        cacheJob = runTask {
            val (ok, fail) =
                core.audioCache.cacheTracksOffline(
                    playlistId = playlist.id.content,
                    tracks = songs,
                    quality = quality,
                    resolver = core.sources,
                    upstreamFactory = core.createRoutingDataSourceFactory(),
                    allowCellular = allowCellular,
                    account = account,
                )
            refreshCacheStats()
            notice(
                if (fail == 0) "已离线缓存 $ok 首曲目"
                else "离线缓存完成：成功 $ok 首，失败 $fail 首"
            )
        }
    }

    fun cacheSingleTrackOffline(track: Track) {
        runTask {
            val (ok, _) =
                core.audioCache.cacheTracksOffline(
                    playlistId = null,
                    tracks = listOf(track),
                    quality = quality,
                    resolver = core.sources,
                    upstreamFactory = core.createRoutingDataSourceFactory(),
                    allowCellular = true,
                    account = account,
                )
            refreshCacheStats()
            notice(if (ok > 0) "已离线缓存：${track.title}" else "该曲目暂不支持离线缓存")
        }
    }

    fun clearAudioCache() = runTask {
        io { core.audioCache.clearAll() }
        refreshCacheStats()
        notice("已清空音频离线缓存")
    }

    fun saveExternalSource(config: ExternalSourceConfig, secret: String?) = runTask {
        io { core.sources.saveSource(config, secret) }
        externalSources = io { core.sources.listSources() }
        notice("已保存外部音源：${config.name}")
    }

    fun toggleExternalSource(config: ExternalSourceConfig, enabled: Boolean) = runTask {
        io { core.sources.saveSource(config.copy(enabled = enabled), null) }
        externalSources = io { core.sources.listSources() }
    }

    fun deleteExternalSource(id: String) = runTask {
        io { core.sources.deleteSource(id) }
        externalSources = io { core.sources.listSources() }
        notice("已删除外部音源")
    }

    fun testExternalSource(config: ExternalSourceConfig, secretOverride: String? = null) = runTask {
        val msg = io { core.sources.testSource(config, secretOverride) }
        notice(msg)
    }

    fun setEqEnabled(enabled: Boolean) = runTask {
        core.playback.setEqEnabled(enabled)
    }

    fun setEqBandDb(band: Int, gainDb: Float) = runTask {
        if (!core.playback.setEqBandDb(band, gainDb)) {
            notice("当前音频设备不支持均衡器")
        }
    }

    fun applyEqPreset(preset: String) = runTask {
        core.playback.applyEqPreset(preset)
    }

    fun transport(op: String) = runTask {
        stopPreview()
        with(core.playback) {
            when (op) {
                "toggle" -> {
                    val cur = state.value
                    if (cur.snapshot.is_playing || cur.isBuffering) pause() else play()
                }
                "next" -> next()
                "previous" -> previous()
            }
        }
    }

    fun seek(value: Float) = runTask { core.playback.seek((value * 1000).toLong()) }

    fun speed(value: Float) = runTask { core.playback.speed(value) }

    fun timer(n: Int) = runTask {
        core.playback.sleepTimer(n)
        sleep = n
    }

    fun gain(i: Int, v: Float) = runTask {
        if (core.playback.equalizer(i, v)) eq = eq.toMutableList().apply { set(i, v) }
        else notice("当前音频设备不支持均衡器")
    }

    fun editQueue(
        list: List<Track>,
        index: Int,
        position: Double = currentPositionSec(),
        mode: PlayMode = state.play_mode,
    ) = runTask {
        core.playback.replaceQueue(
            account,
            list,
            mode,
            index,
            (position * 1000).toLong(),
            state.is_playing,
        )
    }

    fun addNext(t: Track) = runTask {
        val pairedAccountId = lanState.pairing?.accountId
        if (!isTrackPlayable(t, account, pairedAccountId)) {
            notice(
                if (requiresLanPairing(t)) AudioErrorCategory.LAN_UNPAIRED.userMessage
                else AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE.userMessage
            )
            return@runTask
        }
        core.playback.addNext(account, t, state.play_mode)
        notice("已添加到下一首播放")
    }

    fun moveQueueItem(entryId: String, delta: Int) = runTask {
        core.playback.moveQueueItem(entryId, delta)
    }

    fun removeQueueItem(entryId: String) = runTask {
        core.playback.removeQueueItem(entryId)
    }

    fun jumpToQueueEntry(entryId: String) = runTask {
        stopPreview()
        core.playback.jumpToQueueEntry(entryId)
    }

    private fun loadLyrics(t: Track?) {
        lyricJob?.cancel()
        lyrics = ParsedLyrics(emptyList())
        offset = 0
        liked = t?.liked == true
        core.playback.notifyLikedState(liked)
        if (t == null) return
        val key = t.identity()
        lyricJob = runTask {
            val cachedLyrics = io { core.repository.cachedLyrics(key) }
            if (cachedLyrics != null && cachedLyrics.lines.isNotEmpty()) {
                lyrics = cachedLyrics
                return@runTask
            }
            when (t.effectiveProvider().lowercase()) {
                "netease" -> {
                    val id = t.song_id ?: t.id ?: return@runTask
                    val directParsed =
                        io {
                            runCatching {
                                val json = core.netease.fetchLyrics(id)
                                fun part(k: String) =
                                    (json[k] as? JsonObject)?.get("lyric")?.jsonPrimitive?.content.orEmpty()
                                val lrc = part("lrc")
                                val yrc = part("yrc")
                                val tlyric = part("tlyric")
                                val romalrc = part("romalrc")
                                if (lrc.isNotBlank() || yrc.isNotBlank()) {
                                    core.repository.saveLyrics(key, lrc, yrc, tlyric, romalrc, "netease")
                                }
                                LyricsParser.parse(lrc, yrc, tlyric, romalrc)
                            }.getOrNull()
                        }
                    if (directParsed != null && directParsed.lines.isNotEmpty()) {
                        if (state.current_track?.identity() == key) lyrics = directParsed
                    } else if (t.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID) {
                        val ext = io { core.sources.fetchExternalLyrics(t) }
                        if (ext != null && ext.isNotEmpty()) {
                            io {
                                core.repository.saveLyrics(
                                    trackKey = key,
                                    lrc = ext.lrc,
                                    yrc = ext.yrc,
                                    tlyric = ext.tlyric,
                                    romalrc = ext.romalrc,
                                    source = ext.source,
                                )
                            }
                            if (state.current_track?.identity() == key) {
                                lyrics = LyricsParser.parse(ext.lrc, ext.yrc, ext.tlyric, ext.romalrc)
                            }
                        }
                    }
                }
                "subsonic", "opensubsonic", "resolver", "resolver_v2", "local" -> {
                    val ext = io { core.sources.fetchExternalLyrics(t) }
                    if (ext != null && ext.isNotEmpty()) {
                        io {
                            core.repository.saveLyrics(
                                trackKey = key,
                                lrc = ext.lrc,
                                yrc = ext.yrc,
                                tlyric = ext.tlyric,
                                romalrc = ext.romalrc,
                                source = ext.source,
                            )
                        }
                        if (state.current_track?.identity() == key) {
                            lyrics = LyricsParser.parse(ext.lrc, ext.yrc, ext.tlyric, ext.romalrc)
                        }
                    }
                }
                "device" -> {
                    val stream = t.stream_url.orEmpty()
                    if (stream.startsWith("file://")) {
                        val parsed = io {
                            runCatching {
                                val audioFile = File(stream.toUri().path.orEmpty())
                                val sidecar = File(audioFile.parentFile, audioFile.nameWithoutExtension + ".lrc")
                                if (sidecar.isFile && sidecar.length() <= 512_000L) {
                                    val raw = sidecar.readText()
                                    core.repository.saveLyrics(key, raw, source = "local_lrc")
                                    LyricsParser.parse(raw)
                                } else null
                            }.getOrNull()
                        }
                        if (parsed != null && state.current_track?.identity() == key) lyrics = parsed
                    }
                }
                else -> {
                    val ext = io { core.sources.fetchExternalLyrics(t) }
                    if (ext != null && ext.isNotEmpty()) {
                        io {
                            core.repository.saveLyrics(
                                trackKey = key,
                                lrc = ext.lrc,
                                yrc = ext.yrc,
                                tlyric = ext.tlyric,
                                romalrc = ext.romalrc,
                                source = ext.source,
                            )
                        }
                        if (state.current_track?.identity() == key) {
                            lyrics = LyricsParser.parse(ext.lrc, ext.yrc, ext.tlyric, ext.romalrc)
                        }
                        return@runTask
                    }
                    val id = t.song_id ?: t.id
                    if (id != null && id > 0L) {
                        val parsed = io {
                            runCatching {
                                val json = core.netease.fetchLyrics(id)
                                fun part(k: String) =
                                    (json[k] as? JsonObject)?.get("lyric")?.jsonPrimitive?.content.orEmpty()
                                val lrc = part("lrc")
                                val yrc = part("yrc")
                                val tlyric = part("tlyric")
                                val romalrc = part("romalrc")
                                if (lrc.isNotBlank() || yrc.isNotBlank()) {
                                    core.repository.saveLyrics(key, lrc, yrc, tlyric, romalrc, "netease")
                                }
                                LyricsParser.parse(lrc, yrc, tlyric, romalrc)
                            }.getOrNull()
                        }
                        if (parsed != null && state.current_track?.identity() == key) lyrics = parsed
                    }
                }
            }
        }
    }

    fun like(target: Track? = state.current_track) = runTask {
        val t = target ?: return@runTask
        val id = t.song_id ?: t.id ?: return@runTask
        if (uid == null || t.effectiveProvider() != "netease") {
            notice("请先登录网易云音乐")
            return@runTask
        }
        val isCurrent = state.current_track?.identity() == t.identity()
        val value = if (isCurrent) !liked else !t.liked
        io { core.netease.like(id, value) }
        if (isCurrent) {
            liked = value
            core.playback.notifyLikedState(value)
        }
        notice(if (value) "已添加到我喜欢的音乐" else "已取消喜欢")
    }

    fun parsePair(link: String) {
        cancelClaim()
        pairError = ""
        pair =
            try {
                LanPairingUriParser.parse(link.trim())
            } catch (e: Exception) {
                pairError = "配对链接无效，请检查地址、端口、六位配对码和指纹"
                null
            }
    }

    fun discover() = runTask {
        hosts = io { LanDiscoveryClient().discover() }
        if (hosts.isEmpty()) notice("尚未发现桌面")
    }

    fun cancelClaim() {
        claimJob?.cancel()
        claimJob = null
        pair = null
    }

    fun claim(p: PairingUri) {
        if (p !== pair || !pairingReady || pairingClaimInFlight) return
        val expected = lanState.pairing
        claimJob = runTask {
            if (core.lanSession.claim(p, expected) && pair === p) pair = null
        }
    }

    fun unpair() = runTask {
        core.lanSession.unpair()
        hosts = emptyList()
    }
    fun sync() = core.lanSession.sync()
    fun pull() = core.lanSession.pull()

    fun accept() = runTask {
        stopPreview()
        val s = incoming ?: return@runTask
        val pairedAccountId = lanState.pairing?.accountId ?: account
        val filtered = prepareHandoffQueue(s, pairedAccountId)
        if (filtered.tracks.isEmpty()) {
            notice("无法接续该队列")
            return@runTask
        }
        core.lanSession.accept(s) { pairedAccount ->
            applyHandoffPlayback(s, filtered) { index, positionMs, playing ->
                core.playback.replaceQueue(pairedAccount, filtered.tracks, s.play_mode, index, positionMs, playing)
            }
            core.playback.speed(s.playback_rate.toFloat())
        }
    }

    fun push() = runTask {
        val s = state
        val track = s.current_track ?: return@runTask
        if (track.provider == "device") {
            notice("本机音频无法推送到桌面")
            return@runTask
        }
        val queue = s.queue.filter { it.effectiveProvider() != "device" }
        val index =
            s.queue.take(s.queue_index.coerceAtLeast(0)).count {
                it.effectiveProvider() != "device"
            }
        core.lanSession.push(
            HandoffSnapshot(
                track,
                queue,
                index,
                s.play_mode,
                currentPositionSec(),
                s.duration_sec,
                s.playback_rate,
                s.is_playing,
                explicit_handoff = true,
            )
        ) { notice("已推送到桌面") }
    }
}
