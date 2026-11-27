package com.prunerstudio.mobile.core.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

@Dao
interface PrunerDao {
    // Accounts
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertAccount(entity: AccountEntity)

    // Playlists
    @Query("SELECT payload FROM playlists WHERE account_id = :account")
    fun playlists(account: String): List<String>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertPlaylists(items: List<PlaylistEntity>)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertPlaylist(item: PlaylistEntity)

    @Query("DELETE FROM playlists WHERE account_id = :account")
    fun deletePlaylistsForAccount(account: String)

    // Tracks & Playlist join
    @Query("SELECT payload FROM tracks WHERE account_id = :account")
    fun cachedTracks(account: String): List<String>

    @Query(
        "SELECT payload FROM tracks WHERE account_id = :account AND track_key = :trackKey LIMIT 1"
    )
    fun trackByKey(account: String, trackKey: String): String?

    @Query(
        """
        SELECT payload FROM tracks
        WHERE account_id = :account
          AND (title LIKE '%' || :query || '%' OR artist LIKE '%' || :query || '%' OR album LIKE '%' || :query || '%')
        ORDER BY title ASC
        LIMIT :limit OFFSET :offset
        """
    )
    fun searchCachedTracks(account: String, query: String, limit: Int, offset: Int): List<String>

    @Query(
        """
        SELECT t.payload FROM playlist_tracks p
        JOIN tracks t ON p.account_id = t.account_id AND p.track_key = t.id
        WHERE p.account_id = :account AND p.playlist_id = :playlistId
        ORDER BY p.position ASC
        """
    )
    fun playlistTracks(account: String, playlistId: String): List<String>

