package com.prunerstudio.mobile.core.db

import com.prunerstudio.mobile.core.engine.LyricsParser
import com.prunerstudio.mobile.core.lan.*
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.player.AudioCacheKey
import com.prunerstudio.mobile.core.sources.CompositeAudioSourceResolver
import java.time.Instant
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.*
import kotlinx.serialization.encodeToString

class PrunerRepository(private val database: PrunerDatabase) {
    constructor(helper: PrunerDatabaseHelper) : this(openRoomForHelper(helper))

    companion object {
        private fun openRoomForHelper(helper: PrunerDatabaseHelper): PrunerDatabase {
            helper.writableDatabase
            return PrunerDatabase.create(helper.appContext, helper.dbName, allowMainThread = true)
                .also { helper.attachedRoom = it }
        }
    }

    private val dao: PrunerDao = database.dao()
    private val syncLock = Mutex()

    private fun <T> inTransaction(block: () -> T): T = database.runInTransaction<T> { block() }

    private fun Track.artistDisplay(): String = artist.ifBlank { artists.joinToString(" / ") }

    private fun Track.resolvedDurationMs(): Long =
        if (duration_ms > 0L) duration_ms else (durationSeconds() * 1000.0).toLong().coerceAtLeast(0L)

    private fun Track.toTrackEntity(account: String): TrackEntity {
        val key = identity()
        return TrackEntity(
            accountId = account,
            id = key,
            payload = wire.encodeToString(this),
            trackKey = key,
            provider = effectiveProvider(),
            title = title,
            artist = artistDisplay(),
            album = album,
            durationMs = resolvedDurationMs(),
        )
    }

    private fun PlaylistSummary.toEntity(account: String): PlaylistEntity =
        PlaylistEntity(
            accountId = account,
            id = id.content,
            payload = wire.encodeToString(this),
            name = name,
            kind = kind,
            trackCount = track_count,
        )

    private fun Track.toHistoryEntity(account: String): PlayHistoryEntity =
        PlayHistoryEntity(
            accountId = account,
            id = identity(),
            payload = wire.encodeToString(this),
            title = title,
            artist = artistDisplay(),
            album = album,
            playCount = play_count,
            lastPlayedAt = last_played_at.orEmpty(),
        )

    private fun Track.toDeviceEntity(account: String): DeviceTrackEntity =
        DeviceTrackEntity(
            accountId = account,
            id = identity(),
            payload = wire.encodeToString(this),
            title = title,
            artist = artistDisplay(),
            album = album,
            albumId = album_id ?: 0L,
        )

    private fun LanLocalFile.toEntity(account: String): LanLocalTrackEntity =
        LanLocalTrackEntity(
            accountId = account,
            id = file_id,
            payload = wire.encodeToString(this),
            title = title,
            artist = artist,
            album = album,
        )

    fun history(account: String): List<Track> =
        dao.history(account).mapNotNull { runCatching { wire.decodeFromString<Track>(it) }.getOrNull() }

    fun historyPaged(account: String, query: String = "", limit: Int = 100, offset: Int = 0): List<Track> =
        dao.historyPaged(account, query.trim(), limit, offset).mapNotNull {
            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
        }

    fun playlists(account: String): List<PlaylistSummary> =
        dao.playlists(account).mapNotNull {
            runCatching { wire.decodeFromString<PlaylistSummary>(it) }.getOrNull()
        }

    fun playlistTracks(account: String, id: String): List<Track> =
        dao.playlistTracks(account, id).mapNotNull {
            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
        }

    fun playlistTracksPaged(account: String, id: String, limit: Int, offset: Int): List<Track> =
        dao.playlistTracksPaged(account, id, limit, offset).mapNotNull {
            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
        }

    fun trackByKey(account: String, trackKey: String): Track? =
        dao.trackByKey(account, trackKey)?.let {
            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
        }

    fun searchLocalIndexed(account: String, query: String, limit: Int = 50, offset: Int = 0): List<Track> {
        val q = query.trim()
        if (q.isEmpty()) return emptyList()
        val deviceHits =
            dao.searchDeviceTracks(account, q, limit, offset).mapNotNull {
                runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
            }
        val cachedHits =
            dao.searchCachedTracks(account, q, limit, offset).mapNotNull {
                runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
            }
        return (deviceHits + cachedHits).distinctBy { it.identity() }.take(limit)
    }

    fun searchDeviceTracks(account: String, query: String, limit: Int = 100, offset: Int = 0): List<Track> {
        val q = query.trim()
        if (q.isEmpty()) return deviceTracks(account)
        return dao.searchDeviceTracks(account, q, limit, offset).mapNotNull {
            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
        }
    }

