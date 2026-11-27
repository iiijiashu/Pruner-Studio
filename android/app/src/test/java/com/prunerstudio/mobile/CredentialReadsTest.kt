package com.prunerstudio.mobile

import com.prunerstudio.mobile.core.lan.LanSyncClient
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.*
import com.prunerstudio.mobile.core.player.buildQueueItems
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

class CredentialReadsTest {
    @Test
    fun cookieAccessRunsOffCallerThread() = runBlocking {
        val caller = Thread.currentThread()
        var reads = 0
        val stopBeforeNetwork = IllegalStateException("stop before network")
        val store =
            object : SecureCredentialStore by InMemoryCredentialStore() {
                override var cookie: String?
                    get() {
                        assertNotSame(caller, Thread.currentThread())
                        reads++
                        throw stopBeforeNetwork
                    }
                    set(value) = error("unexpected write")
            }
        val client = NeteaseApiClient(store)
        assertEquals(
            stopBeforeNetwork.message,
            runCatching { client.createQrKey() }.exceptionOrNull()?.message,
        )
        assertEquals(
            stopBeforeNetwork.message,
            runCatching { client.resolveAudioUrl(1) }.exceptionOrNull()?.message,
        )
        assertEquals(2, reads)
    }
}

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class PlaybackQueueTest {
    @Test
    fun onePairingReadForEntireLanQueue() = runBlocking {
        val caller = Thread.currentThread()
        var reads = 0
        val config = PairedServerConfig("localhost", 1234, "00".repeat(32), "", "", "account")
        val lan = LanSyncClient(config, InMemoryCredentialStore())
        val tracks = List(12) { Track(provider = "local", external_id = "%024x".format(it)) }
        val items =
            buildQueueItems("account", tracks, PlayMode.order) {
                assertNotSame(caller, Thread.currentThread())
                reads++
                lan
            }
        assertEquals(tracks.map { it.identity() }, items.map { it.mediaId })
        assertEquals(1, reads)
        assertEquals(
            1,
            buildQueueItems("account", listOf(Track(id = 1)), PlayMode.order) {
                    error("unexpected pairing read")
                }
                .size,
        )
    }
}
