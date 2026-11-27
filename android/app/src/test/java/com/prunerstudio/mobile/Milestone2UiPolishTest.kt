package com.prunerstudio.mobile

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import com.prunerstudio.mobile.core.engine.LyricsParser
import com.prunerstudio.mobile.core.model.*
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import com.prunerstudio.mobile.core.player.buildQueueItems
import com.prunerstudio.mobile.core.player.queueEntryId
import kotlinx.coroutines.runBlocking
import com.prunerstudio.mobile.ui.screens.computeWordProgress
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*
import kotlinx.serialization.json.JsonPrimitive
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp")
class Milestone2UiPolishTest {
    @get:Rule val ui = createComposeRule()
    private lateinit var vm: PrunerViewModel
    private var overrideVm by mutableStateOf<PrunerViewModel?>(null)

    @Before
    fun setup() {
        val app = RuntimeEnvironment.getApplication()
        app.getSharedPreferences("pruner_ui_prefs", 0).edit().clear().commit()
        overrideVm = null
        ui.runOnIdle { vm = PrunerViewModel(app) }
        ui.setContent {
            val active = overrideVm ?: vm
            PrunerTheme(active.theme, active.accent, active.scale) { PrunerShell(active) }
        }
    }

    @Test
    fun yrcWordProgressComputesSmoothBoundaries() {
        val w = LyricWord(startMs = 1000L, durationMs = 400L, text = "光")
        assertEquals(0f, computeWordProgress(w, 999L), 0.001f)
        assertEquals(0f, computeWordProgress(w, 1000L), 0.001f)
        assertEquals(0.5f, computeWordProgress(w, 1200L), 0.001f)
        assertEquals(1f, computeWordProgress(w, 1400L), 0.001f)
        assertEquals(1f, computeWordProgress(w, 2500L), 0.001f)
        val zeroDur = LyricWord(startMs = 500L, durationMs = 0L, text = "!")
        assertEquals(1f, computeWordProgress(zeroDur, 500L), 0.001f)
    }

    @Test
    fun stageDisplaysHumanQualityLabelYrcLyricsAndFullLyricsToggle() {
        val track = Track(id = 10, song_id = 10, provider = "netease", title = "晴天", artist = "周杰伦", album = "叶惠美")
        ui.runOnIdle {
            vm.preferences(level = AudioQualityLevel.jymaster)
            vm.state =
                PlaybackSessionSnapshot(
                    current_track = track,
                    queue = listOf(track),
                    queue_index = 0,
                    duration_sec = 269.0,
                    position_sec = 1.2,
                )
            vm.lyrics =
                LyricsParser.parse(
                    lrc = "",
                    yrc = "[1000,800](1000,400,0)故事(1400,400,0)的小黄花",
                    tlyric = "[00:01]Little yellow flower",
                )
        }
        // Open stage from MiniPlayer
        ui.onNodeWithText("晴天").performClick()
        // Quality badge maps raw "jymaster" to human-readable "超清母带"
        ui.onNodeWithText("超清母带").assertExists()
        ui.onNodeWithText("jymaster").assertDoesNotExist()
        // Synced YRC lyric line visible
        ui.onAllNodesWithText("故事的小黄花").onLast().assertExists()
        // Enter full-screen lyrics mode
        ui.onNodeWithText("词").performClick()
        ui.onNodeWithText("返回封面").assertExists()
        // Toggle translation off and verify persistence flag updated
        ui.onNodeWithText("译").performClick()
        ui.runOnIdle { assertFalse(vm.showLyricsTranslation) }
    }

    @Test
    fun tabSwitchingPreservesLibrarySectionAndOpensDedicatedSearchRoute() {
        // Switch to Library and select "桌面曲库"
        ui.onAllNodesWithText("音乐库").onLast().performClick()
        ui.onNodeWithText("桌面曲库").performClick()
        ui.runOnIdle { assertEquals(1, vm.librarySection) }

        // Navigate away to History then back to Library -> section must be preserved!
        ui.onAllNodesWithText("历史").onLast().performClick()
        ui.onAllNodesWithText("音乐库").onLast().performClick()
        ui.runOnIdle { assertEquals(1, vm.librarySection) }
        ui.onNodeWithText("搜索桌面曲库").assertExists()

        // Open dedicated Search route from search bar
        ui.onNodeWithText("搜索曲库、桌面与在线音乐").performClick()
        ui.onNodeWithText("跨源聚合搜索").assertExists()
        ui.onNodeWithContentDescription("返回").performClick()
        ui.onNodeWithText("搜索桌面曲库").assertExists()
    }