    fun rules(account: String): List<PruningRule> =
        dao.rules(account).mapNotNull {
            runCatching { wire.decodeFromString<PruningRule>(it) }.getOrNull()
        }

    fun feedback(account: String): List<FeedbackItem> =
        dao.feedback(account).mapNotNull {
            runCatching { wire.decodeFromString<FeedbackItem>(it) }.getOrNull()
        }

    fun watermark(account: String, fingerprint: String) =
        SyncWatermarks(dao.watermark(account, fingerprint))

    fun cachedTracks(account: String): List<Track> =
        dao.cachedTracks(account).mapNotNull {
            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
        }

    fun cacheSummaries(account: String, playlists: List<PlaylistSummary>) = inTransaction {
        dao.upsertPlaylists(playlists.map { it.toEntity(account) })
    }

    fun cachePlaylist(account: String, playlist: PlaylistSummary, songs: List<Track>) =
        inTransaction {
            val id = playlist.id.content
            dao.upsertAccount(AccountEntity(account, account, account))
            dao.upsertPlaylist(playlist.toEntity(account))
            dao.deletePlaylistTracks(account, id)
            if (songs.isNotEmpty()) {
                dao.upsertTracks(songs.map { it.toTrackEntity(account) })
                dao.upsertPlaylistTracks(
                    songs.mapIndexed { position, song ->
                        PlaylistTrackEntity(account, id, position, song.identity())
                    }
                )
            }
        }

    fun localLibrary(account: String): List<LanLocalFile> =
        dao.lanLocalTracks(account).mapNotNull {
            runCatching { wire.decodeFromString<LanLocalFile>(it) }.getOrNull()
        }

    fun deviceTracks(account: String): List<Track> =
        dao.deviceTracks(account).mapNotNull {
            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
        }

    fun indexDeviceTracks(account: String, tracks: List<Track>) = inTransaction {
        dao.deleteDeviceTracks(account)
        if (tracks.isNotEmpty()) {
            dao.upsertDeviceTracks(tracks.map { it.toDeviceEntity(account) })
        }
    }

    fun saveSession(account: String, snapshot: PlaybackSessionSnapshot) = inTransaction {
        dao.upsertSession(PlaybackSessionEntity(account, "current", wire.encodeToString(snapshot)))
    }

    fun session(account: String): PlaybackSessionSnapshot? =
        dao.session(account)?.let {
            runCatching { wire.decodeFromString<PlaybackSessionSnapshot>(it) }.getOrNull()
        }

    // Lyrics caching
    fun cachedLyrics(trackKey: String): ParsedLyrics? =
        dao.getCachedLyrics(trackKey)?.let { entity ->
            LyricsParser.parse(entity.lrc, entity.yrc, entity.tlyric, entity.romalrc)
        }

    fun saveLyrics(
        trackKey: String,
        lrc: String,
        yrc: String = "",
        tlyric: String = "",
        romalrc: String = "",
        source: String = "netease",
    ) = inTransaction {
        dao.upsertCachedLyrics(
            LyricsCacheEntity(
                trackKey = trackKey,
                lrc = lrc,
                yrc = yrc,
                tlyric = tlyric,
                romalrc = romalrc,
                source = source,
                updatedAt = System.currentTimeMillis(),
            )
        )
    }

    // Search history
    fun searchHistory(account: String, limit: Int = 15): List<String> =
        dao.searchHistory(account, limit)

    fun recordSearchQuery(account: String, query: String) {
        val clean = query.trim()
        if (clean.isEmpty()) return
        dao.upsertSearchHistory(SearchHistoryEntity(account, clean, System.currentTimeMillis()))
    }

    fun deleteSearchQuery(account: String, query: String) =
        dao.deleteSearchHistoryItem(account, query)

    fun clearSearchHistory(account: String) = dao.clearSearchHistory(account)

    // External sources
    fun externalSources(): List<ExternalSourceConfig> =
        dao.externalSources().mapNotNull { e ->
            val kind =
                runCatching { ExternalSourceKind.valueOf(e.kind) }.getOrNull() ?: return@mapNotNull null
            ExternalSourceConfig(
                id = e.id,
                name = e.name,
                kind = kind,
                baseUrl = e.baseUrl,
                username = e.username,
                enabled = e.enabled != 0,
                allowLanHttp = e.allowLanHttp != 0,
            )
        }

    fun saveExternalSource(config: ExternalSourceConfig) = inTransaction {
        dao.upsertExternalSource(
            ExternalSourceEntity(
                id = config.id,
                name = config.name,
                kind = config.kind.name,
                baseUrl = config.baseUrl,
                username = config.username,
                enabled = if (config.enabled) 1 else 0,
                allowLanHttp = if (config.allowLanHttp) 1 else 0,
                updatedAt = System.currentTimeMillis(),
            )
        )
    }

