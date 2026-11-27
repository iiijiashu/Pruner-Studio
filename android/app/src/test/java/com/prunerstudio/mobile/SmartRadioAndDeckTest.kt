package com.prunerstudio.mobile

import android.app.Application
import androidx.lifecycle.viewModelScope
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.InMemoryCredentialStore
import com.prunerstudio.mobile.core.player.PrunerAppContainer
import com.prunerstudio.mobile.ui.state.*
import kotlinx.coroutines.cancel
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SmartRadioAndDeckTest {

    @Test
    fun exploreRatioAcceptsFullZeroToOneRangeWithoutClampingToPointOneOrPointEight() {
        assertEquals(0.0f, sanitizeExploreRatio(0.0f), 0.0001f)
        assertEquals(0.0f, sanitizeExploreRatio(0.0), 0.0001f)
        assertEquals(1.0f, sanitizeExploreRatio(1.0f), 0.0001f)
        assertEquals(1.0f, sanitizeExploreRatio(1.0), 0.0001f)
        assertEquals(0.05f, sanitizeExploreRatio(0.05f), 0.0001f)
        assertEquals(0.95f, sanitizeExploreRatio(0.95f), 0.0001f)

        // Out-of-bounds and non-finite inputs clamp safely to [0.0, 1.0] / default
        assertEquals(0.0f, sanitizeExploreRatio(-0.25f), 0.0001f)
        assertEquals(1.0f, sanitizeExploreRatio(1.5f), 0.0001f)
        assertEquals(SMART_RADIO_EXPLORE_DEFAULT, sanitizeExploreRatio(Float.NaN), 0.0001f)
        assertEquals(SMART_RADIO_EXPLORE_DEFAULT, sanitizeExploreRatio(Double.POSITIVE_INFINITY), 0.0001f)

        // Exact dynamic percent & semantic labels across 0% / 50% / 100%
        assertEquals(0, exploreRatioPercent(0.0f))
        assertEquals("纯熟歌回味", exploreRatioLabel(0.0f))
        assertEquals(50, exploreRatioPercent(0.5f))
        assertEquals("熟歌与新声均衡", exploreRatioLabel(0.5f))
        assertEquals(100, exploreRatioPercent(1.0f))
        assertEquals("纯新歌探索", exploreRatioLabel(1.0f))

        // Wire JSON round-trip preserves 0.0 and 1.0 cleanly
        val zero = wire.decodeFromString<SmartRadioSettings>("""{"explore_ratio":0.0,"smart_radio_explore_ratio":0.0}""")
        val full = wire.decodeFromString<SmartRadioSettings>("""{"explore_ratio":1.0,"smart_radio_explore_ratio":1.0}""")
        assertEquals(0.0f, zero.normalizedRatio(), 0.0001f)
        assertEquals(1.0f, full.normalizedRatio(), 0.0001f)
    }

    @Test
    fun smartRadioQueueEndpointsStrictlyRespectZeroAndOneHundredPercent() {
        val familiar =
            (1L..6L).map { i ->
                Track(
                    id = i,
                    song_id = i,
                    title = "Familiar $i",
                    artist = "Artist F$i",
                    play_count = 5,
                    last_played_at = "2026-11-20T00:00:00Z",
                )
            }
        val novel =
            (101L..106L).map { i ->
                Track(
                    id = i,
                    song_id = i,
                    title = "Novel $i",
                    artist = "Artist N$i",
                    play_count = 0,
                )
            }
        val familiarIds = familiar.map { it.identity() }.toSet()
        val novelIds = novel.map { it.identity() }.toSet()

        // 0% -> strictly 0 novel tracks
        val pureFamiliar = buildSmartRadioQueue(familiar, familiar + novel, novel, exploreRatio = 0.0f, seed = 42L)
        assertEquals(familiar.size, pureFamiliar.size)
        assertTrue(pureFamiliar.all { it.identity() in familiarIds })
        assertEquals(0, pureFamiliar.count { it.identity() in novelIds })

        // 100% -> strictly 0 familiar tracks (no familiar backfill!)
        val pureNovel = buildSmartRadioQueue(familiar, familiar + novel, novel, exploreRatio = 1.0f, seed = 42L)
        assertEquals(novel.size, pureNovel.size)
        assertTrue(pureNovel.all { it.identity() in novelIds })
        assertEquals(0, pureNovel.count { it.identity() in familiarIds })

        // 50% -> blends both familiar and novel tracks
        val balanced = buildSmartRadioQueue(familiar, familiar + novel, novel, exploreRatio = 0.5f, seed = 42L)
        assertTrue(balanced.any { it.identity() in familiarIds })
        assertTrue(balanced.any { it.identity() in novelIds })
    }

    @Test
    fun viewModelPersistsZeroAndOneExploreRatioWithoutClamping() {
        val app: Application = RuntimeEnvironment.getApplication()
        val prefs = app.getSharedPreferences("pruner_ui_prefs", 0)
        prefs.edit().clear().commit()
        val core = PrunerAppContainer(app, InMemoryCredentialStore())
        val vm = PrunerViewModel(app, core)
        try {
            vm.updateExploreRatio(0.0f)
            assertEquals(0.0f, vm.exploreRatio, 0.0001f)
            Thread.sleep(60)
            Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()
            assertEquals(0.0f, prefs.getFloat("smart_radio_explore_ratio", -1f), 0.0001f)

            vm.updateExploreRatio(1.0f)
            assertEquals(1.0f, vm.exploreRatio, 0.0001f)
            Thread.sleep(60)
            Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()
            assertEquals(1.0f, prefs.getFloat("smart_radio_explore_ratio", -1f), 0.0001f)
        } finally {
            vm.viewModelScope.cancel()
        }
    }

    @Test
    fun deckDefaultsToRandomOrderPrioritizesValidTracksAndPreservesDecisions() {
        val app: Application = RuntimeEnvironment.getApplication()
        val core = PrunerAppContainer(app, InMemoryCredentialStore())
        val vm = PrunerViewModel(app, core)
        try {
            assertEquals(DeckOrder.RANDOM, vm.deckOrder)

            val stubBlank = Track(id = 9001, song_id = 9001, title = "")
            val stubId = Track(id = 9002, song_id = 9002, title = "歌曲 #9002")
            val stubDelisted = Track(id = 9003, song_id = 9003, title = "Old Song", availability = "delisted")
            val normal = (1L..8L).map { i -> Track(id = i, song_id = i, title = "Track $i", artist = "Singer $i", duration_ms = 180_000) }
            assertTrue(stubBlank.isStubTrack())
            assertTrue(stubId.isStubTrack())
            assertTrue(stubDelisted.isStubTrack())
            assertFalse(Track(id = 1, title = "曲目1").isStubTrack())

            val source = listOf(stubBlank, stubId, stubDelisted) + normal
            val pl = PlaylistSummary(JsonPrimitive(77), "Mixed Playlist")

            // Default RANDOM order places valid tracks ahead of stubs and is deterministic for the same seed
            vm.startDeck(pl, source, seed = 20261126L)
            val ordered = vm.deck.tracks
            assertEquals(source.size, ordered.size)
            assertTrue("Valid tracks must come before stubs in random order", ordered.take(normal.size).none { it.isStubTrack() })
            assertTrue("Stub tracks placed at the tail", ordered.drop(normal.size).all { it.isStubTrack() })
            assertNotEquals("Random order should differ from original normal sequence", normal.map { it.id }, ordered.take(normal.size).map { it.id })
            assertEquals(ordered.map { it.id }, orderDeckTracks(source, DeckOrder.RANDOM, 20261126L).map { it.id })

            // Decisions stay locked in place when rotating [换一首] or switching order
            val firstDecided = vm.deck.current!!
            vm.decide(true)
            val secondDecided = vm.deck.current!!
            vm.decide(false)
            val beforeSkip = vm.deck.current!!

            vm.skipToAnotherDeckTrack()
            assertEquals(listOf(true, false), vm.deck.decisions)
            assertEquals(firstDecided.identity(), vm.deck.tracks[0].identity())
            assertEquals(secondDecided.identity(), vm.deck.tracks[1].identity())
            assertNotEquals("换一首 must advance to a different undecided track", beforeSkip.identity(), vm.deck.current!!.identity())

            // Switching to ORIGINAL order preserves the decided prefix untouched
            vm.updateDeckOrder(DeckOrder.ORIGINAL)
            assertEquals(DeckOrder.ORIGINAL, vm.deckOrder)
            assertEquals(listOf(true, false), vm.deck.decisions)
            assertEquals(firstDecided.identity(), vm.deck.tracks[0].identity())
            assertEquals(secondDecided.identity(), vm.deck.tracks[1].identity())
        } finally {
            vm.viewModelScope.cancel()
        }
    }
}
