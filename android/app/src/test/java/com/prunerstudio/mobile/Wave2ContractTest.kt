package com.prunerstudio.mobile

import android.net.Uri
import android.os.Bundle
import android.os.Process
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.FileDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheWriter
import androidx.media3.session.MediaSession
import androidx.media3.session.SessionError
import androidx.room.Room
import com.prunerstudio.mobile.core.db.PrunerDatabase
import com.prunerstudio.mobile.core.db.PrunerRepository
import com.prunerstudio.mobile.core.lan.LanSyncClient
import com.prunerstudio.mobile.core.lan.PinnedTlsFactory
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.*
import com.prunerstudio.mobile.core.player.*
import com.prunerstudio.mobile.core.sources.*
import com.prunerstudio.mobile.ui.theme.resolveCoverRequestUrl
import java.io.File
import java.io.IOException
import java.net.InetAddress
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import okhttp3.Dns
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import okio.Buffer
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
@OptIn(UnstableApi::class)
class Wave2ContractTest {
    private val application get() = RuntimeEnvironment.getApplication()
    private val servers = mutableListOf<MockWebServer>()

    @After
    fun stopServers() {
        servers.forEach { it.shutdown() }
    }

    private fun server() = MockWebServer().apply { start(); servers.add(this) }

    private suspend fun withDatabase(block: suspend (PrunerDatabase) -> Unit) {
        val database = Room.inMemoryDatabaseBuilder(application, PrunerDatabase::class.java).build()
        try {
            block(database)
        } finally {
            database.close()
        }
    }

    private fun lanServer(
        store: InMemoryCredentialStore,
    ): Pair<MockWebServer, LanSyncClient> {
        val certificate = HeldCertificate.Builder().commonName("desktop-test").build()
        val server = MockWebServer().apply {
            useHttps(HandshakeCertificates.Builder().heldCertificate(certificate).build().sslSocketFactory(), false)
            start()
            servers.add(this)
        }
        val config = PairedServerConfig(
            server.hostName, server.port, PinnedTlsFactory.computeCertFingerprint(certificate.certificate),
            "test-token", "test-device", "owner",
        )
        store.pairing = config
        return server to LanSyncClient(config, store)
    }

    @Test
    fun exportedServiceRejectsUntrustedControllerAndHeadlessLikeIsUnsupported() {
        val controller = Robolectric.buildService(PrunerPlaybackService::class.java).create()
        try {
            val foreign = MediaSession.ControllerInfo.createTestOnlyControllerInfo(
                "untrusted.test", 1, Process.myUid() + 1, 1, 0, false, Bundle.EMPTY,
            )
            assertNull(controller.get().onGetSession(foreign))
            assertFalse(controllerHasPrivateMediaAccess(Process.myUid() + 1, Process.myUid(), "com.android.systemui", false))
            assertEquals(SessionError.ERROR_NOT_SUPPORTED, dispatchNotificationLike(null))
            var called = false
            assertEquals(0, dispatchNotificationLike { called = true })
            assertTrue(called)
        } finally {
            controller.destroy()
        }
    }

    @Test
    fun crossOriginWebDavEntriesAreFilteredAndStreamHeadersAreRechecked() = runBlocking {
        val dav = server()
        val foreign = server()
        val store = InMemoryCredentialStore()
        withDatabase { db ->
            val resolver = CompositeAudioSourceResolver(PrunerRepository(db), store, NeteaseApiClient(store), { null })
            val config = ExternalSourceConfig("dav", "WebDAV", ExternalSourceKind.WEBDAV, dav.url("/music/").toString(),
                username = "owner", allowLanHttp = true)
            withContext(Dispatchers.IO) { resolver.saveSource(config, "dav-secret") }
            dav.enqueue(MockResponse().setResponseCode(207).setBody(
                "<D:multistatus><D:href>/music/song.flac</D:href><D:href>${foreign.url("/song.flac")}</D:href></D:multistatus>"
            ))
            val safe = resolver.search("song").single()
            assertEquals(dav.url("/music/song.flac").toString(), safe.stream_url)
            val denied = runCatching {
                resolver.resolveStream(safe.copy(stream_url = foreign.url("/song.flac").toString()), AudioQualityLevel.lossless)
            }
            assertTrue(denied.isFailure)
            val resolved = resolver.resolveStream(safe, AudioQualityLevel.lossless)
            val access = requireNotNull(resolver.accessForStream(Uri.parse(resolved.uri)))
            val routing = RoutingDataSourceFactory(application, { null }, streamAccess = { access })
            val dataSource = routing.createDataSource()
            try {
                assertTrue(runCatching { dataSource.open(DataSpec(Uri.parse(foreign.url("/song.flac").toString()))) }.isFailure)
                assertEquals(0, foreign.requestCount)
            } finally {
                dataSource.close()
            }
        }
    }

