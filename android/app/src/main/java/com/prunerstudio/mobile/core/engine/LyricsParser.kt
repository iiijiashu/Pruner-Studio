package com.prunerstudio.mobile.core.engine

import com.prunerstudio.mobile.core.model.*

object LyricsParser {
    private val timestamp = Regex("""\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?]""")
    private val yrcLine = Regex("""^\[(\d+),(\d+)]""")
    private const val maxMs = 86_400_000L

    private fun String.bounded(max: Long) = toLongOrNull()?.takeIf { it in 0..max }

    private val word = Regex("""\((-?\d+),(-?\d+),-?\d+\)(.*?)(?=\(-?\d+,-?\d+,-?\d+\)|$)""")

    private fun lrc(raw: String) =
        buildMap<Long, String> {
            for (line in raw.lineSequence()) {
                val times = timestamp.findAll(line).toList()
                val text = timestamp.replace(line, "").trim()
                for (match in times) {
                    val (minute, second, fraction) = match.destructured
                    val minutes = minute.bounded(maxMs / 60000) ?: continue
                    val seconds = second.bounded(59) ?: continue
                    val time = minutes * 60000 + seconds * 1000 + fraction.padEnd(3, '0').toLong()
                    if (time <= maxMs) put(time, text)
                }
            }
        }

    fun parse(
        lrc: String,
        yrc: String = "",
        tlyric: String = "",
        romalrc: String = "",
    ): ParsedLyrics {
        val lines = lrc(lrc).mapValues { (time, text) -> LyricLine(time, text) }
        val karaoke =
            yrc.lineSequence()
                .mapNotNull { line ->
                    val header = yrcLine.find(line) ?: return@mapNotNull null
                    val start = header.groupValues[1].bounded(maxMs) ?: return@mapNotNull null
                    header.groupValues[2].bounded(maxMs - start) ?: return@mapNotNull null
                    val words =
                        word
                            .findAll(line)
                            .mapNotNull wordToken@{ token ->
                                val (time, length, text) = token.destructured
                                val ms = time.bounded(maxMs) ?: return@wordToken null
                                val duration = length.bounded(maxMs - ms) ?: return@wordToken null
                                LyricWord(ms, duration, text)
                            }
                            .toList()
                    if (words.isEmpty()) null
                    else LyricLine(start, words.joinToString("") { it.text }, words)
                }
                .toList()
        val translations = lrc(tlyric)
        val roman = lrc(romalrc)
        fun lookupAligned(map: Map<Long, String>, startMs: Long): String? {
            if (map.isEmpty()) return null
            map[startMs]?.let { return it }
            var bestKey: Long? = null
            var bestDelta = 151L
            for (k in map.keys) {
                val delta = kotlin.math.abs(k - startMs)
                if (delta < bestDelta) {
                    bestDelta = delta
                    bestKey = k
                }
            }
            return bestKey?.let { map[it] }
        }
        return ParsedLyrics(
            karaoke
                .ifEmpty { lines.values.toList() }
                .sortedBy { it.startMs }
                .map {
                    it.copy(
                        translation = lookupAligned(translations, it.startMs),
                        romaji = lookupAligned(roman, it.startMs),
                    )
                }
        )
    }

    fun activeLine(lyrics: ParsedLyrics, positionMs: Long, offsetMs: Long = 0): Int {
        val index = lyrics.lines.binarySearchBy(positionMs + offsetMs) { it.startMs }
        return if (index >= 0) index else -index - 2
    }
}
