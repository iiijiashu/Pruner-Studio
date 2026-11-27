package com.prunerstudio.mobile.core.db

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(tableName = "accounts", primaryKeys = ["account_id", "id"])
data class AccountEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
)

@Entity(
    tableName = "playlists",
    primaryKeys = ["account_id", "id"],
    indices = [Index(value = ["account_id", "kind"])],
)
data class PlaylistEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
    @ColumnInfo(name = "name") val name: String,
    @ColumnInfo(name = "kind") val kind: String,
    @ColumnInfo(name = "track_count") val trackCount: Int,
)

@Entity(
    tableName = "tracks",
    primaryKeys = ["account_id", "id"],
    indices = [
        Index(value = ["account_id", "provider"]),
        Index(value = ["account_id", "title"]),
        Index(value = ["account_id", "artist"]),
        Index(value = ["account_id", "track_key"]),
    ],
)
data class TrackEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
    @ColumnInfo(name = "track_key") val trackKey: String,
    @ColumnInfo(name = "provider") val provider: String,
    @ColumnInfo(name = "title") val title: String,
    @ColumnInfo(name = "artist") val artist: String,
    @ColumnInfo(name = "album") val album: String,
    @ColumnInfo(name = "duration_ms") val durationMs: Long,
)

@Entity(
    tableName = "playlist_tracks",
    primaryKeys = ["account_id", "playlist_id", "position"],
    indices = [
        Index(value = ["account_id", "playlist_id", "position"]),
        Index(value = ["account_id", "track_key"]),
    ],
)
data class PlaylistTrackEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "playlist_id") val playlistId: String,
    @ColumnInfo(name = "position") val position: Int,
    @ColumnInfo(name = "track_key") val trackKey: String,
)

@Entity(
    tableName = "play_history",
    primaryKeys = ["account_id", "id"],
    indices = [
        Index(value = ["account_id", "last_played_at"]),
        Index(value = ["account_id", "title"]),
    ],
)
data class PlayHistoryEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
    @ColumnInfo(name = "title") val title: String,
    @ColumnInfo(name = "artist") val artist: String,
    @ColumnInfo(name = "album") val album: String,
    @ColumnInfo(name = "play_count") val playCount: Int,
    @ColumnInfo(name = "last_played_at") val lastPlayedAt: String,
)

@Entity(tableName = "playback_session", primaryKeys = ["account_id", "id"])
data class PlaybackSessionEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
)

@Entity(tableName = "pruning_rules", primaryKeys = ["account_id", "id"])
data class PruningRuleEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
)

@Entity(
    tableName = "local_device_tracks",
    primaryKeys = ["account_id", "id"],
    indices = [
        Index(value = ["account_id", "title"]),
        Index(value = ["account_id", "artist"]),
    ],
)
data class DeviceTrackEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
    @ColumnInfo(name = "title") val title: String,
    @ColumnInfo(name = "artist") val artist: String,
    @ColumnInfo(name = "album") val album: String,
    @ColumnInfo(name = "album_id") val albumId: Long,
)

@Entity(
    tableName = "lan_local_tracks",
    primaryKeys = ["account_id", "id"],
    indices = [
        Index(value = ["account_id", "title"]),
        Index(value = ["account_id", "artist"]),
    ],
)
data class LanLocalTrackEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
    @ColumnInfo(name = "title") val title: String,
    @ColumnInfo(name = "artist") val artist: String,
    @ColumnInfo(name = "album") val album: String,
)

@Entity(tableName = "sync_watermarks", primaryKeys = ["account_id", "id"])
data class SyncWatermarkEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
)

@Entity(
    tableName = "play_events",
    indices = [Index(value = ["account_id"])],
)
data class PlayEventEntity(
    @PrimaryKey @ColumnInfo(name = "event_id") val eventId: String,
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "payload") val payload: String,
)

@Entity(
    tableName = "explicit_feedback",
    primaryKeys = ["account_id", "client_event_id"],
    indices = [Index(value = ["account_id"])],
)
data class ExplicitFeedbackEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "client_event_id") val clientEventId: String,
    @ColumnInfo(name = "payload") val payload: String,
)

@Entity(
    tableName = "pending_sync_outbox",
    primaryKeys = ["account_id", "kind", "id"],
    indices = [Index(value = ["account_id", "kind"])],
)
data class PendingSyncOutboxEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "kind") val kind: String,
    @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "payload") val payload: String,
)

@Entity(tableName = "lyrics_cache")
data class LyricsCacheEntity(
    @PrimaryKey @ColumnInfo(name = "track_key") val trackKey: String,
    @ColumnInfo(name = "lrc") val lrc: String,
    @ColumnInfo(name = "yrc") val yrc: String,
    @ColumnInfo(name = "tlyric") val tlyric: String,
    @ColumnInfo(name = "romalrc") val romalrc: String,
    @ColumnInfo(name = "source") val source: String,
    @ColumnInfo(name = "updated_at") val updatedAt: Long,
)

@Entity(
    tableName = "search_history",
    primaryKeys = ["account_id", "query"],
    indices = [Index(value = ["account_id", "updated_at"])],
)
data class SearchHistoryEntity(
    @ColumnInfo(name = "account_id") val accountId: String,
    @ColumnInfo(name = "query") val query: String,
    @ColumnInfo(name = "updated_at") val updatedAt: Long,
)

@Entity(
    tableName = "external_sources",
    indices = [Index(value = ["kind"])],
)
data class ExternalSourceEntity(
    @PrimaryKey @ColumnInfo(name = "id") val id: String,
    @ColumnInfo(name = "name") val name: String,
    @ColumnInfo(name = "kind") val kind: String,
    @ColumnInfo(name = "base_url") val baseUrl: String,
    @ColumnInfo(name = "username") val username: String,
    @ColumnInfo(name = "enabled") val enabled: Int,
    @ColumnInfo(name = "allow_lan_http") val allowLanHttp: Int,
    @ColumnInfo(name = "updated_at") val updatedAt: Long,
)
