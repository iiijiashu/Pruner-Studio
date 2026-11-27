package com.prunerstudio.mobile.core.player

import android.content.*
import android.os.Bundle
import androidx.core.net.toUri
import androidx.media3.common.*
import androidx.media3.datasource.HttpDataSource
import androidx.media3.session.*
import com.prunerstudio.mobile.core.engine.BalancedShuffleEngine
import com.prunerstudio.mobile.core.lan.LanSyncClient
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.sources.CompositeAudioSourceResolver
import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.util.UUID
import javax.net.ssl.SSLException
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import kotlinx.serialization.encodeToString

data class PlaybackUiState(
    val snapshot: PlaybackSessionSnapshot = PlaybackSessionSnapshot(),
    val error: String? = null,
    val errorMessage: String? = null,
    val errorToken: Long = 0L,
    val isBuffering: Boolean = false,
    val queueEntryIds: List<String> = emptyList(),
)

data class PlaybackProgress(
    val positionMs: Long = 0L,
    val durationMs: Long = 0L,
    val bufferedPositionMs: Long = 0L,
    val isPlaying: Boolean = false,
    val playbackRate: Float = 1f,
    val updatedAtElapsedMs: Long = 0L,
)

internal enum class AutoRetryAction {
    NONE,
    RETRY_SAME_POSITION,
    RETRY_FROM_START,
}

internal data class FilteredQueue(
    val tracks: List<Track>,
    val index: Int,
    val skippedLocalCount: Int,
    val skippedInvalidCount: Int,
    val targetDropped: Boolean,
)

internal data class RestorableQueue(
    val queue: List<Track>,
    val index: Int,
    val positionMs: Long,
    val mode: PlayMode,
    val volume: Float,
    val muted: Boolean,
    val speed: Float,
)

internal fun sanitizeResumePositionMs(positionSec: Double, durationSec: Double): Long {
    if (!positionSec.isFinite() || positionSec <= 0.0) return 0L
    val posMs = (positionSec * 1000.0).toLong().coerceAtLeast(0L)
    val durMs = if (durationSec.isFinite() && durationSec > 0.0) (durationSec * 1000.0).toLong() else 0L
    if (durMs > 0L && posMs >= (durMs - 1500L).coerceAtLeast(0L)) return 0L
    return posMs
}

internal fun slicePlayableWindow(
    tracks: List<Track>,
    index: Int,
    maxSize: Int = 2000,
): Pair<List<Track>, Int> {
    if (tracks.isEmpty()) return emptyList<Track>() to 0
    val safeIndex = index.coerceIn(tracks.indices)
    if (tracks.size <= maxSize) return tracks to safeIndex
    val start = (safeIndex - maxSize / 2).coerceIn(0, tracks.size - maxSize)
    val sliced = tracks.subList(start, start + maxSize).toList()
    return sliced to (safeIndex - start)
}

internal fun isTrackPlayable(track: Track, account: String, pairedAccountId: String?): Boolean {
    val provider = track.providerFamily()
    if (
        (track.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID || track.desktop_scope != null) &&
            (pairedAccountId == null || pairedAccountId != account)
    ) {
        return false
    }
    return when (provider) {
        "device" -> {
            val url = track.stream_url
            !url.isNullOrBlank() &&
                (url.startsWith("file:", ignoreCase = true) ||
                    url.startsWith("content:", ignoreCase = true))
        }
        "local" -> {
            val id = track.file_id ?: track.external_id
            pairedAccountId != null &&
                pairedAccountId == account &&
                id != null &&
                id.matches(Regex("[a-f0-9]{24}"))
        }
        "netease" -> (track.song_id ?: track.id) != null
        "subsonic", "opensubsonic" ->
            !track.external_id.isNullOrBlank() || !track.track_key.isNullOrBlank()
        "webdav" -> !track.stream_url.isNullOrBlank() || !track.external_id.isNullOrBlank()
        "resolver", "resolver_v2" ->
            !track.external_id.isNullOrBlank() ||
                !track.stream_url.isNullOrBlank() ||
                !track.track_key.isNullOrBlank()
        else -> {
            val url = track.stream_url.orEmpty()
            (pairedAccountId != null && pairedAccountId == account) &&
                (CompositeAudioSourceResolver.isLanProxyStreamPath(url) ||
                    !track.external_id.isNullOrBlank() ||
                    !track.track_key.isNullOrBlank())
        }
    }
}

