package com.prunerstudio.mobile.core.player

import android.app.PendingIntent
import android.content.Intent
import android.net.Uri
import android.os.*
import androidx.core.net.toUri
import androidx.media3.common.*
import androidx.media3.datasource.ResolvingDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.exoplayer.source.MediaSource
import androidx.media3.session.*
import com.google.common.collect.ImmutableList
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.SettableFuture
import com.prunerstudio.mobile.MainActivity
import com.prunerstudio.mobile.R
import com.prunerstudio.mobile.core.model.*
import java.io.IOException
import java.time.Instant
import java.util.UUID
import java.util.concurrent.Executors
import kotlinx.coroutines.*

@androidx.media3.common.util.UnstableApi
class PrunerPlaybackService : MediaLibraryService(), Player.Listener {
    private lateinit var app: PrunerAppContainer
    private lateinit var player: ExoPlayer
    private var mediaSession: MediaLibrarySession? = null
    private val handler = Handler(Looper.getMainLooper())
    private val writer = Executors.newSingleThreadExecutor()
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val timer = Runnable { player.pause() }
    private var current: Track? = null
    @Volatile private var account: String? = null
    private var eventId = ""
    private var listened = 0L
    private var playingSince: Long? = null
    private var lastSaved = 0L
    private var lastStored: PlaybackSessionSnapshot? = null
    private var lastAccount: String? = null
    private var cachedQueue = emptyList<Track>()
    private var queueEntryIds = emptyList<String>()
    private var retriedMediaKey: String? = null
    private var errorSequence = 0L
    private var lastErrorRef: PlaybackException? = null
    private var savedUnmutedVolume = 1f
    private var tickRunning = false
    private var currentLiked = false

    /**
     * Runs ONLY while actively playing. Updates the decoupled `PlaybackProgress` StateFlow
     * without touching structural `PlaybackUiState`, eliminating whole-screen recompositions.
     */
    private val positionTick =
        object : Runnable {
            override fun run() {
                if (!player.isPlaying) {
                    tickRunning = false
                    return
                }
                publishProgress()
                if (SystemClock.elapsedRealtime() - lastSaved >= 5000L) {
                    persistSnapshot(buildCurrentSnapshot())
                }
                handler.postDelayed(this, POSITION_TICK_MS)
            }
        }

    private fun startPositionTick() {
        if (!tickRunning && player.isPlaying) {
            tickRunning = true
            handler.removeCallbacks(positionTick)
            handler.postDelayed(positionTick, POSITION_TICK_MS)
        }
    }

    private fun stopPositionTick() {
        tickRunning = false
        handler.removeCallbacks(positionTick)
    }

    override fun onCreate() {
        super.onCreate()
        app = PrunerAppContainer.get(this)
        val routing = app.createRoutingDataSourceFactory()
        val sourceFactory = object : MediaSource.Factory by DefaultMediaSourceFactory(routing) {
            override fun createMediaSource(mediaItem: MediaItem): MediaSource {
                val queuedTrack = track(mediaItem)
                val resolving = ResolvingDataSource.Factory(routing) { spec ->
                    val quality = AudioCacheKey.quality(spec.key) ?: app.preferredQuality()
                    when (spec.uri.scheme) {
                        "pruner-song" -> {
                            val songId = requireNotNull(spec.uri.lastPathSegment).toLong()
                            val url = runBlocking { app.netease.resolveAudioUrlOrThrow(songId, quality) }
                            spec.withUri(url.toString().toUri())
                        }
                        "pruner-track" -> {
                            val track = queuedTrack
                                ?: throw AudioResolutionException(AudioErrorCategory.EXTERNAL_SOURCE_ERROR)
                            val resolved = runBlocking { app.sources.resolveStream(track, quality) }
                            if (resolved.requiresLanAuth != (AudioCacheKey.pairing(spec.key) != null)) {
                                throw IOException("Audio stream provenance changed")
                            }
                            val uri = resolved.uri.toUri()
                            spec.buildUpon().setUri(uri).setCustomData(app.sources.accessForStream(uri)).build()
                        }
                        else -> spec
                    }
                }
                val cached = app.audioCache.wrapDataSourceFactory(resolving, app::activeLan)
                return DefaultMediaSourceFactory(cached).createMediaSource(mediaItem)
            }
        }
        player =
            ExoPlayer.Builder(this)
                .setMediaSourceFactory(sourceFactory)
                .setWakeMode(C.WAKE_MODE_NETWORK)
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
                    addListener(this@PrunerPlaybackService)
                }