    fun deleteExternalSource(id: String) = inTransaction { dao.deleteExternalSource(id) }

    fun record(account: String, event: PlayEventPayload) = inTransaction {
        if (!event.completed && event.listen_ms < 25000) return@inTransaction
        val payload = wire.encodeToString(event)
        val added = dao.insertPlayEventIgnore(PlayEventEntity(event.event_id, account, payload)) != -1L
        if (!added) return@inTransaction
        val key = event.track.identity()
        val old =
            dao.historyItem(account, key)?.let {
                runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
            }
        val track =
            event.track.copy(
                play_count = (old?.play_count ?: 0) + 1,
                completed_count = (old?.completed_count ?: 0) + (if (event.completed) 1 else 0),
                total_listen_ms = (old?.total_listen_ms ?: 0) + event.listen_ms,
                last_played_at = Instant.now().toString(),
            )
        dao.upsertHistory(track.toHistoryEntity(account))
        if (track.canSync()) {
            dao.upsertOutbox(PendingSyncOutboxEntity(account, "play", event.event_id, payload))
        }
    }

    fun recordFeedback(account: String, item: FeedbackItem) = inTransaction {
        val id = requireNotNull(item.client_event_id)
        require(
            id.isNotBlank() &&
                item.action in setOf("like_explicit", "not_interested", "replay", "relisten_later")
        )
        val json = wire.encodeToString(item)
        val inserted =
            dao.insertFeedbackIgnore(ExplicitFeedbackEntity(account, id, json)) != -1L
        if (inserted && item.canSync()) {
            dao.upsertOutbox(PendingSyncOutboxEntity(account, "feedback", id, json))
        }
    }