    @Test
    fun resolverApiAndPlaybackRejectPrivateDnsAnswersBeforeConnecting() {
        val local = server()
        val privateIp = InetAddress.getByName("127.0.0.1")
        val mixed = object : Dns {
            override fun lookup(hostname: String) = listOf(InetAddress.getByName("8.8.8.8"), privateIp)
        }
        val publicUrl = "https://resolver.example:${local.port}/audio".toHttpUrl()
        val policy = ResolverDestinationPolicy()
        val transport = OkHttpClient.Builder().dns(mixed).build()
        assertThrows(UnknownHostException::class.java) {
            policy.client(transport).newCall(Request.Builder().url(publicUrl).build()).execute().close()
        }
        val routing = RoutingDataSourceFactory(application, { null }, streamAccess = {
            StreamAccess(emptyMap(), resolverPolicy = policy)
        }, baseHttp = transport)
        val source = routing.createDataSource()
        try {
            assertTrue(runCatching { source.open(DataSpec(Uri.parse(publicUrl.toString()))) }.isFailure)
        } finally {
            source.close()
        }
        listOf("::ffff:192.0.2.1", "100.64.0.1", "192.168.1.1", "169.254.1.1", "2001:db8::1", "ff00::1").forEach {
            assertFalse(it, ResolverDestinationPolicy.isGlobalAddress(InetAddress.getByName(it)))
        }
        assertThrows(IOException::class.java) {
            ResolverDestinationPolicy.forConfig("http://192.168.1.2", true)
        }
        assertThrows(IOException::class.java) {
            policy.validate("https://127.0.0.1/".toHttpUrl())
        }
        assertEquals(0, local.requestCount)
    }

    @Test
    fun completedAudioIsScopedByQualityAndRevokedProxyEntriesAreInaccessibleAndEvicted(): Unit = runBlocking {
        val store = InMemoryCredentialStore()
        val (_, lan) = lanServer(store)
        var active: LanSyncClient? = lan
        val cache = AudioCacheManager(application)
        val data = File.createTempFile("wave2-audio", ".flac", application.cacheDir).apply {
            writeBytes(byteArrayOf(4, 5, 6))
        }
        try {
            val scope = AudioCacheKey.desktopNamespace(lan.config)
            val track = Track(provider = "gdstudio:kuwo", external_id = "123", source_id = "lan_desktop")
            val standard = AudioCacheKey.logical("owner", track, AudioQualityLevel.standard, scope)
            val lossless = AudioCacheKey.logical("owner", track, AudioQualityLevel.lossless, scope)
            val nextPairing = AudioCacheKey.desktopNamespace(lan.config.copy(deviceId = "another-device"))
            assertNotEquals(standard, lossless)
            assertNotEquals(lossless, AudioCacheKey.logical("owner", track, AudioQualityLevel.lossless, nextPairing))
            assertNotEquals(AudioCacheKey.attempt(standard), AudioCacheKey.attempt(standard))
            val direct = Track(id = 1)
            val items = buildQueueItems("owner", listOf(track, direct), PlayMode.order) { lan }
            val player = androidx.media3.exoplayer.ExoPlayer.Builder(application).build()
            try {
                player.setMediaItems(items)
                player.playWhenReady = true
                player.removeStaleDesktopQueueEntries(null)
                assertFalse(player.playWhenReady)
                assertEquals(direct.identity(), player.getMediaItemAt(0).mediaId)
                assertEquals(1, player.mediaItemCount)
            } finally {
                player.release()
            }
            val attempt = AudioCacheKey.attempt(standard)
            val fileSpec = DataSpec.Builder().setUri(Uri.fromFile(data)).setKey(attempt).build()
            val writerSource = CacheDataSource.Factory().setCache(cache.cache)
                .setUpstreamDataSourceFactory(FileDataSource.Factory()).createDataSource()
            CacheWriter(writerSource, fileSpec, null, null).cache()
            assertTrue(cache.isTrackCached(standard))
            assertFalse(cache.isTrackCached(lossless))
            val upstream = androidx.media3.datasource.DataSource.Factory { error("Full hit has no upstream") }
            val playback = cache.wrapDataSourceFactory(upstream) { active }.createDataSource()
            val onlineSpec = DataSpec.Builder().setUri("pruner-track://gdstudio/123").setKey(standard).build()
            assertEquals(3L, playback.open(onlineSpec))
            val buffer = ByteArray(3)
            assertEquals(1, playback.read(buffer, 0, 1))
            lan.close()
            active = null
            assertThrows(IOException::class.java) { playback.read(buffer, 0, buffer.size) }
            playback.close()
            cache.evictLanEntries(scope)
            assertFalse(cache.isTrackCached(standard))
            assertThrows(IOException::class.java) { playback.open(onlineSpec) }
        } finally {
            lan.close()
            cache.clearAll()
            cache.release()
            data.delete()
        }
    }

