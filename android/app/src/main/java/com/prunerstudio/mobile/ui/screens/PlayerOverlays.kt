package com.prunerstudio.mobile.ui.screens

import androidx.activity.compose.BackHandler
import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.QueueMusic
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.*
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.*
import com.prunerstudio.mobile.core.engine.LyricsParser
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.player.EqState
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*
import java.util.Locale
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlinx.coroutines.launch

enum class Overlay {
    STAGE,
    QUEUE,
    EQ,
    SLEEP,
}

private fun formatClock(seconds: Double): String {
    val total = seconds.takeIf { it.isFinite() && it >= 0.0 }?.toInt() ?: 0
    return String.format(Locale.ROOT, "%d:%02d", total / 60, total % 60)
}

private fun playModeIconAndLabel(mode: PlayMode) =
    when (mode) {
        PlayMode.loop -> Icons.Outlined.Repeat to "列表循环"
        PlayMode.repeat_one -> Icons.Outlined.RepeatOne to "单曲循环"
        PlayMode.shuffle, PlayMode.smart_radio -> Icons.Outlined.Shuffle to "均衡随机"
        else -> Icons.Outlined.FormatListNumbered to "顺序播放"
    }

private fun nextPlayMode(mode: PlayMode): PlayMode =
    when (mode) {
        PlayMode.order -> PlayMode.loop
        PlayMode.loop -> PlayMode.repeat_one
        PlayMode.repeat_one -> PlayMode.shuffle
        else -> PlayMode.order
    }