internal fun requiresLanPairing(track: Track): Boolean {
    val provider = track.providerFamily()
    return provider == "local" ||
        track.desktop_scope != null ||
        track.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID ||
        track.stream_url?.startsWith("/api/lan/") == true ||
        provider !in setOf(
            "device",
            "netease",
            "subsonic",
            "opensubsonic",
            "webdav",
            "resolver",
            "resolver_v2",
        )
}

internal fun filterPlayableQueue(
    tracks: List<Track>,
    index: Int,
    account: String,
    pairedAccountId: String?,
): FilteredQueue {
    if (tracks.isEmpty()) return FilteredQueue(emptyList(), 0, 0, 0, targetDropped = false)
    val safeIndex = index.coerceIn(tracks.indices)
    val kept = ArrayList<Track>(tracks.size)
    var newIndex = -1
    var skippedLocal = 0
    var skippedInvalid = 0
    tracks.forEachIndexed { i, track ->
        if (isTrackPlayable(track, account, pairedAccountId)) {
            if (i == safeIndex) newIndex = kept.size
            kept.add(track)
        } else if (requiresLanPairing(track)) {
            skippedLocal++
        } else {
            skippedInvalid++
        }
    }
    val targetDropped = newIndex < 0
    val resolvedIndex = if (kept.isEmpty()) 0 else if (targetDropped) 0 else newIndex
    val (windowed, windowedIndex) = slicePlayableWindow(kept, resolvedIndex)
    return FilteredQueue(windowed, windowedIndex, skippedLocal, skippedInvalid, targetDropped)
}

internal fun prepareRestorableQueue(
    account: String,
    saved: PlaybackSessionSnapshot,
    pairedAccountId: String?,
): RestorableQueue? {
    val raw = saved.queue.ifEmpty { listOfNotNull(saved.current_track) }
    if (raw.isEmpty()) return null
    val rawIdx =
        saved.queue_index.takeIf { it in raw.indices }
            ?: saved.current_track?.let { cur ->
                raw.indexOfFirst { it.identity() == cur.identity() }
            }?.takeIf { it >= 0 }
            ?: 0
    val filtered = filterPlayableQueue(raw, rawIdx, account, pairedAccountId)
    if (filtered.tracks.isEmpty()) return null
    val target = filtered.tracks[filtered.index]
    val durationSec =
        saved.duration_sec.takeIf { it.isFinite() && it > 0.0 } ?: target.durationSeconds()
    val positionMs =
        if (filtered.targetDropped) 0L else sanitizeResumePositionMs(saved.position_sec, durationSec)
    val volume = saved.volume.takeIf { it.isFinite() }?.toFloat()?.coerceIn(0f, 1f) ?: 1f
    val speed = saved.playback_rate.takeIf { it.isFinite() }?.toFloat()?.coerceIn(0.5f, 2f) ?: 1f
    return RestorableQueue(
        queue = filtered.tracks,
        index = filtered.index,
        positionMs = positionMs,
        mode = saved.play_mode,
        volume = volume,
        muted = saved.muted,
        speed = speed,
    )
}

private fun causeChain(root: Throwable?): Sequence<Throwable> = sequence {
    var cur = root
    val seen = mutableSetOf<Throwable>()
    while (cur != null && seen.add(cur)) {
        yield(cur)
        cur = cur.cause
    }
}

