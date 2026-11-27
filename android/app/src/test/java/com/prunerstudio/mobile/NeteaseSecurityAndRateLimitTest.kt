package com.prunerstudio.mobile

import com.prunerstudio.mobile.core.lan.PinnedTlsFactory
import com.prunerstudio.mobile.core.model.AudioErrorCategory
import com.prunerstudio.mobile.core.model.AudioResolutionException
import com.prunerstudio.mobile.core.netease.*
import java.security.MessageDigest
import kotlinx.coroutines.runBlocking
import okhttp3.Call
import okhttp3.Protocol
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class NeteaseSecurityAndRateLimitTest {
    private val servers = mutableListOf<MockWebServer>()

    @After
    fun tearDown() {
        servers.forEach { runCatching { it.shutdown() } }
        servers.clear()
    }

    private fun buildClient(
        store: InMemoryCredentialStore = InMemoryCredentialStore(),
        rateLimiter: NeteaseRateLimiter,
    ): Pair<MockWebServer, NeteaseApiClient> {
        val server = MockWebServer()
        val cert = HeldCertificate.Builder().commonName("netease-rl-mock").build()
        val pin =
            MessageDigest.getInstance("SHA-256").digest(cert.certificate.encoded).joinToString("") {
                "%02x".format(it)
            }
        server.protocols = listOf(Protocol.HTTP_1_1)
        server.useHttps(
            HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(),
            false,
        )
        server.start()
        servers += server
        val http = PinnedTlsFactory(pin).clientBuilder().build()
        val client =
            NeteaseApiClient(
                store = store,
                client =
                    Call.Factory { original ->
                        http.newCall(
                            original.newBuilder().url(server.url(original.url.encodedPath)).build()
                        )
                    },
                rateLimiter = rateLimiter,
            )
        return server to client
    }

    @Test
    fun sendsCleanDesktopUserAgentAndPersistsStableDeviceId() = runBlocking {
        val store = InMemoryCredentialStore()
        var now = 1_000_000L
        val limiter =
            NeteaseRateLimiter(
                nowMs = { now },
                sleeper = { now += it },
                jitterProvider = { 0L },
            )
        val (server, client1) = buildClient(store, limiter)
        server.enqueue(MockResponse().setBody("""{"code":200,"unikey":"k1"}"""))
        server.enqueue(MockResponse().setBody("""{"code":200,"unikey":"k2"}"""))

        client1.createQrKey()
        val req1 = server.takeRequest()
        val ua1 = req1.getHeader("User-Agent").orEmpty()
        val cookie1 = req1.getHeader("Cookie").orEmpty()
        assertEquals(NETEASE_DESKTOP_USER_AGENT, ua1)
        assertFalse(ua1.contains("Pruner", ignoreCase = true))
        assertFalse(ua1.contains("okhttp", ignoreCase = true))
        val persistedId = store.deviceId
        assertNotNull(persistedId)
        assertTrue(persistedId!!.matches(Regex("[a-f0-9]{32}")))
        assertTrue("Cookie should contain persisted deviceId", "deviceId=$persistedId" in cookie1)

        // Second client instance sharing store must keep the exact same deviceId
        val (_, client2) = buildClient(store, limiter)
        assertEquals(persistedId, store.getOrCreateDeviceId())
        client1.createQrKey()
        val req2 = server.takeRequest()
        assertTrue("deviceId=$persistedId" in req2.getHeader("Cookie").orEmpty())
        assertNotNull(client2)
    }

    @Test
    fun unlikeTracksBatchPacesWritesMicroBatchesAndHaltsOnRiskControl460() = runBlocking {
        var now = 10_000L
        val sleeps = mutableListOf<Long>()
        val limiter =
            NeteaseRateLimiter(
                config =
                    WriteRateConfig(
                        minIntervalMs = 900L,
                        jitterMaxMs = 0L,
                        microBatchSize = 3,
                        microBatchPauseMs = 2000L,
                        baseCooldownMs = 30_000L,
                        maxCooldownMs = 300_000L,
                    ),
                nowMs = { now },
                sleeper = { ms ->
                    sleeps += ms
                    now += ms
                },
                jitterProvider = { 0L },
            )
        val (server, client) = buildClient(rateLimiter = limiter)
        // 3 succeed -> micro-batch pause -> 4th triggers -460 risk control -> 5th & 6th never sent!
        repeat(3) { server.enqueue(MockResponse().setBody("""{"code":200}""")) }
        server.enqueue(MockResponse().setBody("""{"code":-460,"message":"cheating"}"""))
        server.enqueue(MockResponse().setBody("""{"code":200}"""))

        val result = client.unlikeTracksBatch(listOf(1L, 2L, 3L, 4L, 5L, 6L))

        assertEquals(listOf(1L, 2L, 3L), result.succeededIds)
        assertEquals(listOf(4L, 5L, 6L), result.failedIds)
        assertTrue("Circuit breaker must trip on -460", result.circuitTripped)
        assertEquals(4, server.requestCount)
        assertTrue("Micro-batch pause must have fired", sleeps.contains(2000L))
        assertTrue("Write interval pacing must have fired", sleeps.count { it == 900L } >= 2)
        assertTrue(limiter.isCircuitOpen())
        assertTrue(limiter.remainingCooldownMs() in 29_000L..30_000L)

        // Immediate follow-up write must be rejected locally without hitting the server
        val blocked = runCatching { client.like(99L, false) }.exceptionOrNull()
        assertTrue(blocked is AudioResolutionException)
        assertEquals(AudioErrorCategory.RATE_LIMITED, (blocked as AudioResolutionException).category)
        assertEquals(4, server.requestCount)
    }

    @Test
    fun circuitBreakerEscalatesExponentiallyOn462AndReadsRetry429() = runBlocking {
        var now = 100_000L
        val sleeps = mutableListOf<Long>()
        val limiter =
            NeteaseRateLimiter(
                config =
                    WriteRateConfig(
                        minIntervalMs = 500L,
                        jitterMaxMs = 0L,
                        baseCooldownMs = 10_000L,
                        maxCooldownMs = 80_000L,
                        readRetryMaxAttempts = 2,
                        readRetryBaseMs = 1_000L,
                        readRetryCapMs = 4_000L,
                    ),
                nowMs = { now },
                sleeper = { ms ->
                    sleeps += ms
                    now += ms
                },
                jitterProvider = { 0L },
            )
        val (server, client) = buildClient(rateLimiter = limiter)

        // Read hitting 429 once with Retry-After=2 then succeeding
        server.enqueue(MockResponse().setResponseCode(429).setHeader("Retry-After", "2"))
        server.enqueue(MockResponse().setBody("""{"code":200,"unikey":"recovered"}"""))
        assertEquals("recovered", client.createQrKey())
        assertEquals(listOf(2000L), sleeps)
        assertFalse(limiter.isCircuitOpen())

        // Repeated -462 strikes escalate cooldown exponentially: 10s -> 20s -> 40s
        val c1 = limiter.recordRateLimit(200, -462)
        assertEquals(10_000L, c1)
        now += 10_000L
        val c2 = limiter.recordRateLimit(200, -462)
        assertEquals(20_000L, c2)
        now += 20_000L
        val c3 = limiter.recordRateLimit(200, -462)
        assertEquals(40_000L, c3)
    }

    @Test
    fun changePlaylistTracksMicroBatchesLargeRemovals() = runBlocking {
        var now = 50_000L
        val limiter =
            NeteaseRateLimiter(
                config = WriteRateConfig(minIntervalMs = 100L, jitterMaxMs = 0L, microBatchPauseMs = 500L),
                nowMs = { now },
                sleeper = { now += it },
                jitterProvider = { 0L },
            )
        val (server, client) = buildClient(rateLimiter = limiter)
        repeat(3) { server.enqueue(MockResponse().setBody("""{"code":200}""")) }

        val ids = (1L..55L).toList()
        client.changePlaylistTracks(id = 777L, tracks = ids, add = false, chunkSize = 25)
        // 55 tracks chunked by 25 => 3 requests (25 + 25 + 5)
        assertEquals(3, server.requestCount)
    }

    @Test
    fun changePlaylistTracksBatchPreservesChunk1WhenChunk2Trips460AndUnlikePreservesOnIoError() = runBlocking {
        var now = 80_000L
        val limiter =
            NeteaseRateLimiter(
                config = WriteRateConfig(minIntervalMs = 50L, jitterMaxMs = 0L, microBatchPauseMs = 100L),
                nowMs = { now },
                sleeper = { now += it },
                jitterProvider = { 0L },
            )
        val (server, client) = buildClient(rateLimiter = limiter)
        // Chunk 1 succeeds, chunk 2 triggers -460 -> chunk 3 must NOT be sent
        server.enqueue(MockResponse().setBody("""{"code":200}"""))
        server.enqueue(MockResponse().setBody("""{"code":-460,"message":"risk"}"""))

        val batch = client.changePlaylistTracksBatch(id = 888L, tracks = (1L..60L).toList(), add = false, chunkSize = 25)
        assertEquals((1L..25L).toList(), batch.succeededIds)
        assertEquals((26L..60L).toList(), batch.failedIds)
        assertTrue(batch.circuitTripped)
        assertEquals(2, server.requestCount)

        // Reset circuit on a fresh server and verify unlikeTracksBatch preserves succeeded IDs on HTTP 500 IOException
        limiter.reset()
        val (server2, client2) = buildClient(rateLimiter = limiter)
        server2.enqueue(MockResponse().setBody("""{"code":200}"""))
        server2.enqueue(MockResponse().setBody("""{"code":200}"""))
        server2.enqueue(MockResponse().setResponseCode(500).setBody("error"))

        val partial = client2.unlikeTracksBatch(listOf(10L, 11L, 12L, 13L))
        assertEquals(listOf(10L, 11L), partial.succeededIds)
        assertEquals(listOf(12L, 13L), partial.failedIds)
        assertFalse(partial.circuitTripped)
    }

    @Test
    fun longRetryAfterPublishesSharedCooldownWithoutAnotherRequest() = runBlocking {
        var now = 100_000L
        val limiter = NeteaseRateLimiter(nowMs = { now }, sleeper = { now += it }, jitterProvider = { 0L })
        val (server, client) = buildClient(rateLimiter = limiter)
        server.enqueue(MockResponse().setResponseCode(429).setHeader("Retry-After", "600"))
        assertTrue(runCatching { client.createQrKey() }.exceptionOrNull() is AudioResolutionException)
        assertEquals(600_000L, limiter.remainingCooldownMs())
        assertTrue(runCatching { client.like(1, true) }.exceptionOrNull() is AudioResolutionException)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun changingLoginMidBatchPreservesCompletedIdsAndStopsWithPinnedCookie() = runBlocking {
        val store = InMemoryCredentialStore().apply { cookie = "MUSIC_U=account-one" }
        val auth = AuthSessionCoordinator(store)
        val pinned = requireNotNull(auth.captureWriteSession("one"))
        val limiter = NeteaseRateLimiter(jitterProvider = { 0L })
        val (server, client) = buildClient(store, limiter)
        server.enqueue(MockResponse().setBody("""{"code":200}"""))
        val result = client.unlikeTracksBatch(listOf(1, 2, 3), pinned) { _, _ ->
            runBlocking { auth.cancel(logout = true) }
            store.cookie = "MUSIC_U=account-two"
        }
        assertEquals(listOf(1L), result.succeededIds)
        assertEquals(listOf(2L, 3L), result.failedIds)
        assertEquals(1, server.requestCount)
        assertTrue(server.takeRequest().getHeader("Cookie")!!.contains("account-one"))
    }
}
