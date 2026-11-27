package com.prunerstudio.mobile

import android.app.Application
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.lifecycle.viewModelScope
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.*
import com.prunerstudio.mobile.core.player.PrunerAppContainer
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.PrunerTheme
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.*
import okhttp3.Call
import com.prunerstudio.mobile.core.lan.PairingUri
import com.prunerstudio.mobile.core.lan.PinnedTlsFactory
import okhttp3.mockwebserver.*
import okhttp3.tls.*
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp")
class SessionRaceTest {
    @get:Rule val ui = createComposeRule()
    private val app: Application get() = RuntimeEnvironment.getApplication()
    private val server = MockWebServer()
    private val release = CountDownLatch(1)
    private val scopes = mutableListOf<PrunerViewModel>()

    @After fun close() {
        release.countDown()
        ui.runOnIdle { scopes.forEach { it.viewModelScope.cancel() } }
        server.shutdown()
    }

    private fun model(core: PrunerAppContainer): PrunerViewModel =
        ui.runOnIdle { PrunerViewModel(app, core).also(scopes::add) }

    private fun waitFor(condition: () -> Boolean) = ui.waitUntil(15000) {
        org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()
        condition()
    }

    private fun respond(handle: (String) -> MockResponse) {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest) = handle(request.path.orEmpty())
        }
    }

    private fun held(body: String): MockResponse {
        check(release.await(30, TimeUnit.SECONDS))
        return MockResponse().setBody(body)
    }

    private fun tlsDesktop(): PairedServerConfig {
        val cert = HeldCertificate.Builder().commonName("desktop").build()
        val pin = MessageDigest.getInstance("SHA-256").digest(cert.certificate.encoded)
            .joinToString("") { "%02x".format(it) }
        server.protocols = listOf(okhttp3.Protocol.HTTP_1_1)
        server.useHttps(HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(), false)
        server.start()
        return PairedServerConfig(server.hostName, server.port, pin, "token", "device", "account", "Desktop A")
    }

    @Test fun onlyCurrentAttemptCanCommitAnAlreadyReceived803() = runBlocking {
        val endpoint = tlsDesktop()
        server.enqueue(MockResponse().setBody("""{"code":803}""").setHeader("Set-Cookie", "MUSIC_U=candidate; Path=/"))
        val http = PinnedTlsFactory(endpoint.fingerprint).clientBuilder().build()
        val store = InMemoryCredentialStore()
        val auth = AuthSessionCoordinator(store)
        val api = NeteaseApiClient(store, Call.Factory { original ->
            http.newCall(original.newBuilder().url(server.url(original.url.encodedPath)).build())
        })
        val old = CompletableDeferred<Long>()
        auth.start(this) { token -> old.complete(token); awaitCancellation() }
        val result = api.checkQrStatus("old")
        assertEquals(803, result.code)
        assertNull(store.cookie)
        val current = CompletableDeferred<Long>()
        auth.start(this) { token -> current.complete(token); awaitCancellation() }
        try {
            val cookie = requireNotNull(result.candidateCookie)
            assertFalse(auth.commit(old.await(), cookie))
            assertNull(store.cookie)
            assertTrue(auth.commit(current.await(), cookie))
            assertEquals("MUSIC_U=candidate", store.cookie)
        } finally {
            auth.cancel()
        }
    }

    @Test fun cancelQrBefore803() = qrRace(logout = false)
    @Test fun logoutBefore803() = qrRace(logout = true)

    private fun qrRace(logout: Boolean) {
        val pollStarted = CountDownLatch(1)
        respond { path ->
            if (path.endsWith("/unikey")) MockResponse().setBody("""{"code":200,"unikey":"test"}""")
            else {
                pollStarted.countDown()
                held("""{"code":803}""").setHeader("Set-Cookie", "MUSIC_U=candidate; Path=/")
            }
        }
        val desktop = tlsDesktop()
        val writes = AtomicInteger()
        val store = object : SecureCredentialStore by InMemoryCredentialStore() {
            @Volatile override var cookie: String? = null
                set(value) { if (value != null) writes.incrementAndGet(); field = value }
        }
        val http = PinnedTlsFactory(desktop.fingerprint).clientBuilder().build()
        val calls = Call.Factory { original ->
            http.newCall(original.newBuilder().url(server.url(original.url.encodedPath)).build())
        }
        val vm = model(PrunerAppContainer(app, store, NeteaseApiClient(store, calls)))
        ui.runOnIdle { vm.login() }
        waitFor { pollStarted.count == 0L }
        val pollCall = http.dispatcher.runningCalls().single { it.request().url.encodedPath.endsWith("/client/login") }
        val invalidation = ui.runOnIdle { if (logout) vm.logout() else vm.cancelLogin() }
        waitFor { invalidation.isCompleted && http.dispatcher.runningCallsCount() == 0 }
        release.countDown()
        ui.waitForIdle()
        assertTrue("QR transport was cancelled", pollCall.isCanceled())
        assertNull(store.cookie)
        assertEquals(0, writes.get())
    }

    @Test fun twoViewModelsShareOneLiveSseAndUnpairFencesBothPendingRequests() {
        val config = tlsDesktop()
        val store = InMemoryCredentialStore().apply { pairing = config }
        val core = PrunerAppContainer(app, store)
        val events = AtomicInteger()
        val browse = CountDownLatch(1)
        val sync = CountDownLatch(1)
        respond { path ->
            when {
                path.endsWith("/events") -> {
                    events.incrementAndGet()
                    MockResponse().setHeader("Content-Type", "text/event-stream")
                        .setBody("event: connected\ndata: {}\n\n: stay open\n")
                        .setHeader("Content-Length", "1000000")
                }
                path.endsWith("/handoff") -> MockResponse().setBody("null")
                path.contains("/local-library") -> {
                    browse.countDown()
                    held("""{"files":[{"file_id":"remote","stream_url":"x"}],"total":1,"offset":0,"limit":50,"has_more":false}""")
                }
                path.endsWith("/sync/pull") -> {
                    sync.countDown()
                    held("""{"account_id":"account","sync_until":"stale","server_time":"now","play_history":[{"id":1}],"feedback":[],"history_has_more":false,"feedback_has_more":false}""")
                }
                else -> MockResponse().setResponseCode(404)
            }
        }
        val first = model(core)
        waitFor { first.live == "已连接" }
        val second = model(core)
        waitFor { second.live == "已连接" }
        ui.runOnIdle { second.browseLan("remote"); first.sync() }
        waitFor { browse.count == 0L && sync.count == 0L }
        assertEquals(1, events.get())
        val http = core.lan()!!.client
        val pending = http.dispatcher.runningCalls().filter {
            it.request().url.encodedPath.endsWith("/local-library") || it.request().url.encodedPath.endsWith("/sync/pull")
        }
        assertEquals(2, pending.size)
        val invalidation = ui.runOnIdle { second.unpair() }
        waitFor { invalidation.isCompleted && http.dispatcher.runningCallsCount() == 0 }
        release.countDown()
        ui.waitForIdle()
        assertNull(store.pairing)
        assertTrue("Both transports were cancelled", pending.all { it.isCanceled() })
        assertTrue(first.browse.isEmpty() && second.browse.isEmpty())
        assertNull(core.lanSession.state.value.lastSyncAt)
        val (historyAfterUnpair, watermarkAfterUnpair) =
            runBlocking(Dispatchers.IO) {
                core.repository.history("account") to core.repository.watermark("account", config.fingerprint)
            }
        assertTrue(historyAfterUnpair.isEmpty())
        assertNull(watermarkAfterUnpair.since)
        assertEquals(1, events.get())
    }

    @Test fun replaceDialogSingleFlightAndCancelPreservesCurrentDesktop() {
        val endpoint = tlsDesktop()
        val current = endpoint.copy(host = "127.0.0.1")
        val store = InMemoryCredentialStore().apply { pairing = current }
        val core = PrunerAppContainer(app, store)
        val claims = AtomicInteger()
        respond { path ->
            if (path.endsWith("/pair/claim")) {
                claims.incrementAndGet()
                held("""{"protocol_version":1,"server_name":"Desktop B","device_id":"new","token":"new","account":{"account_id":"account"}}""")
            } else MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE)
        }
        val vm = model(core)
        ui.setContent { PrunerTheme(vm.theme, vm.accent) { PrunerShell(vm) } }
        ui.runOnIdle {
            vm.tab = Tab.SETTINGS
            vm.parsePair("pruner-lan://pair?v=1&tls=1&host=${endpoint.host}&port=${endpoint.port}&code=012345&fp=${endpoint.fingerprint}&name=Desktop%20B")
        }
        waitFor { vm.pairingReady }
        ui.onNodeWithText("当前已配对：Desktop A (${current.host}:${current.port})").assertExists()
        ui.onNodeWithText("替换为：Desktop B (${endpoint.host}:${endpoint.port})").assertExists()
        ui.onNode(hasText(endpoint.fingerprint.chunked(2).joinToString(":")) and hasAnyAncestor(isDialog())).assertExists()
        val confirm = ui.onNodeWithText("替换配对")
        confirm.performClick()
        waitFor { claims.get() == 1 }
        confirm.assertIsNotEnabled().performTouchInput { click() }
        ui.runOnIdle { vm.claim(requireNotNull(vm.pair)) }
        ui.onNodeWithText("取消").performClick()
        waitFor { !core.lanSession.state.value.pairingClaimInFlight }
        release.countDown()
        ui.waitForIdle()
        assertEquals(1, claims.get())
        assertEquals(current, store.pairing)
        val cleanup = ui.runOnIdle { vm.unpair() }
        waitFor { cleanup.isCompleted }
    }

    @androidx.media3.common.util.UnstableApi
    private fun assertBlockedInFlightLanReadAborts(
        config: PairedServerConfig,
        core: PrunerAppContainer,
        fileId: String,
        trigger: () -> Unit,
    ) {
        val factory = com.prunerstudio.mobile.core.player.RoutingDataSourceFactory(app, core::lan)
        val source = factory.createDataSource()
        val streamUrl = "https://${config.host}:${config.port}/api/lan/v1/local-library/stream/$fileId"
        val spec = androidx.media3.datasource.DataSpec(android.net.Uri.parse(streamUrl))
        val firstChunkRead = CountDownLatch(1)
        val readFinished = CountDownLatch(1)
        val returnedBytes = AtomicInteger(-99)
        val caught = java.util.concurrent.atomic.AtomicReference<Throwable?>(null)
        val tail = ByteArray(16)
        val reader = Thread {
            try {
                try {
                    assertTrue(source.open(spec) > 4L)
                    val head = ByteArray(4)
                    assertEquals(4, source.read(head, 0, head.size))
                    assertEquals("ID3A", String(head))
                    firstChunkRead.countDown()
                    returnedBytes.set(source.read(tail, 0, tail.size))
                } finally {
                    source.close()
                }
            } catch (t: Throwable) {
                caught.set(t)
            } finally {
                readFinished.countDown()
            }
        }.apply { isDaemon = true; start() }

        try {
            assertTrue("Reader did not receive the first audio chunk", firstChunkRead.await(5, TimeUnit.SECONDS))
            ui.waitUntil(5000) {
                val stack = reader.stackTrace
                readFinished.count == 0L || (
                    stack.any { it.className == "androidx.media3.datasource.okhttp.OkHttpDataSource" && it.methodName == "read" } &&
                        stack.any { it.className.startsWith("okhttp3.internal.") && it.methodName == "read" }
                )
            }
            assertEquals("Reader finished before revocation", 1L, readFinished.count)
            trigger()

            assertTrue(
                "Blocked in-flight LAN read() did not abort promptly after revocation",
                readFinished.await(5, TimeUnit.SECONDS),
            )
            assertTrue(
                "Expected IOException from aborted in-flight read(), got ${caught.get()} (bytes=${returnedBytes.get()})",
                caught.get() is java.io.IOException,
            )
            assertEquals(-99, returnedBytes.get())
            assertTrue("Buffer must not contain stale bytes after revocation", tail.all { it == 0.toByte() })
        } finally {
            if (reader.isAlive) core.lan()?.close()
            reader.join(2000)
        }
    }

    private fun stalledAudioResponse() =
        MockResponse()
            .setResponseCode(200)
            .setHeader("Content-Type", "audio/mpeg")
            .setBody("ID3A")
            // Keep the socket open without sending the remaining promised bytes.
            .setHeader("Content-Length", 68)

    @androidx.media3.common.util.UnstableApi
    @Test fun unpairAbortsBlockedInFlightLanAudioRead() {
        val config = tlsDesktop()
        val store = InMemoryCredentialStore().apply { pairing = config }
        val core = PrunerAppContainer(app, store)
        val fileId = "a".repeat(24)
        respond { path ->
            if (path.endsWith("/local-library/stream/$fileId")) stalledAudioResponse()
            else MockResponse().setResponseCode(404)
        }
        assertBlockedInFlightLanReadAborts(config, core, fileId) {
            runBlocking { core.lanSession.unpair() }
        }
        assertNull(store.pairing)
    }

    @androidx.media3.common.util.UnstableApi
    @Test fun deviceRevokedSseAbortsBlockedInFlightLanAudioRead() {
        val config = tlsDesktop()
        val store = InMemoryCredentialStore().apply { pairing = config }
        val core = PrunerAppContainer(app, store)
        val fileId = "b".repeat(24)
        val sseSubscribed = CountDownLatch(1)
        val emitRevoke = CountDownLatch(1)
        respond { path ->
            when {
                path.endsWith("/local-library/stream/$fileId") -> stalledAudioResponse()
                path.endsWith("/events") -> {
                    sseSubscribed.countDown()
                    check(emitRevoke.await(15, TimeUnit.SECONDS))
                    MockResponse()
                        .setHeader("Content-Type", "text/event-stream")
                        .setBody("event: device_revoked\ndata: {}\n\n")
                }
                else -> MockResponse().setResponseCode(404)
            }
        }
        try {
            val initJob = ui.runOnIdle { CoroutineScope(Dispatchers.Main.immediate).launch { core.lanSession.ensureConnected() } }
            waitFor { initJob.isCompleted }
            assertTrue("SSE subscription did not reach server", sseSubscribed.await(5, TimeUnit.SECONDS))
            assertBlockedInFlightLanReadAborts(config, core, fileId) {
                emitRevoke.countDown()
            }
            waitFor { store.pairing == null && core.lanSession.state.value.pairing == null }
        } finally {
            emitRevoke.countDown()
            val cleanup = ui.runOnIdle { CoroutineScope(Dispatchers.Main.immediate).launch { core.lanSession.unpair() } }
            waitFor { cleanup.isCompleted }
        }
    }

    @androidx.media3.common.util.UnstableApi
    @Test fun replacePairingAbortsBlockedInFlightLanAudioRead() {
        val configA = tlsDesktop()
        val store = InMemoryCredentialStore().apply { pairing = configA }
        val core = PrunerAppContainer(app, store)
        val fileId = "c".repeat(24)
        val serverB = MockWebServer()
        val certB = HeldCertificate.Builder().commonName("desktop-b").build()
        val pinB = MessageDigest.getInstance("SHA-256").digest(certB.certificate.encoded)
            .joinToString("") { "%02x".format(it) }
        serverB.protocols = listOf(okhttp3.Protocol.HTTP_1_1)
        serverB.useHttps(HandshakeCertificates.Builder().heldCertificate(certB).build().sslSocketFactory(), false)
        serverB.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path.orEmpty()
                return when {
                    path.endsWith("/pair/claim") -> MockResponse().setBody(
                        """{"protocol_version":1,"device_id":"device-b","token":"token-b","server_name":"Desktop B","account":{"account_id":"account-b"}}"""
                    )
                    path.endsWith("/events") -> MockResponse()
                        .setHeader("Content-Type", "text/event-stream")
                        .setBody("event: connected\ndata: {}\n\n")
                    path.endsWith("/handoff") -> MockResponse().setBody("null")
                    path.endsWith("/sync/pull") -> MockResponse().setBody(
                        """{"account_id":"account-b","sync_until":"2026-11-25T00:00:00.000000+00:00","play_history":[],"feedback":[],"history_has_more":false,"feedback_has_more":false}"""
                    )
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        serverB.start()
        respond { path ->
            when {
                path.endsWith("/local-library/stream/$fileId") -> stalledAudioResponse()
                path.endsWith("/events") -> MockResponse()
                    .setHeader("Content-Type", "text/event-stream")
                    .setBody("event: connected\ndata: {}\n\n")
                path.endsWith("/handoff") -> MockResponse().setBody("null")
                else -> MockResponse().setResponseCode(404)
            }
        }
        try {
            val initJob = ui.runOnIdle { CoroutineScope(Dispatchers.Main.immediate).launch { core.lanSession.ensureConnected() } }
            waitFor { initJob.isCompleted }
            val candidateB = PairingUri(serverB.hostName, serverB.port, "123456", pinB, "Desktop B")
            val claimed = java.util.concurrent.atomic.AtomicBoolean(false)
            assertBlockedInFlightLanReadAborts(configA, core, fileId) {
                val claimJob = ui.runOnIdle {
                    CoroutineScope(Dispatchers.Main.immediate).launch {
                        claimed.set(core.lanSession.claim(candidateB, configA))
                    }
                }
                waitFor { claimJob.isCompleted }
            }
            assertTrue("Expected pairing replacement claim() to succeed", claimed.get())
            assertEquals("Desktop B", store.pairing?.name)
            assertEquals(serverB.port, store.pairing?.port)
            assertEquals(pinB, store.pairing?.fingerprint)
        } finally {
            val cleanup = ui.runOnIdle { CoroutineScope(Dispatchers.Main.immediate).launch { core.lanSession.unpair() } }
            waitFor { cleanup.isCompleted }
            serverB.shutdown()
        }
    }
}