@androidx.media3.common.util.UnstableApi
internal fun classifyPlaybackError(
    error: PlaybackException?,
    currentTrack: Track? = null,
): Pair<AudioErrorCategory, String> {
    if (error == null) return AudioErrorCategory.UNKNOWN to AudioErrorCategory.UNKNOWN.userMessage
    val causes = causeChain(error).toList()
    causes.firstOrNull { it is AudioResolutionException }?.let { resolved ->
        val ex = resolved as AudioResolutionException
        return ex.category to (ex.message?.takeIf(String::isNotBlank) ?: ex.category.userMessage)
    }
    causes.firstOrNull { it is HttpDataSource.InvalidResponseCodeException }?.let { http ->
        val code = (http as HttpDataSource.InvalidResponseCodeException).responseCode
        val provider = currentTrack?.effectiveProvider()
        val isLanBacked =
            provider == "local" ||
                currentTrack?.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID ||
                currentTrack?.stream_url?.startsWith("/api/lan/") == true
        return when {
            code == 401 && isLanBacked ->
                AudioErrorCategory.LAN_UNPAIRED to AudioErrorCategory.LAN_UNPAIRED.userMessage
            code in setOf(401, 403, 410) && provider == "local" ->
                AudioErrorCategory.LAN_UNPAIRED to AudioErrorCategory.LAN_UNPAIRED.userMessage
            code in setOf(401, 403, 410) ->
                AudioErrorCategory.STREAM_EXPIRED to AudioErrorCategory.STREAM_EXPIRED.userMessage
            code == 404 ->
                AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE to
                    AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE.userMessage
            else ->
                AudioErrorCategory.NETWORK_ERROR to "音频服务响应异常 (HTTP $code)，请稍后重试"
        }
    }
    if (
        causes.any { c ->
            val msg = c.message.orEmpty()
            "Desktop pairing required" in msg ||
                "Desktop pairing revoked" in msg ||
                "Pairing has changed" in msg
        }
    ) {
        return AudioErrorCategory.LAN_UNPAIRED to AudioErrorCategory.LAN_UNPAIRED.userMessage
    }
    if (
        causes.any {
            it is ConnectException ||
                it is SocketTimeoutException ||
                it is UnknownHostException ||
                it is SSLException
        }
    ) {
        val isLanBacked =
            currentTrack?.effectiveProvider() == "local" ||
                currentTrack?.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID ||
                currentTrack?.stream_url?.startsWith("/api/lan/") == true
        return if (isLanBacked) {
            AudioErrorCategory.LAN_UNREACHABLE to AudioErrorCategory.LAN_UNREACHABLE.userMessage
        } else {
            AudioErrorCategory.NETWORK_ERROR to AudioErrorCategory.NETWORK_ERROR.userMessage
        }
    }
    return when {
        error.errorCode == PlaybackException.ERROR_CODE_IO_FILE_NOT_FOUND ||
            error.errorCode == PlaybackException.ERROR_CODE_IO_NO_PERMISSION ||
            (currentTrack?.effectiveProvider() == "device" && error.errorCode in 2000..2999) ->
            AudioErrorCategory.DEVICE_FILE_ERROR to AudioErrorCategory.DEVICE_FILE_ERROR.userMessage
        error.errorCode in 3000..4999 ->
            AudioErrorCategory.CODEC_ERROR to AudioErrorCategory.CODEC_ERROR.userMessage
        error.errorCode in 2000..2999 ->
            if (
                currentTrack?.effectiveProvider() == "local" ||
                    currentTrack?.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID ||
                    currentTrack?.stream_url?.startsWith("/api/lan/") == true
            )
                AudioErrorCategory.LAN_UNREACHABLE to AudioErrorCategory.LAN_UNREACHABLE.userMessage
            else AudioErrorCategory.NETWORK_ERROR to AudioErrorCategory.NETWORK_ERROR.userMessage
        else -> AudioErrorCategory.UNKNOWN to AudioErrorCategory.UNKNOWN.userMessage
    }
}

@androidx.media3.common.util.UnstableApi
internal fun shouldAutoRetryOnce(
    error: PlaybackException,
    currentTrack: Track?,
    positionMs: Long,
    alreadyRetried: Boolean,
): AutoRetryAction {
    if (alreadyRetried) return AutoRetryAction.NONE
    if (error.errorCode == PlaybackException.ERROR_CODE_BEHIND_LIVE_WINDOW) {
        return AutoRetryAction.RETRY_FROM_START
    }
    val http =
        causeChain(error).firstOrNull { it is HttpDataSource.InvalidResponseCodeException }
            as? HttpDataSource.InvalidResponseCodeException ?: return AutoRetryAction.NONE
    return when {
        http.responseCode == 416 && positionMs > 0L -> AutoRetryAction.RETRY_FROM_START
        http.responseCode in setOf(403, 410) && currentTrack?.effectiveProvider() == "netease" ->
            AutoRetryAction.RETRY_SAME_POSITION
        http.responseCode == 404 &&
            currentTrack?.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID &&
            currentTrack.effectiveProvider() != "local" ->
            AutoRetryAction.RETRY_SAME_POSITION
        else -> AutoRetryAction.NONE
    }
}

