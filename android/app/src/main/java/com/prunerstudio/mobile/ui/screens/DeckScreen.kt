package com.prunerstudio.mobile.ui.screens

import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.*
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.*
import com.prunerstudio.mobile.core.engine.*
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*
import kotlin.math.abs
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DeckScreen(vm: PrunerViewModel) {
    var review by remember { mutableStateOf<Deck?>(null) }
    var picker by remember { mutableStateOf(false) }
    val deck = vm.deck
    val remainingCount = (deck.tracks.size - deck.decisions.size).coerceAtLeast(0)
    val nextTrack = deck.tracks.getOrNull(deck.decisions.size + 1)
    val haptic = LocalHapticFeedback.current
    val scope = rememberCoroutineScope()

    fun decide(keep: Boolean) {
        if (vm.deck === deck) {
            haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
            vm.decide(keep)
        }
    }
    DisposableEffect(deck.current) { onDispose { vm.stopPreview() } }

    Page {
        item(key = "deck_toolbar") {
            Actions {
                Action(
                    deck.playlist?.displayName() ?: "选择待整理歌单",
                    style = ActionStyle.Secondary,
                ) {
                    picker = true
                }
                Action(
                    vm.deckOrder.label,
                    style = ActionStyle.Ghost,
                ) {
                    vm.updateDeckOrder(
                        if (vm.deckOrder == DeckOrder.RANDOM) DeckOrder.ORIGINAL
                        else DeckOrder.RANDOM
                    )
                }
                Action(
                    "换一首",
                    enabled = !vm.removing && remainingCount > 1,
                    style = ActionStyle.Secondary,
                ) {
                    vm.skipToAnotherDeckTrack()
                }
            }
        }
        item(key = "deck_stats") {
            Panel(
                Modifier.fillMaxWidth(),
                containerColor = colors.surfaceWell,
                contentPadding = PaddingValues(vertical = 12.dp, horizontal = 8.dp),
                spacing = 0.dp,
            ) {
                Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min)) {
                    listOf(
                            "已保留" to deck.decisions.count { it },
                            "待移除" to deck.staged.size,
                            "剩余" to remainingCount,
                        )
                        .forEachIndexed { idx, (label, n) ->
                            if (idx > 0) {
                                VerticalDivider(
                                    modifier = Modifier.fillMaxHeight(),
                                    color = colors.borderSubtle,
                                )
                            }
                            Column(
                                Modifier.weight(1f),
                                horizontalAlignment = Alignment.CenterHorizontally,
                                verticalArrangement = Arrangement.spacedBy(2.dp),
                            ) {
                                Text(
                                    label,
                                    style = MaterialTheme.typography.labelMedium,
                                    color = colors.secondary,
                                )
                                Text(
                                    n.toString(),
                                    style =
                                        PrunerTypography.number.copy(
                                            fontSize = 18.sp,
                                            fontWeight = FontWeight.SemiBold,
                                        ),
                                    color = colors.text,
                                )
                            }
                        }
                }
            }
        }
        deck.current?.let { t ->
            item(key = "deck_card_${deck.decisions.size}_${t.identity()}") {
                key(deck.decisions.size, t.identity()) {
                    val dragX = remember { Animatable(0f) }
                    var crossedThreshold by remember { mutableStateOf(false) }
                    val stub = t.isStubTrack()
                    val displayTitle =
                        t.title.ifBlank {
                            val rawId = t.song_id ?: t.id ?: t.external_id
                            if (rawId != null) "歌曲 #$rawId" else "未知曲目"
                        }
                    val displayArtist =
                        t.artist
                            .ifBlank { t.artists.joinToString(" / ") }
                            .ifBlank { if (stub) "暂无元数据" else "未知歌手" }
                    val durationText = formatTrackDuration(t.durationSeconds())
                    val dragProgress = (abs(dragX.value) / 260f).coerceIn(0f, 1f)
                    val isPreviewing = vm.previewTrackKey == t.identity()

                    Box(Modifier.fillMaxWidth()) {
                        // Stacked next-card depth layer behind the active card
                        if (nextTrack != null) {
                            val backScale = 0.94f + 0.05f * dragProgress
                            val backOffsetY = (12f - 8f * dragProgress).dp
                            Panel(
                                modifier =
                                    Modifier.fillMaxWidth()
                                        .graphicsLayer {
                                            scaleX = backScale
                                            scaleY = backScale
                                            translationY = backOffsetY.toPx()
                                        }
                                        .alpha(0.55f + 0.35f * dragProgress),
                                containerColor = colors.surfaceWell,
                            ) {
                                Spacer(Modifier.height(140.dp))
                                Text(
                                    nextTrack.title.ifBlank { "下一首待审曲目" },
                                    style = MaterialTheme.typography.titleMedium,
                                    color = colors.secondary,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                )
                                Text(
                                    nextTrack.artist
                                        .ifBlank { nextTrack.artists.joinToString(" / ") }
                                        .ifBlank { "未知歌手" },
                                    style = MaterialTheme.typography.bodySmall,
                                    color = colors.secondary,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                )
                            }
                        }

                        // Active draggable card with tilt rotation + left/right stamp overlays
                        Box(
                            modifier =
                                Modifier.fillMaxWidth()
                                    .graphicsLayer {
                                        translationX = dragX.value
                                        rotationZ = (dragX.value / 28f).coerceIn(-12f, 12f)
                                    }
                                    .pointerInput(deck, t.identity()) {
                                        detectHorizontalDragGestures(
                                            onDragEnd = {
                                                val delta = dragX.value
                                                val threshold = size.width * 0.28f
                                                crossedThreshold = false
                                                scope.launch {
                                                    if (abs(delta) > threshold) {
                                                        val keep = delta > 0f
                                                        val flingTarget =
                                                            if (keep) size.width * 1.15f
                                                            else -size.width * 1.15f
                                                        dragX.animateTo(
                                                            flingTarget,
                                                            tween(170, easing = FastOutLinearInEasing),
                                                        )
                                                        decide(keep)
                                                    } else {
                                                        dragX.animateTo(
                                                            0f,
                                                            spring(
                                                                dampingRatio = Spring.DampingRatioMediumBouncy,
                                                                stiffness = Spring.StiffnessMediumLow,
                                                            ),
                                                        )
                                                    }
                                                }
                                            },
                                            onDragCancel = {
                                                crossedThreshold = false
                                                scope.launch {
                                                    dragX.animateTo(
                                                        0f,
                                                        spring(dampingRatio = Spring.DampingRatioMediumBouncy),
                                                    )
                                                }
                                            },
                                        ) { change, amount ->
                                            change.consume()
                                            val next = dragX.value + amount
                                            val threshold = size.width * 0.28f
                                            val nowCrossed = abs(next) > threshold
                                            if (nowCrossed && !crossedThreshold) {
                                                crossedThreshold = true
                                                haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                                            } else if (!nowCrossed) {
                                                crossedThreshold = false
                                            }
                                            scope.launch { dragX.snapTo(next) }
                                        }
                                    }
                        ) {
                            Panel(Modifier.fillMaxWidth()) {
                                // 1:1 square album cover with directional decision stamps
                                Box(
                                    Modifier.fillMaxWidth(),
                                    contentAlignment = Alignment.Center,
                                ) {
                                    Cover(
                                        url = if (stub) "" else t.cover_url,
                                        sizeTier = 384,
                                        modifier =
                                            Modifier.widthIn(max = 248.dp)
                                                .fillMaxWidth(0.74f)
                                                .aspectRatio(1f),
                                    )
                                    // Right-keep stamp
                                    if (dragX.value > 24f) {
                                        val stampAlpha = ((dragX.value - 24f) / 140f).coerceIn(0f, 1f)
                                        Box(
                                            Modifier.align(Alignment.TopStart)
                                                .padding(10.dp)
                                                .alpha(stampAlpha)
                                                .graphicsLayer { rotationZ = -10f }
                                                .clip(controlShape)
                                                .background(colors.keep)
                                                .padding(horizontal = 12.dp, vertical = 6.dp)
                                        ) {
                                            Text(
                                                "保留",
                                                style = MaterialTheme.typography.titleMedium,
                                                fontWeight = FontWeight.Bold,
                                                color = colors.semanticInk,
                                            )
                                        }
                                    }
                                    // Left-prune stamp
                                    if (dragX.value < -24f) {
                                        val stampAlpha = ((-dragX.value - 24f) / 140f).coerceIn(0f, 1f)
                                        Box(
                                            Modifier.align(Alignment.TopEnd)
                                                .padding(10.dp)
                                                .alpha(stampAlpha)
                                                .graphicsLayer { rotationZ = 10f }
                                                .clip(controlShape)
                                                .background(colors.prune)
                                                .padding(horizontal = 12.dp, vertical = 6.dp)
                                        ) {
                                            Text(
                                                "待移除",
                                                style = MaterialTheme.typography.titleMedium,
                                                fontWeight = FontWeight.Bold,
                                                color = colors.semanticInk,
                                            )
                                        }
                                    }
                                }

                                if (stub) {
                                    Row(
                                        Modifier.clip(controlShape)
                                            .background(colors.surfaceWell)
                                            .border(1.dp, colors.borderMedium, controlShape)
                                            .padding(horizontal = 10.dp, vertical = 5.dp),
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                                    ) {
                                        Box(
                                            Modifier.size(7.dp)
                                                .clip(CircleShape)
                                                .background(colors.prune)
                                        )
                                        Text(
                                            "曲目已下架 / 仅存 ID",
                                            style = MaterialTheme.typography.labelMedium,
                                            color = colors.text,
                                        )
                                    }
                                }
                                Row(
                                    Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.SpaceBetween,
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Column(
                                        Modifier.weight(1f),
                                        verticalArrangement = Arrangement.spacedBy(2.dp),
                                    ) {
                                        Text(
                                            displayTitle,
                                            style = MaterialTheme.typography.titleLarge,
                                            color = colors.text,
                                            maxLines = 2,
                                            overflow = TextOverflow.Ellipsis,
                                        )
                                        Row(
                                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                                            verticalAlignment = Alignment.CenterVertically,
                                        ) {
                                            Text(
                                                displayArtist,
                                                style = MaterialTheme.typography.bodyMedium,
                                                color = colors.secondary,
                                                maxLines = 1,
                                                overflow = TextOverflow.Ellipsis,
                                                modifier = Modifier.weight(1f, fill = false),
                                            )
                                            if (durationText.isNotEmpty()) {
                                                Text(
                                                    durationText,
                                                    style = PrunerTypography.number.copy(fontSize = 12.sp),
                                                    color = colors.secondary,
                                                )
                                            }
                                        }
                                    }
                                    if (!stub) {
                                        val previewLabel =
                                            when {
                                                isPreviewing && vm.isPreviewLoading -> "缓冲中"
                                                isPreviewing -> "停止试听"
                                                else -> "试听"
                                            }
                                        Action(
                                            label = previewLabel,
                                            style =
                                                if (isPreviewing) ActionStyle.Primary
                                                else ActionStyle.Secondary,
                                        ) {
                                            vm.preview(t)
                                        }
                                    }
                                }
                                vm.rules.forEach { rule ->
                                    when (RulesEvaluator.evaluateRule(rule, t)) {
                                        RuleEvaluationResult.Matched -> Meta("匹配规则 · " + rule.name)
                                        is RuleEvaluationResult.Unevaluable -> Meta("无法评估 · " + rule.name)
                                        else -> Unit
                                    }
                                }
                                HorizontalDivider(color = colors.borderSubtle)
                                // Elevated decision dock: coral-red Remove left, quiet Undo center, sage-green Keep right.
                                Row(
                                    Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Action(
                                        "待移除",
                                        enabled = !vm.removing,
                                        fill = colors.prune,
                                        modifier = Modifier.weight(1f),
                                    ) {
                                        decide(false)
                                    }
                                    Action(
                                        "撤销",
                                        enabled = !vm.removing && deck.decisions.isNotEmpty(),
                                        style = ActionStyle.Ghost,
                                    ) {
                                        vm.undo()
                                    }
                                    Action(
                                        "保留",
                                        enabled = !vm.removing,
                                        fill = colors.keep,
                                        modifier = Modifier.weight(1f),
                                    ) {
                                        decide(true)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
            ?: item(key = "deck_empty") {
                EmptyStatePanel(
                    icon = Icons.Outlined.ContentCut,
                    title = if (deck.playlist == null) "选择歌单开始整理" else "本轮已整理完毕",
                    subtitle =
                        if (deck.playlist == null) "右滑保留，左滑移入待移除清单。所有云端移除均需最终二次确认。"
                        else "你可以撤销上一步，或点击下方核对并确认待移除清单。",
                    actionLabel = if (deck.playlist == null && vm.playlists.isNotEmpty()) "挑选歌单" else null,
                    onAction = if (deck.playlist == null && vm.playlists.isNotEmpty()) ({ picker = true }) else null,
                )
            }
        if (vm.removing && vm.removeProgress.isNotBlank()) {
            item(key = "remove_progress") {
                Panel(Modifier.fillMaxWidth(), containerColor = colors.surfaceWell) {
                    Text(
                        "正在分批安全移除：${vm.removeProgress}",
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.accentText,
                    )
                }
            }
        }
        item(key = "deck_footer") {
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                if (deck.current == null) {
                    Action(
                        "撤销",
                        enabled = !vm.removing && deck.decisions.isNotEmpty(),
                        style = ActionStyle.Ghost,
                    ) {
                        vm.undo()
                    }
                } else {
                    Spacer(Modifier.width(1.dp))
                }
                Action(
                    "核对移除清单",
                    enabled = !vm.removing && deck.staged.isNotEmpty(),
                    style = ActionStyle.Primary,
                ) {
                    review = deck
                }
            }
        }
    }

    if (picker) {
        ModalBottomSheet(onDismissRequest = { picker = false }, containerColor = colors.surface) {
            Column(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Heading("选择待整理歌单")
                HorizontalDivider(color = colors.borderSubtle)
                if (vm.playlists.isEmpty()) {
                    EmptyStatePanel(
                        icon = Icons.Outlined.LibraryMusic,
                        title = "暂无可整理歌单",
                        subtitle = "登录网易云音乐或同步桌面歌单后即可在此挑选。",
                    )
                } else {
                    LazyColumn(
                        modifier = Modifier.fillMaxWidth().heightIn(max = 420.dp),
                        contentPadding = PaddingValues(bottom = 24.dp),
                    ) {
                        itemsIndexed(
                            items = vm.playlists,
                            key = { _, pl -> "pick_${pl.id.content}" },
                        ) { idx, p ->
                            val kindBadge =
                                when (p.kind) {
                                    "liked" -> "我喜欢"
                                    "self_created" -> "自建"
                                    "subscribed" -> "只读收藏"
                                    else -> "缓存"
                                }
                            Row(
                                Modifier.fillMaxWidth()
                                    .clip(controlShape)
                                    .clickable {
                                        picker = false
                                        vm.openPlaylist(p, true)
                                    }
                                    .padding(horizontal = 8.dp, vertical = 10.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(12.dp),
                            ) {
                                Cover(p.cover_url, Modifier.size(44.dp), sizeTier = 96)
                                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                                    Text(
                                        p.displayName(),
                                        style = MaterialTheme.typography.titleMedium,
                                        color = colors.text,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                    )
                                    Text(
                                        "${p.track_count} 首",
                                        style = PrunerTypography.number.copy(fontSize = 12.sp),
                                        color = colors.secondary,
                                    )
                                }
                                Meta(kindBadge)
                            }
                            if (idx < vm.playlists.lastIndex) {
                                HorizontalDivider(color = colors.borderSubtle)
                            }
                        }
                    }
                }
            }
        }
    }

    review?.let { frozen ->
        AlertDialog(
            onDismissRequest = { review = null },
            containerColor = colors.surface,
            titleContentColor = colors.text,
            textContentColor = colors.text,
            title = { Text("核对待移除曲目 (${frozen.staged.size} 首)") },
            text = {
                LazyColumn(
                    modifier = Modifier.heightIn(max = 320.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    item {
                        Text(
                            if (vm.canRemove(frozen)) "确认后将按限流保护分批从网易云歌单移除以下曲目。"
                            else "确认后仅从本次本地整理队列移除。",
                            color = colors.secondary,
                        )
                    }
                    items(frozen.staged) {
                        Text(it.title.ifBlank { "歌曲 #${it.song_id ?: it.id ?: it.external_id.orEmpty()}" })
                    }
                }
            },
            confirmButton = {
                Action("确认移除", fill = colors.prune) {
                    review = null
                    if (vm.deck === frozen) vm.confirmReview(frozen)
                }
            },
            dismissButton = {
                Action("取消", style = ActionStyle.Ghost) { review = null }
            },
        )
    }
}
