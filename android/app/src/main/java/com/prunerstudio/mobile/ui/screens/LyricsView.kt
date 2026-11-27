package com.prunerstudio.mobile.ui.screens

import android.os.SystemClock
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.interaction.collectIsDraggedAsState
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.prunerstudio.mobile.core.engine.LyricsParser
import com.prunerstudio.mobile.core.model.LyricLine
import com.prunerstudio.mobile.core.model.LyricWord
import com.prunerstudio.mobile.core.model.ParsedLyrics
import com.prunerstudio.mobile.core.player.PlaybackProgress
import com.prunerstudio.mobile.ui.theme.blend
import com.prunerstudio.mobile.ui.theme.colors
import com.prunerstudio.mobile.ui.theme.controlShape
import kotlin.math.abs
import kotlinx.coroutines.delay

/** Pure word-by-word progress [0f..1f] for YRC karaoke sweep highlighting. */
internal fun computeWordProgress(word: LyricWord, effectivePositionMs: Long): Float {
    if (effectivePositionMs < word.startMs) return 0f
    if (word.durationMs <= 0L) return 1f
    val elapsed = effectivePositionMs - word.startMs
    return (elapsed.toFloat() / word.durationMs.toFloat()).coerceIn(0f, 1f)
}

internal fun buildKaraokeLineText(
    line: LyricLine,
    effectivePositionMs: Long,
    activeColor: Color,
    upcomingColor: Color,
): AnnotatedString = buildAnnotatedString {
    if (line.words.isEmpty()) {
        append(line.text)
        return@buildAnnotatedString
    }
    for (word in line.words) {
        val progress = computeWordProgress(word, effectivePositionMs)
        val start = length
        append(word.text)
        val color =
            when {
                progress >= 0.999f -> activeColor
                progress <= 0.001f -> upcomingColor
                else -> blend(upcomingColor, activeColor, progress)
            }
        addStyle(
            SpanStyle(
                color = color,
                fontWeight = if (progress > 0.05f) FontWeight.SemiBold else FontWeight.Medium,
            ),
            start,
            length,
        )
    }
}

@Composable
internal fun rememberInterpolatedPositionMs(
    progress: PlaybackProgress,
    highFrequencySweep: Boolean = true,
): Long {
    var interpolated by remember { mutableLongStateOf(progress.positionMs) }
    LaunchedEffect(
        progress.positionMs,
        progress.isPlaying,
        progress.playbackRate,
        progress.updatedAtElapsedMs,
        highFrequencySweep,
    ) {
        interpolated = progress.positionMs
        if (!progress.isPlaying) return@LaunchedEffect
        val anchorPos = progress.positionMs
        val anchorWall = progress.updatedAtElapsedMs.takeIf { it > 0L } ?: SystemClock.elapsedRealtime()
        val rate = progress.playbackRate.coerceIn(0.5f, 2f)
        val stepMs = if (highFrequencySweep) 32L else 120L
        var lastTick = 0L
        while (true) {
            withFrameNanos {
                val now = SystemClock.elapsedRealtime()
                if (now - lastTick >= stepMs) {
                    lastTick = now
                    val delta = (now - anchorWall).coerceAtLeast(0L)
                    val projected = anchorPos + (delta * rate).toLong()
                    interpolated =
                        if (progress.durationMs > 0L) projected.coerceAtMost(progress.durationMs)
                        else projected
                }
            }
        }
    }
    return interpolated
}