@androidx.media3.common.util.UnstableApi
class PlaybackController(private val context: Context, private val app: PrunerAppContainer) {
    private val mutableState = MutableStateFlow(PlaybackUiState())
    val state = mutableState.asStateFlow()
    private val mutableProgress = MutableStateFlow(PlaybackProgress())
    val progress = mutableProgress.asStateFlow()
    val eqState: StateFlow<EqState>
        get() = app.equalizer.state

    @Volatile private var lastAccount: String? = null
    @Volatile internal var onNotificationLikeToggle: (() -> Unit)? = null
        set(value) {
            field = value
            effects?.refreshLikeButton()
        }

    internal fun update(value: PlaybackUiState) {
        mutableState.value = value
    }

    internal fun updateProgress(value: PlaybackProgress) {
        mutableProgress.value = value
    }

    fun notifyLikedState(liked: Boolean) {
        effects?.updateLikeButton(liked)
    }

    private val future by lazy {
        MediaController.Builder(
                context,
                SessionToken(context, ComponentName(context, PrunerPlaybackService::class.java)),
            )
            .buildAsync()
    }

    private suspend fun controller(): MediaController {
        val pending = withContext(Dispatchers.Main.immediate) { future }
        return withContext(Dispatchers.IO) { pending.get() }
    }

    suspend fun setQueue(
        account: String,
        tracks: List<Track>,
        mode: PlayMode = PlayMode.order,
        index: Int = 0,
        positionMs: Long = 0,
        play: Boolean = true,
    ) {
        installQueue(
            account,
            tracks,
            mode,
            index,
            positionMs,
            play,
            shuffle = mode == PlayMode.shuffle,
            prepare = play,
        )
    }

    suspend fun replaceQueue(
        account: String,
        tracks: List<Track>,
        mode: PlayMode,
        index: Int,
        positionMs: Long,
        play: Boolean,
    ) =
        installQueue(
            account,
            tracks,
            mode,
            index,
            positionMs,
            play,
            shuffle = false,
            prepare = play,
        )

    private suspend fun installQueue(
        account: String,
        tracks: List<Track>,
        mode: PlayMode,
        index: Int,
        positionMs: Long,
        play: Boolean,
        shuffle: Boolean,
        prepare: Boolean,
        volume: Float? = null,
        speed: Float? = null,
        unmutedVolume: Float? = null,
        muted: Boolean? = null,
    ) {
        require(tracks.size <= 2000 && (tracks.isEmpty() || index in tracks.indices))
        val queue =
            if (shuffle)
                withContext(Dispatchers.IO) {
                    val historyMap =
                        app.repository
                            .history(account)
                            .mapNotNull { h -> (h.id ?: h.song_id)?.takeIf { it > 0L }?.let { it to h } }
                            .toMap()
                    BalancedShuffleEngine.shuffle(tracks, historyMap)
                }
            else tracks
        val items = buildQueueItems(account, queue, mode, app.preferredQuality(), app::lan)
        val player = controller()
        lastAccount = account
        withContext(Dispatchers.Main.immediate) {
            if (items.isEmpty()) {
                player.stop()
                player.clearMediaItems()
                return@withContext
            }
            if (unmutedVolume != null && muted != null) {
                effects?.setRestoredVolume(unmutedVolume, muted)
            }
            if (volume != null) player.volume = volume.coerceIn(0f, 1f)
            if (speed != null) player.setPlaybackSpeed(speed.coerceIn(0.5f, 2f))
            player.repeatMode =
                when (mode) {
                    PlayMode.repeat_one -> Player.REPEAT_MODE_ONE
                    PlayMode.loop -> Player.REPEAT_MODE_ALL
                    else -> Player.REPEAT_MODE_OFF
                }
            val safeIndex = index.coerceIn(items.indices)
            val safePos = positionMs.coerceAtLeast(0L)
            val shouldPrepare = prepare || player.playbackState != Player.STATE_IDLE
            player.setMediaItems(items, safeIndex, safePos)
            if (shouldPrepare) {
                player.prepare()
            }
            player.playWhenReady = play
        }
    }