    @Test
    fun offlineCacheCancellationClosesTheInFlightHttpCall() = runBlocking {
        val server = server()
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        val routing = RoutingDataSourceFactory(application, { null }, allowExternalUri = { true })
        val transfer = routing.createDataSource()
        val cache = AudioCacheManager(application)
        try {
            val sink = CacheDataSource.Factory().setCache(cache.cache)
                .setUpstreamDataSourceFactory { transfer }.createDataSource()
            val spec = DataSpec.Builder().setUri(server.url("/waiting.flac").toString()).setKey("cancelled").build()
            val writer = CacheWriter(sink, spec, null, null)
            val job = launch {
                runCancellableCacheTransfer(writer::cache) {
                    writer.cancel()
                    (transfer as CancellableTransfer).cancelTransfer()
                }
            }
            val request = withContext(Dispatchers.IO) { server.takeRequest(5, TimeUnit.SECONDS) }
            assertNotNull(request)
            withTimeout(5_000) { job.cancelAndJoin() }
            assertFalse(cache.isTrackCached("cancelled"))
        } finally {
            cache.clearAll()
            cache.release()
        }
    }

    @Test
    fun playlistOfflineCountReflectsLruEvictionAtEndOfBatch() = runBlocking {
        val server = server()
        val store = InMemoryCredentialStore()
        withDatabase { db ->
            val resolver = CompositeAudioSourceResolver(PrunerRepository(db), store, NeteaseApiClient(store), { null })
            val config = ExternalSourceConfig(
                "dav", "WebDAV", ExternalSourceKind.WEBDAV, server.url("/music/").toString(), allowLanHttp = true,
            )
            withContext(Dispatchers.IO) { resolver.saveSource(config, null) }
            val tracks = listOf("first.flac", "second.flac").map {
                server.enqueue(MockResponse().setBody(Buffer().write(ByteArray(4096))))
                Track(provider = "webdav", external_id = it, source_id = "dav", stream_url = server.url("/music/$it").toString())
            }
            val cache = AudioCacheManager(application, maxCacheBytes = 5_000)
            val routing = RoutingDataSourceFactory(application, { null }, streamAccess = resolver::accessForStream)
            try {
                assertEquals(
                    1 to 1,
                    cache.cacheTracksOffline("list", tracks, AudioQualityLevel.lossless, resolver, routing, allowCellular = true),
                )
                assertEquals(2, server.requestCount)
            } finally {
                cache.clearAll()
                cache.release()
            }
        }
    }