    suspend fun syncWithDesktop(
        lan: LanSyncClient,
        includeCatalog: Boolean = true,
        includeLocal: Boolean = true,
        sessionMutex: Mutex = Mutex(),
        isStillActive: () -> Boolean = { true },
    ) =
        withContext(Dispatchers.IO) {
            syncLock.withLock {
                suspend fun guardedTransaction(operation: () -> Unit) {
                    sessionMutex.withLock {
                        currentCoroutineContext().ensureActive()
                        inTransaction {
                            operation()
                            if (!isStillActive()) throw CancellationException("Pairing changed")
                        }
                    }
                }
                val account = lan.config.accountId
                suspend fun sendOutbox(batch: List<PendingSyncOutboxEntity>) {
                    val localOnly = mutableListOf<PendingSyncOutboxEntity>()
                    val syncable = mutableListOf<PendingSyncOutboxEntity>()
                    val plays = mutableListOf<PlayEventPayload>()
                    val feedback = mutableListOf<FeedbackItem>()
                    var decodeError = false
                    for (row in batch) {
                        when (row.kind) {
                            "play" -> {
                                val parsed = runCatching { wire.decodeFromString<PlayEventPayload>(row.payload) }.getOrNull()
                                if (parsed == null) {
                                    decodeError = true
                                } else if (!parsed.track.canSync()) {
                                    localOnly += row
                                } else {
                                    syncable += row
                                    plays += parsed
                                }
                            }
                            "feedback" -> {
                                val parsed = runCatching { wire.decodeFromString<FeedbackItem>(row.payload) }.getOrNull()
                                if (parsed == null) {
                                    decodeError = true
                                } else if (!parsed.canSync()) {
                                    localOnly += row
                                } else {
                                    syncable += row
                                    feedback += parsed
                                }
                            }
                            else -> decodeError = true
                        }
                    }
                    if (localOnly.isNotEmpty()) {
                        guardedTransaction {
                            localOnly.forEach { dao.deleteOutboxItem(account, it.kind, it.id) }
                        }
                    }
                    if (!decodeError && syncable.isEmpty()) return
                    val body =
                        if (decodeError) null
                        else
                            try {
                                lan.encodeSyncPush(SyncPushRequest(account, plays, feedback))
                            } catch (_: IllegalArgumentException) {
                                null
                            }
                    val rejected = body == null || try {
                        lan.sendSyncPush(body)
                        false
                    } catch (e: LanException) {
                        if (e.status != 400 || e.code != "INVALID_INPUT") throw e
                        true
                    }
                    if (rejected) {
                        val remaining = if (decodeError) batch.filterNot { it in localOnly } else syncable
                        if (remaining.size > 1) {
                            val midpoint = remaining.size / 2
                            sendOutbox(remaining.take(midpoint))
                            sendOutbox(remaining.drop(midpoint))
                        } else if (remaining.size == 1) {
                            val row = remaining.single()
                            guardedTransaction {
                                dao.upsertOutbox(row.copy(kind = "quarantine:" + row.kind))
                                dao.deleteOutboxItem(account, row.kind, row.id)
                            }
                        }
                        return
                    }
                    guardedTransaction {
                        syncable.forEach { dao.deleteOutboxItem(account, it.kind, it.id) }
                    }
                }
                while (true) {
                    val batch = dao.pendingOutboxBatch(account, 100)
                    if (batch.isEmpty()) break
                    sendOutbox(batch)
                }
                val server = lan.config.fingerprint
                val since = watermark(account, server).since
                val data = lan.pullAllDeltas(since, includeCatalog, includeLocal)
                val desktopScope = AudioCacheKey.desktopNamespace(lan.config)
                fun desktopTrack(track: Track): Track =
                    if (track.providerFamily() in setOf("netease", "device")) track
                    else track.copy(
                        source_id = CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID,
                        desktop_scope = desktopScope,
                    )
                guardedTransaction {
                    dao.upsertAccount(AccountEntity(account, account, account))
                    val pending =
                        dao.pendingPlayPayloads(account)
                            .mapNotNull { runCatching { wire.decodeFromString<PlayEventPayload>(it) }.getOrNull() }
                            .groupBy { it.track.identity() }
                    data.play_history.forEach { row ->
                        val key = row.identity()
                        val additions = pending[key].orEmpty()
                        val merged =
                            if (additions.isEmpty()) row
                            else
                                row.copy(
                                    play_count = row.play_count + additions.size,
                                    completed_count =
                                        row.completed_count + additions.count { it.completed },
                                    total_listen_ms =
                                        row.total_listen_ms + additions.sumOf { it.listen_ms },
                                    last_played_at =
                                        dao.historyItem(account, key)?.let {
                                            runCatching { wire.decodeFromString<Track>(it) }
                                                .getOrNull()
                                                ?.last_played_at
                                        } ?: row.last_played_at,
                                )
                        val previous = dao.historyItem(account, key)?.let {
                            runCatching { wire.decodeFromString<Track>(it) }.getOrNull()
                        }
                        val track = if (previous?.source_id != null && previous.desktop_scope == null) {
                            merged.copy(source_id = previous.source_id)
                        } else desktopTrack(merged)
                        dao.upsertHistory(track.toHistoryEntity(account))
                    }
                    data.feedback.forEach {
                        dao.upsertFeedback(
                            ExplicitFeedbackEntity(
                                account,
                                it.client_event_id?.takeIf(String::isNotBlank) ?: "desktop:${it.id}",
                                wire.encodeToString(it),
                            )
                        )
                    }
                    dao.deleteRules(account)
                    dao.upsertRules(
                        data.rules.mapIndexed { idx, rule ->
                            PruningRuleEntity(
                                account,
                                if (rule.id.isBlank()) "missing:$idx" else "id:${rule.id}",
                                wire.encodeToString(rule),
                            )
                        }
                    )
                    data.playlists?.let { list ->
                        dao.deletePlaylistsForAccount(account)
                        dao.deleteAllPlaylistTracksForAccount(account)
                        dao.upsertPlaylists(list.map { it.toEntity(account) })
                    }
                    data.songs?.let { songs ->
                        dao.upsertTracks(songs.map { desktopTrack(it).toTrackEntity(account) })
                        val joins =
                            songs.mapIndexedNotNull { index, t ->
                                t.source_playlist_id?.let {
                                    PlaylistTrackEntity(account, it.content, index, t.identity())
                                }
                            }
                        if (joins.isNotEmpty()) dao.upsertPlaylistTracks(joins)
                    }
                    data.local_library?.let { list ->
                        dao.deleteLanLocalTracks(account)
                        dao.upsertLanLocalTracks(list.map { it.toEntity(account) })
                    }
                    data.playback_session?.let { remote ->
                        val local = session(account)
                        fun time(snapshot: PlaybackSessionSnapshot?) =
                            snapshot?.updated_at?.let {
                                runCatching { Instant.parse(it) }.getOrNull()
                            } ?: Instant.MIN
                        if (local == null || time(remote) >= time(local)) {
                            dao.upsertSession(
                                PlaybackSessionEntity(
                                    account,
                                    "current",
                                    wire.encodeToString(remote.copy(
                                        current_track = remote.current_track?.let(::desktopTrack),
                                        queue = remote.queue.map(::desktopTrack),
                                    )),
                                )
                            )
                        }
                    }
                    dao.upsertWatermark(SyncWatermarkEntity(account, server, data.sync_until))
                }
                data
            }
        }
}