    suspend fun restore(account: String) {
        val saved = withContext(Dispatchers.IO) { app.repository.session(account) } ?: return
        val needsLan =
            saved.queue.any(::requiresLanPairing) ||
                saved.current_track?.let(::requiresLanPairing) == true
        val pairedAccountId =
            if (needsLan) withContext(Dispatchers.IO) { app.lan()?.config?.accountId } else null
        val plan = prepareRestorableQueue(account, saved, pairedAccountId) ?: return
        installQueue(
            account = account,
            tracks = plan.queue,
            mode = plan.mode,
            index = plan.index,
            positionMs = plan.positionMs,
            play = false,
            shuffle = false,
            prepare = false,
            volume = if (plan.muted) 0f else plan.volume,
            speed = plan.speed,
            unmutedVolume = plan.volume,
            muted = plan.muted,
        )
    }

    private suspend fun control(action: MediaController.() -> Unit) {
        val player = controller()
        withContext(Dispatchers.Main.immediate) { player.action() }
    }

    suspend fun play() {
        val player = controller()
        val needsHydrate =
            withContext(Dispatchers.Main.immediate) {
                if (player.mediaItemCount > 0) {
                    if (player.playbackState == Player.STATE_IDLE) {
                        player.prepare()
                    } else if (player.playbackState == Player.STATE_ENDED) {
                        player.seekToDefaultPosition()
                    }
                    player.play()
                    false
                } else {
                    true
                }
            }
        if (needsHydrate) {
            val snap = state.value.snapshot
            val acct = lastAccount ?: return
            val fallbackQueue = snap.queue.ifEmpty { listOfNotNull(snap.current_track) }
            if (fallbackQueue.isEmpty()) return
            val idx = snap.queue_index.coerceIn(fallbackQueue.indices)
            val cur = fallbackQueue[idx]
            val dur =
                snap.duration_sec.takeIf { it.isFinite() && it > 0.0 } ?: cur.durationSeconds()
            val livePosSec =
                progress.value.positionMs.takeIf { it > 0L }?.div(1000.0) ?: snap.position_sec
            val posMs = sanitizeResumePositionMs(livePosSec, dur)
            installQueue(
                account = acct,
                tracks = fallbackQueue,
                mode = snap.play_mode,
                index = idx,
                positionMs = posMs,
                play = true,
                shuffle = false,
                prepare = true,
            )
        }
    }

    suspend fun pause() = control { pause() }

    suspend fun next() = control {
        if (mediaItemCount == 0) return@control
        if (hasNextMediaItem()) seekToNextMediaItem()
        if (playbackState == Player.STATE_IDLE) {
            prepare()
            play()
        }
    }

    suspend fun jump(index: Int) = control {
        require(index in 0 until mediaItemCount)
        if (index != currentMediaItemIndex || playbackState == Player.STATE_ENDED) {
            seekToDefaultPosition(index)
        }
        if (playbackState == Player.STATE_IDLE) prepare()
        play()
    }

    suspend fun previous() = control {
        if (mediaItemCount == 0) return@control
        if (hasPreviousMediaItem()) seekToPreviousMediaItem() else seekToDefaultPosition()
        if (playbackState == Player.STATE_IDLE) {
            prepare()
            play()
        }
    }

    suspend fun seek(ms: Long) = control { seekTo(ms.coerceAtLeast(0)) }

    suspend fun speed(value: Float) = control { setPlaybackSpeed(value.coerceIn(.5f, 2f)) }

    suspend fun addNext(account: String, track: Track, mode: PlayMode) {
        val items = buildQueueItems(account, listOf(track), mode, app.preferredQuality(), app::lan)
        val player = controller()
        lastAccount = account
        withContext(Dispatchers.Main.immediate) {
            if (player.mediaItemCount == 0) {
                player.setMediaItems(items, 0, 0L)
                player.playWhenReady = false
            } else {
                val insertAt = (player.currentMediaItemIndex + 1).coerceIn(0, player.mediaItemCount)
                player.addMediaItem(insertAt, items.single())
            }
        }
    }

    suspend fun appendToQueue(account: String, track: Track, mode: PlayMode) {
        val items = buildQueueItems(account, listOf(track), mode, app.preferredQuality(), app::lan)
        val player = controller()
        lastAccount = account
        withContext(Dispatchers.Main.immediate) {
            if (player.mediaItemCount == 0) {
                player.setMediaItems(items, 0, 0L)
                player.playWhenReady = false
            } else {
                player.addMediaItem(items.single())
            }
        }
    }