        val sessionIntent =
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_OPEN_STAGE, true)
            }
        val sessionActivity =
            PendingIntent.getActivity(
                this,
                0,
                sessionIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )

        val callback =
            object : MediaLibrarySession.Callback {
                override fun onConnect(
                    session: MediaSession,
                    controller: MediaSession.ControllerInfo,
                ): MediaSession.ConnectionResult {
                    if (!isTrustedMediaController(controller)) return MediaSession.ConnectionResult.reject()
                    val base = super.onConnect(session, controller)
                    val commands = base.availableSessionCommands.buildUpon().add(COMMAND_TOGGLE_LIKE).build()
                    return MediaSession.ConnectionResult.AcceptedResultBuilder(session)
                        .setAvailableSessionCommands(commands)
                        .setAvailablePlayerCommands(base.availablePlayerCommands)
                        .setCustomLayout(currentCustomButtons())
                        .build()
                }

                override fun onCustomCommand(
                    session: MediaSession,
                    controller: MediaSession.ControllerInfo,
                    customCommand: SessionCommand,
                    args: Bundle,
                ): ListenableFuture<SessionResult> {
                    if (customCommand.customAction == ACTION_TOGGLE_LIKE) {
                        val handler = app.playback.onNotificationLikeToggle.takeIf {
                            isTrustedMediaController(controller)
                        }
                        return Futures.immediateFuture(SessionResult(dispatchNotificationLike(handler)))
                    }
                    return super.onCustomCommand(session, controller, customCommand, args)
                }

                override fun onAddMediaItems(
                    mediaSession: MediaSession,
                    controller: MediaSession.ControllerInfo,
                    mediaItems: MutableList<MediaItem>,
                ): ListenableFuture<MutableList<MediaItem>> {
                    if (!isTrustedMediaController(controller)) {
                        return Futures.immediateFailedFuture(SecurityException("Media controller denied"))
                    }
                    val ownController = controller.uid == Process.myUid()
                    val future = SettableFuture.create<MutableList<MediaItem>>()
                    serviceScope.launch {
                        try {
                            val owner = resolveAccountId()
                            val resolved = mediaItems.mapNotNull { item ->
                                val supplied = track(item)
                                val trustedLocalItem = ownController && item.localConfiguration != null &&
                                    item.queueEntryId() != null && supplied?.identity() == item.mediaId
                                val playable = if (trustedLocalItem) item else {
                                    val found = resolveBrowseTrack(owner, item.mediaId) ?: return@mapNotNull null
                                    runCatching {
                                        buildQueueItems(owner, listOf(found), PlayMode.order, app.preferredQuality(), app::lan).single()
                                    }.getOrNull() ?: return@mapNotNull null
                                }
                                playable
                            }.toMutableList()
                            future.set(resolved)
                        } catch (exception: Exception) {
                            future.setException(exception)
                        }
                    }.invokeOnCompletion { if (!future.isDone) future.cancel(false) }
                    return future
                }

                override fun onGetLibraryRoot(
                    session: MediaLibrarySession,
                    browser: MediaSession.ControllerInfo,
                    params: LibraryParams?,
                ): ListenableFuture<LibraryResult<MediaItem>> {
                    if (!isTrustedMediaController(browser)) {
                        return Futures.immediateFuture(LibraryResult.ofError(SessionError.ERROR_PERMISSION_DENIED))
                    }
                    val root =
                        MediaItem.Builder()
                            .setMediaId(BROWSE_ROOT)
                            .setMediaMetadata(
                                MediaMetadata.Builder()
                                    .setTitle("Pruner Studio")
                                    .setIsBrowsable(true)
                                    .setIsPlayable(false)
                                    .build()
                            )
                            .build()
                    return Futures.immediateFuture(LibraryResult.ofItem(root, params))
                }

                override fun onGetChildren(
                    session: MediaLibrarySession,
                    browser: MediaSession.ControllerInfo,
                    parentId: String,
                    page: Int,
                    pageSize: Int,
                    params: LibraryParams?,
                ): ListenableFuture<LibraryResult<ImmutableList<MediaItem>>> {
                    if (!isTrustedMediaController(browser)) {
                        return Futures.immediateFuture(LibraryResult.ofError(SessionError.ERROR_PERMISSION_DENIED))
                    }
                    val future = SettableFuture.create<LibraryResult<ImmutableList<MediaItem>>>()
                    val safePage = page.coerceAtLeast(0)
                    val safeSize = pageSize.coerceIn(1, 200)
                    val offset = (safePage.toLong() * safeSize).coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
                    serviceScope.launch {
                        try {
                            val acct = resolveAccountId()
                            val items: List<MediaItem> =
                                when (parentId) {
                                    BROWSE_ROOT ->
                                        listOf(
                                            browsableCategory(BROWSE_RECENT, "最近播放"),
                                            browsableCategory(BROWSE_DEVICE, "本机音频"),
                                            browsableCategory(BROWSE_PLAYLISTS, "歌单"),
                                        ).drop(offset).take(safeSize)
                                    BROWSE_RECENT ->
                                        app.repository.historyPaged(acct, "", safeSize, offset).map {
                                            it.toBrowseMediaItem()
                                        }
                                    BROWSE_DEVICE ->
                                        app.repository.deviceTracks(acct).drop(offset).take(safeSize).map {
                                            it.toBrowseMediaItem()
                                        }
                                    BROWSE_PLAYLISTS ->
                                        app.repository.playlists(acct).drop(offset).take(safeSize).map { pl ->
                                            browsableCategory("playlist:${pl.id.content}", pl.displayName())
                                        }
                                    else ->
                                        if (parentId.startsWith("playlist:")) {
                                            val pid = parentId.removePrefix("playlist:")
                                            app.repository.playlistTracksPaged(acct, pid, safeSize, offset).map {
                                                it.toBrowseMediaItem()
                                            }
                                        } else emptyList()
                                }
                            future.set(LibraryResult.ofItemList(ImmutableList.copyOf(items), params))
                        } catch (t: Throwable) {
                            future.set(LibraryResult.ofError(SessionError.ERROR_IO))
                        }
                    }.invokeOnCompletion { if (!future.isDone) future.cancel(false) }
                    return future
                }
            }

        mediaSession =
            MediaLibrarySession.Builder(this, player, callback)
                .setSessionActivity(sessionActivity)
                .setCustomLayout(currentCustomButtons())
                .build()
        app.playback.effects = this
        serviceScope.launch(Dispatchers.Main.immediate) {
            app.pairingState.collect { player.removeStaleDesktopQueueEntries(activeDesktopScope()) }
        }
    }

    private fun activeDesktopScope() = app.activeLan()?.config?.let(AudioCacheKey::desktopNamespace)

    private fun resolveAccountId(): String =
        account
            ?: app.pairingState.value?.accountId
            ?: getSharedPreferences("pruner_ui_prefs", 0).getString("lastAccount", null)
            ?: "offline"

    private fun isTrustedMediaController(controller: MediaSession.ControllerInfo): Boolean =
        controllerHasPrivateMediaAccess(
            controller.uid, Process.myUid(), controller.packageName, controller.isTrusted,
        )

    private fun resolveBrowseTrack(owner: String, mediaId: String): Track? {
        if (mediaId.isBlank() || mediaId.length > 512 || ':' !in mediaId) return null
        return app.repository.trackByKey(owner, mediaId)
            ?: app.repository.deviceTracks(owner).firstOrNull { it.identity() == mediaId }
            ?: app.repository.history(owner).firstOrNull { it.identity() == mediaId }
    }

    private fun currentCustomButtons(): List<CommandButton> =
        if (app.playback.onNotificationLikeToggle == null) emptyList() else buildCustomButtons(currentLiked)

    private fun buildCustomButtons(liked: Boolean): List<CommandButton> =
        listOf(
            CommandButton.Builder(
                    if (liked) CommandButton.ICON_HEART_FILLED else CommandButton.ICON_HEART_UNFILLED
                )
                .setDisplayName(if (liked) "取消喜欢" else "喜欢")
                .setSessionCommand(COMMAND_TOGGLE_LIKE)
                .build()
        )

    internal fun updateLikeButton(liked: Boolean) {
        if (currentLiked == liked) return
        currentLiked = liked
        refreshLikeButton()
    }

    internal fun refreshLikeButton() {
        handler.post { mediaSession?.setCustomLayout(currentCustomButtons()) }
    }

    private fun browsableCategory(id: String, title: String): MediaItem =
        MediaItem.Builder()
            .setMediaId(id)
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(title)
                    .setIsBrowsable(true)
                    .setIsPlayable(false)
                    .build()
            )
            .build()

    private fun Track.toBrowseMediaItem(): MediaItem =
        MediaItem.Builder()
            .setMediaId(identity())
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(title.ifBlank { "未知曲目" })
                    .setArtist(artist.ifBlank { artists.joinToString(" / ") })
                    .setAlbumTitle(album)
                    .setIsBrowsable(false)
                    .setIsPlayable(true)
                    .build()
            )
            .build()

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaLibrarySession? =
        mediaSession.takeIf {
            isTrustedMediaController(controllerInfo) ||
                (controllerInfo.uid == -1 &&
                    controllerInfo.packageName == MediaSession.ControllerInfo.LEGACY_CONTROLLER_PACKAGE_NAME)
        }

    internal fun refreshQuality(quality: AudioQualityLevel) {
        if (player.mediaItemCount == 0) return
        val items = (0 until player.mediaItemCount).map { index ->
            val item = player.getMediaItemAt(index)
            val key = item.localConfiguration?.customCacheKey
            if (key == null) item
            else item.buildUpon().setCustomCacheKey(AudioCacheKey.withQuality(key, quality)).build()
        }
        val prepareNow = player.playbackState != Player.STATE_IDLE
        player.setMediaItems(items, player.currentMediaItemIndex, player.currentPosition)
        if (prepareNow) player.prepare()
    }

    internal fun setRestoredVolume(volume: Float, muted: Boolean) {
        if (volume > 0f) savedUnmutedVolume = volume.coerceIn(0f, 1f)
        if (!muted && volume <= 0f) savedUnmutedVolume = 1f
    }

    override fun onAudioSessionIdChanged(audioSessionId: Int) {
        app.equalizer.attachToSession(audioSessionId)
    }

    internal fun gain(band: Int, value: Float): Boolean =
        app.equalizer.setNormalizedGain(band, value)

    internal fun sleep(minutes: Int) {
        require(minutes in 0..1440)
        handler.removeCallbacks(timer)
        if (minutes > 0) handler.postDelayed(timer, minutes * 60000L)
    }

    private fun accrue() {
        val now = SystemClock.elapsedRealtime()
        playingSince?.let { listened += now - it }
        playingSince = null
    }

    private fun finish(completed: Boolean) {
        accrue()
        val track = current ?: return
        val owner = account ?: return
        val event = PlayEventPayload(eventId, track, listened, completed)
        if (completed || listened >= 30000) writer.execute { app.repository.record(owner, event) }
        current = null
    }

    override fun onIsPlayingChanged(isPlaying: Boolean) {
        accrue()
        if (isPlaying) {
            playingSince = SystemClock.elapsedRealtime()
            retriedMediaKey = null
            startPositionTick()
        } else {
            stopPositionTick()
            persistSnapshot(buildCurrentSnapshot())
        }
        publishProgress()
    }

    override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
        if (
            playWhenReady &&
                player.playbackState == Player.STATE_IDLE &&
                player.mediaItemCount > 0 &&
                player.playerError == null
        ) {
            player.prepare()
        }
    }

    private fun track(item: MediaItem?) =
        item?.mediaMetadata?.extras?.getString("track")?.let {
            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
        }

    override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
        finish(
            reason == Player.MEDIA_ITEM_TRANSITION_REASON_AUTO ||
                reason == Player.MEDIA_ITEM_TRANSITION_REASON_REPEAT
        )
        current = track(mediaItem)
        account = mediaItem?.mediaMetadata?.extras?.getString("account")
        eventId = UUID.randomUUID().toString()
        listened = 0
        retriedMediaKey = null
        if (player.isPlaying) {
            playingSince = SystemClock.elapsedRealtime()
            startPositionTick()
        }
        updateLikeButton(current?.liked == true)
    }

    override fun onPlaybackStateChanged(playbackState: Int) {
        if (playbackState == Player.STATE_ENDED) {
            stopPositionTick()
            finish(true)
        } else if (playbackState == Player.STATE_IDLE) {
            stopPositionTick()
        }
    }

    override fun onPlayerError(error: PlaybackException) {
        stopPositionTick()
        val currentTrack = track(player.currentMediaItem) ?: current
        val mediaKey = currentTrack?.identity() ?: player.currentMediaItem?.mediaId
        val retry =
            shouldAutoRetryOnce(
                error = error,
                currentTrack = currentTrack,
                positionMs = player.currentPosition.coerceAtLeast(0L),
                alreadyRetried = mediaKey != null && mediaKey == retriedMediaKey,
            )
        when (retry) {
            AutoRetryAction.RETRY_SAME_POSITION -> {
                retriedMediaKey = mediaKey
                player.prepare()
                player.play()
                return
            }
            AutoRetryAction.RETRY_FROM_START -> {
                retriedMediaKey = mediaKey
                player.seekTo(player.currentMediaItemIndex, 0L)
                player.prepare()
                player.play()
                return
            }
            AutoRetryAction.NONE -> publishStructural(save = false)
        }
    }

    override fun onEvents(player: Player, events: Player.Events) {
        if (events.contains(Player.EVENT_TIMELINE_CHANGED)) {
            player.removeStaleDesktopQueueEntries(activeDesktopScope())
            rebuildQueueCache()
        }
        publishStructural(save = true)
    }

    private fun rebuildQueueCache() {
        val entries = (0 until player.mediaItemCount).mapNotNull { index ->
            val item = player.getMediaItemAt(index)
            val id = item.queueEntryId() ?: return@mapNotNull null
            track(item)?.let { id to it }
        }
        cachedQueue = entries.map { it.second }
        queueEntryIds = entries.map { it.first }
    }

    private fun buildCurrentSnapshot(): PlaybackSessionSnapshot {
        if (cachedQueue.size != player.mediaItemCount) rebuildQueueCache()
        val item = player.currentMediaItem
        val extras = item?.mediaMetadata?.extras
        val currentTrack = track(item)
        val rawDurationMs = player.duration.takeIf { it != C.TIME_UNSET && it > 0 }
        val durationSec = rawDurationMs?.div(1000.0) ?: currentTrack?.durationSeconds() ?: 0.0
        val currentVol = player.volume
        val muted = currentVol <= 0f
        if (!muted) savedUnmutedVolume = currentVol
        val playMode =
            extras?.getString("mode")?.let { runCatching { PlayMode.valueOf(it) }.getOrNull() }
                ?: PlayMode.order
        return PlaybackSessionSnapshot(
            current_track = currentTrack,
            queue = cachedQueue,
            queue_index = if (item == null) -1 else player.currentMediaItemIndex,
            position_sec = player.currentPosition.coerceAtLeast(0) / 1000.0,
            duration_sec = durationSec,
            is_playing = player.isPlaying,
            play_mode = playMode,
            playback_rate = player.playbackParameters.speed.toDouble(),
            volume = if (muted) savedUnmutedVolume.toDouble() else currentVol.toDouble(),
            muted = muted,
            updated_at = Instant.now().toString(),
        )
    }

    private fun publishProgress() {
        val item = player.currentMediaItem
        val currentTrack = track(item) ?: current
        val rawDurationMs = player.duration.takeIf { it != C.TIME_UNSET && it > 0 }
        val durationMs =
            rawDurationMs
                ?: currentTrack?.let {
                    if (it.duration_ms > 0) it.duration_ms
                    else (it.durationSeconds() * 1000.0).toLong()
                }
                ?: 0L
        app.playback.updateProgress(
            PlaybackProgress(
                positionMs = player.currentPosition.coerceAtLeast(0L),
                durationMs = durationMs.coerceAtLeast(0L),
                bufferedPositionMs = player.bufferedPosition.coerceAtLeast(0L),
                isPlaying = player.isPlaying,
                playbackRate = player.playbackParameters.speed,
                updatedAtElapsedMs = SystemClock.elapsedRealtime(),
            )
        )
    }

    private fun persistSnapshot(snapshot: PlaybackSessionSnapshot) {
        val item = player.currentMediaItem
        val owner = item?.mediaMetadata?.extras?.getString("account") ?: return
        if (snapshot.current_track == null) return
        lastSaved = SystemClock.elapsedRealtime()
        val comparable = snapshot.copy(updated_at = null)
        if (comparable != lastStored || owner != lastAccount) {
            lastStored = comparable
            lastAccount = owner
            writer.execute { app.repository.saveSession(owner, snapshot) }
        }
    }

    private fun publishStructural(save: Boolean) {
        val snapshot = buildCurrentSnapshot()
        val currentTrack = snapshot.current_track
        val err = player.playerError
        val errorMessage =
            if (err != null) {
                if (err !== lastErrorRef) {
                    lastErrorRef = err
                    errorSequence++
                }
                classifyPlaybackError(err, currentTrack).second
            } else {
                lastErrorRef = null
                null
            }
        val isBuffering = player.playWhenReady && player.playbackState == Player.STATE_BUFFERING
        app.playback.update(
            PlaybackUiState(
                snapshot = snapshot,
                error = err?.errorCodeName,
                errorMessage = errorMessage,
                errorToken = if (err != null) errorSequence else 0L,
                isBuffering = isBuffering,
                queueEntryIds = queueEntryIds,
            )
        )
        publishProgress()
        if (save) persistSnapshot(snapshot)
    }

    override fun onDestroy() {
        stopPositionTick()
        handler.removeCallbacksAndMessages(null)
        finish(false)
        publishStructural(save = true)
        serviceScope.cancel()
        writer.shutdown()
        app.playback.effects = null
        player.removeListener(this)
        app.equalizer.releaseEffects()
        mediaSession?.release()
        player.release()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_OPEN_STAGE = "com.prunerstudio.mobile.OPEN_STAGE"
        const val ACTION_TOGGLE_LIKE = "com.prunerstudio.mobile.TOGGLE_LIKE"
        private val COMMAND_TOGGLE_LIKE = SessionCommand(ACTION_TOGGLE_LIKE, Bundle.EMPTY)
        private const val POSITION_TICK_MS = 500L
        private const val BROWSE_ROOT = "pruner_root"
        private const val BROWSE_RECENT = "pruner_recent"
        private const val BROWSE_DEVICE = "pruner_device"
        private const val BROWSE_PLAYLISTS = "pruner_playlists"

        internal fun decodePrunerTrackKey(uri: Uri): String = uri.lastPathSegment.orEmpty()
    }
}

internal fun controllerHasPrivateMediaAccess(uid: Int, appUid: Int, packageName: String, trusted: Boolean): Boolean =
    uid == appUid || uid == Process.SYSTEM_UID || trusted && packageName in setOf(
        "com.android.systemui", "com.android.bluetooth",
        "com.google.android.projection.gearhead", "com.google.android.wearable.app",
    )

@androidx.media3.common.util.UnstableApi
internal fun dispatchNotificationLike(handler: (() -> Unit)?): Int {
    if (handler == null) return SessionError.ERROR_NOT_SUPPORTED
    handler()
    return SessionResult.RESULT_SUCCESS
}
