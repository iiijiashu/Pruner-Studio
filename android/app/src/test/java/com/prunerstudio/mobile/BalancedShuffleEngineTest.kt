package com.prunerstudio.mobile

import com.prunerstudio.mobile.core.engine.BalancedShuffleEngine
import com.prunerstudio.mobile.core.model.Track
import org.junit.Assert.*
import org.junit.Test

class BalancedShuffleEngineTest {
    @Test
    fun repairsArtistCollisionsWithoutDroppingTracks() {
        val tracks =
            (1..12).map {
                Track(
                    id = it.toLong(),
                    artist = if (it % 2 == 0) "A" else "B",
                    album = (it % 3).toString(),
                )
            }
        repeat(32) {
            val result = BalancedShuffleEngine.shuffle(tracks, seed = it.toLong())
            assertEquals(tracks.toSet(), result.toSet())
            assertEquals(12, result.size)
            assertTrue(result.zipWithNext().all { (a, b) -> a.artist != b.artist })
        }
    }

    @Test
    fun matchesSeededDesktopOrder() {
        val tracks =
            (0..23).map {
                Track(
                    id = it + 1L,
                    artist = listOf("a", "b", "c")[it % 3],
                    album = (it % 2).toString(),
                )
            }
        val history =
            listOf(1L, 4L, 8L, 7L).associateWith {
                Track(play_count = 1, last_played_at = "900000")
            }
        val result = BalancedShuffleEngine.shuffle(tracks, history, 17, 1000000)
        assertEquals(
            listOf(
                2,
                6,
                19,
                14,
                10,
                3,
                23,
                24,
                16,
                15,
                13,
                17,
                12,
                20,
                22,
                11,
                21,
                5,
                18,
                1,
                9,
                7,
                8,
                4,
            ),
            result.map { it.id!!.toInt() },
        )
    }

    @Test
    fun cooldownMovesTracksLaterAcrossSeeds() {
        val tracks = (1L..30L).map { Track(id = it, artist = "A$it") }
        val history =
            tracks.take(15).associate {
                it.id!! to it.copy(play_count = 1, last_played_at = "1000000")
            }
        val mean =
            (0..80)
                .map {
                    BalancedShuffleEngine.shuffle(tracks, history, it.toLong(), 1000000)
                        .withIndex()
                        .filter { it.value.id!! <= 15 }
                        .map { it.index }
                        .average()
                }
                .average()
        assertTrue(mean > 16)
    }
}
