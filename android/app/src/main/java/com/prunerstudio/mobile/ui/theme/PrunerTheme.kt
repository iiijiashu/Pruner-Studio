package com.prunerstudio.mobile.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.graphics.*
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.*
import kotlin.math.roundToInt

fun rgb(value: Int) = Color(value or (0xff shl 24))

enum class PrunerThemeId(
    val label: String,
    val bg: Int,
    val surface: Int,
    val well: Int,
    val elevated: Int,
    val ink: Int,
    val muted: Int,
    val border: Int,
    private val samples: String,
) {
    parchment(
        "暖阳羊皮",
        0xf3ede2,
        0xfbf8f2,
        0xede5d7,
        0xeae1d1,
        0x241e17,
        0x584e42,
        0x685b4d,
        "fbf8f2 faf6ee f3ede2 efe8db ede5d7 eae1d1 e5dac7",
    ),
    light(
        "纸感浅白",
        0xf5f3ee,
        0xfdfcfa,
        0xeceae3,
        0xe8e5dd,
        0x1b1a18,
        0x54524c,
        0x64615b,
        "fdfcfa fcfbf8 f5f3ee efede7 eceae3 e8e5dd e2dfd6",
    ),
    celadon(
        "晨雾青瓷",
        0xebf0ee,
        0xf7faf9,
        0xe1e9e6,
        0xdce5e1,
        0x172420,
        0x445650,
        0x52645e,
        "f7faf9 f5f9f7 ebf0ee e4ebe8 e1e9e6 dce5e1 d3dfda",
    ),
    dark(
        "曜石深灰",
        0x111215,
        0x18191e,
        0x15161b,
        0x23252d,
        0xf2f1ed,
        0xa0a2aa,
        0x91939d,
        "111215 141519 15161b 16171c 18191e 1d1f25 23252d",
    ),
    midnight(
        "OLED 纯黑",
        0x000000,
        0x0d0d10,
        0x0a0a0d,
        0x1c1c22,
        0xf2f1ed,
        0xa3a3ab,
        0x878994,
        "000000 08080a 0a0a0d 0b0b0e 0d0d10 141418 1c1c22",
    ),
    nordic(
        "深海雾蓝",
        0x111822,
        0x19222e,
        0x141c27,
        0x222d36,
        0xf2f1ed,
        0xa1a1a6,
        0x8c9eb2,
        "111822 19222e 141c27 222d36 28333c",
    ),
    espresso(
        "暖咖黑胶",
        0x181412,
        0x201a17,
        0x1c1714,
        0x2b231f,
        0xf2f1ed,
        0xa1a1a6,
        0x9a8c82,
        "181412 201a17 1c1714 2b231f 312925",
    ),
    emerald(
        "松林暗夜",
        0x101714,
        0x19231e,
        0x141d19,
        0x222d25,
        0xf2f1ed,
        0xa1a1a6,
        0x89a094,
        "101714 19231e 141d19 222d25 28332b",
    ),
    auto("跟随系统", 0, 0, 0, 0, 0, 0, 0, "");

    val isPaper
        get() = this == PrunerThemeId.light || this == celadon || this == parchment

    val surfaces
        get() =
            (samples.split(" ").filter(String::isNotEmpty).map { rgb(it.toInt(16)) } +
                    listOf(rgb(bg), rgb(surface), rgb(well), rgb(elevated)))
                .distinct()
}

enum class AccentPreset(val label: String, val hex: Int) {
    sage("鼠尾草", 0x62a368),
    cobalt("钴蓝", 0x4c8bf5),
    violet("紫罗兰", 0x8b6ce8),
    amber("琥珀", 0xd99e4e),
    coral("珊瑚", 0xe07a5f),
    rose("玫瑰", 0xc86b85),
}

fun blend(a: Color, b: Color, t: Float): Color {
    fun channel(x: Float, y: Float) = ((x * (1 - t) + y * t) * 255).roundToInt().coerceIn(0, 255)
    return Color(channel(a.red, b.red), channel(a.green, b.green), channel(a.blue, b.blue))
}

fun contrast(a: Color, b: Color): Float {
    val x = a.luminance()
    val y = b.luminance()
    return (maxOf(x, y) + 0.05f) / (minOf(x, y) + 0.05f)
}

fun computeReadableAccentText(accent: Color, themeId: PrunerThemeId): Color {
    val theme = if (themeId == PrunerThemeId.auto) PrunerThemeId.parchment else themeId
    val samples =
        theme.surfaces.flatMap { listOf(it, blend(it, accent, .16f), blend(it, accent, .20f)) }
    val target = rgb(if (theme.isPaper) 0x101412 else 0xf5f5f7)
    return (0..40)
        .map { blend(accent, target, it / 40f) }
        .first { c -> samples.all { contrast(c, it) >= 4.6f } }
}