    @Query(
        """
        SELECT t.payload FROM playlist_tracks p
        JOIN tracks t ON p.account_id = t.account_id AND p.track_key = t.id
        WHERE p.account_id = :account AND p.playlist_id = :playlistId
        ORDER BY p.position ASC
        LIMIT :limit OFFSET :offset
        """
    )
    fun playlistTracksPaged(
        account: String,
        playlistId: String,
        limit: Int,
        offset: Int,
    ): List<String>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertTracks(items: List<TrackEntity>)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertTrack(item: TrackEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertPlaylistTracks(items: List<PlaylistTrackEntity>)

    @Query(
        "DELETE FROM playlist_tracks WHERE account_id = :account AND playlist_id = :playlistId"
    )
    fun deletePlaylistTracks(account: String, playlistId: String)

    @Query("DELETE FROM playlist_tracks WHERE account_id = :account")
    fun deleteAllPlaylistTracksForAccount(account: String)

    // Play History
    @Query("SELECT payload FROM play_history WHERE account_id = :account ORDER BY last_played_at DESC")
    fun history(account: String): List<String>

    @Query(
        """
        SELECT payload FROM play_history
        WHERE account_id = :account
          AND (:query = '' OR title LIKE '%' || :query || '%' OR artist LIKE '%' || :query || '%' OR album LIKE '%' || :query || '%')
        ORDER BY last_played_at DESC
        LIMIT :limit OFFSET :offset
        """
    )
    fun historyPaged(account: String, query: String, limit: Int, offset: Int): List<String>

    @Query("SELECT payload FROM play_history WHERE account_id = :account AND id = :trackKey LIMIT 1")
    fun historyItem(account: String, trackKey: String): String?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertHistory(item: PlayHistoryEntity)

    // Device local tracks
    @Query("SELECT payload FROM local_device_tracks WHERE account_id = :account")
    fun deviceTracks(account: String): List<String>

    @Query(
        """
        SELECT payload FROM local_device_tracks
        WHERE account_id = :account
          AND (title LIKE '%' || :query || '%' OR artist LIKE '%' || :query || '%' OR album LIKE '%' || :query || '%')
        ORDER BY title ASC
        LIMIT :limit OFFSET :offset
        """
    )
    fun searchDeviceTracks(account: String, query: String, limit: Int, offset: Int): List<String>

    @Query("DELETE FROM local_device_tracks WHERE account_id = :account")
    fun deleteDeviceTracks(account: String)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertDeviceTracks(items: List<DeviceTrackEntity>)

    // LAN local tracks
    @Query("SELECT payload FROM lan_local_tracks WHERE account_id = :account")
    fun lanLocalTracks(account: String): List<String>

    @Query(
        """
        SELECT payload FROM lan_local_tracks
        WHERE account_id = :account
          AND (title LIKE '%' || :query || '%' OR artist LIKE '%' || :query || '%' OR album LIKE '%' || :query || '%')
        ORDER BY title ASC
        LIMIT :limit OFFSET :offset
        """
    )
    fun searchLanLocalTracks(account: String, query: String, limit: Int, offset: Int): List<String>

    @Query("DELETE FROM lan_local_tracks WHERE account_id = :account")
    fun deleteLanLocalTracks(account: String)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertLanLocalTracks(items: List<LanLocalTrackEntity>)

    // Playback Session
    @Query("SELECT payload FROM playback_session WHERE account_id = :account AND id = 'current' LIMIT 1")
    fun session(account: String): String?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertSession(entity: PlaybackSessionEntity)

    // Pruning rules
    @Query("SELECT payload FROM pruning_rules WHERE account_id = :account")
    fun rules(account: String): List<String>

    @Query("DELETE FROM pruning_rules WHERE account_id = :account")
    fun deleteRules(account: String)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertRules(items: List<PruningRuleEntity>)

    // Explicit feedback
    @Query("SELECT payload FROM explicit_feedback WHERE account_id = :account")
    fun feedback(account: String): List<String>

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    fun insertFeedbackIgnore(entity: ExplicitFeedbackEntity): Long

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertFeedback(entity: ExplicitFeedbackEntity)

    // Play events
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    fun insertPlayEventIgnore(entity: PlayEventEntity): Long

    // Pending sync outbox
    @Query(
        "SELECT * FROM pending_sync_outbox WHERE account_id = :account AND kind NOT LIKE 'quarantine:%' ORDER BY rowid ASC LIMIT :limit"
    )
    fun pendingOutboxBatch(account: String, limit: Int = 100): List<PendingSyncOutboxEntity>

    @Query(
        "SELECT payload FROM pending_sync_outbox WHERE account_id = :account AND kind = 'play'"
    )
    fun pendingPlayPayloads(account: String): List<String>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertOutbox(entity: PendingSyncOutboxEntity)

    @Query(
        "DELETE FROM pending_sync_outbox WHERE account_id = :account AND kind = :kind AND id = :id"
    )
    fun deleteOutboxItem(account: String, kind: String, id: String)

    // Sync watermarks
    @Query("SELECT payload FROM sync_watermarks WHERE account_id = :account AND id = :serverPin LIMIT 1")
    fun watermark(account: String, serverPin: String): String?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertWatermark(entity: SyncWatermarkEntity)

    // Lyrics Cache
    @Query("SELECT * FROM lyrics_cache WHERE track_key = :trackKey LIMIT 1")
    fun getCachedLyrics(trackKey: String): LyricsCacheEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertCachedLyrics(entity: LyricsCacheEntity)

    @Query("DELETE FROM lyrics_cache WHERE updated_at < :cutoffMs")
    fun pruneOldLyrics(cutoffMs: Long)

    // Search History
    @Query(
        "SELECT query FROM search_history WHERE account_id = :account ORDER BY updated_at DESC LIMIT :limit"
    )
    fun searchHistory(account: String, limit: Int = 15): List<String>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertSearchHistory(entity: SearchHistoryEntity)

    @Query("DELETE FROM search_history WHERE account_id = :account AND query = :query")
    fun deleteSearchHistoryItem(account: String, query: String)

    @Query("DELETE FROM search_history WHERE account_id = :account")
    fun clearSearchHistory(account: String)

    // External Sources
    @Query("SELECT * FROM external_sources ORDER BY updated_at ASC")
    fun externalSources(): List<ExternalSourceEntity>

    @Query("SELECT * FROM external_sources WHERE enabled = 1 ORDER BY updated_at ASC")
    fun enabledExternalSources(): List<ExternalSourceEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertExternalSource(entity: ExternalSourceEntity)

    @Query("DELETE FROM external_sources WHERE id = :id")
    fun deleteExternalSource(id: String)
}
