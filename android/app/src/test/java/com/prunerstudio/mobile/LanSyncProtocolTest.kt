package com.prunerstudio.mobile

import com.prunerstudio.mobile.core.lan.*
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.*
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLHandshakeException
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.toList
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.mockwebserver.*
import okhttp3.tls.*
import org.junit.*
import org.junit.Assert.*

class LanSyncProtocolTest {
    private val cert = HeldCertificate.Builder().commonName("Stage 3 desktop").build()
    private val pin =
        MessageDigest.getInstance("SHA-256").digest(cert.certificate.encoded).joinToString("") {
            "%02x".format(it)
        }
    private val server = MockWebServer()
    private lateinit var lan: LanSyncClient
    private val store = InMemoryCredentialStore()

    @Before
    fun setup() {
        server.protocols = listOf(Protocol.HTTP_1_1)
        server.useHttps(
            HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(),
            false,
        )
        server.start()
        val config =
            PairedServerConfig(server.hostName, server.port, pin, "test-token", "device", "account")
        store.pairing = config
        lan = LanSyncClient(config, store)
    }

    @After fun close() = server.shutdown()

    private fun reply(json: String, code: Int = 200) =
        server.enqueue(MockResponse().setResponseCode(code).setBody(json))

    @Test
    fun pairingValidation() {
        val uri =
            "pruner-lan://pair?v=1&tls=1&host=192.168.0.9&port=19212&code=012345&fp=$pin&name=A%20B"
        assertEquals("A B", LanPairingUriParser.parse(uri).name)
        for (bad in
            listOf(
                uri.replace("tls=1&", ""),
                uri.replace(pin, "00"),
                uri.replace("012345", "12345"),
                uri.replace("19212", "0"),
                uri.replace("192.168.0.9", "example.com%2Fpath"),
                uri + "&host=evil.test",
            )) assertThrows(Exception::class.java) { LanPairingUriParser.parse(bad) }
    }

    @Test
    fun realTlsRangeAndWrongLeaf() {
        reply("abc", 206)
        val req =
            Request.Builder()
                .url(server.url("/api/lan/v1/local-library/stream/0123456789abcdef01234567"))
                .header("Range", "bytes=1-3")
                .build()
        lan.client.newCall(req).execute().use {
            assertEquals(206, it.code)
            assertEquals("abc", it.body!!.string())
        }
        val received = server.takeRequest()
        assertEquals("bytes=1-3", received.getHeader("Range"))
        assertEquals("Bearer test-token", received.getHeader("Authorization"))
        assertThrows(SSLHandshakeException::class.java) {
            lan.createAuthenticatedOkHttpClient("00".repeat(32)).newCall(req).execute().close()
        }
    }

    private fun page(more: Boolean) =
        """{"sync_until":"fixed","server_time":"now","account_id":"account","play_history":[{"id":${if(more) 1 else 2}}],"feedback":[],"history_has_more":$more,"feedback_has_more":false,"history_next_cursor":${if(more) """{"after_ts":"old","after_track_key":"netease:1"}""" else "null"}}"""

    @Test
    fun completesBothCursorsBeforePublishingWatermark() = runBlocking {
        reply(page(true))
        reply(page(false))
        val result = lan.pullAllDeltas("old", true, true)
        assertEquals(listOf(1L, 2L), result.play_history.map { it.id })
        assertEquals("fixed", result.sync_until)
        assertEquals("/api/lan/v1/sync/pull", server.takeRequest().path)
        val body = wire.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        assertEquals(JsonPrimitive("fixed"), body["until"])
        assertEquals(JsonNull, body["feedback_cursor"])
        assertEquals(
            "netease:1",
            body["history_cursor"]!!.jsonObject["after_track_key"]!!.jsonPrimitive.content,
        )
    }

    @Test
    fun pageTwoFailurePreservesWatermark() = runBlocking {
        reply(page(true))
        reply("""{"error":"SYNC_DATABASE_FAILED"}""", 500)
        var saved = "old"
        try {
            saved = lan.pullAllDeltas(saved).sync_until
            fail()
        } catch (e: LanException) {
            assertEquals("SYNC_DATABASE_FAILED", e.code)
        }
        assertEquals("old", saved)
    }