data class PrunerColors(val theme: PrunerThemeId, val accent: Color) {
    val background = rgb(theme.bg)
    val surface = rgb(theme.surface)
    val surfaceWell = rgb(theme.well)
    val surfaceElevated = rgb(theme.elevated)
    val surfacePlayer = if (theme.isPaper) blend(surface, background, 0.14f) else surface
    val text = rgb(theme.ink)
    val secondary = rgb(theme.muted)
    val borderSubtle =
        (if (theme.isPaper) Color(0xFF261E14) else Color.White).copy(alpha = 0.08f)
    val borderMedium =
        (if (theme.isPaper) Color(0xFF261E14) else Color.White).copy(alpha = 0.14f)
    val borderControl = rgb(theme.border)
    val accentText = computeReadableAccentText(accent, theme)
    val primaryActionBg =
        when (theme) {
            PrunerThemeId.parchment -> rgb(0x3a3127)
            PrunerThemeId.light -> rgb(0x32302c)
            PrunerThemeId.celadon -> rgb(0x233731)
            PrunerThemeId.midnight -> rgb(0xf2f1ed)
            else -> rgb(0xecebe6)
        }
    val primaryActionFg = if (theme.isPaper) rgb(0xfaf6ee) else rgb(0x111215)
    val playCircleBg = if (theme.isPaper) accentText else primaryActionBg
    val playCircleFg = if (theme.isPaper) rgb(0xfcf9f3) else primaryActionFg
    val keep = rgb(0x62a368)
    val prune = rgb(0xdf584c)
    val semanticInk = rgb(0x121214)
}

val LocalPrunerColors = staticCompositionLocalOf {
    PrunerColors(PrunerThemeId.parchment, rgb(AccentPreset.sage.hex))
}
val colors: PrunerColors
    @Composable get() = LocalPrunerColors.current

object PrunerTypography {
    val body =
        TextStyle(
            fontFamily = FontFamily.SansSerif,
            fontSize = 15.sp,
            lineHeight = 22.sp,
            fontWeight = FontWeight.Normal,
        )
    val number =
        TextStyle(
            fontFamily = FontFamily.Monospace,
            fontFeatureSettings = "tnum",
        )
    val material =
        Typography(
            headlineLarge =
                TextStyle(
                    fontFamily = FontFamily.SansSerif,
                    fontSize = 26.sp,
                    lineHeight = 32.sp,
                    fontWeight = FontWeight.SemiBold,
                    letterSpacing = (-0.25).sp,
                ),
            headlineMedium =
                TextStyle(
                    fontFamily = FontFamily.SansSerif,
                    fontSize = 22.sp,
                    lineHeight = 28.sp,
                    fontWeight = FontWeight.SemiBold,
                    letterSpacing = (-0.2).sp,
                ),
            titleLarge =
                TextStyle(
                    fontFamily = FontFamily.SansSerif,
                    fontSize = 17.sp,
                    lineHeight = 24.sp,
                    fontWeight = FontWeight.SemiBold,
                ),
            titleMedium =
                TextStyle(
                    fontFamily = FontFamily.SansSerif,
                    fontSize = 15.sp,
                    lineHeight = 21.sp,
                    fontWeight = FontWeight.SemiBold,
                ),
            bodyLarge = body,
            bodyMedium =
                TextStyle(
                    fontFamily = FontFamily.SansSerif,
                    fontSize = 14.sp,
                    lineHeight = 20.sp,
                ),
            bodySmall =
                TextStyle(
                    fontFamily = FontFamily.SansSerif,
                    fontSize = 12.5.sp,
                    lineHeight = 18.sp,
                ),
            labelLarge =
                TextStyle(
                    fontFamily = FontFamily.SansSerif,
                    fontSize = 13.5.sp,
                    lineHeight = 18.sp,
                    fontWeight = FontWeight.Medium,
                ),
            labelMedium =
                TextStyle(
                    fontFamily = FontFamily.SansSerif,
                    fontSize = 12.sp,
                    lineHeight = 16.sp,
                    fontWeight = FontWeight.Medium,
                ),
        )
}

fun sanitizeAlbumAccent(raw: Color, isPaper: Boolean): Color {
    val hsv = FloatArray(3)
    android.graphics.Color.colorToHSV(raw.toArgb(), hsv)
    // Keep album accents calm, low-saturation, and editorial rather than neon.
    hsv[1] = hsv[1].coerceIn(0.18f, if (isPaper) 0.48f else 0.44f)
    hsv[2] = hsv[2].coerceIn(if (isPaper) 0.38f else 0.48f, if (isPaper) 0.72f else 0.80f)
    return Color(android.graphics.Color.HSVToColor(hsv))
}

@Composable
fun PrunerTheme(
    theme: PrunerThemeId,
    accent: AccentPreset,
    scale: Float = 1f,
    dynamicAccent: Color? = null,
    content: @Composable () -> Unit,
) {
    val resolved =
        if (theme != PrunerThemeId.auto) theme
        else if (isSystemInDarkTheme()) PrunerThemeId.dark else PrunerThemeId.light
    val effectiveAccent =
        remember(resolved, accent, dynamicAccent) {
            dynamicAccent?.let { sanitizeAlbumAccent(it, resolved.isPaper) } ?: rgb(accent.hex)
        }
    val c = remember(resolved, effectiveAccent) { PrunerColors(resolved, effectiveAccent) }
    val scheme =
        (if (resolved.isPaper) lightColorScheme() else darkColorScheme()).copy(
            background = c.background,
            surface = c.surface,
            surfaceVariant = c.surfaceWell,
            onBackground = c.text,
            onSurface = c.text,
            onSurfaceVariant = c.secondary,
            primary = c.accentText,
            onPrimary = c.playCircleFg,
            outline = c.borderMedium,
            outlineVariant = c.borderSubtle,
            surfaceContainerHigh = c.surface,
            surfaceContainer = c.surfaceWell,
        )
    val density = LocalDensity.current
    CompositionLocalProvider(
        LocalPrunerColors provides c,
        LocalDensity provides Density(density.density, density.fontScale * scale),
    ) {
        MaterialTheme(
            colorScheme = scheme,
            typography = PrunerTypography.material,
            shapes = Shapes(small = RoundedCornerShape(6.dp), medium = RoundedCornerShape(10.dp)),
            content = content,
        )
    }
}