    suspend fun playInsertingNext(account: String, track: Track, mode: PlayMode) {
        val items = buildQueueItems(account, listOf(track), mode, app.preferredQuality(), app::lan)
        val player = controller()
        lastAccount = account
        withContext(Dispatchers.Main.immediate) {
            if (player.mediaItemCount == 0) {
                player.setMediaItems(items, 0, 0L)
                player.prepare()
                player.play()
            } else {
                val insertAt = (player.currentMediaItemIndex + 1).coerceIn(0, player.mediaItemCount)
                player.addMediaItem(insertAt, items.single())
                player.seekToDefaultPosition(insertAt)
                if (player.playbackState == Player.STATE_IDLE) player.prepare()
                player.play()
            }
        }
    }

    suspend fun clearQueue() = control {
        stop()
        clearMediaItems()
    }

    suspend fun moveQueueItem(entryId: String, delta: Int) = control { moveQueueEntry(entryId, delta) }

    suspend fun removeQueueItem(entryId: String) = control { removeQueueEntry(entryId) }

    suspend fun jumpToQueueEntry(entryId: String) = control {
        queueIndex(entryId)?.let { index ->
                seekToDefaultPosition(index)
                if (playbackState == Player.STATE_IDLE) prepare()
                play()
            }
    }

    suspend fun refreshQuality(quality: AudioQualityLevel) =
        withContext(Dispatchers.Main.immediate) { effects?.refreshQuality(quality) }

    internal var effects: PrunerPlaybackService? = null

    suspend fun sleepTimer(minutes: Int) {
        if (effects == null) controller()
        withContext(Dispatchers.Main.immediate) { requireNotNull(effects).sleep(minutes) }
    }

    suspend fun equalizer(band: Int, level: Float): Boolean {
        return withContext(Dispatchers.Main.immediate) { app.equalizer.setNormalizedGain(band, level) }
    }

    suspend fun setEqBandDb(band: Int, gainDb: Float): Boolean {
        return withContext(Dispatchers.Main.immediate) { app.equalizer.setBandDb(band, gainDb) }
    }

    suspend fun setEqEnabled(enabled: Boolean): Boolean {
        return withContext(Dispatchers.Main.immediate) { app.equalizer.setEnabled(enabled) }
    }

    suspend fun applyEqPreset(name: String): Boolean {
        return withContext(Dispatchers.Main.immediate) { app.equalizer.applyPreset(name) }
    }
}

internal const val QUEUE_ENTRY_ID = "pruner_queue_entry"
internal const val DESKTOP_SCOPE = "pruner_desktop_scope"
internal fun MediaItem.queueEntryId(): String? = mediaMetadata.extras?.getString(QUEUE_ENTRY_ID)

internal fun Player.queueIndex(entryId: String): Int? =
    (0 until mediaItemCount).firstOrNull { getMediaItemAt(it).queueEntryId() == entryId }

internal fun Player.moveQueueEntry(entryId: String, delta: Int) {
    val from = queueIndex(entryId) ?: return
    val to = (from.toLong() + delta).coerceIn(0, (mediaItemCount - 1).toLong()).toInt()
    if (from != to) moveMediaItem(from, to)
}

internal fun Player.removeQueueEntry(entryId: String) {
    queueIndex(entryId)?.let { removeMediaItem(it) }
}

internal fun Player.removeStaleDesktopQueueEntries(activeScope: String?) {
    val stale = (0 until mediaItemCount).filter { index ->
        val itemScope = getMediaItemAt(index).mediaMetadata.extras?.getString(DESKTOP_SCOPE)
        itemScope != null && itemScope != activeScope
    }
    if (currentMediaItemIndex in stale) pause()
    stale.asReversed().forEach(::removeMediaItem)
}

internal fun prepareHandoffQueue(snapshot: HandoffSnapshot, account: String): FilteredQueue {
    val queue = snapshot.queue.ifEmpty { listOfNotNull(snapshot.track) }.map { track ->
        if (track.providerFamily() == "device") track
        else track.copy(source_id = CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID)
    }
    val index = snapshot.queue_index.takeIf { it in queue.indices }
        ?: queue.indexOfFirst { it.identity() == snapshot.track?.identity() }.coerceAtLeast(0)
    return filterPlayableQueue(queue, index, account, account)
}