    @Test
    fun idempotencyKeysSurviveRetry() = runBlocking {
        val request =
            SyncPushRequest(
                "account",
                listOf(PlayEventPayload("evt", Track(id = 7), 40000, false)),
                listOf(FeedbackItem("like_explicit", "netease:7", 7, client_event_id = "fb")),
            )
        repeat(2) {
            reply(
                """{"accepted_events":0,"accepted_feedback":0,"session_updated":false,"server_time":"now"}"""
            )
            lan.pushDeltas(request)
        }
        val a = server.takeRequest().body.readUtf8()
        assertEquals(a, server.takeRequest().body.readUtf8())
        val obj = wire.parseToJsonElement(a).jsonObject
        assertEquals(JsonPrimitive("evt"), obj["play_events"]!!.jsonArray[0].jsonObject["event_id"])
        assertEquals(JsonPrimitive("fb"), obj["feedback"]!!.jsonArray[0].jsonObject["event_id"])
    }

    @Test
    fun handoffAndAck() = runBlocking {
        val json = """{"track":{"id":9},"handoff_id":"h"}"""
        reply(json)
        reply(json)
        reply("""{"acknowledged":true}""")
        val snapshot = lan.fetchHandoff()!!
        lan.pushHandoff(snapshot)
        lan.ackHandoff("h")
        assertEquals("GET", server.takeRequest().method)
        assertEquals("POST", server.takeRequest().method)
        assertEquals("/api/lan/v1/handoff/ack", server.takeRequest().path)
    }

    @Test
    fun sseUnauthorizedClearsPairing() = runBlocking {
        reply("""{"error":"UNAUTHORIZED"}""", 401)
        assertEquals(
            listOf(LanSseEvent.DeviceRevoked),
            withTimeout(5000) { LanSseClient(lan).events().toList() },
        )
        assertNull(store.pairing)
    }

    @Test
    fun unauthorizedDoesNotWaitForAnErrorBody() = runBlocking {
        // Headers have denied authorization. The server leaves the error body unfinished.
        server.enqueue(MockResponse().setResponseCode(401).setHeader("Content-Length", 64))
        val error = runCatching {
            withTimeout(3000) { lan.fetchLocalLibrary() }
        }.exceptionOrNull()
        assertTrue("Expected a LAN authorization error, got $error", error is LanException)
        assertEquals(401, (error as LanException).status)
        assertEquals("LAN_UNAUTHORIZED", error.code)
        assertNull(store.pairing)
    }

    @Test
    fun catalogReadRecoversFromClosedConnectionWithoutRetryingPost() = runBlocking {
        val emptyCatalog = """{"files":[],"total":0,"limit":200,"offset":0,"has_more":false}"""
        reply(emptyCatalog)
        assertEquals(0, lan.fetchLocalLibrary().total)

        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        reply(emptyCatalog)
        assertEquals(0, lan.fetchLocalLibrary().total)
        assertEquals(3, server.requestCount)

        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        val error = runCatching { lan.request("sync/push", JsonObject(emptyMap())) }.exceptionOrNull()
        assertTrue(error is java.io.IOException)
        assertEquals(4, server.requestCount)
    }

