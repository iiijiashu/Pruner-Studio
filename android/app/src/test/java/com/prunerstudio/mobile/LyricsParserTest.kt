package com.prunerstudio.mobile

import com.prunerstudio.mobile.core.engine.LyricsParser
import org.junit.Assert.*
import org.junit.Test

class LyricsParserTest {
    @Test
    fun overflowingTimesDoNotHideValidLinesOrWords() {
        val huge = "9".repeat(30)
        val lrc = "[00:01]a\n[$huge:01]x\n[00:60]y\n[00:03]b"
        assertEquals(listOf("a", "b"), LyricsParser.parse(lrc).lines.map { it.text })
        val yrc =
            "[1,30](1,2,0)a($huge,2,0)x(4,$huge,0)y(8,3,0)b\n" +
                "[100,400](100,50,0)A(-1,10,0)bad(200,50,0)B\n" +
                "[$huge,30](1,2,0)x\n[1,$huge](1,2,0)y\n[50,3](50,2,0)c"
        assertEquals(listOf("ab", "c", "AB"), LyricsParser.parse("", yrc).lines.map { it.text })
    }

    @Test
    fun parsesMultipleTimestampsAndOffset() {
        val lyrics = LyricsParser.parse("[01:02.3][02:01.23]Hi")
        assertEquals(listOf(62300L, 121230L), lyrics.lines.map { it.startMs })
        assertEquals(-1, LyricsParser.activeLine(lyrics, 0))
        assertEquals(1, LyricsParser.activeLine(lyrics, 121200, 30))
    }

    @Test
    fun karaokeAndTranslation() {
        val line =
            LyricsParser.parse(
                    "",
                    "[1000,400](1000,200,0)你(1200,200,0)好",
                    "[00:01]Hello",
                    "[00:01]ni hao",
                )
                .lines
                .single()
        assertEquals("你好", line.text)
        assertEquals(1200L, line.words.last().startMs)
        assertEquals("Hello", line.translation)
        assertEquals("ni hao", line.romaji)
    }
}