internal suspend fun applyHandoffPlayback(
    snapshot: HandoffSnapshot,
    queue: FilteredQueue,
    install: suspend (index: Int, positionMs: Long, play: Boolean) -> Unit,
) {
    val positionMs = if (queue.targetDropped) 0L else (snapshot.position_sec * 1000).toLong()
    install(queue.index, positionMs, snapshot.is_playing)
}

@androidx.media3.common.util.UnstableApi
internal suspend fun buildQueueItems(
    account: String,
    queue: List<Track>,
    mode: PlayMode,
    quality: AudioQualityLevel = AudioQualityLevel.jymaster,
    pairing: () -> LanSyncClient?,
): List<MediaItem> {
    val lan = queueLanClient(account, queue, pairing)
    val lanBase = lan?.base
    return queue.map { original ->
        val desktopScope = if (requiresLanPairing(original)) {
            AudioCacheKey.desktopNamespace(requireNotNull(lan).config).also {
                if (original.desktop_scope != null && original.desktop_scope != it) {
                    throw AudioResolutionException(AudioErrorCategory.LAN_UNPAIRED)
                }
            }
        } else null
        val t = original.copy(desktop_scope = desktopScope)
        val entryId = UUID.randomUUID().toString()
        val provider = t.providerFamily()
        val uri =
            when (provider) {
                "device" ->
                    requireNotNull(t.stream_url).also {
                        require(it.toUri().scheme in setOf("file", "content"))
                    }
                "local" -> {
                    val id =
                        requireNotNull(t.file_id ?: t.external_id).also {
                            require(it.matches(Regex("[a-f0-9]{24}")))
                        }
                    requireNotNull(lanBase).resolve("local-library/stream/$id").toString()
                }
                "netease" ->
                    if (t.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID) {
                        "pruner-track://netease/${android.net.Uri.encode(t.identity())}"
                    } else {
                        "pruner-song://track/${requireNotNull(t.song_id ?: t.id)}"
                    }
                "subsonic", "opensubsonic", "webdav", "resolver", "resolver_v2" ->
                    "pruner-track://${android.net.Uri.encode(t.effectiveProvider().lowercase())}/${android.net.Uri.encode(t.identity())}"
                else -> {
                    val direct = t.stream_url.orEmpty()
                    if (CompositeAudioSourceResolver.isLanProxyStreamPath(direct) && lanBase != null) {
                        lanBase.newBuilder().encodedPath(direct).build().toString()
                    } else if (lanBase != null || !t.external_id.isNullOrBlank() || !t.track_key.isNullOrBlank()) {
                        "pruner-track://${android.net.Uri.encode(t.effectiveProvider().lowercase())}/${android.net.Uri.encode(t.identity())}"
                    } else {
                        throw IOException("Unsupported track provider: $provider")
                    }
                }
            }
        val extras =
            Bundle().apply {
                putString(QUEUE_ENTRY_ID, entryId)
                desktopScope?.let { putString(DESKTOP_SCOPE, it) }
                putString("account", account)
                putString("track", wire.encodeToString(t))
                putString("mode", mode.name)
            }
        val artworkUri =
            t.cover_url
                .trim()
                .replaceFirst(Regex("^http://"), "https://")
                .takeIf {
                    it.startsWith("https://", ignoreCase = true) ||
                        it.startsWith("content://", ignoreCase = true)
                }
                ?.let { runCatching { it.toUri() }.getOrNull() }
        val durationMs =
            if (t.duration_ms > 0L) t.duration_ms
            else (t.durationSeconds() * 1000.0).toLong().takeIf { it > 0L }
        val metaBuilder =
            MediaMetadata.Builder()
                .setTitle(t.title)
                .setArtist(t.artists.joinToString(" / ").ifEmpty { t.artist })
                .setExtras(extras)
        if (t.album.isNotBlank()) metaBuilder.setAlbumTitle(t.album)
        if (artworkUri != null) metaBuilder.setArtworkUri(artworkUri)
        if (durationMs != null) metaBuilder.setDurationMs(durationMs)
        MediaItem.Builder()
            .setMediaId(t.identity())
            .setCustomCacheKey(AudioCacheKey.logical(account, t, quality, desktopScope))
            .setUri(uri)
            .setMediaMetadata(metaBuilder.build())
            .build()
    }
}