    @Test
    fun scalesCleanlyAt125PercentFontScaleAcrossAllDestinationsAndStage() {
        val track = Track(id = 88, song_id = 88, title = "稻香", artist = "周杰伦")
        ui.runOnIdle {
            vm.preferences(font = 1.25f)
            vm.playlists = listOf(PlaylistSummary(JsonPrimitive(1), "通勤精选", "self_created", 24))
            vm.state =
                PlaybackSessionSnapshot(
                    current_track = track,
                    queue = listOf(track),
                    queue_index = 0,
                    duration_sec = 220.0,
                    position_sec = 30.0,
                )
        }
        for (label in listOf("发现", "音乐库", "历史", "清理", "设置")) {
            ui.onAllNodesWithText(label).onLast().performClick()
            ui.waitForIdle()
        }
        // Open Queue sheet and verify clear queue button exists
        ui.onNodeWithContentDescription("播放队列").performClick()
        ui.onNodeWithText("播放队列").assertExists()
        ui.onNodeWithText("清空").assertExists()
    }

    @Test
    fun deckPlaylistPickerSheetOpensCleanly() {
        ui.runOnIdle {
            vm.playlists = listOf(PlaylistSummary(JsonPrimitive(55), "深夜爵士", "self_created", 18))
            vm.tab = Tab.DECK
        }
        ui.onNodeWithText("选择待整理歌单").performClick()
        ui.onNodeWithText("深夜爵士").assertExists()
        ui.onNodeWithText("自建").assertExists()
    }

    @Test
    fun livePositionPreservesProgressAcrossDecoupledStateAndQueueKeysStayStable() {
        val trackA = Track(id = 1, song_id = 1, title = "Song A")
        val trackB = Track(id = 2, song_id = 2, title = "Song B")
        val app = RuntimeEnvironment.getApplication()
        val core = com.prunerstudio.mobile.core.player.PrunerAppContainer(app, com.prunerstudio.mobile.core.netease.InMemoryCredentialStore())
        val isolatedVm = ui.runOnIdle { PrunerViewModel(app, core) }
        ui.runOnIdle {
            isolatedVm.state =
                PlaybackSessionSnapshot(
                    current_track = trackA,
                    queue = listOf(trackA, trackB),
                    queue_index = 0,
                    duration_sec = 240.0,
                    position_sec = 0.0,
                    is_playing = false,
                )
            // Simulate PrunerPlaybackService ticking decoupled PlaybackProgress to 135.4s while state.position_sec stays 0.0
            core.playback.updateProgress(
                com.prunerstudio.mobile.core.player.PlaybackProgress(
                    positionMs = 135_400L,
                    durationMs = 240_000L,
                    isPlaying = false,
                    playbackRate = 1f,
                    updatedAtElapsedMs = 1000L,
                )
            )
        }
        ui.waitUntil(3000) { isolatedVm.playbackProgress.value.positionMs == 135_400L }
        ui.runOnIdle {
            assertEquals("Structural snapshot stays decoupled at 0.0", 0.0, isolatedVm.state.position_sec, 0.01)
            assertEquals("Live position reads decoupled progress stream", 135.4, isolatedVm.currentPositionSec(), 0.05)
        }
        // YRC translation alignment tolerates slight <=150ms NetEase timestamp drift
        val parsed =
            LyricsParser.parse(
                lrc = "",
                yrc = "[1040,600](1040,300,0)晴(1340,300,0)天",
                tlyric = "[00:01.00]Sunny Day",
                romalrc = "[00:01.05]qing tian",
            )
        assertEquals("Sunny Day", parsed.lines.single().translation)
        assertEquals("qing tian", parsed.lines.single().romaji)
    }

