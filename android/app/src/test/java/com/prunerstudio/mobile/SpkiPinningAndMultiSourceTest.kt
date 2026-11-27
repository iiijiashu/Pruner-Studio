package com.prunerstudio.mobile

import android.net.Uri
import androidx.media3.common.PlaybackException
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import com.prunerstudio.mobile.core.db.PrunerDatabase
import com.prunerstudio.mobile.core.db.PrunerRepository
import com.prunerstudio.mobile.core.lan.LanPairingUriParser
import com.prunerstudio.mobile.core.lan.PinnedTlsFactory
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.InMemoryCredentialStore
import com.prunerstudio.mobile.core.netease.NeteaseApiClient
import com.prunerstudio.mobile.core.player.AutoRetryAction
import com.prunerstudio.mobile.core.player.PrunerAppContainer
import com.prunerstudio.mobile.core.player.PrunerPlaybackService
import com.prunerstudio.mobile.core.player.buildQueueItems
import com.prunerstudio.mobile.core.player.classifyPlaybackError
import com.prunerstudio.mobile.core.player.filterPlayableQueue
import com.prunerstudio.mobile.core.player.isTrackPlayable
import com.prunerstudio.mobile.core.player.prepareRestorableQueue
import com.prunerstudio.mobile.core.player.requiresLanPairing
import com.prunerstudio.mobile.core.player.shouldAutoRetryOnce
import com.prunerstudio.mobile.core.sources.CompositeAudioSourceResolver
import com.prunerstudio.mobile.core.sources.ExternalUrlValidator
import com.prunerstudio.mobile.ui.state.track
import java.net.ConnectException
import java.security.KeyPairGenerator
import java.security.spec.ECGenParameterSpec
import javax.net.ssl.SSLHandshakeException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@UnstableApi
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SpkiPinningAndMultiSourceTest {
    private val servers = mutableListOf<MockWebServer>()

    @After
    fun tearDown() {
        servers.forEach { runCatching { it.shutdown() } }
        servers.clear()
    }

    @Test
    fun spkiPinSurvivesDesktopCertSanRotationAndRejectsDifferentKeypair() {
        val kpGen = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }
        val sameKeyPair = kpGen.generateKeyPair()
        val otherKeyPair = kpGen.generateKeyPair()

        // Cert A and Cert B share the exact same EC P-256 keypair (just like lan_tls.py on SAN change)
        val certA =
            HeldCertificate.Builder()
                .keyPair(sameKeyPair)
                .commonName("Pruner Studio LAN")
                .addSubjectAlternativeName("127.0.0.1")
                .serialNumber(1001L)
                .build()
        val certB =
            HeldCertificate.Builder()
                .keyPair(sameKeyPair)
                .commonName("Pruner Studio LAN")
                .addSubjectAlternativeName("127.0.0.1")
                .addSubjectAlternativeName("192.168.1.42")
                .serialNumber(2002L)
                .build()
        val rogueCert =
            HeldCertificate.Builder()
                .keyPair(otherKeyPair)
                .commonName("Rogue")
                .serialNumber(3003L)
                .build()

        val fpA = PinnedTlsFactory.computeCertFingerprint(certA.certificate)
        val fpB = PinnedTlsFactory.computeCertFingerprint(certB.certificate)
        assertNotEquals("Rotated cert must have a different leaf cert SHA-256", fpA, fpB)

        val spkiHex = PinnedTlsFactory.computeSpkiPin(certA.certificate)
        val spkiB64 = PinnedTlsFactory.computeSpkiBase64Pin(certA.certificate)
        assertEquals(spkiHex, PinnedTlsFactory.computeSpkiPin(certB.certificate))
        assertEquals(spkiB64, PinnedTlsFactory.computeSpkiBase64Pin(certB.certificate))
        assertTrue(spkiHex.startsWith("spki-sha256:"))
        assertTrue(spkiB64.startsWith("sha256/"))

        // Start MockWebServer serving rotated certB
        val serverB = MockWebServer().apply {
            protocols = listOf(Protocol.HTTP_1_1)
            useHttps(HandshakeCertificates.Builder().heldCertificate(certB).build().sslSocketFactory(), false)
            start()
        }
        servers += serverB
        serverB.enqueue(MockResponse().setBody("ok-hex"))
        serverB.enqueue(MockResponse().setBody("ok-b64"))

        // Legacy leaf-cert pin for certA MUST reject rotated certB
        assertThrows(SSLHandshakeException::class.java) {
            PinnedTlsFactory(fpA)
                .clientBuilder()
                .build()
                .newCall(Request.Builder().url(serverB.url("/ping")).build())
                .execute()
                .close()
        }

        // SPKI hex pin (with legacy fpA fallback field) MUST accept rotated certB
        PinnedTlsFactory(fpA, spkiPin = spkiHex)
            .clientBuilder()
            .build()
            .newCall(Request.Builder().url(serverB.url("/ping")).build())
            .execute()
            .use { assertEquals("ok-hex", it.body!!.string()) }

        // RFC 7469 Base64 SPKI pin MUST also accept rotated certB
        PinnedTlsFactory(spkiB64)
            .clientBuilder()
            .build()
            .newCall(Request.Builder().url(serverB.url("/ping")).build())
            .execute()
            .use { assertEquals("ok-b64", it.body!!.string()) }

        // Rogue cert with different public key MUST be rejected
        val rogueServer = MockWebServer().apply {
            protocols = listOf(Protocol.HTTP_1_1)
            useHttps(HandshakeCertificates.Builder().heldCertificate(rogueCert).build().sslSocketFactory(), false)
            start()
        }
        servers += rogueServer
        assertThrows(SSLHandshakeException::class.java) {
            PinnedTlsFactory(fpA, spkiPin = spkiHex)
                .clientBuilder()
                .build()
                .newCall(Request.Builder().url(rogueServer.url("/ping")).build())
                .execute()
                .close()
        }

        // Pairing URI parser parses optional spki query param (both prefixed, RFC 7469 Base64, AND desktop raw 64-hex!)
        val uri =
            "pruner-lan://pair?v=1&tls=1&host=192.168.1.42&port=19212&code=123456&fp=$fpA&spki=$spkiHex&name=Studio"
        val parsed = LanPairingUriParser.parse(uri)
        assertEquals(spkiHex, parsed.spkiPin)
        val rawSpkiHex64 = spkiHex.removePrefix("spki-sha256:")
        val desktopRealUri =
            "pruner-lan://pair?v=1&tls=1&host=192.168.1.42&port=19212&code=123456&fp=$fpA&spki=$rawSpkiHex64&name=Desktop"
        assertEquals(spkiHex, LanPairingUriParser.parse(desktopRealUri).spkiPin)
        val uriB64 =
            "pruner-lan://pair?v=1&tls=1&host=192.168.1.42&port=19212&code=123456&fp=$fpA&spki=${Uri.encode(spkiB64)}"
        assertEquals(spkiHex, LanPairingUriParser.parse(uriB64).spkiPin)
        assertThrows(Exception::class.java) {
            LanPairingUriParser.parse(
                "pruner-lan://pair?v=1&tls=1&host=192.168.1.42&port=19212&code=123456&fp=$fpA&spki=badpin"
            )
        }
    }

    @Test
    fun externalUrlValidatorBlocksPublicHttpAndAllowsPrivateAddressesWithoutDns() {
        assertThrows(AudioResolutionException::class.java) {
            ExternalUrlValidator.validateBaseUrl("http://music.example.com", allowLanHttp = true)
        }
        assertThrows(AudioResolutionException::class.java) {
            ExternalUrlValidator.validateBaseUrl("http://192.168.1.20:4533", allowLanHttp = false)
        }
        assertThrows(AudioResolutionException::class.java) {
            ExternalUrlValidator.validateBaseUrl("http://8.8.8.8:8080", allowLanHttp = true)
        }
        assertThrows(AudioResolutionException::class.java) {
            ExternalUrlValidator.validateBaseUrl("http://172.32.0.1:8080", allowLanHttp = true)
        }
        val lanOk = ExternalUrlValidator.validateBaseUrl("http://192.168.1.20:4533", allowLanHttp = true)
        assertEquals("192.168.1.20", lanOk.host)
        assertTrue(ExternalUrlValidator.isPrivateOrLocalHost("127.0.0.1"))
        assertTrue(ExternalUrlValidator.isPrivateOrLocalHost("10.0.0.8"))
        assertTrue(ExternalUrlValidator.isPrivateOrLocalHost("172.16.5.2"))
        assertTrue(ExternalUrlValidator.isPrivateOrLocalHost("::1"))
        assertTrue(ExternalUrlValidator.isPrivateOrLocalHost("fe80::1"))
        assertTrue(ExternalUrlValidator.isPrivateOrLocalHost("nas.local"))
        assertFalse(ExternalUrlValidator.isPrivateOrLocalHost("music.example.com"))
        val httpsOk = ExternalUrlValidator.validateBaseUrl("https://navidrome.example.com", allowLanHttp = false)
        assertEquals("https", httpsOk.scheme)
    }

    @Test
    fun multiSourceResolverMatchesHttpResolverV2SpecCircuitBreakerAndDesktopProxy() = runBlocking {
        val server = MockWebServer().apply { start() }
        servers += server
        val baseUrl = "http://127.0.0.1:${server.port}"
        var clockMs = 1_000_000L

        val ctx = RuntimeEnvironment.getApplication()
        val db = PrunerDatabase.create(ctx, name = "multi_source_test.db", allowMainThread = true)
        val repo = PrunerRepository(db)
        val creds = InMemoryCredentialStore()
        val resolver =
            CompositeAudioSourceResolver(
                repository = repo,
                credentials = creds,
                netease = NeteaseApiClient(creds),
                lanProvider = { null },
                nowMs = { clockMs },
            )
        try {
            // 1. Spec-compliant HTTP Resolver v2 (/manifest, POST /search, POST /resolve candidates[], POST /lyric)
            val r2Cfg =
                ExternalSourceConfig(
                    id = "r2",
                    name = "Local Resolver",
                    kind = ExternalSourceKind.RESOLVER_V2,
                    baseUrl = baseUrl,
                    allowLanHttp = true,
                )
            resolver.saveSource(r2Cfg, "r2-secret")

            // GET /manifest health probe
            server.enqueue(
                MockResponse().setBody(
                    """{"protocol_version":2,"id":"sidecar","name":"Sidecar","version":"2.0.0","capabilities":["search","resolve","lyric"]}"""
                )
            )
            val probeMsg = resolver.testSource(r2Cfg)
            assertTrue(probeMsg.contains("Sidecar"))
            val manifestReq = server.takeRequest()
            assertEquals("GET", manifestReq.method)
            assertEquals("/manifest", manifestReq.path)
            assertEquals("r2-secret", manifestReq.getHeader("X-Resolver-Token"))
            assertEquals("r2-secret", manifestReq.getHeader("X-API-Key"))
            assertNull("Never leak NetEase cookies to external resolvers", manifestReq.getHeader("Cookie"))

            // POST /search with spec "artists" array
            server.enqueue(
                MockResponse().setBody(
                    """{"has_more":false,"tracks":[{"id":"abc","title":"晴天","artists":["周杰伦"],"album":"叶惠美","duration_ms":269000}]}"""
                )
            )
            val r2Hits = resolver.search("晴天")
            assertEquals(1, r2Hits.size)
            val r2Track = r2Hits.single()
            assertEquals("resolver_v2:r2", r2Track.provider)
            assertEquals("resolver_v2:r2:abc", r2Track.identity())
            assertEquals("周杰伦", r2Track.artist)
            assertEquals(listOf("周杰伦"), r2Track.artists)
            val r2SearchReq = server.takeRequest()
            assertEquals("POST", r2SearchReq.method)
            assertEquals("/search", r2SearchReq.path)
            assertEquals("r2-secret", r2SearchReq.getHeader("X-Resolver-Token"))
            assertEquals("r2-secret", r2SearchReq.getHeader("X-API-Key"))

            // POST /resolve returning spec candidates[] array
            server.enqueue(
                MockResponse().setBody(
                    """{"candidates":[{"id":"abc","url":"$baseUrl/audio/abc.flac","format":"flac","bitrate":980000,"headers":{"X-Custom":"ok"}}]}"""
                )
            )
            val resolvedR2 = resolver.resolveStream(r2Track, AudioQualityLevel.lossless)
            assertEquals("$baseUrl/audio/abc.flac", resolvedR2.uri)
            assertEquals("audio/flac", resolvedR2.mimeType)
            assertEquals("ok", resolver.headersForStream(Uri.parse(resolvedR2.uri))?.get("X-Custom"))
            assertTrue(resolver.isAuthorizedExternalStream(Uri.parse(resolvedR2.uri)))
            val resolveReq = server.takeRequest()
            assertEquals("POST", resolveReq.method)
            assertEquals("/resolve", resolveReq.path)

            // POST /lyric returning spec lyrics
            server.enqueue(
                MockResponse().setBody(
                    """{"lrc":"[00:01.00]故事的小黄花","tlyric":"[00:01.00]Little yellow flower"}"""
                )
            )
            val r2Lyrics = resolver.fetchExternalLyrics(r2Track)
            assertNotNull(r2Lyrics)
            assertTrue(r2Lyrics!!.lrc.contains("故事的小黄花"))
            val lyricReq = server.takeRequest()
            assertEquals("POST", lyricReq.method)
            assertEquals("/lyric", lyricReq.path)

            // Circuit breaker opens after 3 consecutive failures and blocks further calls during cooldown
            repeat(3) { server.enqueue(MockResponse().setResponseCode(500)) }
            repeat(3) {
                assertThrows(AudioResolutionException::class.java) {
                    runBlocking { resolver.resolveStream(r2Track, AudioQualityLevel.lossless) }
                }
                server.takeRequest()
            }
            assertTrue("Circuit breaker must open after 3 failures", resolver.isSourceCircuitOpen("r2"))
            val reqBeforeCircuit = server.requestCount
            assertThrows(AudioResolutionException::class.java) {
                runBlocking { resolver.resolveStream(r2Track, AudioQualityLevel.lossless) }
            }
            assertEquals("Open circuit must fail fast locally", reqBeforeCircuit, server.requestCount)
            clockMs += 31_000L
            assertFalse("Circuit must close after 30s cooldown", resolver.isSourceCircuitOpen("r2"))

            // 2. OpenSubsonic search + stream + lyrics
            val subCfg =
                ExternalSourceConfig(
                    id = "sub1",
                    name = "Navidrome",
                    kind = ExternalSourceKind.OPENSUBSONIC,
                    baseUrl = baseUrl,
                    username = "alice",
                    allowLanHttp = true,
                )
            resolver.deleteSource("r2")
            resolver.saveSource(subCfg, "s3cret")
            server.enqueue(
                MockResponse().setBody(
                    """{"subsonic-response":{"status":"ok","version":"1.16.1","searchResult3":{"song":[{"id":"s42","title":"Moonlight","artist":"Debussy","album":"Suite","duration":240,"coverArt":"cov1"}]}}}"""
                )
            )
            val subHits = resolver.search("Moonlight")
            assertEquals(1, subHits.size)
            val subTrack = subHits.single()
            assertEquals("subsonic", subTrack.provider)
            assertTrue(subTrack.cover_url.contains("getCoverArt.view"))
            val searchReq = server.takeRequest()
            assertEquals("alice", searchReq.requestUrl?.queryParameter("u"))
            assertNotNull(searchReq.requestUrl?.queryParameter("t"))
            assertNotNull(searchReq.requestUrl?.queryParameter("s"))

            val subStream = resolver.resolveStream(subTrack, AudioQualityLevel.lossless)
            assertTrue(subStream.uri.contains("/rest/stream.view"))
            assertTrue(subStream.uri.contains("id=s42"))

            // 3. WebDAV search + Basic auth stream
            resolver.deleteSource("sub1")
            val davCfg =
                ExternalSourceConfig(
                    id = "dav1",
                    name = "NAS WebDAV",
                    kind = ExternalSourceKind.WEBDAV,
                    baseUrl = baseUrl,
                    username = "bob",
                    allowLanHttp = true,
                )
            resolver.saveSource(davCfg, "davpass")
            server.enqueue(
                MockResponse()
                    .setResponseCode(207)
                    .setBody(
                        """<D:multistatus xmlns:D="DAV:"><D:response><D:href>/music/Nocturne.flac</D:href></D:response></D:multistatus>"""
                    )
            )
            val davHits = resolver.search("Nocturne")
            assertEquals(1, davHits.size)
            val davTrack = davHits.single()
            assertEquals("webdav", davTrack.provider)
            val davStream = resolver.resolveStream(davTrack, AudioQualityLevel.lossless)
            assertTrue(davStream.headers["Authorization"].orEmpty().startsWith("Basic "))

            // 4. Multi-source queue playability + paired desktop proxy stream (/api/lan/v1/audio/stream/{id})
            val desktopProxyTrack =
                Track(
                    provider = "qq",
                    external_id = "001abc",
                    track_key = "qq:001abc",
                    title = "Cross-Platform Handoff",
                    stream_url = "/api/lan/v1/audio/stream/token1234567890",
                )
            assertTrue(isTrackPlayable(desktopProxyTrack, "acct", "acct"))
            assertFalse(isTrackPlayable(desktopProxyTrack, "acct", null))
            assertTrue(isTrackPlayable(r2Track, "acct", null))
            assertTrue(isTrackPlayable(subTrack, "acct", null))
            assertTrue(isTrackPlayable(davTrack, "acct", null))
            val items = buildQueueItems("acct", listOf(subTrack, davTrack), PlayMode.order) { null }
            assertEquals(2, items.size)
            assertEquals("Suite", items[0].mediaMetadata.albumTitle)
            assertEquals(240_000L, items[0].mediaMetadata.durationMs)
            assertEquals(
                com.prunerstudio.mobile.core.player.AudioCacheKey.logical("acct", subTrack, AudioQualityLevel.jymaster),
                items[0].localConfiguration?.customCacheKey,
            )
            assertTrue(items[0].localConfiguration?.uri.toString().startsWith("pruner-track://subsonic/"))
        } finally {
            db.close()
            ctx.deleteDatabase("multi_source_test.db")
        }
    }

    @Test
    fun pairedDesktopProxyResolvesCrossPlatformAudioLyricsAndRelativeCovers() = runBlocking {
        val cert = HeldCertificate.Builder().commonName("desktop-proxy").build()
        val spki = PinnedTlsFactory.computeSpkiPin(cert.certificate)
        val fp = PinnedTlsFactory.computeCertFingerprint(cert.certificate)
        val lanServer =
            MockWebServer().apply {
                protocols = listOf(Protocol.HTTP_1_1)
                useHttps(HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(), false)
                start()
            }
        servers += lanServer

        val ctx = RuntimeEnvironment.getApplication()
        val db = PrunerDatabase.create(ctx, name = "lan_proxy_test.db", allowMainThread = true)
        val repo = PrunerRepository(db)
        val creds = InMemoryCredentialStore()
        val cfg =
            PairedServerConfig(
                host = lanServer.hostName,
                port = lanServer.port,
                fingerprint = fp,
                token = "lan-token",
                deviceId = "dev1",
                accountId = "acct1",
                name = "Desk",
                spki_pin = spki,
            )
        creds.pairing = cfg
        val lanClient = com.prunerstudio.mobile.core.lan.LanSyncClient(cfg, creds)
        val resolver =
            CompositeAudioSourceResolver(
                repository = repo,
                credentials = creds,
                netease = NeteaseApiClient(creds),
                lanProvider = { lanClient },
            )
        try {
            val handoffTrack =
                Track(
                    provider = "bilibili",
                    external_id = "BV1xx411c7mD",
                    track_key = "bilibili:BV1xx411c7mD",
                    title = "Live Session",
                    artist = "Artist",
                )
            lanServer.enqueue(
                MockResponse().setBody(
                    """{"track_key":"bilibili:BV1xx411c7mD","provider":"bilibili","stream_url":"/api/lan/v1/audio/stream/streamToken_998877","format":"flac","bitrate":950000}"""
                )
            )
            val resolved = resolver.resolveStream(handoffTrack, AudioQualityLevel.lossless)
            assertTrue(resolved.requiresLanAuth)
            assertTrue(resolved.uri.endsWith("/api/lan/v1/audio/stream/streamToken_998877"))
            val resolveReq = lanServer.takeRequest()
            assertEquals("POST", resolveReq.method)
            assertEquals("/api/lan/v1/audio/resolve", resolveReq.path)

            // RoutingDataSourceFactory accepts desktop's /api/lan/v1/audio/stream/{stream_id}
            lanServer.enqueue(MockResponse().setBody("FLACDATA"))
            val routing = com.prunerstudio.mobile.core.player.RoutingDataSourceFactory(ctx, { lanClient })
            val ds = routing.createDataSource()
            try {
                val len = ds.open(androidx.media3.datasource.DataSpec(Uri.parse(resolved.uri)))
                assertEquals(8L, len)
                val buf = ByteArray(8)
                assertEquals(8, ds.read(buf, 0, 8))
                assertEquals("FLACDATA", String(buf))
            } finally {
                ds.close()
            }
            val streamReq = lanServer.takeRequest()
            assertEquals("/api/lan/v1/audio/stream/streamToken_998877", streamReq.path)
            assertEquals("Bearer lan-token", streamReq.getHeader("Authorization"))

            // Relative LAN cover resolves against paired desktop origin
            val coverUrl = resolver.resolveLanCoverUrl("/api/lan/v1/cover?local_cover_id=abc123")
            assertNotNull(coverUrl)
            assertTrue(coverUrl!!.startsWith("https://${lanServer.hostName}:${lanServer.port}/api/lan/v1/cover"))
        } finally {
            lanClient.close()
            db.close()
            ctx.deleteDatabase("lan_proxy_test.db")
        }
    }

    @Test
    fun lanLocalFileCoverUrlPropagatesThroughModelTrackRoomAndPinnedCoverFetch() = runBlocking {
        val cert = HeldCertificate.Builder().commonName("desktop-cover").build()
        val spki = PinnedTlsFactory.computeSpkiPin(cert.certificate)
        val fp = PinnedTlsFactory.computeCertFingerprint(cert.certificate)
        val lanServer =
            MockWebServer().apply {
                protocols = listOf(Protocol.HTTP_1_1)
                useHttps(HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(), false)
                start()
            }
        servers += lanServer

        val ctx = RuntimeEnvironment.getApplication()
        val dbName = "lan_local_cover_test.db"
        ctx.deleteDatabase(dbName)
        val db = PrunerDatabase.create(ctx, name = dbName, allowMainThread = false)
        val repo = PrunerRepository(db)
        val creds = InMemoryCredentialStore()
        val cfg =
            PairedServerConfig(
                host = lanServer.hostName,
                port = lanServer.port,
                fingerprint = fp,
                token = "cover-token",
                deviceId = "dev-cover",
                accountId = "acct-cover",
                name = "Studio",
                spki_pin = spki,
            )
        creds.pairing = cfg
        val lanClient = com.prunerstudio.mobile.core.lan.LanSyncClient(cfg, creds)
        val resolver =
            CompositeAudioSourceResolver(
                repository = repo,
                credentials = creds,
                netease = NeteaseApiClient(creds),
                lanProvider = { lanClient },
            )
        val coverId = "0123456789abcdef01234567"
        val fileId1 = "a".repeat(24)
        val fileId2 = "b".repeat(24)
        val relCoverPath = "/api/lan/v1/cover?local_cover_id=$coverId"
        try {
            // 1. LocalPage JSON with cover_url + legacy item omitting cover_url
            lanServer.enqueue(
                MockResponse().setBody(
                    """{
                        "files": [
                            {"file_id":"$fileId1","stream_url":"/api/lan/v1/local-library/stream/$fileId1","title":"Nocturne Op.9","artist":"Chopin","album":"Nocturnes","duration_ms":272000,"format":"flac","bitrate":950000,"mtime":1700000000.0,"cover_url":"$relCoverPath"},
                            {"file_id":"$fileId2","stream_url":"/api/lan/v1/local-library/stream/$fileId2","title":"Plain MP3","artist":"Unknown","album":"","duration_ms":180000}
                        ],
                        "total": 2,
                        "limit": 200,
                        "offset": 0,
                        "has_more": false
                    }"""
                )
            )
            val page = lanClient.fetchLocalLibrary("", 200, 0)
            assertEquals(2, page.files.size)
            assertEquals(relCoverPath, page.files[0].cover_url)
            assertEquals("", page.files[1].cover_url)

            val trackWithCover = page.files[0].track()
            val trackNoCover = page.files[1].track()
            assertEquals("local", trackWithCover.provider)
            assertEquals(fileId1, trackWithCover.file_id)
            assertEquals(relCoverPath, trackWithCover.cover_url)
            assertEquals("", trackNoCover.cover_url)

            // 2. Sync pull persists LanLocalFile.cover_url in Room and restores it cleanly
            lanServer.enqueue(
                MockResponse().setBody(
                    """{
                        "sync_until": "2026-11-26T12:00:00Z",
                        "account_id": "acct-cover",
                        "server_time": "2026-11-26T12:00:00Z",
                        "history_has_more": false,
                        "feedback_has_more": false,
                        "play_history": [],
                        "feedback": [],
                        "local_library": [
                            {"file_id":"$fileId1","stream_url":"/api/lan/v1/local-library/stream/$fileId1","title":"Nocturne Op.9","artist":"Chopin","album":"Nocturnes","duration_ms":272000,"cover_url":"$relCoverPath"}
                        ]
                    }"""
                )
            )
            repo.syncWithDesktop(lanClient, includeCatalog = false, includeLocal = true)
            val stored = withContext(Dispatchers.IO) { repo.localLibrary("acct-cover") }
            assertEquals(1, stored.size)
            assertEquals(relCoverPath, stored.single().cover_url)
            assertEquals(relCoverPath, stored.single().track().cover_url)

            // 3. Relative cover path resolves to pinned HTTPS desktop URL and fetches authenticated bytes
            val resolvedCover = resolver.resolveLanCoverUrl(stored.single().track().cover_url)
            assertNotNull(resolvedCover)
            val parsedCover = resolvedCover!!.toHttpUrl()
            assertTrue(resolver.isAllowedCoverUrl(parsedCover))
            assertEquals("/api/lan/v1/cover", parsedCover.encodedPath)
            assertEquals(coverId, parsedCover.queryParameter("local_cover_id"))

            lanServer.enqueue(
                MockResponse()
                    .setHeader("Content-Type", "image/jpeg")
                    .setBody("JPEG_COVER_BYTES")
            )
            withContext(Dispatchers.IO) {
                lanClient.readClient
                    .newCall(Request.Builder().url(parsedCover).build())
                    .execute()
                    .use { resp ->
                        assertEquals(200, resp.code)
                        assertEquals("JPEG_COVER_BYTES", resp.body!!.string())
                    }
            }
            // Drain earlier requests and check the cover request carried Bearer auth
            var coverReq: RecordedRequest? = null
            repeat(3) {
                val r = lanServer.takeRequest()
                if (r.path?.startsWith("/api/lan/v1/cover") == true) coverReq = r
            }
            assertNotNull("Expected authenticated GET /api/lan/v1/cover request", coverReq)
            assertEquals("/api/lan/v1/cover?local_cover_id=$coverId", coverReq!!.path)
            assertEquals("Bearer cover-token", coverReq!!.getHeader("Authorization"))
        } finally {
            lanClient.close()
            db.close()
            ctx.deleteDatabase(dbName)
        }
    }

    @Test
    fun pairedDesktopSearchMultiIntegratesWithResolverDeduplicatesAndResolvesPlayback() = runBlocking {
        val cert = HeldCertificate.Builder().commonName("desktop-multi").build()
        val spki = PinnedTlsFactory.computeSpkiPin(cert.certificate)
        val fp = PinnedTlsFactory.computeCertFingerprint(cert.certificate)
        val lanServer =
            MockWebServer().apply {
                protocols = listOf(Protocol.HTTP_1_1)
                useHttps(HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(), false)
                start()
            }
        val localSubsonicServer = MockWebServer().apply { start() }
        servers += lanServer
        servers += localSubsonicServer

        val ctx = RuntimeEnvironment.getApplication()
        val dbName = "lan_multi_search_test.db"
        ctx.deleteDatabase(dbName)
        val db = PrunerDatabase.create(ctx, name = dbName, allowMainThread = false)
        val repo = PrunerRepository(db)
        val creds = InMemoryCredentialStore()
        val cfg =
            PairedServerConfig(
                host = lanServer.hostName,
                port = lanServer.port,
                fingerprint = fp,
                token = "multi-token",
                deviceId = "dev-multi",
                accountId = "acct-multi",
                name = "Desk Multi",
                spki_pin = spki,
            )
        creds.pairing = cfg
        val lanClient = com.prunerstudio.mobile.core.lan.LanSyncClient(cfg, creds)
        val resolver =
            CompositeAudioSourceResolver(
                repository = repo,
                credentials = creds,
                netease = NeteaseApiClient(creds),
                lanProvider = { lanClient },
            )
        try {
            // 1. Direct LanSyncClient.searchMulti parses tracks, has_more, channels_queried, channel_errors
            lanServer.enqueue(
                MockResponse().setBody(
                    """{
                        "query": "晴天",
                        "offset": 10,
                        "limit": 20,
                        "has_more": true,
                        "channels_queried": ["bilibili", "opensubsonic", "qq"],
                        "channel_errors": {"qq": "rate limited"},
                        "tracks": [
                            {"provider":"bilibili","external_id":"BV1xx411c7mD","track_key":"bilibili:BV1xx411c7mD","title":"晴天 Live","artists":["周杰伦"],"album":"Live","duration_ms":269000}
                        ]
                    }"""
                )
            )
            val directResp = lanClient.searchMulti("晴天", limit = 20, offset = 10)
            assertTrue(directResp.has_more)
            assertEquals(listOf("bilibili", "opensubsonic", "qq"), directResp.channels_queried)
            assertEquals("rate limited", directResp.channel_errors["qq"])
            assertEquals(1, directResp.tracks.size)
            val directReq = lanServer.takeRequest()
            assertEquals("GET", directReq.method)
            assertEquals("/api/lan/v1/search/multi", directReq.requestUrl?.encodedPath)
            assertEquals("晴天", directReq.requestUrl?.queryParameter("q"))
            assertEquals("20", directReq.requestUrl?.queryParameter("limit"))
            assertEquals("10", directReq.requestUrl?.queryParameter("offset"))
            assertEquals("Bearer multi-token", directReq.getHeader("Authorization"))

            // 2. Paired desktop ONLY (zero Android-local external sources) searches + resolves + streams
            lanServer.enqueue(
                MockResponse().setBody(
                    """{
                        "query": "晴天",
                        "has_more": false,
                        "channels_queried": ["bilibili", "opensubsonic", "resolver:sidecar", "netease"],
                        "channel_errors": {},
                        "tracks": [
                            {"provider":"bilibili","external_id":"BV1xx411c7mD","track_key":"bilibili:BV1xx411c7mD","title":"晴天 Live","artists":["周杰伦"],"duration_ms":269000},
                            {"provider":"opensubsonic","external_id":"desk_os_99","track_key":"opensubsonic:desk_os_99","title":"晴天 FLAC","artist":"周杰伦","cover_url":"/api/lan/v1/cover?opensubsonic_cover_id=cov9","duration_ms":270000},
                            {"provider":"resolver:sidecar","external_id":"sc_77","track_key":"resolver:sidecar:sc_77","title":"晴天 Sidecar","artist":"周杰伦","duration_ms":268000},
                            {"id":186016,"song_id":186016,"provider":"netease","external_id":"186016","track_key":"netease:186016","title":"晴天","artists":["周杰伦"],"duration_ms":269000}
                        ]
                    }"""
                )
            )
            val desktopOnlyHits = resolver.search("晴天", limit = 25)
            assertEquals(4, desktopOnlyHits.size)
            val biliTrack = desktopOnlyHits[0]
            val deskOsTrack = desktopOnlyHits[1]
            val sidecarTrack = desktopOnlyHits[2]
            val deskNeteaseTrack = desktopOnlyHits[3]
            assertEquals("周杰伦", biliTrack.artist)
            assertEquals(CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID, biliTrack.source_id)
            assertEquals("Bilibili", biliTrack.displayProviderLabel())
            assertEquals("HTTP Resolver", sidecarTrack.displayProviderLabel())
            assertTrue(isTrackPlayable(biliTrack, "acct-multi", "acct-multi"))
            assertFalse(isTrackPlayable(biliTrack, "acct-multi", null))
            assertTrue(isTrackPlayable(deskOsTrack, "acct-multi", "acct-multi"))
            assertFalse("Stale desktop opensubsonic track must not claim playable when unpaired", isTrackPlayable(deskOsTrack, "acct-multi", null))
            assertTrue(isTrackPlayable(sidecarTrack, "acct-multi", "acct-multi"))
            assertTrue(isTrackPlayable(deskNeteaseTrack, "acct-multi", "acct-multi"))

            // Queue item construction handles colon providers and desktop netease tracks cleanly
            val queueItems = buildQueueItems("acct-multi", desktopOnlyHits, PlayMode.order) { lanClient }
            assertEquals(4, queueItems.size)
            assertTrue(queueItems[0].localConfiguration!!.uri.toString().startsWith("pruner-track://bilibili/"))
            assertTrue(queueItems[2].localConfiguration!!.uri.toString().startsWith("pruner-track://resolver%3Asidecar/"))
            assertTrue(queueItems[3].localConfiguration!!.uri.toString().startsWith("pruner-track://netease/"))
            lanServer.takeRequest() // consume search/multi

            // Playback resolution + authenticated streaming for desktop Bilibili track
            lanServer.enqueue(
                MockResponse().setBody(
                    """{"track_key":"bilibili:BV1xx411c7mD","provider":"bilibili","stream_url":"/api/lan/v1/audio/stream/biliStream_123456","format":"m4a"}"""
                )
            )
            val biliStream = resolver.resolveStream(biliTrack, AudioQualityLevel.lossless)
            assertTrue(biliStream.requiresLanAuth)
            assertTrue(biliStream.uri.endsWith("/api/lan/v1/audio/stream/biliStream_123456"))
            val resolveBiliReq = lanServer.takeRequest()
            assertEquals("POST", resolveBiliReq.method)
            assertEquals("/api/lan/v1/audio/resolve", resolveBiliReq.path)

            lanServer.enqueue(MockResponse().setBody("M4A_AUDIO"))
            val routing = com.prunerstudio.mobile.core.player.RoutingDataSourceFactory(ctx, { lanClient })
            val ds = routing.createDataSource()
            try {
                assertEquals(9L, ds.open(androidx.media3.datasource.DataSpec(Uri.parse(biliStream.uri))))
                val buf = ByteArray(9)
                assertEquals(9, ds.read(buf, 0, 9))
                assertEquals("M4A_AUDIO", String(buf))
            } finally {
                ds.close()
            }
            val streamBiliReq = lanServer.takeRequest()
            assertEquals("/api/lan/v1/audio/stream/biliStream_123456", streamBiliReq.path)
            assertEquals("Bearer multi-token", streamBiliReq.getHeader("Authorization"))

            // Desktop-sourced NetEase track also resolves via paired desktop proxy
            lanServer.enqueue(
                MockResponse().setBody(
                    """{"track_key":"netease:186016","provider":"netease","stream_url":"/api/lan/v1/audio/stream/neStream_654321","format":"flac"}"""
                )
            )
            val neStream = resolver.resolveStream(deskNeteaseTrack, AudioQualityLevel.lossless)
            assertTrue(neStream.requiresLanAuth)
            assertTrue(neStream.uri.endsWith("/api/lan/v1/audio/stream/neStream_654321"))
            assertEquals("/api/lan/v1/audio/resolve", lanServer.takeRequest().path)

            // 3. Combined Android-local OpenSubsonic + paired desktop search: deduplication + non-colliding resolution
            val localSubCfg =
                ExternalSourceConfig(
                    id = "sub_local",
                    name = "Phone Navidrome",
                    kind = ExternalSourceKind.OPENSUBSONIC,
                    baseUrl = localSubsonicServer.url("/").toString().trimEnd('/'),
                    username = "alice",
                    allowLanHttp = true,
                )
            withContext(Dispatchers.IO) { resolver.saveSource(localSubCfg, "pw") }

            localSubsonicServer.enqueue(
                MockResponse().setBody(
                    """{"subsonic-response":{"status":"ok","version":"1.16.1","searchResult3":{"song":[{"id":"s1","title":"Local Copy","artist":"Artist","album":"Alb","duration":200}]}}}"""
                )
            )
            lanServer.enqueue(
                MockResponse().setBody(
                    """{
                        "query": "Local",
                        "has_more": false,
                        "channels_queried": ["opensubsonic", "bilibili"],
                        "channel_errors": {},
                        "tracks": [
                            {"provider":"subsonic","external_id":"s1","track_key":"subsonic:sub_local:s1","title":"Duplicate From Desktop","artist":"Artist","duration_ms":200000},
                            {"provider":"opensubsonic","external_id":"desk_os_99","track_key":"opensubsonic:desk_os_99","title":"Desktop Navidrome Track","artist":"Artist","duration_ms":210000}
                        ]
                    }"""
                )
            )
            val combined = resolver.search("Local", limit = 20)
            assertEquals("Duplicate identity must be deduplicated", 2, combined.size)
            assertEquals("Local Copy", combined[0].title)
            assertEquals("sub_local", combined[0].source_id)
            assertEquals("Desktop Navidrome Track", combined[1].title)
            assertEquals(CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID, combined[1].source_id)
            localSubsonicServer.takeRequest()
            lanServer.takeRequest()

            // Resolving desktop's OpenSubsonic track MUST hit desktop proxy, NOT Android's local Navidrome!
            val localReqsBefore = localSubsonicServer.requestCount
            lanServer.enqueue(
                MockResponse().setBody(
                    """{"track_key":"opensubsonic:desk_os_99","provider":"opensubsonic","stream_url":"/api/lan/v1/audio/stream/deskOsStream_111","format":"flac"}"""
                )
            )
            val deskOsStream = resolver.resolveStream(combined[1], AudioQualityLevel.lossless)
            assertTrue(deskOsStream.requiresLanAuth)
            assertTrue(deskOsStream.uri.endsWith("/api/lan/v1/audio/stream/deskOsStream_111"))
            assertEquals(localReqsBefore, localSubsonicServer.requestCount)
            assertEquals("/api/lan/v1/audio/resolve", lanServer.takeRequest().path)

            // 4. Edge cases: blank query sends 0 requests; desktop failure does not break local source search
            val lanReqsBeforeBlank = lanServer.requestCount
            assertTrue(resolver.search("   ").isEmpty())
            assertEquals(lanReqsBeforeBlank, lanServer.requestCount)

            localSubsonicServer.enqueue(
                MockResponse().setBody(
                    """{"subsonic-response":{"status":"ok","version":"1.16.1","searchResult3":{"song":[{"id":"s2","title":"Still Works","artist":"Artist","duration":180}]}}}"""
                )
            )
            lanServer.enqueue(MockResponse().setResponseCode(500).setBody("""{"error":"SEARCH_FAILED"}"""))
            val degraded = resolver.search("Still", limit = 10)
            assertEquals(1, degraded.size)
            assertEquals("Still Works", degraded.single().title)
        } finally {
            lanClient.close()
            db.close()
            ctx.deleteDatabase(dbName)
        }
    }

    @Test
    fun productionAppContainerDisallowsRoomMainThreadQueries() = runBlocking {
        val ctx = RuntimeEnvironment.getApplication()
        val container = PrunerAppContainer(ctx, InMemoryCredentialStore())
        try {
            assertThrows(IllegalStateException::class.java) {
                container.repository.playlists("offline")
            }
            val offMain = withContext(Dispatchers.IO) { container.repository.playlists("offline") }
            assertNotNull(offMain)
        } finally {
            container.database.close()
        }
    }

    @Test
    fun pairedDesktopLyricsParsesDesktopLyricsAndTlyricsKeys() = runBlocking {
        val cert = HeldCertificate.Builder().commonName("desktop-lyrics").build()
        val spki = PinnedTlsFactory.computeSpkiPin(cert.certificate)
        val fp = PinnedTlsFactory.computeCertFingerprint(cert.certificate)
        val lanServer =
            MockWebServer().apply {
                protocols = listOf(Protocol.HTTP_1_1)
                useHttps(HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(), false)
                start()
            }
        servers += lanServer

        val ctx = RuntimeEnvironment.getApplication()
        val dbName = "lan_lyrics_schema_test.db"
        ctx.deleteDatabase(dbName)
        val db = PrunerDatabase.create(ctx, name = dbName, allowMainThread = false)
        val repo = PrunerRepository(db)
        val creds = InMemoryCredentialStore()
        val cfg =
            PairedServerConfig(
                host = lanServer.hostName,
                port = lanServer.port,
                fingerprint = fp,
                token = "lyrics-token",
                deviceId = "dev-ly",
                accountId = "acct-ly",
                name = "Desk",
                spki_pin = spki,
            )
        creds.pairing = cfg
        val lanClient = com.prunerstudio.mobile.core.lan.LanSyncClient(cfg, creds)
        val resolver =
            CompositeAudioSourceResolver(
                repository = repo,
                credentials = creds,
                netease = NeteaseApiClient(creds),
                lanProvider = { lanClient },
            )
        try {
            // Desktop app.py:_lyrics() returns "lyrics" and "tlyrics" (not "lrc"/"tlyric")
            lanServer.enqueue(
                MockResponse().setBody(
                    """{
                        "id": 0,
                        "track_key": "opensubsonic:os_42",
                        "lyrics": "[00:01.00]晴天",
                        "tlyrics": "[00:01.00]Sunny Day",
                        "romalrc": "[00:01.00]qing tian",
                        "yrc": ""
                    }"""
                )
            )
            val track =
                Track(
                    provider = "opensubsonic",
                    external_id = "os_42",
                    track_key = "opensubsonic:os_42",
                    title = "晴天",
                    artist = "周杰伦",
                    source_id = CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID,
                )
            val payload = resolver.fetchExternalLyrics(track)
            assertNotNull("Desktop lyrics/tlyrics payload must be parsed", payload)
            assertEquals("[00:01.00]晴天", payload!!.lrc)
            assertEquals("[00:01.00]Sunny Day", payload.tlyric)
            assertEquals("[00:01.00]qing tian", payload.romalrc)
            assertEquals("lan_desktop", payload.source)
        } finally {
            lanClient.close()
            db.close()
            ctx.deleteDatabase(dbName)
        }
    }

    @Test
    fun pairedDesktopResolveErrorsClassifyAccuratelyWithoutLeakingToLocalResolver() = runBlocking {
        val cert = HeldCertificate.Builder().commonName("desktop-err").build()
        val spki = PinnedTlsFactory.computeSpkiPin(cert.certificate)
        val fp = PinnedTlsFactory.computeCertFingerprint(cert.certificate)
        val lanServer =
            MockWebServer().apply {
                protocols = listOf(Protocol.HTTP_1_1)
                useHttps(HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(), false)
                start()
            }
        val localResolverServer = MockWebServer().apply { start() }
        servers += lanServer
        servers += localResolverServer

        val ctx = RuntimeEnvironment.getApplication()
        val dbName = "lan_err_isolation_test.db"
        ctx.deleteDatabase(dbName)
        val db = PrunerDatabase.create(ctx, name = dbName, allowMainThread = false)
        val repo = PrunerRepository(db)
        val creds = InMemoryCredentialStore()
        val cfg =
            PairedServerConfig(
                host = lanServer.hostName,
                port = lanServer.port,
                fingerprint = fp,
                token = "err-token",
                deviceId = "dev-err",
                accountId = "acct-err",
                name = "Desk",
                spki_pin = spki,
            )
        creds.pairing = cfg
        var activeLan: com.prunerstudio.mobile.core.lan.LanSyncClient? =
            com.prunerstudio.mobile.core.lan.LanSyncClient(cfg, creds)
        val resolver =
            CompositeAudioSourceResolver(
                repository = repo,
                credentials = creds,
                netease = NeteaseApiClient(creds),
                lanProvider = { activeLan },
            )
        try {
            // Configure a local Android HTTP Resolver v2 to confirm desktop tracks NEVER leak into it
            withContext(Dispatchers.IO) {
                resolver.saveSource(
                    ExternalSourceConfig(
                        id = "local_r2",
                        name = "Phone Resolver",
                        kind = ExternalSourceKind.RESOLVER_V2,
                        baseUrl = "http://127.0.0.1:${localResolverServer.port}/v2",
                        allowLanHttp = true,
                    ),
                    "secret",
                )
            }

            // Desktop search returns kugou_lite (with artist string only), gdstudio:kuwo, custom_endpoint, and netease (id=0, external_id="186016")
            lanServer.enqueue(
                MockResponse().setBody(
                    """{
                        "query": "test",
                        "has_more": false,
                        "channels_queried": ["kugou_lite", "gdstudio", "custom_endpoint", "netease"],
                        "channel_errors": {},
                        "tracks": [
                            {"provider":"kugou_lite","external_id":"kg_1","track_key":"kugou_lite:kg_1","title":"Song KG","artist":"周杰伦","duration_ms":200000},
                            {"provider":"gdstudio:kuwo","external_id":"gd_2","track_key":"gdstudio:kuwo:gd_2","title":"Song GD","artists":["林俊杰"],"duration_ms":210000},
                            {"provider":"custom_endpoint","external_id":"ce_3","track_key":"custom_endpoint:ce_3","title":"Song CE","artists":["陈奕迅"],"duration_ms":220000},
                            {"id":0,"provider":"netease","external_id":"186016","track_key":"netease:186016","title":"晴天","artists":["周杰伦"],"duration_ms":269000}
                        ]
                    }"""
                )
            )
            localResolverServer.enqueue(MockResponse().setBody("""{"tracks":[]}"""))
            val hits = resolver.search("test", limit = 20)
            assertEquals(4, hits.size)
            val kg = hits[0]
            val gd = hits[1]
            val ce = hits[2]
            val ne = hits[3]
            assertEquals("酷狗音乐", kg.displayProviderLabel())
            assertEquals("GDStudio", gd.displayProviderLabel())
            assertEquals("HTTP Resolver", ce.displayProviderLabel())
            assertEquals(listOf("周杰伦"), kg.artists)
            assertEquals(186016L, ne.id)
            assertEquals(186016L, ne.song_id)
            lanServer.takeRequest()
            val localReqsBefore = localResolverServer.requestCount

            // Desktop 404 -> COPYRIGHT_OR_UNAVAILABLE (0 requests to localResolverServer)
            lanServer.enqueue(MockResponse().setResponseCode(404).setBody("""{"error":"AUDIO_UNAVAILABLE"}"""))
            val err404 =
                runCatching { resolver.resolveStream(kg, AudioQualityLevel.lossless) }.exceptionOrNull()
                    as? AudioResolutionException
            assertNotNull(err404)
            assertEquals(AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE, err404!!.category)
            assertEquals(localReqsBefore, localResolverServer.requestCount)
            val sentBody = lanServer.takeRequest().body.readUtf8()
            assertTrue("Desktop resolve payload should carry populated artists list", "\"artists\":[\"周杰伦\"]" in sentBody)

            // Desktop 401 -> LAN_UNPAIRED (0 requests to localResolverServer)
            lanServer.enqueue(MockResponse().setResponseCode(401).setBody("""{"error":"LAN_UNAUTHORIZED"}"""))
            val err401 =
                runCatching { resolver.resolveStream(kg, AudioQualityLevel.lossless) }.exceptionOrNull()
                    as? AudioResolutionException
            assertNotNull(err401)
            assertEquals(AudioErrorCategory.LAN_UNPAIRED, err401!!.category)
            assertEquals(localReqsBefore, localResolverServer.requestCount)

            // Unpaired desktop -> LAN_UNPAIRED (0 requests to localResolverServer)
            activeLan = null
            val errUnpaired =
                runCatching { resolver.resolveStream(kg, AudioQualityLevel.lossless) }.exceptionOrNull()
                    as? AudioResolutionException
            assertNotNull(errUnpaired)
            assertEquals(AudioErrorCategory.LAN_UNPAIRED, errUnpaired!!.category)
            assertEquals(localReqsBefore, localResolverServer.requestCount)
        } finally {
            activeLan?.close()
            db.close()
            ctx.deleteDatabase(dbName)
        }
    }

    @Test
    fun prunerTrackUriDecodingPreservesPercentEncodedIdentities() = runBlocking {
        val webdavTrack =
            Track(
                provider = "webdav",
                source_id = "dav1",
                external_id = "/music/%E6%99%B4%E5%A4%A9.flac",
                track_key = "webdav:/music/%E6%99%B4%E5%A4%A9.flac",
                title = "晴天",
            )
        val colonPercentTrack =
            Track(
                provider = "resolver:sidecar",
                external_id = "100%25/track%20id",
                track_key = "resolver:sidecar:100%25/track%20id",
                title = "Special",
            )
        val items = buildQueueItems("acct", listOf(webdavTrack, colonPercentTrack), PlayMode.order) { null }
        assertEquals(2, items.size)
        assertEquals(
            webdavTrack.identity(),
            PrunerPlaybackService.decodePrunerTrackKey(items[0].localConfiguration!!.uri),
        )
        assertEquals(
            colonPercentTrack.identity(),
            PrunerPlaybackService.decodePrunerTrackKey(items[1].localConfiguration!!.uri),
        )
    }

    @Test
    fun mainThreadCanResolveNeverTouchesRoomAndIsolatesUnpairedDesktopTracks() = runBlocking {
        val ctx = RuntimeEnvironment.getApplication()
        val creds = InMemoryCredentialStore()
        val container = PrunerAppContainer(ctx, creds)
        try {
            // Cold cache on Main thread (Robolectric test thread IS main thread) with allowMainThreadDb = false
            val subTrack =
                Track(
                    provider = "opensubsonic",
                    external_id = "os1",
                    track_key = "opensubsonic:os1",
                    title = "Desktop OS",
                    source_id = CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID,
                )
            val biliTrack =
                Track(
                    provider = "bilibili",
                    external_id = "BV1",
                    track_key = "bilibili:BV1",
                    title = "Bili",
                    source_id = CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID,
                )
            val localTrack = Track(provider = "local", file_id = "a".repeat(24), title = "Local")
            val genericSub = Track(provider = "subsonic", external_id = "s1", title = "Local OS")
            val neteaseTrack = Track(id = 42L, song_id = 42L, provider = "netease", title = "NE")

            assertFalse(container.sources.canResolve(subTrack))
            assertFalse(container.sources.canResolve(biliTrack))
            assertFalse(container.sources.canResolve(localTrack))
            assertFalse(container.sources.canResolve(genericSub))
            assertTrue(container.sources.canResolve(neteaseTrack))

            // Even if an unrelated Android-local OpenSubsonic source is saved, unpaired LAN_DESKTOP_SOURCE_ID returns false
            withContext(Dispatchers.IO) {
                container.sources.saveSource(
                    ExternalSourceConfig(
                        id = "phone_os",
                        name = "Phone Navidrome",
                        kind = ExternalSourceKind.OPENSUBSONIC,
                        baseUrl = "https://music.example.com",
                        username = "u",
                    ),
                    "p",
                )
            }
            assertTrue(container.sources.canResolve(genericSub))
            assertFalse(container.sources.canResolve(subTrack))
        } finally {
            withContext(Dispatchers.IO) { container.sources.deleteSource("phone_os") }
            container.database.close()
        }
    }

    @Test
    fun desktopMultiPlatformSessionRestoreFilteringAndErrorClassification() {
        val deskBili =
            Track(
                provider = "bilibili",
                external_id = "BV1xx411c7mD",
                track_key = "bilibili:BV1xx411c7mD",
                title = "Live",
                duration_ms = 240_000L,
                source_id = CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID,
            )
        val deskOs =
            Track(
                provider = "opensubsonic",
                external_id = "desk_os_1",
                track_key = "opensubsonic:desk_os_1",
                title = "FLAC",
                duration_ms = 210_000L,
                source_id = CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID,
            )
        val localSub =
            Track(
                provider = "subsonic",
                external_id = "local_1",
                track_key = "subsonic:phone_os:local_1",
                title = "Phone",
                source_id = "phone_os",
            )
        assertTrue(requiresLanPairing(deskBili))
        assertTrue(requiresLanPairing(deskOs))
        assertFalse(requiresLanPairing(localSub))

        // Session restore preserves desktop multi-platform queue when paired and drops when unpaired
        val saved =
            PlaybackSessionSnapshot(
                current_track = deskBili,
                queue = listOf(deskBili, deskOs),
                queue_index = 0,
                position_sec = 42.0,
                duration_sec = 240.0,
            )
        val restored = prepareRestorableQueue("acct1", saved, pairedAccountId = "acct1")
        assertNotNull(restored)
        assertEquals(2, restored!!.queue.size)
        assertEquals(42_000L, restored.positionMs)
        assertNull(prepareRestorableQueue("acct1", saved, pairedAccountId = null))

        // Unpaired filterPlayableQueue counts desktop-origin tracks under skippedLocalCount for clear LAN_UNPAIRED messaging
        val filtered = filterPlayableQueue(listOf(deskBili, deskOs), 0, "acct1", pairedAccountId = null)
        assertTrue(filtered.tracks.isEmpty())
        assertEquals(2, filtered.skippedLocalCount)
        assertEquals(0, filtered.skippedInvalidCount)

        // Error classification & 404 proxy stream TTL auto-retry for LAN_DESKTOP_SOURCE_ID tracks
        val spec = DataSpec(Uri.parse("https://127.0.0.1:8443/api/lan/v1/audio/stream/tok123456"))
        val err401 =
            PlaybackException(
                "401",
                HttpDataSource.InvalidResponseCodeException(401, "Unauthorized", null, emptyMap(), spec, ByteArray(0)),
                PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS,
            )
        assertEquals(AudioErrorCategory.LAN_UNPAIRED, classifyPlaybackError(err401, deskBili).first)

        val connErr =
            PlaybackException("conn", ConnectException("refused"), PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED)
        assertEquals(AudioErrorCategory.LAN_UNREACHABLE, classifyPlaybackError(connErr, deskBili).first)

        val err404 =
            PlaybackException(
                "404",
                HttpDataSource.InvalidResponseCodeException(404, "Not Found", null, emptyMap(), spec, ByteArray(0)),
                PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS,
            )
        assertEquals(
            AutoRetryAction.RETRY_SAME_POSITION,
            shouldAutoRetryOnce(err404, deskBili, 30_000L, alreadyRetried = false),
        )
        assertEquals(
            AutoRetryAction.NONE,
            shouldAutoRetryOnce(err404, deskBili, 30_000L, alreadyRetried = true),
        )
    }
}
