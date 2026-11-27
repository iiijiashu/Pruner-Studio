package com.prunerstudio.mobile.core.db

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import com.prunerstudio.mobile.core.model.LanLocalFile
import com.prunerstudio.mobile.core.model.PlaylistSummary
import com.prunerstudio.mobile.core.model.Track
import com.prunerstudio.mobile.core.model.wire

object PrunerMigrations {
    val MIGRATION_1_2: Migration =
        object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                migrateV1ToV2(db)
            }
        }

    internal fun migrateV1ToV2(db: SupportSQLiteDatabase) {
        // 1. Rebuild playlists with indexed columns
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS playlists_v2 (
                account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                track_count INTEGER NOT NULL,
                PRIMARY KEY(account_id, id)
            )
            """
        )
        copyTable(db, "playlists") { accountId, id, payload ->
            val p = runCatching { wire.decodeFromString<PlaylistSummary>(payload) }.getOrNull()
            db.execSQL(
                "INSERT OR REPLACE INTO playlists_v2(account_id, id, payload, name, kind, track_count) VALUES(?,?,?,?,?,?)",
                arrayOf<Any>(
                    accountId,
                    id,
                    payload,
                    p?.name.orEmpty(),
                    p?.kind.orEmpty(),
                    p?.track_count ?: 0,
                ),
            )
        }
        db.execSQL("DROP TABLE IF EXISTS playlists")
        db.execSQL("ALTER TABLE playlists_v2 RENAME TO playlists")
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_playlists_account_id_kind ON playlists(account_id, kind)"
        )

        // 2. Rebuild tracks with canonical track_key and indexed metadata
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS tracks_v2 (
                account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                track_key TEXT NOT NULL,
                provider TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                PRIMARY KEY(account_id, id)
            )
            """
        )
        copyTable(db, "tracks") { accountId, id, payload ->
            val t = runCatching { wire.decodeFromString<Track>(payload) }.getOrNull()
            val artist =
                t?.artist?.ifBlank { t.artists.joinToString(" / ") }.orEmpty()
            val durationMs =
                t?.let {
                    if (it.duration_ms > 0) it.duration_ms
                    else (it.durationSeconds() * 1000.0).toLong().coerceAtLeast(0L)
                } ?: 0L
            db.execSQL(
                """
                INSERT OR REPLACE INTO tracks_v2(
                    account_id, id, payload, track_key, provider, title, artist, album, duration_ms
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                arrayOf<Any>(
                    accountId,
                    id,
                    payload,
                    t?.identity() ?: id,
                    t?.effectiveProvider().orEmpty(),
                    t?.title.orEmpty(),
                    artist,
                    t?.album.orEmpty(),
                    durationMs,
                ),
            )
        }
        db.execSQL("DROP TABLE IF EXISTS tracks")
        db.execSQL("ALTER TABLE tracks_v2 RENAME TO tracks")
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_tracks_account_id_provider ON tracks(account_id, provider)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_tracks_account_id_title ON tracks(account_id, title)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_tracks_account_id_artist ON tracks(account_id, artist)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_tracks_account_id_track_key ON tracks(account_id, track_key)"
        )

        // 3. Indices on playlist_tracks
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_playlist_tracks_account_id_playlist_id_position ON playlist_tracks(account_id, playlist_id, position)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_playlist_tracks_account_id_track_key ON playlist_tracks(account_id, track_key)"
        )

        // 4. Rebuild play_history with indexed last_played_at & title
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS play_history_v2 (
                account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                play_count INTEGER NOT NULL,
                last_played_at TEXT NOT NULL,
                PRIMARY KEY(account_id, id)
            )
            """
        )
        copyTable(db, "play_history") { accountId, id, payload ->
            val t = runCatching { wire.decodeFromString<Track>(payload) }.getOrNull()
            val artist = t?.artist?.ifBlank { t.artists.joinToString(" / ") }.orEmpty()
            db.execSQL(
                """
                INSERT OR REPLACE INTO play_history_v2(
                    account_id, id, payload, title, artist, album, play_count, last_played_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                arrayOf<Any>(
                    accountId,
                    id,
                    payload,
                    t?.title.orEmpty(),
                    artist,
                    t?.album.orEmpty(),
                    t?.play_count ?: 0,
                    t?.last_played_at.orEmpty(),
                ),
            )
        }
        db.execSQL("DROP TABLE IF EXISTS play_history")
        db.execSQL("ALTER TABLE play_history_v2 RENAME TO play_history")
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_play_history_account_id_last_played_at ON play_history(account_id, last_played_at)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_play_history_account_id_title ON play_history(account_id, title)"
        )

        // 5. Rebuild local_device_tracks with indexed metadata & album_id
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS local_device_tracks_v2 (
                account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                album_id INTEGER NOT NULL,
                PRIMARY KEY(account_id, id)
            )
            """
        )
        copyTable(db, "local_device_tracks") { accountId, id, payload ->
            val t = runCatching { wire.decodeFromString<Track>(payload) }.getOrNull()
            val artist = t?.artist?.ifBlank { t.artists.joinToString(" / ") }.orEmpty()
            db.execSQL(
                """
                INSERT OR REPLACE INTO local_device_tracks_v2(
                    account_id, id, payload, title, artist, album, album_id
                ) VALUES(?,?,?,?,?,?,?)
                """,
                arrayOf<Any>(
                    accountId,
                    id,
                    payload,
                    t?.title.orEmpty(),
                    artist,
                    t?.album.orEmpty(),
                    t?.album_id ?: 0L,
                ),
            )
        }
        db.execSQL("DROP TABLE IF EXISTS local_device_tracks")
        db.execSQL("ALTER TABLE local_device_tracks_v2 RENAME TO local_device_tracks")
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_local_device_tracks_account_id_title ON local_device_tracks(account_id, title)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_local_device_tracks_account_id_artist ON local_device_tracks(account_id, artist)"
        )

        // 6. Rebuild lan_local_tracks with indexed metadata
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS lan_local_tracks_v2 (
                account_id TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                PRIMARY KEY(account_id, id)
            )
            """
        )
        copyTable(db, "lan_local_tracks") { accountId, id, payload ->
            val f = runCatching { wire.decodeFromString<LanLocalFile>(payload) }.getOrNull()
            db.execSQL(
                """
                INSERT OR REPLACE INTO lan_local_tracks_v2(
                    account_id, id, payload, title, artist, album
                ) VALUES(?,?,?,?,?,?)
                """,
                arrayOf<Any>(
                    accountId,
                    id,
                    payload,
                    f?.title.orEmpty(),
                    f?.artist.orEmpty(),
                    f?.album.orEmpty(),
                ),
            )
        }
        db.execSQL("DROP TABLE IF EXISTS lan_local_tracks")
        db.execSQL("ALTER TABLE lan_local_tracks_v2 RENAME TO lan_local_tracks")
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_lan_local_tracks_account_id_title ON lan_local_tracks(account_id, title)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_lan_local_tracks_account_id_artist ON lan_local_tracks(account_id, artist)"
        )

        // 7. Indices on existing v1 event/feedback/outbox tables
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_play_events_account_id ON play_events(account_id)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_explicit_feedback_account_id ON explicit_feedback(account_id)"
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_pending_sync_outbox_account_id_kind ON pending_sync_outbox(account_id, kind)"
        )

        // 8. New v2 tables: lyrics_cache, search_history, external_sources
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS lyrics_cache (
                track_key TEXT NOT NULL PRIMARY KEY,
                lrc TEXT NOT NULL,
                yrc TEXT NOT NULL,
                tlyric TEXT NOT NULL,
                romalrc TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS search_history (
                account_id TEXT NOT NULL,
                query TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(account_id, query)
            )
            """
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_search_history_account_id_updated_at ON search_history(account_id, updated_at)"
        )
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS external_sources (
                id TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                base_url TEXT NOT NULL,
                username TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                allow_lan_http INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        db.execSQL(
            "CREATE INDEX IF NOT EXISTS index_external_sources_kind ON external_sources(kind)"
        )
    }

    private fun copyTable(
        db: SupportSQLiteDatabase,
        table: String,
        consumer: (accountId: String, id: String, payload: String) -> Unit,
    ) {
        val exists =
            db.query(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    arrayOf(table),
                )
                .use { it.moveToFirst() }
        if (!exists) return
        db.query("SELECT account_id, id, payload FROM $table").use { c ->
            while (c.moveToNext()) {
                consumer(c.getString(0), c.getString(1), c.getString(2))
            }
        }
    }
}

