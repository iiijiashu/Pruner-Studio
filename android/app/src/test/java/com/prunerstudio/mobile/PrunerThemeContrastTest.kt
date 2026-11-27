package com.prunerstudio.mobile

import androidx.compose.ui.graphics.toArgb
import com.prunerstudio.mobile.ui.theme.*
import kotlin.math.pow
import org.junit.Assert.*
import org.junit.Test

class PrunerThemeContrastTest {
    private fun luminance(rgb: Int): Double {
        fun channel(shift: Int): Double {
            val value = ((rgb ushr shift) and 255) / 255.0
            return if (value <= .04045) value / 12.92 else ((value + .055) / 1.055).pow(2.4)
        }
        return .2126 * channel(16) + .7152 * channel(8) + .0722 * channel(0)
    }

    private fun ratio(aRgb: Int, bRgb: Int): Double {
        val a = luminance(aRgb)
        val b = luminance(bRgb)
        return (maxOf(a, b) + .05) / (minOf(a, b) + .05)
    }

    @Test
    fun allFixedPalettesMeetContrast() {
        var count = 0
        for (theme in PrunerThemeId.entries.filter { it != PrunerThemeId.auto }) for (preset in
            AccentPreset.entries) {
            count++
            val palette = PrunerColors(theme, rgb(preset.hex))
            assertEquals(0xff62a368.toInt(), palette.keep.toArgb())
            assertEquals(0xffdf584c.toInt(), palette.prune.toArgb())
            assertEquals(0xff121214.toInt(), palette.semanticInk.toArgb())

            // All chrome and elevated surfaces must be 100% opaque (alpha = 1.0)
            for (opaque in listOf(
                palette.background,
                palette.surface,
                palette.surfaceWell,
                palette.surfaceElevated,
                palette.surfacePlayer,
            )) {
                assertEquals("$theme opaque surface", 1f, opaque.alpha, 0.0001f)
                assertTrue(
                    "$theme primary ink >= 4.5",
                    ratio(palette.text.toArgb(), opaque.toArgb()) >= 4.5,
                )
                assertTrue(
                    "$theme secondary ink >= 4.5",
                    ratio(palette.secondary.toArgb(), opaque.toArgb()) >= 4.5,
                )
            }

            // Primary CTA softened in light themes (no pitch-black slab) and >= 7:1 legible
            assertTrue(
                "$theme primaryAction >= 7.0",
                ratio(palette.primaryActionFg.toArgb(), palette.primaryActionBg.toArgb()) >= 7.0,
            )
            if (theme.isPaper) {
                assertTrue(
                    "$theme primaryActionBg softened (not pure black)",
                    luminance(palette.primaryActionBg.toArgb()) > 0.025,
                )
            }

            // Harmonized Play circle icon contrast >= 4.6:1
            assertTrue(
                "$theme $preset playCircle >= 4.6",
                ratio(palette.playCircleFg.toArgb(), palette.playCircleBg.toArgb()) >= 4.6,
            )

            // Border control meets WCAG >= 4.5:1 on main surfaces
            assertTrue(
                "$theme borderControl on bg >= 4.5",
                ratio(palette.borderControl.toArgb(), palette.background.toArgb()) >= 4.5,
            )
            assertTrue(
                "$theme borderControl on surface >= 4.5",
                ratio(palette.borderControl.toArgb(), palette.surface.toArgb()) >= 4.5,
            )

            val fore = luminance(computeReadableAccentText(rgb(preset.hex), theme).toArgb())
            for (surface in theme.surfaces) for (alpha in listOf(0f, .16f, .2f)) {
                val bg = luminance(blend(surface, rgb(preset.hex), alpha).toArgb())
                assertTrue(
                    "$theme $preset $alpha",
                    (maxOf(fore, bg) + .05) / (minOf(fore, bg) + .05) >= 4.6,
                )
            }
        }
        assertEquals(48, count)
    }

    @Test
    fun redundantDailyRecommendReasonTagsAreSuppressed() {
        assertNull(sanitizeReasonTag(null))
        assertNull(sanitizeReasonTag("   "))
        assertNull(sanitizeReasonTag("每日推荐"))
        assertNull(sanitizeReasonTag("网易云每日推荐"))
        assertEquals("超 85% 人播放", sanitizeReasonTag("超 85% 人播放"))
        assertEquals("沉睡 48 天", sanitizeReasonTag(" 沉睡 48 天 "))
    }
}