    @Test
    @OptIn(UnstableApi::class)
    fun duplicateQueueRowKeepsItsOwnKeyWhenFirstCopyIsDismissed() = runBlocking {
        val duplicate = Track(id = 1)
        val queue = buildQueueItems("account", listOf(duplicate, Track(id = 2), duplicate), PlayMode.order) { null }
            .toMutableList()
        val ids = queue.map { requireNotNull(it.queueEntryId()) }
        assertEquals(3, ids.toSet().size)
        queue.removeAt(0)
        assertEquals(listOf(ids[1], ids[2]), queue.map { it.queueEntryId() })
    }

    @Test
    fun searchScreenShowsMultiSourceTabWhenPairedWithoutLocalExternalSources() {
        val cert = okhttp3.tls.HeldCertificate.Builder().commonName("desktop-search-ui").build()
        val spki = com.prunerstudio.mobile.core.lan.PinnedTlsFactory.computeSpkiPin(cert.certificate)
        val fp = com.prunerstudio.mobile.core.lan.PinnedTlsFactory.computeCertFingerprint(cert.certificate)
        val server =
            okhttp3.mockwebserver.MockWebServer().apply {
                protocols = listOf(okhttp3.Protocol.HTTP_1_1)
                useHttps(
                    okhttp3.tls.HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(),
                    false,
                )
                dispatcher =
                    object : okhttp3.mockwebserver.Dispatcher() {
                        override fun dispatch(
                            request: okhttp3.mockwebserver.RecordedRequest
                        ): okhttp3.mockwebserver.MockResponse {
                            val path = request.path.orEmpty()
                            return when {
                                path.endsWith("/events") ->
                                    okhttp3.mockwebserver.MockResponse()
                                        .setHeader("Content-Type", "text/event-stream")
                                        .setBody("event: connected\ndata: {}\n\n")
                                path.endsWith("/handoff") ->
                                    okhttp3.mockwebserver.MockResponse().setBody("null")
                                path.contains("/local-library") ->
                                    okhttp3.mockwebserver.MockResponse().setBody(
                                        """{"files":[],"total":0,"offset":0,"limit":100,"has_more":false}"""
                                    )
                                path.contains("/search/multi") ->
                                    okhttp3.mockwebserver.MockResponse().setBody(
                                        """{"query":"晴天","has_more":false,"channels_queried":["bilibili"],"channel_errors":{},"tracks":[{"provider":"bilibili","external_id":"BV1xx411c7mD","track_key":"bilibili:BV1xx411c7mD","title":"晴天 桌面多平台","artists":["周杰伦"],"duration_ms":269000}]}"""
                                    )
                                else -> okhttp3.mockwebserver.MockResponse().setResponseCode(404)
                            }
                        }
                    }
                start()
            }
        val app = RuntimeEnvironment.getApplication()
        val creds =
            com.prunerstudio.mobile.core.netease.InMemoryCredentialStore().apply {
                pairing =
                    PairedServerConfig(
                        host = server.hostName,
                        port = server.port,
                        fingerprint = fp,
                        token = "ui-token",
                        deviceId = "dev-ui",
                        accountId = "acct-ui",
                        name = "Studio Desk",
                        spki_pin = spki,
                    )
            }
        val offlineNetease =
            com.prunerstudio.mobile.core.netease.NeteaseApiClient(
                creds,
                okhttp3.Call.Factory { req ->
                    object : okhttp3.Call {
                        private val canceled = java.util.concurrent.atomic.AtomicBoolean(false)
                        override fun request() = req
                        override fun execute(): okhttp3.Response = throw java.io.IOException("Offline in test")
                        override fun enqueue(responseCallback: okhttp3.Callback) {
                            responseCallback.onFailure(this, java.io.IOException("Offline in test"))
                        }
                        override fun cancel() { canceled.set(true) }
                        override fun isExecuted() = true
                        override fun isCanceled() = canceled.get()
                        override fun timeout() = okio.Timeout.NONE
                        override fun clone() = this
                    }
                },
            )
        val core = com.prunerstudio.mobile.core.player.PrunerAppContainer(app, creds, offlineNetease)
        val pairedVm = ui.runOnIdle { PrunerViewModel(app, core) }
        fun waitUntilIdle(predicate: () -> Boolean) =
            ui.waitUntil(5000) {
                org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper())
                    .idleFor(java.time.Duration.ofMillis(50))
                predicate()
            }
        try {
            // Unpaired default VM with no external sources hides the multi-source tab
            ui.onAllNodesWithText("音乐库").onLast().performClick()
            ui.onNodeWithText("搜索曲库、桌面与在线音乐").performClick()
            ui.onNodeWithText("多源 / 外部").assertDoesNotExist()
            ui.onNodeWithContentDescription("返回").performClick()

            // Paired VM with zero local externalSources still exposes multi-source tab & desktop search results
            waitUntilIdle { pairedVm.desktop != null }
            ui.runOnIdle {
                assertTrue(pairedVm.externalSources.isEmpty())
                assertTrue(pairedVm.externalResults.isEmpty())
                assertTrue(pairedVm.hasMultiSourceSearch)
                pairedVm.tab = Tab.LIBRARY
                overrideVm = pairedVm
            }
            ui.onNodeWithText("搜索曲库、桌面与在线音乐").performClick()
            // Tab is visible BEFORE any results arrive solely because vm.desktop != null
            ui.onNodeWithText("多源 / 外部").assertExists()
            ui.onNodeWithText("多源 / 外部").performClick()

            // Real typing hits desktop GET /api/lan/v1/search/multi via CompositeAudioSourceResolver even when NetEase is offline
            ui.onNode(hasSetTextAction()).performTextInput("晴天")
            waitUntilIdle { pairedVm.externalResults.isNotEmpty() }
            ui.onNodeWithText("多源 / 外部音源").assertExists()
            ui.onNodeWithText("晴天 桌面多平台").assertExists()
            ui.onNodeWithText("Bilibili").assertExists()

            // Unpairing immediately clears stale desktop externalResults and hides the tab without needing manual clear
            ui.runOnIdle { pairedVm.unpair() }
            waitUntilIdle { pairedVm.desktop == null }
            ui.runOnIdle { assertTrue(pairedVm.externalResults.isEmpty()) }
            ui.onNodeWithText("多源 / 外部").assertDoesNotExist()
            ui.onNodeWithContentDescription("清空搜索").performClick()
        } finally {
            ui.runOnIdle {
                overrideVm = null
                pairedVm.unpair()
            }
            waitUntilIdle { pairedVm.desktop == null }
            core.database.close()
            server.shutdown()
        }
    }

    @androidx.media3.common.util.UnstableApi
    @Test
    fun coverRecomputesRelativeLanCoverUrlAcrossPairingTransitions() {
        val app = RuntimeEnvironment.getApplication()
        val creds = com.prunerstudio.mobile.core.netease.InMemoryCredentialStore()
        val container = com.prunerstudio.mobile.core.player.PrunerAppContainer(app, creds)
        val relUrl = "/api/lan/v1/cover?local_cover_id=0123456789abcdef01234567"
        try {
            // Unpaired: relative LAN cover cannot resolve
            assertNull(resolveCoverRequestUrl(relUrl, 192, app, appOverride = container))

            // Pair to desktop A -> lan() updates pairingState & resolves cover URL
            creds.pairing =
                PairedServerConfig(
                    host = "192.168.1.50",
                    port = 18443,
                    fingerprint = "a".repeat(64),
                    token = "tokA",
                    deviceId = "d1",
                    accountId = "acct1",
                    name = "Desk A",
                )
            assertNotNull(container.lan())
            assertEquals(creds.pairing, container.pairingState.value)
            assertEquals(
                "https://192.168.1.50:18443$relUrl",
                resolveCoverRequestUrl(relUrl, 192, app, appOverride = container),
            )

            // Re-pair to desktop B -> pairingState emits new host/port & resolves to B
            creds.pairing = creds.pairing!!.copy(host = "192.168.1.88", port = 19443, token = "tokB")
            assertNotNull(container.lan())
            assertEquals("192.168.1.88", container.pairingState.value?.host)
            assertEquals(
                "https://192.168.1.88:19443$relUrl",
                resolveCoverRequestUrl(relUrl, 192, app, appOverride = container),
            )

            // Unpair -> pairingState clears to null & relative LAN cover resolves back to null
            creds.pairing = null
            assertNull(container.lan())
            assertNull(container.pairingState.value)
            assertNull(resolveCoverRequestUrl(relUrl, 192, app, appOverride = container))
        } finally {
            container.database.close()
        }
    }
}
