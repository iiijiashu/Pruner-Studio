package com.prunerstudio.mobile

import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.ResolvingDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheWriter
import androidx.media3.datasource.okhttp.OkHttpDataSource
import com.prunerstudio.mobile.core.db.PrunerDatabase
import com.prunerstudio.mobile.core.db.PrunerRepository
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.player.AudioCacheManager
import com.prunerstudio.mobile.core.player.EqualizerController
import java.io.IOException
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@UnstableApi
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class RoomMigrationAndCacheTest {

    @Test
    fun roomMigration1To2PreservesAllV1DataAndEnablesIndexedSearchAndV2Tables() {
        val ctx = RuntimeEnvironment.getApplication()
        val dbName = "migration_v1_to_v2_test.db"
        ctx.deleteDatabase(dbName)

        val track =
            Track(
                id = 777L,
                song_id = 777L,
                provider = "netease",
                external_id = "777",
                track_key = "netease:777",
                title = "夜的第七章",
                artist = "周杰伦",
                album = "依然范特西",
                duration_ms = 228_000L,
                play_count = 12,
                last_played_at = "2026-11-25T10:00:00Z",
            )
        val deviceTrack =
            Track(
                provider = "device",
                external_id = "42",
                track_key = "device:42",
                title = "Clair de Lune",
                artist = "Debussy",
                album = "Suite bergamasque",
                duration_ms = 300_000L,
                stream_url = "content://media/external/audio/media/42",
            )
        val playlist = PlaylistSummary(JsonPrimitive(100), "深夜歌单", "self_created", 1)
        val rule = PruningRule(id = "r1", name = "过短曲目")
        val feedback = FeedbackItem("like_explicit", "netease:777", 777L, client_event_id = "fb-1")
        val lanFile = LanLocalFile("a".repeat(24), "/stream", "FLAC Master", "Artist", "Album", 200_000L)
        val session = PlaybackSessionSnapshot(current_track = track, queue = listOf(track), queue_index = 0, position_sec = 33.5)

        // Step 1: Create a REAL legacy v1 database using the exact pre-Room v1 DDL
        val legacyV1 =
            object : SQLiteOpenHelper(ctx, dbName, null, 1) {
                override fun onCreate(db: SQLiteDatabase) {
                    for (table in
                        listOf(
                            "accounts",
                            "playlists",
                            "tracks",
                            "play_history",
                            "playback_session",
                            "pruning_rules",
                            "local_device_tracks",
                            "lan_local_tracks",
                            "sync_watermarks",
                        )) db.execSQL(
                        "CREATE TABLE $table(account_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,id))"
                    )
                    db.execSQL(
                        "CREATE TABLE playlist_tracks(account_id TEXT NOT NULL, playlist_id TEXT NOT NULL, position INTEGER NOT NULL, track_key TEXT NOT NULL, PRIMARY KEY(account_id,playlist_id,position))"
                    )
                    db.execSQL(
                        "CREATE TABLE play_events(event_id TEXT PRIMARY KEY NOT NULL, account_id TEXT NOT NULL, payload TEXT NOT NULL)"
                    )
                    db.execSQL(
                        "CREATE TABLE explicit_feedback(account_id TEXT NOT NULL, client_event_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,client_event_id))"
                    )
                    db.execSQL(
                        "CREATE TABLE pending_sync_outbox(account_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,kind,id))"
                    )
                }

                override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
            }
        legacyV1.writableDatabase.use { db ->
            db.execSQL("INSERT INTO accounts VALUES('u1','u1','u1')")
            db.execSQL("INSERT INTO playlists VALUES('u1','100',?)", arrayOf(wire.encodeToString(playlist)))
            db.execSQL("INSERT INTO tracks VALUES('u1','netease:777',?)", arrayOf(wire.encodeToString(track)))
            db.execSQL("INSERT INTO playlist_tracks VALUES('u1','100',0,'netease:777')")
            db.execSQL("INSERT INTO play_history VALUES('u1','netease:777',?)", arrayOf(wire.encodeToString(track)))
            db.execSQL("INSERT INTO playback_session VALUES('u1','current',?)", arrayOf(wire.encodeToString(session)))
            db.execSQL("INSERT INTO pruning_rules VALUES('u1','id:r1',?)", arrayOf(wire.encodeToString(rule)))
            db.execSQL("INSERT INTO local_device_tracks VALUES('u1','device:42',?)", arrayOf(wire.encodeToString(deviceTrack)))
            db.execSQL("INSERT INTO lan_local_tracks VALUES('u1',?,?)", arrayOf(lanFile.file_id, wire.encodeToString(lanFile)))
            db.execSQL("INSERT INTO sync_watermarks VALUES('u1','fp123','2026-11-25T11:00:00Z')")
            db.execSQL("INSERT INTO explicit_feedback VALUES('u1','fb-1',?)", arrayOf(wire.encodeToString(feedback)))
            // Corrupt row must NOT crash migration
            db.execSQL("INSERT INTO tracks VALUES('u1','corrupt:1','{not valid json')")
        }
        legacyV1.close()

        // Step 2: Open with Room v2 (executes MIGRATION_1_2 + full Room schema verification!)
        val roomDb = PrunerDatabase.create(ctx, name = dbName, allowMainThread = true)
        try {
            val repo = PrunerRepository(roomDb)
            assertEquals(listOf(playlist), repo.playlists("u1"))
            assertEquals(listOf(track), repo.playlistTracks("u1", "100"))
            assertEquals(listOf(track), repo.playlistTracksPaged("u1", "100", limit = 10, offset = 0))
            assertEquals(track, repo.trackByKey("u1", "netease:777"))
            assertEquals(listOf(track), repo.history("u1"))
            assertEquals(listOf(track), repo.historyPaged("u1", query = "周杰伦", limit = 10, offset = 0))
            assertEquals(session, repo.session("u1"))
            assertEquals(listOf(rule), repo.rules("u1"))
            assertEquals(listOf(deviceTrack), repo.deviceTracks("u1"))
            assertEquals(listOf(lanFile), repo.localLibrary("u1"))
            assertEquals("2026-11-25T11:00:00Z", repo.watermark("u1", "fp123").since)
            assertEquals(listOf(feedback), repo.feedback("u1"))

            // Indexed SQL search works immediately on migrated data
            val hits = repo.searchLocalIndexed("u1", "Debussy")
            assertEquals(listOf(deviceTrack), hits)
            val cachedHits = repo.searchLocalIndexed("u1", "第七章")
            assertEquals(listOf(track), cachedHits)

            // New v2 tables: lyrics cache, search history, external sources
            repo.saveLyrics(
                trackKey = "netease:777",
                lrc = "[00:01.00]1983年小巷",
                yrc = "[1000,600](1000,300,0)1983(1300,300,0)年",
                tlyric = "[00:01.00]Alley of 1983",
            )
            val cachedLyrics = repo.cachedLyrics("netease:777")
            assertNotNull(cachedLyrics)
            assertEquals("1983年", cachedLyrics!!.lines.single().text)
            assertEquals(2, cachedLyrics.lines.single().words.size)

            repo.recordSearchQuery("u1", "周杰伦")
            repo.recordSearchQuery("u1", "Debussy")
            assertEquals(listOf("Debussy", "周杰伦"), repo.searchHistory("u1"))
            repo.deleteSearchQuery("u1", "周杰伦")
            assertEquals(listOf("Debussy"), repo.searchHistory("u1"))
        } finally {
            roomDb.close()
            ctx.deleteDatabase(dbName)
        }
    }

    @Test
    fun equalizerControllerPersistsPresetAndBandDbAcrossInstances() {
        val ctx = RuntimeEnvironment.getApplication()
        ctx.getSharedPreferences("pruner_ui_prefs", 0).edit().clear().commit()

        val eq1 = EqualizerController(ctx)
        assertFalse(eq1.state.value.enabled)
        assertTrue(eq1.applyPreset("人声清晰"))
        assertTrue(eq1.state.value.enabled)
        assertEquals("人声清晰", eq1.state.value.presetName)
        assertTrue(eq1.setBandDb(0, 4.5f))
        assertEquals("自定义", eq1.state.value.presetName)
        assertEquals(4.5f, eq1.state.value.bandGainsDb[0], 0.01f)

        // Fresh instance must restore persisted EQ settings
        val eq2 = EqualizerController(ctx)
        assertTrue(eq2.state.value.enabled)
        assertEquals("自定义", eq2.state.value.presetName)
        assertEquals(4.5f, eq2.state.value.bandGainsDb[0], 0.05f)

        // Verify DynamicsProcessing.Config enables stereo 10-band Post-EQ (NOT MBC!)
        val dpConfig = EqualizerController.buildDynamicsConfig(10)
        assertNotNull("Channel 0 must exist", dpConfig.getChannelByChannelIndex(0))
        assertNotNull("Stereo Channel 1 must exist", dpConfig.getChannelByChannelIndex(1))
        assertTrue("Post-EQ must be enabled", dpConfig.isPostEqInUse)
        assertEquals(10, dpConfig.postEqBandCount)
        assertFalse("MBC must NOT be accidentally enabled instead of Post-EQ", dpConfig.isMbcInUse)
        assertTrue("Limiter protects against clipping", dpConfig.isLimiterInUse)
    }

    @Test
    fun audioCacheManagerServesOfflineWithoutUpstreamResolveDistinguishesPartialAndEvictsLan() {
        val ctx = RuntimeEnvironment.getApplication()
        val cacheManager = AudioCacheManager(ctx)
        cacheManager.clearAll()
        val server = MockWebServer().apply { start() }
        try {
            val fullPayload = ByteArray(4096) { ((it * 31) and 0xff).toByte() }
            val lanPayload = ByteArray(1024) { ((it * 17) and 0xff).toByte() }
            server.enqueue(
                MockResponse()
                    .setResponseCode(200)
                    .setBody(Buffer().write(fullPayload))
            )
            server.enqueue(
                MockResponse()
                    .setResponseCode(200)
                    .setBody(Buffer().write(lanPayload))
            )
            server.enqueue(
                MockResponse()
                    .setResponseCode(200)
                    .setBody(Buffer().write(fullPayload))
            )

            val httpFactory = OkHttpDataSource.Factory(OkHttpClient())
            val writerFactory =
                CacheDataSource.Factory()
                    .setCache(cacheManager.cache)
                    .setUpstreamDataSourceFactory(httpFactory)

            // 1. Fully cache a NetEase track & a LAN local track
            val neteaseKey = "netease:999"
            val lanKey = "local:" + "a".repeat(24)
            CacheWriter(
                writerFactory.createDataSource(),
                DataSpec.Builder().setUri(Uri.parse(server.url("/audio/999.flac").toString())).setKey(neteaseKey).build(),
                null,
                null,
            ).cache()
            CacheWriter(
                writerFactory.createDataSource(),
                DataSpec.Builder().setUri(Uri.parse(server.url("/audio/lan.flac").toString())).setKey(lanKey).build(),
                null,
                null,
            ).cache()

            assertTrue("Fully cached track must report cached", cacheManager.isTrackCached(neteaseKey))
            assertTrue("Fully cached LAN track must report cached", cacheManager.isTrackCached(lanKey))

            // 2. Partial cache (only 512 bytes of 4096) must NOT report fully cached!
            val partialKey = "netease:partial"
            CacheWriter(
                writerFactory.createDataSource(),
                DataSpec.Builder()
                    .setUri(Uri.parse(server.url("/audio/partial.flac").toString()))
                    .setKey(partialKey)
                    .setPosition(0L)
                    .setLength(512L)
                    .build(),
                null,
                null,
            ).cache()
            assertFalse("Partial span must NOT falsely report fully cached", cacheManager.isTrackCached(partialKey))

            // 3. Shut down server completely and verify wrapped ResolvingDataSource serves 100% offline with 0 upstream calls!
            server.shutdown()
            val upstreamResolveCount = AtomicInteger(0)
            val resolvingUpstream =
                ResolvingDataSource.Factory(httpFactory) { _ ->
                    upstreamResolveCount.incrementAndGet()
                    throw IOException("Network offline — upstream must not be called on full cache hit")
                }
            val playbackFactory = cacheManager.wrapDataSourceFactory(resolvingUpstream) { null }
            val ds = playbackFactory.createDataSource()
            try {
                val readSpec =
                    DataSpec.Builder()
                        .setUri(Uri.parse("pruner-song://track/999"))
                        .setKey(neteaseKey)
                        .build()
                val opened = ds.open(readSpec)
                assertEquals(4096L, opened)
                val out = ByteArray(4096)
                var pos = 0
                while (pos < out.size) {
                    val n = ds.read(out, pos, out.size - pos)
                    if (n <= 0) break
                    pos += n
                }
                assertEquals(4096, pos)
                assertArrayEquals(fullPayload, out)
                assertEquals("Full disk cache hit must NOT invoke upstream resolver", 0, upstreamResolveCount.get())
            } finally {
                ds.close()
            }

            // 4. Evicting LAN entries clears local:* while keeping netease:* intact
            cacheManager.evictLanEntries()
            assertFalse("evictLanEntries must remove local:* entry", cacheManager.isTrackCached(lanKey))
            assertTrue("evictLanEntries must preserve non-LAN cache", cacheManager.isTrackCached(neteaseKey))
        } finally {
            runCatching { server.shutdown() }
            cacheManager.clearAll()
            cacheManager.release()
        }
    }
}