@Composable
fun MiniPlayer(vm: PrunerViewModel, open: (Overlay) -> Unit) {
    val s = vm.state
    val t = s.current_track ?: return
    val haptic = LocalHapticFeedback.current
    var dragX by remember(t.identity()) { mutableFloatStateOf(0f) }
    var dragY by remember(t.identity()) { mutableFloatStateOf(0f) }

    Column(
        Modifier.fillMaxWidth()
            .background(colors.surfacePlayer)
            .pointerInput(t.identity()) {
                detectHorizontalDragGestures(
                    onDragEnd = {
                        if (dragX < -96f) {
                            haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            vm.transport("next")
                        } else if (dragX > 96f) {
                            haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            vm.transport("previous")
                        }
                        dragX = 0f
                    },
                    onDragCancel = { dragX = 0f },
                ) { _, amount ->
                    dragX += amount
                }
            }
            .pointerInput(t.identity()) {
                detectVerticalDragGestures(
                    onDragEnd = {
                        if (dragY < -64f) {
                            open(Overlay.STAGE)
                        }
                        dragY = 0f
                    },
                    onDragCancel = { dragY = 0f },
                ) { _, amount ->
                    if (amount < 0f) dragY += amount
                }
            }
    ) {
        MiniPlayerProgressBar(vm, t)
        Row(
            Modifier.fillMaxWidth()
                .heightIn(min = 64.dp)
                .padding(start = 12.dp, end = 4.dp, top = 6.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Row(
                Modifier.weight(1f)
                    .clip(controlShape)
                    .clickable { open(Overlay.STAGE) }
                    .padding(horizontal = 4.dp, vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Cover(
                    url = t.cover_url,
                    modifier = Modifier.size(44.dp),
                    sizeTier = 96,
                    onPaletteExtracted = { color ->
                        if (vm.dynamicCoverAccent) vm.extractedCoverColor = color
                    },
                )
                Column(
                    Modifier.weight(1f),
                    verticalArrangement = Arrangement.spacedBy(2.dp),
                ) {
                    Text(
                        t.title.ifBlank { "未知曲目" },
                        style = MaterialTheme.typography.titleMedium,
                        fontSize = 14.sp,
                        color = colors.text,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    MiniPlayerSecondLine(vm, t)
                }
            }
            Transport(vm, size = 38.dp)
            Glyph(Icons.Outlined.SkipNext, "下一首") { vm.transport("next") }
            Glyph(Icons.AutoMirrored.Outlined.QueueMusic, "播放队列") { open(Overlay.QUEUE) }
        }
    }
}

@Composable
private fun MiniPlayerProgressBar(vm: PrunerViewModel, track: Track) {
    val prog by vm.playbackProgress.collectAsState()
    val durMs =
        prog.durationMs.takeIf { it > 0L }
            ?: (track.durationSeconds() * 1000.0).toLong().coerceAtLeast(0L)
    val fraction =
        if (durMs > 0L) (prog.positionMs.toFloat() / durMs.toFloat()).coerceIn(0f, 1f) else 0f
    Box(Modifier.fillMaxWidth().height(2.dp).background(colors.surfaceWell)) {
        if (fraction > 0f) {
            Box(Modifier.fillMaxWidth(fraction).fillMaxHeight().background(colors.accent))
        }
    }
}

@Composable
private fun MiniPlayerSecondLine(vm: PrunerViewModel, track: Track) {
    val prog by vm.playbackProgress.collectAsState()
    val lineIdx = LyricsParser.activeLine(vm.lyrics, prog.positionMs, vm.offset)
    val activeLyric = vm.lyrics.lines.getOrNull(lineIdx)?.text?.takeIf { it.isNotBlank() }
    val artist = track.artist.ifBlank { track.artists.joinToString(" / ") }.ifBlank { "未知歌手" }
    Text(
        text = activeLyric ?: artist,
        color = if (activeLyric != null) colors.accentText else colors.secondary,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
        style = MaterialTheme.typography.bodySmall,
        fontSize = 12.sp,
    )
}

@Composable
fun Transport(vm: PrunerViewModel, size: Dp = 38.dp) {
    val activePlaying = vm.state.is_playing || vm.isBuffering
    val label = if (activePlaying) "暂停" else "播放"
    // Enforce >= 48dp accessible hit area around visual circle.
    Box(
        modifier =
            Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp)
                .clip(CircleShape)
                .semantics { contentDescription = label }
                .clickable(role = Role.Button) { vm.transport("toggle") },
        contentAlignment = Alignment.Center,
    ) {
        Box(
            Modifier.size(size).clip(CircleShape).background(colors.playCircleBg),
            contentAlignment = Alignment.Center,
        ) {
            if (vm.isBuffering) {
                CircularProgressIndicator(
                    modifier = Modifier.size(size * 0.50f),
                    strokeWidth = 2.dp,
                    color = colors.playCircleFg,
                )
            } else {
                Icon(
                    if (vm.state.is_playing) Icons.Outlined.Pause else Icons.Outlined.PlayArrow,
                    null,
                    modifier = Modifier.size(size * 0.56f),
                    tint = colors.playCircleFg,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PlayerOverlays(
    vm: PrunerViewModel,
    overlay: Overlay?,
    open: (Overlay) -> Unit,
    close: () -> Unit,
) {
    // Full-screen Stage mounted smoothly inside root Box instead of hard-cut Dialog.
    AnimatedVisibility(
        visible = overlay == Overlay.STAGE && vm.state.current_track != null,
        enter = slideInVertically(tween(260, easing = FastOutSlowInEasing)) { it / 2 } + fadeIn(tween(200)),
        exit = slideOutVertically(tween(220, easing = FastOutSlowInEasing)) { it / 2 } + fadeOut(tween(180)),
    ) {
        BackHandler(enabled = overlay == Overlay.STAGE) { close() }
        Stage(vm = vm, open = open, close = close)
    }

    when (overlay) {
        Overlay.QUEUE -> QueueSheet(vm, close)
        Overlay.EQ -> EqSheet(vm, close)
        Overlay.SLEEP -> SleepSheet(vm, close)
        else -> Unit
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun QueueSheet(vm: PrunerViewModel, close: () -> Unit) {
    val s = vm.state
    val listState = rememberLazyListState()
    val haptic = LocalHapticFeedback.current
    val density = LocalDensity.current
    val fallbackRowHeightPx = with(density) { 64.dp.toPx() }
    var draggingKey by remember { mutableStateOf<String?>(null) }
    val stableKeys = vm.queueEntryIds

    fun estimatedRowHeightPx(): Float {
        val visible = listState.layoutInfo.visibleItemsInfo
        if (visible.isEmpty()) return fallbackRowHeightPx
        return visible.map { it.size }.average().toFloat().coerceAtLeast(fallbackRowHeightPx * 0.75f)
    }

    LaunchedEffect(Unit) {
        if (s.queue_index in s.queue.indices) {
            listState.scrollToItem((s.queue_index - 1).coerceAtLeast(0))
        }
    }

    ModalBottomSheet(onDismissRequest = close, containerColor = colors.surface) {
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Heading("播放队列")
                    Text(
                        "${s.queue.size} 首 · ${formatClock(s.queue.sumOf { it.durationSeconds() })}",
                        style = PrunerTypography.number.copy(fontSize = 12.5.sp),
                        color = colors.secondary,
                    )
                }
                if (s.queue.isNotEmpty()) {
                    Action("清空", style = ActionStyle.Ghost) {
                        vm.clearQueue()
                        close()
                    }
                }
            }
            HorizontalDivider(color = colors.borderSubtle)
            if (s.queue.isEmpty()) {
                EmptyStatePanel(
                    icon = Icons.AutoMirrored.Outlined.QueueMusic,
                    title = "播放队列为空",
                    subtitle = "在发现、音乐库或搜索中点按曲目即可开始播放。",
                    modifier = Modifier.padding(vertical = 12.dp),
                )
            } else if (stableKeys.size == s.queue.size) {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxWidth().heightIn(max = 480.dp),
                    contentPadding = PaddingValues(bottom = 24.dp),
                ) {
                    itemsIndexed(
                        items = s.queue,
                        key = { idx, _ -> stableKeys[idx] },
                    ) { i, t ->
                        val itemKey = stableKeys[i]
                        val dismissState =
                            rememberSwipeToDismissBoxState(
                                confirmValueChange = { value ->
                                    if (value == SwipeToDismissBoxValue.EndToStart) {
                                        haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        vm.removeQueueItem(itemKey)
                                        true
                                    } else false
                                }
                            )
                        SwipeToDismissBox(
                            state = dismissState,
                            enableDismissFromStartToEnd = false,
                            backgroundContent = {
                                Box(
                                    Modifier.fillMaxSize()
                                        .clip(controlShape)
                                        .background(colors.prune.copy(alpha = 0.16f))
                                        .padding(horizontal = 16.dp),
                                    contentAlignment = Alignment.CenterEnd,
                                ) {
                                    Text(
                                        "移出队列",
                                        style = MaterialTheme.typography.labelLarge,
                                        color = colors.prune,
                                    )
                                }
                            },
                        ) {
                            Column(
                                Modifier.fillMaxWidth()
                                    .background(
                                        if (draggingKey == itemKey) colors.surfaceWell
                                        else colors.surface
                                    )
                            ) {
                                Row(
                                    Modifier.fillMaxWidth(),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Box(
                                        modifier =
                                            Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp)
                                                .pointerInput(itemKey) {
                                                    var dragAccumulatedPx = 0f
                                                    detectDragGestures(
                                                        onDragStart = {
                                                            draggingKey = itemKey
                                                            dragAccumulatedPx = 0f
                                                            haptic.performHapticFeedback(
                                                                HapticFeedbackType.LongPress
                                                            )
                                                        },
                                                        onDragEnd = {
                                                            draggingKey = null
                                                            dragAccumulatedPx = 0f
                                                        },
                                                        onDragCancel = {
                                                            draggingKey = null
                                                            dragAccumulatedPx = 0f
                                                        },
                                                    ) { change, dragAmount ->
                                                        change.consume()
                                                        dragAccumulatedPx += dragAmount.y
                                                        val stepPx = estimatedRowHeightPx()
                                                        val steps = (dragAccumulatedPx / stepPx).toInt()
                                                        if (steps != 0) {
                                                            vm.moveQueueItem(itemKey, steps)
                                                            dragAccumulatedPx -= steps * stepPx
                                                            haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                                        }
                                                    }
                                                },
                                        contentAlignment = Alignment.Center,
                                    ) {
                                        Icon(
                                            Icons.Outlined.DragHandle,
                                            "拖动排序",
                                            tint = colors.secondary,
                                            modifier = Modifier.size(20.dp),
                                        )
                                    }
                                    Box(Modifier.weight(1f)) {
                                        TrackRow(
                                            track = t,
                                            click = { vm.jumpToQueueEntry(itemKey) },
                                            active = i == s.queue_index,
                                        ) {
                                            QueueItemMenu(
                                                canMoveUp = i > 0,
                                                canMoveDown = i < s.queue.lastIndex,
                                                onMoveUp = { vm.moveQueueItem(itemKey, -1) },
                                                onMoveDown = { vm.moveQueueItem(itemKey, 1) },
                                                onRemove = { vm.removeQueueItem(itemKey) },
                                            )
                                        }
                                    }
                                }
                                if (i < s.queue.lastIndex) {
                                    HorizontalDivider(color = colors.borderSubtle)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun QueueItemMenu(
    canMoveUp: Boolean,
    canMoveDown: Boolean,
    onMoveUp: () -> Unit,
    onMoveDown: () -> Unit,
    onRemove: () -> Unit,
) {
    var open by remember { mutableStateOf(false) }
    Box {
        Glyph(Icons.Outlined.MoreVert, "队列曲目操作", tint = colors.secondary) { open = true }
        DropdownMenu(open, { open = false }, containerColor = colors.surface) {
            if (canMoveUp) {
                DropdownMenuItem(
                    text = { Text("上移一位", color = colors.text) },
                    leadingIcon = { Icon(Icons.Outlined.ArrowUpward, null, tint = colors.text) },
                    onClick = {
                        open = false
                        onMoveUp()
                    },
                )
            }
            if (canMoveDown) {
                DropdownMenuItem(
                    text = { Text("下移一位", color = colors.text) },
                    leadingIcon = { Icon(Icons.Outlined.ArrowDownward, null, tint = colors.text) },
                    onClick = {
                        open = false
                        onMoveDown()
                    },
                )
            }
            DropdownMenuItem(
                text = { Text("移出队列", color = colors.prune) },
                leadingIcon = { Icon(Icons.Outlined.DeleteOutline, null, tint = colors.prune) },
                onClick = {
                    open = false
                    onRemove()
                },
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun EqSheet(vm: PrunerViewModel, close: () -> Unit) {
    val eqState by vm.eqStateFlow.collectAsState()
    ModalBottomSheet(onDismissRequest = close, containerColor = colors.surface) {
        LazyColumn(
            modifier = Modifier.fillMaxWidth().heightIn(max = 540.dp),
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            item {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Heading("音频均衡器")
                        Text(
                            eqState.engineName,
                            style = MaterialTheme.typography.bodySmall,
                            color = colors.secondary,
                        )
                    }
                    Switch(
                        checked = eqState.enabled,
                        onCheckedChange = { vm.setEqEnabled(it) },
                    )
                }
            }
            item {
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(EqState.PRESETS.keys.toList()) { preset ->
                        val selected = eqState.presetName == preset
                        Action(
                            label = preset,
                            style = if (selected) ActionStyle.Primary else ActionStyle.Secondary,
                        ) {
                            vm.applyEqPreset(preset)
                        }
                    }
                }
            }
            itemsIndexed(eqState.bandFrequenciesHz) { i, freqHz ->
                val savedDb = eqState.bandGainsDb.getOrElse(i) { 0f }
                var gainDb by remember(savedDb) { mutableFloatStateOf(savedDb) }
                val freqLabel = if (freqHz >= 1000) "${freqHz / 1000} kHz" else "$freqHz Hz"
                Column {
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            freqLabel,
                            style = PrunerTypography.number.copy(fontSize = 13.sp),
                            color = colors.text,
                        )
                        Text(
                            String.format(Locale.ROOT, "%+.1f dB", gainDb),
                            style = PrunerTypography.number.copy(fontSize = 12.sp),
                            color = colors.secondary,
                        )
                    }
                    Slider(
                        value = gainDb,
                        onValueChange = { gainDb = it },
                        valueRange = eqState.minDb..eqState.maxDb,
                        onValueChangeFinished = { vm.setEqBandDb(i, gainDb) },
                        colors =
                            SliderDefaults.colors(
                                thumbColor = colors.accentText,
                                activeTrackColor = colors.accent,
                                inactiveTrackColor = colors.surfaceElevated,
                            ),
                    )
                }
            }
            item { Spacer(Modifier.height(16.dp)) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SleepSheet(vm: PrunerViewModel, close: () -> Unit) {
    ModalBottomSheet(onDismissRequest = close, containerColor = colors.surface) {
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Heading("睡眠定时")
            Actions {
                listOf(0, 15, 30, 45, 60, 90).forEach { n ->
                    Action(
                        label = if (n == 0) "关闭定时" else "$n 分钟",
                        style = if (vm.sleep == n) ActionStyle.Primary else ActionStyle.Secondary,
                    ) {
                        vm.timer(n)
                        close()
                    }
                }
            }
            Spacer(Modifier.height(12.dp))
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Stage(vm: PrunerViewModel, open: (Overlay) -> Unit, close: () -> Unit) {
    val s = vm.state
    val t = s.current_track ?: return
    val scope = rememberCoroutineScope()
    val haptic = LocalHapticFeedback.current
    var fullLyricsMode by remember { mutableStateOf(false) }
    var showMoreSheet by remember { mutableStateOf(false) }
    val dismissOffsetY = remember { Animatable(0f) }
    val coverDragX = remember { Animatable(0f) }
    val density = LocalDensity.current
    val dismissThresholdPx = with(density) { 130.dp.toPx() }
    val skipThresholdPx = with(density) { 88.dp.toPx() }

    // Subtle opaque album tint grounded in DESIGN.md (zero glassmorphism).
    val baseBg = colors.background
    val isPaperTheme = colors.theme.isPaper
    val stageBg =
        remember(baseBg, vm.extractedCoverColor, isPaperTheme) {
            val extracted = vm.extractedCoverColor
            if (extracted != null) {
                blend(
                    baseBg,
                    sanitizeAlbumAccent(extracted, isPaperTheme),
                    if (isPaperTheme) 0.06f else 0.09f,
                )
            } else baseBg
        }

    val qualityBadge =
        when (t.effectiveProvider().lowercase()) {
            "netease" -> vm.quality.displayLabel()
            else -> t.displayProviderLabel()
        }

    Surface(
        modifier =
            Modifier.fillMaxSize()
                .graphicsLayer { translationY = dismissOffsetY.value.coerceAtLeast(0f) }
                .pointerInput(fullLyricsMode) {
                    if (fullLyricsMode) return@pointerInput
                    detectVerticalDragGestures(
                        onDragEnd = {
                            scope.launch {
                                if (dismissOffsetY.value > dismissThresholdPx) {
                                    close()
                                    dismissOffsetY.snapTo(0f)
                                } else {
                                    dismissOffsetY.animateTo(
                                        0f,
                                        spring(stiffness = Spring.StiffnessMediumLow),
                                    )
                                }
                            }
                        },
                        onDragCancel = {
                            scope.launch { dismissOffsetY.animateTo(0f) }
                        },
                    ) { _, dragAmount ->
                        val next = (dismissOffsetY.value + dragAmount).coerceAtLeast(0f)
                        scope.launch { dismissOffsetY.snapTo(next) }
                    }
                },
        color = stageBg,
    ) {
        Column(
            Modifier.fillMaxSize()
                .statusBarsPadding()
                .navigationBarsPadding()
                .padding(horizontal = 20.dp, vertical = 10.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            // 1. Subtle drag pill + Top bar (always swipe-down dismissible even in full-screen lyrics mode)
            Column(
                modifier =
                    Modifier.fillMaxWidth().pointerInput(Unit) {
                        detectVerticalDragGestures(
                            onDragEnd = {
                                scope.launch {
                                    if (dismissOffsetY.value > dismissThresholdPx) {
                                        close()
                                        dismissOffsetY.snapTo(0f)
                                    } else {
                                        dismissOffsetY.animateTo(
                                            0f,
                                            spring(stiffness = Spring.StiffnessMediumLow),
                                        )
                                    }
                                }
                            },
                            onDragCancel = {
                                scope.launch { dismissOffsetY.animateTo(0f) }
                            },
                        ) { _, dragAmount ->
                            val next = (dismissOffsetY.value + dragAmount).coerceAtLeast(0f)
                            scope.launch { dismissOffsetY.snapTo(next) }
                        }
                    },
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Box(
                    Modifier.align(Alignment.CenterHorizontally)
                        .width(36.dp)
                        .height(4.dp)
                        .clip(RoundedCornerShape(2.dp))
                        .background(colors.borderMedium)
                )
                Row(
                    Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Glyph(Icons.Outlined.KeyboardArrowDown, "收起", click = close)
                    Meta(qualityBadge)
                    Glyph(Icons.Outlined.MoreVert, "更多控制") { showMoreSheet = true }
                }
            }

            // 2. Main stage content: Artwork + compact synced lyrics OR Full-Screen Lyrics Mode
            if (fullLyricsMode) {
                // Compact track header inside full-screen lyrics mode
                Row(
                    Modifier.fillMaxWidth()
                        .clip(controlShape)
                        .background(colors.surface)
                        .border(1.dp, colors.borderSubtle, controlShape)
                        .clickable { fullLyricsMode = false }
                        .padding(10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    Cover(t.cover_url, Modifier.size(42.dp), sizeTier = 96)
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(1.dp)) {
                        Text(
                            t.title.ifBlank { "未知曲目" },
                            style = MaterialTheme.typography.titleMedium,
                            color = colors.text,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        Text(
                            t.artist.ifBlank { t.artists.joinToString(" / ") }.ifBlank { "未知歌手" },
                            style = MaterialTheme.typography.bodySmall,
                            color = colors.secondary,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                    Meta("返回封面")
                }
                StageLyricsPane(
                    vm = vm,
                    fullScreen = true,
                    modifier = Modifier.weight(1f),
                )
            } else {
                // Artwork with swipe-left/right track skipping + gentle breathing scale
                val breathScale =
                    if (s.is_playing)
                        rememberInfiniteTransition(label = "stage_cover")
                            .animateFloat(
                                initialValue = 0.97f,
                                targetValue = 1f,
                                animationSpec =
                                    infiniteRepeatable(
                                        tween(3200, easing = FastOutSlowInEasing),
                                        RepeatMode.Reverse,
                                    ),
                                label = "cover_breath",
                            )
                            .value
                    else 0.95f
                Box(
                    Modifier.fillMaxWidth().weight(1f, fill = false),
                    contentAlignment = Alignment.Center,
                ) {
                    Cover(
                        url = t.cover_url,
                        sizeTier = 384,
                        onPaletteExtracted = { vm.extractedCoverColor = it },
                        modifier =
                            Modifier.widthIn(max = 264.dp)
                                .fillMaxWidth(0.76f)
                                .aspectRatio(1f)
                                .graphicsLayer {
                                    scaleX = breathScale
                                    scaleY = breathScale
                                    translationX = coverDragX.value
                                    rotationZ = (coverDragX.value / 42f).coerceIn(-8f, 8f)
                                }
                                .pointerInput(t.identity()) {
                                    detectHorizontalDragGestures(
                                        onDragEnd = {
                                            val dx = coverDragX.value
                                            scope.launch {
                                                if (dx < -skipThresholdPx) {
                                                    haptic.performHapticFeedback(
                                                        HapticFeedbackType.TextHandleMove
                                                    )
                                                    vm.transport("next")
                                                } else if (dx > skipThresholdPx) {
                                                    haptic.performHapticFeedback(
                                                        HapticFeedbackType.TextHandleMove
                                                    )
                                                    vm.transport("previous")
                                                }
                                                coverDragX.animateTo(
                                                    0f,
                                                    spring(stiffness = Spring.StiffnessMediumLow),
                                                )
                                            }
                                        },
                                        onDragCancel = {
                                            scope.launch { coverDragX.animateTo(0f) }
                                        },
                                    ) { change, amount ->
                                        change.consume()
                                        scope.launch {
                                            coverDragX.snapTo(
                                                (coverDragX.value + amount).coerceIn(-180f, 180f)
                                            )
                                        }
                                    }
                                },
                    )
                }

                // Track title + artist + album + heart
                Row(
                    Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Text(
                            t.title.ifBlank { "未知曲目" },
                            style = MaterialTheme.typography.headlineMedium,
                            fontSize = 20.sp,
                            color = colors.text,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        val artistLine =
                            listOfNotNull(
                                    t.artist.ifBlank { t.artists.joinToString(" / ") }.takeIf { it.isNotBlank() },
                                    t.album.takeIf { it.isNotBlank() },
                                )
                                .joinToString(" · ")
                                .ifBlank { "未知歌手" }
                        Text(
                            artistLine,
                            style = MaterialTheme.typography.bodyMedium,
                            color = colors.secondary,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                    Glyph(
                        icon = if (vm.liked) Icons.Filled.Favorite else Icons.Outlined.FavoriteBorder,
                        label = "喜欢",
                        tint = if (vm.liked) colors.prune else colors.text,
                    ) {
                        vm.like()
                    }
                }

                vm.playbackError?.let { err ->
                    Panel(containerColor = colors.surfaceWell, contentPadding = PaddingValues(10.dp)) {
                        Row(
                            Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween,
                        ) {
                            Text(
                                err,
                                style = MaterialTheme.typography.bodySmall,
                                color = colors.text,
                                modifier = Modifier.weight(1f),
                            )
                            Action("重试", style = ActionStyle.Primary) { vm.transport("toggle") }
                        }
                    }
                }

                // Compact synced lyrics preview box (tap header/area to enter full-screen lyrics)
                Surface(
                    modifier =
                        Modifier.fillMaxWidth()
                            .weight(1f)
                            .heightIn(min = 96.dp)
                            .clickable { fullLyricsMode = true },
                    shape = surfaceShape,
                    color = colors.surfaceWell,
                    border = BorderStroke(1.dp, colors.borderSubtle),
                ) {
                    StageLyricsPane(
                        vm = vm,
                        fullScreen = false,
                        modifier = Modifier.fillMaxSize(),
                        onTapEmptyOrToggle = { fullLyricsMode = true },
                    )
                }
            }

            // 3. Isolated Scrubber
            StageScrubber(vm, t)

            // 4. Clean 5-control primary transport bar
            val (modeIcon, modeLabel) = playModeIconAndLabel(s.play_mode)
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceEvenly,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Glyph(
                    icon = modeIcon,
                    label = modeLabel,
                    tint = if (s.play_mode != PlayMode.order) colors.accentText else colors.secondary,
                ) {
                    val next = nextPlayMode(s.play_mode)
                    vm.mode(next)
                    vm.notice(playModeIconAndLabel(next).second)
                }
                Glyph(Icons.Outlined.SkipPrevious, "上一首") { vm.transport("previous") }
                Transport(vm, size = 54.dp)
                Glyph(Icons.Outlined.SkipNext, "下一首") { vm.transport("next") }
                Glyph(Icons.AutoMirrored.Outlined.QueueMusic, "播放队列") { open(Overlay.QUEUE) }
            }

            // 5. Calm single-row utility strip
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    CompactStageChip(
                        label = if (fullLyricsMode) "封面" else "词",
                        active = fullLyricsMode,
                    ) {
                        fullLyricsMode = !fullLyricsMode
                    }
                    if (vm.lyrics.lines.any { !it.translation.isNullOrBlank() }) {
                        CompactStageChip(label = "译", active = vm.showLyricsTranslation) {
                            vm.updateLyricsDisplay(translation = !vm.showLyricsTranslation)
                        }
                    }
                    if (vm.lyrics.lines.any { !it.romaji.isNullOrBlank() }) {
                        CompactStageChip(label = "音", active = vm.showLyricsRomaji) {
                            vm.updateLyricsDisplay(romaji = !vm.showLyricsRomaji)
                        }
                    }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    if (vm.desktop != null) {
                        CompactStageChip(label = "推送桌面", active = false) { vm.push() }
                    }
                    CompactStageChip(
                        label = if (vm.sleep > 0) "${vm.sleep}分" else "定时",
                        active = vm.sleep > 0,
                    ) {
                        open(Overlay.SLEEP)
                    }
                }
            }
        }
    }

    if (showMoreSheet) {
        StageMoreSheet(
            vm = vm,
            track = t,
            onOpenEq = {
                showMoreSheet = false
                open(Overlay.EQ)
            },
            onOpenSleep = {
                showMoreSheet = false
                open(Overlay.SLEEP)
            },
            onDismiss = { showMoreSheet = false },
        )
    }
}

@Composable
private fun StageLyricsPane(
    vm: PrunerViewModel,
    fullScreen: Boolean,
    modifier: Modifier = Modifier,
    onTapEmptyOrToggle: (() -> Unit)? = null,
) {
    val prog by vm.playbackProgress.collectAsState()
    SyncedLyricsView(
        lyrics = vm.lyrics,
        progress = prog,
        offsetMs = vm.offset,
        showTranslation = vm.showLyricsTranslation,
        showRomaji = vm.showLyricsRomaji,
        fullScreen = fullScreen,
        onSeekMs = { ms -> vm.seek(ms / 1000f) },
        modifier = modifier,
        onTapEmptyOrToggle = onTapEmptyOrToggle,
    )
}

@Composable
private fun CompactStageChip(label: String, active: Boolean, onClick: () -> Unit) {
    Box(
        Modifier.clip(controlShape)
            .background(if (active) blend(colors.surface, colors.accent, 0.16f) else colors.surfaceWell)
            .border(1.dp, if (active) colors.accent else colors.borderSubtle, controlShape)
            .clickable(onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 6.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelMedium,
            fontWeight = if (active) FontWeight.SemiBold else FontWeight.Medium,
            color = if (active) colors.accentText else colors.secondary,
        )
    }
}

@Composable
private fun StageScrubber(vm: PrunerViewModel, track: Track) {
    val prog by vm.playbackProgress.collectAsState()
    val effectiveDurationSec =
        prog.durationMs.takeIf { it > 0L }?.div(1000.0)
            ?: vm.state.duration_sec.takeIf { it.isFinite() && it > 0.0 }
            ?: track.durationSeconds()
    val maxSec = effectiveDurationSec.toFloat().coerceAtLeast(0.01f)
    val currentSec = (prog.positionMs / 1000f).coerceIn(0f, maxSec)
    var dragSec by remember(track.identity()) { mutableStateOf<Float?>(null) }

    Column {
        Slider(
            value = dragSec ?: currentSec,
            onValueChange = { dragSec = it },
            valueRange = 0f..maxSec,
            onValueChangeFinished = {
                dragSec?.let { vm.seek(it) }
                dragSec = null
            },
            colors =
                SliderDefaults.colors(
                    thumbColor = colors.accentText,
                    activeTrackColor = colors.accent,
                    inactiveTrackColor = colors.surfaceElevated,
                ),
        )
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(
                formatClock((dragSec ?: currentSec).toDouble()),
                style = PrunerTypography.number.copy(fontSize = 12.sp),
                color = colors.secondary,
            )
            Text(
                formatClock(effectiveDurationSec),
                style = PrunerTypography.number.copy(fontSize = 12.sp),
                color = colors.secondary,
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun StageMoreSheet(
    vm: PrunerViewModel,
    track: Track,
    onOpenEq: () -> Unit,
    onOpenSleep: () -> Unit,
    onDismiss: () -> Unit,
) {
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = colors.surface) {
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Heading("播放控制与工具")

            // Playback speed
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("播放倍速", style = MaterialTheme.typography.labelLarge, color = colors.secondary)
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(listOf(0.5f, 0.75f, 1.0f, 1.25f, 1.5f, 2.0f)) { spd ->
                        val active = abs(vm.state.playback_rate - spd.toDouble()) < 0.01
                        Action(
                            label = "${spd}x",
                            style = if (active) ActionStyle.Primary else ActionStyle.Secondary,
                        ) {
                            vm.speed(spd)
                        }
                    }
                }
            }

            // Lyrics offset stepper
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("歌词时间偏移", style = MaterialTheme.typography.labelLarge, color = colors.secondary)
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Action("−0.5 s", style = ActionStyle.Secondary) { vm.offset -= 500 }
                    Action(
                        String.format(Locale.ROOT, "%+.1f s", vm.offset / 1000.0),
                        style = ActionStyle.Ghost,
                    ) {
                        vm.offset = 0
                    }
                    Action("+0.5 s", style = ActionStyle.Secondary) { vm.offset += 500 }
                }
            }

            // Quick tools
            Actions {
                Action("音频均衡器", style = ActionStyle.Secondary, onClick = onOpenEq)
                Action("睡眠定时", style = ActionStyle.Secondary, onClick = onOpenSleep)
                if (track.effectiveProvider() != "device") {
                    Action("离线缓存此曲", style = ActionStyle.Secondary) {
                        vm.cacheSingleTrackOffline(track)
                        onDismiss()
                    }
                }
                if (vm.desktop != null) {
                    Action("推送到桌面", style = ActionStyle.Secondary) {
                        vm.push()
                        onDismiss()
                    }
                }
            }
            Spacer(Modifier.height(16.dp))
        }
    }
}