    @Test
    fun legacyResolverHandoffNormalizesIdentityAndPoisonedBatchStillPulls() = runBlocking {
        val store = InMemoryCredentialStore()
        val (server, client) = lanServer(store)
        val legacy = Track(provider = "resolver_v2", source_id = "archive", external_id = "t:1",
            track_key = "resolver_v2:archive:t:1")
        server.enqueue(MockResponse().setBody("""{"track":{"provider":"resolver_v2:archive","external_id":"t:1"}}"""))
        client.pushHandoff(HandoffSnapshot(track = legacy, queue = listOf(legacy)))
        val handoff = wire.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        val sent = handoff.getValue("track").jsonObject
        assertEquals("resolver_v2:archive", sent["provider"]?.jsonPrimitive?.content)
        assertEquals("resolver_v2:archive:t:1", sent.getValue("track_key").jsonPrimitive.content)
        val malformedAcknowledgement = java.util.concurrent.atomic.AtomicBoolean()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse =
                if (request.path.orEmpty().endsWith("/sync/pull")) {
                    MockResponse().setBody("""{"sync_until":"2026-01-01T00:00:00Z","account_id":"owner","server_time":"2026-01-01T00:00:00Z","history_has_more":false,"feedback_has_more":false,"play_history":[],"feedback":[]}""")
                } else {
                    val body = request.body.readUtf8()
                    if ("poison" in body) MockResponse().setResponseCode(400).setBody("""{"error":"INVALID_INPUT"}""")
                    else if (malformedAcknowledgement.getAndSet(false)) MockResponse().setBody("{}")
                    else MockResponse().setBody("""{"accepted_events":1,"accepted_feedback":0,"session_updated":false,"server_time":"2026-01-01T00:00:00Z"}""")
                }
        }
        withDatabase { db ->
            val repository = PrunerRepository(db)
            withContext(Dispatchers.IO) {
                repository.record("owner", PlayEventPayload("poison", legacy, 30_000, false))
                repository.record("owner", PlayEventPayload("good", legacy, 30_000, false))
            }
            assertEquals("owner", repository.syncWithDesktop(client).account_id)
            assertEquals(5, server.requestCount)
            assertEquals("owner", repository.syncWithDesktop(client).account_id)
            assertEquals(6, server.requestCount)
            withContext(Dispatchers.IO) {
                repository.record("owner", PlayEventPayload("unconfirmed", legacy, 30_000, false))
            }
            malformedAcknowledgement.set(true)
            assertTrue(runCatching { repository.syncWithDesktop(client) }.isFailure)
            assertEquals(7, server.requestCount)
            assertEquals("owner", repository.syncWithDesktop(client).account_id)
            assertEquals(9, server.requestCount)
        }
        client.close()
    }

    @Test
    fun handoffKeepsPausedStateAndSeekPosition() = runBlocking {
        val track = Track(provider = "resolver:archive", external_id = "1")
        val snapshot = HandoffSnapshot(track = track, position_sec = 11.5, is_playing = false)
        val queue = prepareHandoffQueue(snapshot, "owner")
        assertEquals("lan_desktop", queue.tracks.single().source_id)
        applyHandoffPlayback(snapshot, queue) { index, ms, play ->
            assertEquals(0, index)
            assertEquals(11_500L, ms)
            assertFalse(play)
        }
    }

    @Test
    fun queueCommandsStillTargetTheSameOccurrenceAfterEarlierMoves() = runBlocking {
        val duplicate = Track(id = 1)
        val items = buildQueueItems("owner", listOf(duplicate, Track(id = 2), duplicate), PlayMode.order) { null }
        val first = requireNotNull(items[0].queueEntryId())
        val last = requireNotNull(items[2].queueEntryId())
        val player = androidx.media3.exoplayer.ExoPlayer.Builder(application).build()
        try {
            player.setMediaItems(items)
            player.moveQueueEntry(last, -2)
            player.moveQueueEntry(last, 1)
            player.removeQueueEntry(first)
            assertEquals(0, player.queueIndex(last))
            assertNull(player.queueIndex(first))
            assertEquals(listOf("netease:1", "netease:2"),
                (0 until player.mediaItemCount).map { player.getMediaItemAt(it).mediaId })
        } finally {
            player.release()
        }
    }

    @Test
    fun corruptEncryptedPreferencesAndCoverCompositionFailClosed() {
        val prefs = application.getSharedPreferences("credentials", 0)
        prefs.edit().putString("pairing", "%%%").putString("cookie", "%%%").commit()
        try {
            val credentials = AndroidKeystoreCredentialStore(application)
            assertNull(credentials.pairing)
            assertNull(credentials.cookie)
        } finally {
            prefs.edit().clear().commit()
        }
        val store = object : SecureCredentialStore by InMemoryCredentialStore() {
            override var pairing: PairedServerConfig?
                get() = error("Cover path read encrypted pairing preferences")
                set(_) = error("Unexpected write")
        }
        val app = PrunerAppContainer(application, store)
        assertNull(resolveCoverRequestUrl("/api/lan/v1/cover?local_cover_id=1", 96, application, app))
        assertNotNull(resolveCoverRequestUrl("https://p1.music.126.net/cover.jpg", 96, application, app))
    }
}
