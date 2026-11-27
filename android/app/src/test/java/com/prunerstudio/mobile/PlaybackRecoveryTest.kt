package com.prunerstudio.mobile

import android.app.Application
import android.net.Uri
import androidx.lifecycle.viewModelScope
import androidx.media3.common.PlaybackException
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import com.prunerstudio.mobile.core.db.PrunerDatabaseHelper
import com.prunerstudio.mobile.core.db.PrunerRepository
import com.prunerstudio.mobile.core.lan.PinnedTlsFactory
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.InMemoryCredentialStore
import com.prunerstudio.mobile.core.netease.NeteaseApiClient
import com.prunerstudio.mobile.core.player.*
import com.prunerstudio.mobile.ui.state.PrunerViewModel
import java.io.IOException
import java.net.ConnectException
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.spec.SecretKeySpec
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
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
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@androidx.media3.common.util.UnstableApi
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class PlaybackRecoveryTest {
    private val servers = mutableListOf<MockWebServer>()

    @After
    fun tearDown() {
        servers.forEach { runCatching { it.shutdown() } }
        servers.clear()
    }

    private fun eapiHexResponse(plainJson: String): String {
        val cipher = Cipher.getInstance("AES/ECB/PKCS5Padding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec("e82ckenh8dichen8".toByteArray(), "AES"))
        return cipher.doFinal(plainJson.toByteArray()).joinToString("") { "%02X".format(it) }
    }

    private fun mockNeteaseClient(store: InMemoryCredentialStore = InMemoryCredentialStore()): Pair<MockWebServer, NeteaseApiClient> {
        val server = MockWebServer()
        val cert = HeldCertificate.Builder().commonName("netease-mock").build()
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
                store,
                Call.Factory { original ->
                    http.newCall(
                        original.newBuilder().url(server.url(original.url.encodedPath)).build()
                    )
                },
            )
        return server to client
    }

    @Test
    fun sanitizeResumePositionHandlesEofNegativeAndNan() {
        assertEquals(0L, sanitizeResumePositionMs(Double.NaN, 180.0))
        assertEquals(0L, sanitizeResumePositionMs(-5.0, 180.0))
        assertEquals(45_200L, sanitizeResumePositionMs(45.2, 180.0))
        // Near-EOF or past-EOF must reset to start so Play never deadlocks in STATE_ENDED.
        assertEquals(0L, sanitizeResumePositionMs(179.2, 180.0))
        assertEquals(0L, sanitizeResumePositionMs(210.0, 180.0))
        // Unknown duration keeps positive saved position.
        assertEquals(30_000L, sanitizeResumePositionMs(30.0, 0.0))
    }

    @Test
    fun prepareRestorableQueueRecoversEmptyQueueOutOfBoundsAndUnpairedLan() {
        val netease = Track(id = 101, song_id = 101, provider = "netease", title = "A", duration_ms = 200_000)
        val local = Track(provider = "local", file_id = "a".repeat(24), title = "LAN Track", duration_ms = 180_000)
        val device = Track(provider = "device", external_id = "9", stream_url = "content://media/external/audio/media/9", title = "Local", duration_ms = 120_000)

        // 1. Empty queue falls back to current_track
        val singlePlan =
            prepareRestorableQueue(
                account = "acct",
                saved =
                    PlaybackSessionSnapshot(
                        current_track = netease,
                        queue = emptyList(),
                        queue_index = 4,
                        position_sec = 42.0,
                        duration_sec = 200.0,
                        volume = 0.75,
                        muted = true,
                        playback_rate = 1.25,
                    ),
                pairedAccountId = null,
            )
        assertNotNull(singlePlan)
        assertEquals(listOf(netease), singlePlan!!.queue)
        assertEquals(0, singlePlan.index)
        assertEquals(42_000L, singlePlan.positionMs)
        assertEquals(0.75f, singlePlan.volume, 0.001f)
        assertTrue(singlePlan.muted)
        assertEquals(1.25f, singlePlan.speed, 0.001f)

        // 2. Unpaired mixed queue drops LAN track and remaps target index safely
        val mixedPlan =
            prepareRestorableQueue(
                account = "acct",
                saved =
                    PlaybackSessionSnapshot(
                        current_track = device,
                        queue = listOf(local, netease, device),
                        queue_index = 2,
                        position_sec = 15.0,
                    ),
                pairedAccountId = null,
            )
        assertNotNull(mixedPlan)
        assertEquals(listOf(netease, device), mixedPlan!!.queue)
        assertEquals(1, mixedPlan.index)
        assertEquals(15_000L, mixedPlan.positionMs)

        // 3. Target track was unpaired LAN track -> drops it, resets position to 0 on surviving track
        val droppedTarget =
            prepareRestorableQueue(
                account = "acct",
                saved =
                    PlaybackSessionSnapshot(
                        current_track = local,
                        queue = listOf(local, netease),
                        queue_index = 0,
                        position_sec = 60.0,
                    ),
                pairedAccountId = null,
            )
        assertNotNull(droppedTarget)
        assertEquals(listOf(netease), droppedTarget!!.queue)
        assertEquals(0, droppedTarget.index)
        assertEquals(0L, droppedTarget.positionMs)

        // 4. Only unpaired LAN tracks -> returns null cleanly instead of throwing
        assertNull(
            prepareRestorableQueue(
                account = "acct",
                saved = PlaybackSessionSnapshot(current_track = local, queue = listOf(local), queue_index = 0),
                pairedAccountId = null,
            )
        )
    }

    @Test
    fun slicePlayableWindowCapsLargeQueuesAroundTargetIndex() {
        val tracks = (0 until 2500).map { Track(id = it.toLong() + 1, title = "Song $it") }
        val (sliced, mappedIndex) = slicePlayableWindow(tracks, 1800, maxSize = 2000)
        assertEquals(2000, sliced.size)
        assertEquals("Song 1800", sliced[mappedIndex].title)
    }

    @Test
    fun errorClassificationAndSingleAutoRetryDecisions() {
        val neteaseTrack = Track(id = 42, song_id = 42, provider = "netease", title = "Track")
        val lanTrack = Track(provider = "local", file_id = "b".repeat(24), title = "Lan")
        val deviceTrack = Track(provider = "device", stream_url = "content://audio/1", title = "Dev")
        val spec = DataSpec(Uri.parse("https://m801.music.126.net/test.flac"))

        val vipErr =
            PlaybackException(
                "vip",
                AudioResolutionException(AudioErrorCategory.VIP_OR_TRIAL_ONLY),
                PlaybackException.ERROR_CODE_IO_UNSPECIFIED,
            )
        assertEquals(AudioErrorCategory.VIP_OR_TRIAL_ONLY, classifyPlaybackError(vipErr, neteaseTrack).first)
        assertEquals(AutoRetryAction.NONE, shouldAutoRetryOnce(vipErr, neteaseTrack, 10_000, false))

        val expiredCdn =
            PlaybackException(
                "403",
                HttpDataSource.InvalidResponseCodeException(403, "Forbidden", null, emptyMap(), spec, ByteArray(0)),
                PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS,
            )
        assertEquals(AudioErrorCategory.STREAM_EXPIRED, classifyPlaybackError(expiredCdn, neteaseTrack).first)
        assertEquals(AutoRetryAction.RETRY_SAME_POSITION, shouldAutoRetryOnce(expiredCdn, neteaseTrack, 25_000, false))
        assertEquals(AutoRetryAction.NONE, shouldAutoRetryOnce(expiredCdn, neteaseTrack, 25_000, true))

        val range416 =
            PlaybackException(
                "416",
                HttpDataSource.InvalidResponseCodeException(416, "Range Not Satisfiable", null, emptyMap(), spec, ByteArray(0)),
                PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS,
            )
        assertEquals(AutoRetryAction.RETRY_FROM_START, shouldAutoRetryOnce(range416, neteaseTrack, 90_000, false))
        assertEquals(AutoRetryAction.NONE, shouldAutoRetryOnce(range416, neteaseTrack, 0, false))

        val lanAuth =
            PlaybackException(
                "401",
                HttpDataSource.InvalidResponseCodeException(401, "Unauthorized", null, emptyMap(), spec, ByteArray(0)),
                PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS,
            )
        assertEquals(AudioErrorCategory.LAN_UNPAIRED, classifyPlaybackError(lanAuth, lanTrack).first)
        assertEquals(AutoRetryAction.NONE, shouldAutoRetryOnce(lanAuth, lanTrack, 5_000, false))

        val lanOffline =
            PlaybackException("conn", ConnectException("refused"), PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED)
        assertEquals(AudioErrorCategory.LAN_UNREACHABLE, classifyPlaybackError(lanOffline, lanTrack).first)
        assertEquals(AudioErrorCategory.NETWORK_ERROR, classifyPlaybackError(lanOffline, neteaseTrack).first)

        val codecErr = PlaybackException("parse", IOException("bad header"), PlaybackException.ERROR_CODE_PARSING_CONTAINER_MALFORMED)
        assertEquals(AudioErrorCategory.CODEC_ERROR, classifyPlaybackError(codecErr, neteaseTrack).first)

        val fileErr = PlaybackException("file", IOException("missing"), PlaybackException.ERROR_CODE_IO_FILE_NOT_FOUND)
        assertEquals(AudioErrorCategory.DEVICE_FILE_ERROR, classifyPlaybackError(fileErr, deviceTrack).first)
    }

    @Test
    fun neteaseEapiDecryptsTrailingWhitespaceHexAndFallsBackAcrossQualities() = runBlocking {
        val (server, client) = mockNeteaseClient()
        // Tier 1 (jymaster): HTTP 500 error -> should fall back instead of aborting
        server.enqueue(MockResponse().setResponseCode(500).setBody("server error"))
        // Tier 2 (hires): freeTrialInfo -> should fall back to lossless
        val trialJson = """{"code":200,"data":[{"id":77,"url":"http://m801.music.163.com/trial.mp3","freeTrialInfo":{"start":0,"end":30}}]}"""
        server.enqueue(MockResponse().setBody(eapiHexResponse(trialJson) + "\r\n"))
        // Tier 3 (lossless): valid full audio URL with trailing newline in hex ciphertext
        val okJson = """{"code":200,"data":[{"id":77,"url":"http://m801.music.126.net/full.flac","freeTrialInfo":null}]}"""
        server.enqueue(MockResponse().setBody("  " + eapiHexResponse(okJson) + "\n"))

        val resolved = client.resolveAudioUrlOrThrow(77L, AudioQualityLevel.jymaster)
        assertEquals("https://m801.music.126.net/full.flac", resolved.toString())
        assertEquals(3, server.requestCount)
    }

    @Test
    fun neteaseResolutionCategorizesVipParseAndLoginErrors() = runBlocking {
        val (server, client) = mockNeteaseClient()

        // All tiers return freeTrialInfo -> VIP_OR_TRIAL_ONLY
        val trialJson = """{"code":200,"data":[{"id":1,"url":"http://m7.music.126.net/t.mp3","freeTrialInfo":{"start":0,"end":30}}]}"""
        server.enqueue(MockResponse().setBody(trialJson))
        val vipErr = runCatching { client.resolveAudioUrlOrThrow(1L, AudioQualityLevel.standard) }.exceptionOrNull()
        assertTrue(vipErr is AudioResolutionException)
        assertEquals(AudioErrorCategory.VIP_OR_TRIAL_ONLY, (vipErr as AudioResolutionException).category)

        // Malformed HTML response -> PARSE_ERROR
        server.enqueue(MockResponse().setBody("<html>WAF blocked</html>"))
        val parseErr = runCatching { client.resolveAudioUrlOrThrow(2L, AudioQualityLevel.standard) }.exceptionOrNull()
        assertTrue(parseErr is AudioResolutionException)
        assertEquals(AudioErrorCategory.PARSE_ERROR, (parseErr as AudioResolutionException).category)

        // Code 301 -> LOGIN_REQUIRED
        server.enqueue(MockResponse().setBody("""{"code":301}"""))
        val loginErr = runCatching { client.resolveAudioUrlOrThrow(3L, AudioQualityLevel.standard) }.exceptionOrNull()
        assertTrue(loginErr is AudioResolutionException)
        assertEquals(AudioErrorCategory.LOGIN_REQUIRED, (loginErr as AudioResolutionException).category)
    }

    @Test
    fun fetchPlaylistTracksToleratesDeletedSongsWhenNotStrictAndRejectsWhenStrict() = runBlocking {
        val (server, client) = mockNeteaseClient()
        val detailPayload =
            """{"code":200,"playlist":{"trackCount":3,"trackIds":[{"id":10},{"id":20},{"id":10}],"tracks":[{"id":10,"name":"Alive","ar":[{"id":1,"name":"Artist"}],"al":{"name":"Album"},"dt":180000}]}}"""
        val emptySongDetail = """{"code":200,"songs":[]}"""

        server.enqueue(MockResponse().setBody(detailPayload))
        server.enqueue(MockResponse().setBody(emptySongDetail))
        val browsed = client.fetchPlaylistTracks(999L, strict = false)
        assertEquals(1, browsed.size)
        assertEquals(10L, browsed.single().song_id)

        server.enqueue(MockResponse().setBody(detailPayload))
        val strictErr = runCatching { client.fetchPlaylistTracks(999L, strict = true) }.exceptionOrNull()
        assertTrue("Expected strict count/duplicate mismatch rejection", strictErr is IllegalArgumentException)
    }

    @Test
    fun corruptSavedSessionDoesNotCrashRepositoryRead() {
        val helper = PrunerDatabaseHelper(RuntimeEnvironment.getApplication())
        try {
            helper.writableDatabase.execSQL(
                "INSERT OR REPLACE INTO playback_session VALUES('acct','current','{corrupt json')"
            )
            val repo = PrunerRepository(helper)
            assertNull(repo.session("acct"))
        } finally {
            helper.close()
        }
    }

    @Test
    fun viewModelSurfacesRepeatedPlaybackErrorsAndGuardsEmptyModeSwitch() {
        val app: Application = RuntimeEnvironment.getApplication()
        val core = PrunerAppContainer(app, InMemoryCredentialStore())
        val vm = PrunerViewModel(app, core)
        val received = java.util.Collections.synchronizedList(mutableListOf<String>())
        val collector =
            kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
                vm.messages.collect { received.add(it) }
            }
        fun pumpUntil(timeoutMs: Long = 5000, predicate: () -> Boolean) {
            val deadline = System.currentTimeMillis() + timeoutMs
            while (System.currentTimeMillis() < deadline) {
                org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()
                if (predicate()) return
                Thread.sleep(15)
            }
            fail("Condition not met within ${timeoutMs}ms; received=$received")
        }
        try {
            // Mode switch on empty queue / invalid index must not throw.
            vm.mode(PlayMode.shuffle)
            vm.mode(PlayMode.loop)
            org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()

            // Repeated identical error code with new errorToken must notify every time.
            val snap = PlaybackSessionSnapshot(current_track = Track(id = 7, title = "Test"))
            core.playback.update(
                PlaybackUiState(
                    snapshot = snap,
                    error = "ERROR_CODE_IO_BAD_HTTP_STATUS",
                    errorMessage = AudioErrorCategory.STREAM_EXPIRED.userMessage,
                    errorToken = 1L,
                )
            )
            pumpUntil { received.size >= 1 }
            assertEquals(AudioErrorCategory.STREAM_EXPIRED.userMessage, received[0])

            core.playback.update(
                PlaybackUiState(
                    snapshot = snap,
                    error = "ERROR_CODE_IO_BAD_HTTP_STATUS",
                    errorMessage = AudioErrorCategory.STREAM_EXPIRED.userMessage,
                    errorToken = 2L,
                )
            )
            pumpUntil { received.size >= 2 }
            assertEquals(AudioErrorCategory.STREAM_EXPIRED.userMessage, received[1])

            // Playing an unpaired LAN track reports clear LAN_UNPAIRED message.
            vm.play(listOf(Track(provider = "local", file_id = "c".repeat(24), title = "Desktop Song")))
            pumpUntil { received.size >= 3 }
            assertEquals(AudioErrorCategory.LAN_UNPAIRED.userMessage, received[2])
        } finally {
            collector.cancel()
            vm.viewModelScope.cancel()
        }
    }
}