    @Test
    fun cancelingTheCallerAfterUnauthorizedHeadersStillClearsPairing() = runBlocking {
        val requestArrived = CountDownLatch(1)
        val releaseResponse = CountDownLatch(1)
        val callerPaused = CountDownLatch(1)
        val resumeCaller = CountDownLatch(1)
        server.dispatcher = object : okhttp3.mockwebserver.Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                requestArrived.countDown()
                check(releaseResponse.await(5, TimeUnit.SECONDS))
                return MockResponse().setResponseCode(401).setBody("""{"error":"LAN_UNAUTHORIZED"}""")
            }
        }
        val executor = Executors.newSingleThreadExecutor()
        executor.asCoroutineDispatcher().use { caller ->
            supervisorScope {
                val request = async(caller) { lan.fetchLocalLibrary() }
                try {
                    assertTrue(requestArrived.await(5, TimeUnit.SECONDS))
                    executor.execute {
                        callerPaused.countDown()
                        resumeCaller.await(5, TimeUnit.SECONDS)
                    }
                    assertTrue(callerPaused.await(5, TimeUnit.SECONDS))
                    releaseResponse.countDown()
                    withTimeout(3000) {
                        while (!lan.isClosed) delay(10)
                    }
                    request.cancel()
                    resumeCaller.countDown()
                    request.join()
                    withTimeout(3000) {
                        while (store.pairing != null) delay(10)
                    }
                } finally {
                    releaseResponse.countDown()
                    resumeCaller.countDown()
                    request.cancelAndJoin()
                }
            }
        }
    }

    @Test
    fun pairingAndCatalogMatchDesktopFields() = runBlocking {
        val candidate =
            LanSyncClient.forPairing(
                PairingUri(server.hostName, server.port, "000001", pin, "Desk"),
                store,
            )
        reply(
            """{"protocol_version":1,"device_id":"id","token":"new-token","server_name":"Desk","account":{"account_id":"account"}}"""
        )
        store.pairing = candidate.claimPairing("000001", "Android")
        val claim = server.takeRequest()
        assertNull(claim.getHeader("Authorization"))
        assertEquals("/api/lan/v1/pair/claim", claim.path)
        val fields = wire.parseToJsonElement(claim.body.readUtf8()).jsonObject
        assertEquals(JsonPrimitive("000001"), fields["code"])
        assertEquals(JsonPrimitive("android"), fields["platform"])
        assertEquals("new-token", store.pairing!!.token)
        val paired = LanSyncClient(store.pairing!!, store)
        val body =
            JsonObject(
                wire.parseToJsonElement(page(false)).jsonObject +
                    mapOf(
                        "playlists" to
                            wire.parseToJsonElement(
                                """[{"id":"import:abc","name":"Local","kind":"imported","track_count":1}]"""
                            ),
                        "songs" to
                            wire.parseToJsonElement(
                                """[{"id":42,"source_playlist_id":"import:abc","title":"Music"}]"""
                            ),
                    )
            )
        reply(body.toString())
        val data = paired.pullAllDeltas(includeCatalog = true)
        assertEquals("import:abc", data.playlists!!.single().id.content)
        assertEquals(data.playlists!!.single().id, data.songs!!.single().source_playlist_id)
        assertEquals("Bearer new-token", server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun completedHistoryIsNotReplayedWhileFeedbackContinues() = runBlocking {
        val first =
            JsonObject(
                wire.parseToJsonElement(page(false)).jsonObject +
                    mapOf(
                        "feedback_has_more" to JsonPrimitive(true),
                        "feedback_next_cursor" to
                            wire.parseToJsonElement("""{"after_ts":"old","after_id":19}"""),
                    )
            )
        reply(first.toString())
        reply(page(false))
        lan.pullAllDeltas()
        server.takeRequest()
        val request = wire.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        assertEquals(JsonNull, request["history_cursor"])
        assertEquals(JsonPrimitive(19), request["feedback_cursor"]!!.jsonObject["after_id"])
    }

    @Test
    fun authenticatedRedirectIsNeverFollowed() {
        server.enqueue(
            MockResponse().setResponseCode(302).setHeader("Location", "http://invalid.test")
        )
        lan.client
            .newCall(Request.Builder().url(server.url("/api/lan/v1/info")).build())
            .execute()
            .use { assertEquals(302, it.code) }
        assertEquals(1, server.requestCount)
    }

    @Test
    fun desktopNotificationsRespectPairedAccount() = runBlocking {
        val stream =
            listOf(
                    "sync_available" to """{"server_time":"one"}""",
                    "sync_available" to """{"server_time":"two","account_id":"account"}""",
                    "sync_available" to """{"account_id":"other"}""",
                    "sync_available" to """{"account_id":" ","server_time":"three"}""",
                    "handoff" to """{"account_id":"other","handoff_id":"foreign"}""",
                    "handoff" to """{"account_id":"account","handoff_id":"paired"}""",
                )
                .joinToString("") { (type, json) -> "event: $type\ndata: $json\n\n" }
        server.enqueue(
            MockResponse().setHeader("Content-Type", "text/event-stream").setBody(stream)
        )
        assertEquals(
            listOf(
                LanSseEvent.SyncAvailable(null, "one"),
                LanSseEvent.SyncAvailable("account", "two"),
                LanSseEvent.SyncAvailable(null, "three"),
                LanSseEvent.Handoff(HandoffSnapshot(account_id = "account", handoff_id = "paired")),
            ),
            withTimeout(5000) { LanSseClient(lan).events().toList() },
        )
    }

    @Test
    fun targetedRevocationEvent() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Content-Type", "text/event-stream")
                .setBody("event: connected\ndata: {}\n\nevent: device_revoked\ndata: {}\n\n")
        )
        assertEquals(
            listOf(LanSseEvent.Connected, LanSseEvent.DeviceRevoked),
            withTimeout(5000) { LanSseClient(lan).events().toList() },
        )
        assertNull(store.pairing)
    }
}