@Composable
fun SyncedLyricsView(
    lyrics: ParsedLyrics,
    progress: PlaybackProgress,
    offsetMs: Long,
    showTranslation: Boolean,
    showRomaji: Boolean,
    fullScreen: Boolean,
    onSeekMs: (Long) -> Unit,
    modifier: Modifier = Modifier,
    onTapEmptyOrToggle: (() -> Unit)? = null,
) {
    val hasWordTimings = remember(lyrics) { lyrics.lines.any { it.words.isNotEmpty() } }
    val livePosMs = rememberInterpolatedPositionMs(progress, highFrequencySweep = hasWordTimings)
    val effectiveMs = livePosMs + offsetMs
    val activeIndex = remember(lyrics, effectiveMs) { LyricsParser.activeLine(lyrics, livePosMs, offsetMs) }
    val listState = rememberLazyListState()
    var viewportHeightPx by remember { mutableIntStateOf(0) }
    var userScrollingHold by remember { mutableStateOf(false) }

    val isUserDragging by listState.interactionSource.collectIsDraggedAsState()
    LaunchedEffect(isUserDragging) {
        if (isUserDragging) {
            userScrollingHold = true
        } else if (userScrollingHold) {
            delay(3800)
            userScrollingHold = false
        }
    }

    LaunchedEffect(activeIndex, lyrics, viewportHeightPx, fullScreen, isUserDragging, userScrollingHold) {
        if (activeIndex >= 0 && !isUserDragging && !userScrollingHold && lyrics.lines.isNotEmpty()) {
            val centerOffset = if (viewportHeightPx > 0) -(viewportHeightPx / 3) else 0
            runCatching {
                listState.animateScrollToItem(
                    index = activeIndex.coerceIn(lyrics.lines.indices),
                    scrollOffset = centerOffset,
                )
            }
        }
    }

    if (lyrics.lines.isEmpty()) {
        Box(
            modifier =
                modifier
                    .fillMaxWidth()
                    .then(if (onTapEmptyOrToggle != null) Modifier.clickable(onClick = onTapEmptyOrToggle) else Modifier),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                "暂无同步歌词",
                style = MaterialTheme.typography.bodyMedium,
                color = colors.secondary,
            )
        }
        return
    }

    val fadeModifier =
        Modifier.graphicsLayer(compositingStrategy = CompositingStrategy.Offscreen).drawWithContent {
            drawContent()
            val topFade = listState.canScrollBackward
            val bottomFade = listState.canScrollForward
            if (topFade || bottomFade) {
                val stops = buildList {
                    if (topFade) {
                        add(0f to Color.Transparent)
                        add(0.11f to Color.Black)
                    } else {
                        add(0f to Color.Black)
                    }
                    if (bottomFade) {
                        add(0.89f to Color.Black)
                        add(1f to Color.Transparent)
                    } else {
                        add(1f to Color.Black)
                    }
                }
                drawRect(
                    brush = Brush.verticalGradient(colorStops = stops.toTypedArray()),
                    blendMode = BlendMode.DstIn,
                )
            }
        }

    // Detect long (>5s) instrumental interlude before first line or between lines
    val nextLineStart = lyrics.lines.getOrNull(activeIndex + 1)?.startMs
    val currentLineStart = lyrics.lines.getOrNull(activeIndex)?.startMs ?: 0L
    val inInterlude =
        progress.isPlaying &&
            nextLineStart != null &&
            ((activeIndex < 0 && nextLineStart > 5000L && effectiveMs < nextLineStart - 1000L) ||
                (activeIndex >= 0 &&
                    lyrics.lines[activeIndex].text.isBlank() &&
                    nextLineStart - currentLineStart > 5000L))

    LazyColumn(
        state = listState,
        modifier =
            modifier
                .fillMaxWidth()
                .onSizeChanged { viewportHeightPx = it.height }
                .then(fadeModifier),
        contentPadding =
            PaddingValues(
                horizontal = if (fullScreen) 20.dp else 14.dp,
                vertical = if (fullScreen) 36.dp else 10.dp,
            ),
        verticalArrangement = Arrangement.spacedBy(if (fullScreen) 14.dp else 8.dp),
    ) {
        if (inInterlude) {
            item(key = "interlude_dots") {
                InterludeDotsIndicator(
                    modifier =
                        Modifier.fillMaxWidth()
                            .padding(vertical = 6.dp)
                )
            }
        }
        itemsIndexed(
            items = lyrics.lines,
            key = { idx, line -> "${idx}_${line.startMs}" },
        ) { idx, line ->
            val isCurrent = idx == activeIndex
            val distance = if (activeIndex < 0) idx + 1 else abs(idx - activeIndex)
            val lineAlpha =
                when {
                    isCurrent -> 1f
                    distance == 1 -> 0.72f
                    distance == 2 -> 0.50f
                    else -> 0.34f
                }
            val upcomingColor = colors.secondary.copy(alpha = if (isCurrent) 0.55f else lineAlpha)
            val mainFontSize =
                when {
                    fullScreen && isCurrent -> 21.sp
                    fullScreen -> 17.sp
                    isCurrent -> 16.sp
                    else -> 14.sp
                }
            val mainLineHeight =
                when {
                    fullScreen && isCurrent -> 30.sp
                    fullScreen -> 24.sp
                    isCurrent -> 23.sp
                    else -> 20.sp
                }
            Column(
                modifier =
                    Modifier.fillMaxWidth()
                        .clip(controlShape)
                        .clickable {
                            val targetMs = (line.startMs - offsetMs).coerceAtLeast(0L)
                            onSeekMs(targetMs)
                        }
                        .padding(horizontal = 6.dp, vertical = if (fullScreen) 6.dp else 3.dp),
                horizontalAlignment = if (fullScreen) Alignment.Start else Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(3.dp),
            ) {
                if (isCurrent && line.words.isNotEmpty()) {
                    val annotated =
                        buildKaraokeLineText(
                            line = line,
                            effectivePositionMs = effectiveMs,
                            activeColor = colors.accentText,
                            upcomingColor = upcomingColor,
                        )
                    Text(
                        text = annotated,
                        fontSize = mainFontSize,
                        lineHeight = mainLineHeight,
                        textAlign = if (fullScreen) TextAlign.Start else TextAlign.Center,
                        modifier = Modifier.fillMaxWidth(),
                    )
                } else {
                    Text(
                        text = line.text.ifBlank { "···" },
                        color = if (isCurrent) colors.accentText else colors.text.copy(alpha = lineAlpha),
                        fontWeight = if (isCurrent) FontWeight.SemiBold else FontWeight.Normal,
                        fontSize = mainFontSize,
                        lineHeight = mainLineHeight,
                        textAlign = if (fullScreen) TextAlign.Start else TextAlign.Center,
                        maxLines = if (fullScreen) 4 else 2,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                if (showTranslation && !line.translation.isNullOrBlank()) {
                    Text(
                        text = line.translation,
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.secondary.copy(alpha = if (isCurrent) 0.92f else lineAlpha),
                        textAlign = if (fullScreen) TextAlign.Start else TextAlign.Center,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                if (showRomaji && !line.romaji.isNullOrBlank()) {
                    Text(
                        text = line.romaji,
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.secondary.copy(alpha = if (isCurrent) 0.80f else lineAlpha * 0.9f),
                        textAlign = if (fullScreen) TextAlign.Start else TextAlign.Center,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
        }
    }
}

@Composable
private fun InterludeDotsIndicator(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "interlude")
    val phase by
        transition.animateFloat(
            initialValue = 0f,
            targetValue = 3f,
            animationSpec =
                infiniteRepeatable(
                    animation = tween(durationMillis = 1500, easing = LinearEasing),
                    repeatMode = RepeatMode.Restart,
                ),
            label = "dot_phase",
        )
    Row(
        modifier = modifier,
        horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.CenterHorizontally),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        for (i in 0..2) {
            val active = (1f - abs(phase - i).coerceAtMost(1f)).coerceIn(0.25f, 1f)
            Box(
                Modifier.size((6 + active * 2.5f).dp)
                    .clip(CircleShape)
                    .background(colors.accent.copy(alpha = active))
            )
        }
    }
}
