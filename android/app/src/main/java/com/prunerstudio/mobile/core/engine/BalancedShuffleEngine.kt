package com.prunerstudio.mobile.core.engine

import com.prunerstudio.mobile.core.model.Track
import java.text.Normalizer
import java.time.*
import kotlin.random.Random

object BalancedShuffleEngine {
    private fun key(text: String): String = buildString {
        Normalizer.normalize(text, Normalizer.Form.NFKC).codePoints().forEach { cp ->
            val scalar = String(Character.toChars(cp))
            // Full case folding expands ligatures and sharp S, preserves dotless i,
            // and folds both Cherokee cases to their uppercase form.
            val folded =
                when (cp) {
                    0x0131 -> scalar
                    0x1E9E -> "ss"
                    in 0x13A0..0x13F5,
                    in 0x13F8..0x13FD,
                    in 0xAB70..0xABBF -> scalar.uppercase()
                    else -> scalar.uppercase().lowercase()
                }
            folded.codePoints().forEach { c ->
                val type = Character.getType(c)
                if (
                    Character.isLetterOrDigit(c) ||
                        type == Character.LETTER_NUMBER.toInt() ||
                        type == Character.OTHER_NUMBER.toInt()
                )
                    appendCodePoint(c)
            }
        }
    }

    private data class Slot(val position: Double, val tie: Double, val track: IndexedValue<Track>)

    private fun age(history: Track?, now: Long): Double? {
        if (history == null || history.play_count <= 0) return null
        val raw = history.last_played_at ?: return null
        val seconds =
            raw.toDoubleOrNull()?.takeIf { it > 0 }?.let { if (it > 1e10) it / 1000 else it }
                ?: runCatching { Instant.parse(raw).epochSecond }
                    .recoverCatching { OffsetDateTime.parse(raw).toEpochSecond() }
                    .recoverCatching { LocalDateTime.parse(raw).toEpochSecond(ZoneOffset.UTC) }
                    .getOrNull()
                    ?.toDouble()
                ?: return null
        return ((now - seconds) / 86400.0).coerceAtLeast(0.0)
    }

    fun shuffle(
        tracks: List<Track>,
        history: Map<Long, Track> = emptyMap(),
        seed: Long = Random.nextLong(),
        nowSeconds: Long = Instant.now().epochSecond,
    ): List<Track> {
        if (tracks.size < 2) return tracks.toList()
        val random = DesktopShuffleRandom(seed)
        val artistKeys = mutableMapOf<String, String>()
        fun artist(t: Track): String {
            val raw = t.artists.firstOrNull { it.isNotBlank() } ?: t.artist
            return artistKeys.getOrPut(raw) { key(raw) }
        }
        fun days(t: Track) =
            age((t.id ?: t.song_id)?.takeIf { it > 0 }?.let(history::get), nowSeconds)
        fun slots(items: List<IndexedValue<Track>>, jitter: Double, album: Boolean): List<Slot> {
            val step = 1.0 / items.size
            val phase = random.uniform(0.0, step)
            val scores =
                items.mapIndexed { i, item ->
                    val coord = phase + i * step + random.uniform(-jitter * step, jitter * step)
                    val d = days(item.value)
                    val score =
                        if (album) ((coord % 1) + 1) % 1
                        else
                            coord +
                                when {
                                    d == null -> -0.12
                                    d < 7 -> .24 * (1 - d / 7) + .04
                                    d > 30 -> -.10
                                    else -> 0.0
                                }
                    Slot(score, if (album) random.fraction() else 0.0, item)
                }
            return if (album) scores else scores.map { it.copy(tie = random.fraction()) }
        }
        val order = compareBy<Slot> { it.position }.thenBy { it.tie }
        val scheduled =
            tracks
                .withIndex()
                .groupBy { artist(it.value).ifEmpty { "__solo_${it.index}" } }
                .values
                .flatMap { group ->
                    val tiers =
                        group
                            .groupBy {
                                val d = days(it.value)
                                when {
                                    d == null -> 0
                                    d > 30 -> 1
                                    d >= 7 -> 2
                                    else -> 3
                                }
                            }
                            .toSortedMap()
                    val bucket =
                        tiers.values.flatMap { tier ->
                            if (tier.size <= 2) random.shuffle(tier)
                            else
                                tier
                                    .groupBy {
                                        key(it.value.album).ifEmpty { "__album_${it.index}" }
                                    }
                                    .values
                                    .flatMap { slots(random.shuffle(it), .35, true) }
                                    .sortedWith(order)
                                    .map { it.track }
                        }
                    slots(bucket, .28, false)
                }
                .sortedWith(order)
                .map { it.track.value }
                .toMutableList()
        val result = mutableListOf<Track>()
        while (scheduled.isNotEmpty()) {
            val last = result.lastOrNull()?.let(::artist).orEmpty()
            val i = scheduled.indexOfFirst { last.isEmpty() || artist(it) != last }.coerceAtLeast(0)
            result.add(scheduled.removeAt(i))
        }
        repeat(result.size) {
            val i =
                (1 until result.size).firstOrNull {
                    artist(result[it]).isNotEmpty() && artist(result[it]) == artist(result[it - 1])
                } ?: return result
            val a = artist(result[i])
            val j =
                result.indices.firstOrNull {
                    it != i &&
                        it != i - 1 &&
                        result.getOrNull(it - 1)?.let(::artist) != a &&
                        artist(result[it]) != a
                } ?: return result
            val item = result.removeAt(i)
            result.add(if (j < i) j else j - 1, item)
        }
        return result
    }
}