@Database(
    entities = [
        AccountEntity::class,
        PlaylistEntity::class,
        TrackEntity::class,
        PlaylistTrackEntity::class,
        PlayHistoryEntity::class,
        PlaybackSessionEntity::class,
        PruningRuleEntity::class,
        DeviceTrackEntity::class,
        LanLocalTrackEntity::class,
        SyncWatermarkEntity::class,
        PlayEventEntity::class,
        ExplicitFeedbackEntity::class,
        PendingSyncOutboxEntity::class,
        LyricsCacheEntity::class,
        SearchHistoryEntity::class,
        ExternalSourceEntity::class,
    ],
    version = 2,
    exportSchema = true,
)
abstract class PrunerDatabase : RoomDatabase() {
    abstract fun dao(): PrunerDao

    companion object {
        const val DB_NAME = "pruner.db"

        fun create(context: Context, name: String = DB_NAME, allowMainThread: Boolean = false): PrunerDatabase =
            Room.databaseBuilder(context.applicationContext, PrunerDatabase::class.java, name)
                .setJournalMode(JournalMode.WRITE_AHEAD_LOGGING)
                .addMigrations(PrunerMigrations.MIGRATION_1_2)
                .apply { if (allowMainThread) allowMainThreadQueries() }
                .build()
    }
}
