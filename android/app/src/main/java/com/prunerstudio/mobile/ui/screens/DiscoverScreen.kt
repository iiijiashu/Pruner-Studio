package com.prunerstudio.mobile.ui.screens

import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.ui.*
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.*
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*

@Composable
fun Page(
    state: LazyListState = rememberLazyListState(),
    verticalSpacing: Dp = 12.dp,
    content: LazyListScope.() -> Unit,
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        state = state,
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(verticalSpacing),
        content = content,
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DiscoverScreen(vm: PrunerViewModel) {
    val mix =
        remember(vm.history, vm.device, vm.files, vm.cached, vm.notInterestedKeys) {
            (vm.history + vm.device + vm.files + vm.cached)
                .distinctBy { it.identity() }
                .filterNot { it.identity() in vm.notInterestedKeys }
        }
    val filteredDaily =
        remember(vm.daily, vm.notInterestedKeys) {
            vm.daily.filterNot { it.identity() in vm.notInterestedKeys }
        }
    val activeId = vm.state.current_track?.identity()
    val dusty = remember(mix) { mix.sortedBy { it.last_played_at.orEmpty() }.take(12) }
    val radioReady = mix.isNotEmpty() || filteredDaily.isNotEmpty()
    val radioPercent = exploreRatioPercent(vm.exploreRatio)
    val radioLabel = exploreRatioLabel(vm.exploreRatio)

    PullToRefreshBox(
        isRefreshing = vm.isRefreshing,
        onRefresh = { vm.pullToRefresh() },
        modifier = Modifier.fillMaxSize(),
    ) {
        Page(verticalSpacing = 0.dp) {
            vm.incoming?.let { s ->
                item(key = "handoff_banner") {
                    Panel(Modifier.fillMaxWidth().padding(bottom = 12.dp)) {
                        Text(
                            "来自桌面的播放",
                            style = MaterialTheme.typography.labelMedium,
                            color = colors.accentText,
                        )
                        Heading(s.track?.title?.ifBlank { "未知曲目" }.orEmpty())
                        s.track?.let { tr ->
                            val artist = tr.artist.ifBlank { tr.artists.joinToString(" / ") }
                            if (artist.isNotBlank()) {
                                Text(
                                    artist,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = colors.secondary,
                                )
                            }
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Action("接续播放", style = ActionStyle.Primary) { vm.accept() }
                            Action("忽略", style = ActionStyle.Ghost) { vm.dismissHandoff() }
                        }
                    }
                }
            }
            item(key = "smart_radio") {
                Panel(Modifier.fillMaxWidth().padding(bottom = 12.dp)) {
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(14.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        LivingAcousticOrb(
                            ratio = vm.exploreRatio,
                            playing = vm.state.is_playing && vm.state.play_mode == PlayMode.smart_radio,
                            modifier = Modifier.size(88.dp),
                        )
                        Column(
                            Modifier.weight(1f),
                            verticalArrangement = Arrangement.spacedBy(4.dp),
                        ) {
                            Row(
                                Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Heading("心动电台")
                                Text(
                                    "$radioPercent%",
                                    style =
                                        PrunerTypography.number.copy(
                                            fontSize = 14.sp,
                                            fontWeight = FontWeight.SemiBold,
                                        ),
                                    color = colors.accentText,
                                )
                            }
                            Text(
                                radioLabel,
                                style = MaterialTheme.typography.bodyMedium,
                                color = colors.secondary,
                            )
                            Spacer(Modifier.height(2.dp))
                            Action(
                                "一键开播",
                                enabled = radioReady,
                                style = ActionStyle.Primary,
                            ) {
                                vm.playSmartRadio()
                            }
                        }
                    }
                    Panel(
                        Modifier.fillMaxWidth(),
                        containerColor = colors.surfaceWell,
                        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
                        spacing = 2.dp,
                    ) {
                        Slider(
                            value = vm.exploreRatio,
                            onValueChange = { vm.updateExploreRatio(it) },
                            valueRange = SMART_RADIO_EXPLORE_MIN..SMART_RADIO_EXPLORE_MAX,
                            colors =
                                SliderDefaults.colors(
                                    thumbColor = colors.accentText,
                                    activeTrackColor = colors.accent,
                                    inactiveTrackColor = colors.surfaceElevated,
                                ),
                        )
                        Row(
                            Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                        ) {
                            listOf(
                                    0f to "0% 纯熟歌",
                                    0.5f to "50% 均衡",
                                    1f to "100% 纯新歌",
                                )
                                .forEach { (target, caption) ->
                                    val selected =
                                        kotlin.math.abs(vm.exploreRatio - target) < 0.01f
                                    Text(
                                        caption,
                                        Modifier.clip(controlShape)
                                            .clickable { vm.updateExploreRatio(target) }
                                            .padding(horizontal = 4.dp, vertical = 2.dp),
                                        style = MaterialTheme.typography.labelMedium,
                                        fontWeight =
                                            if (selected) FontWeight.SemiBold else FontWeight.Medium,
                                        color = if (selected) colors.accentText else colors.secondary,
                                    )
                                }
                        }
                    }
                }
            }
            item(key = "insights_header") {
                Box(Modifier.padding(bottom = 10.dp)) { Heading("听歌洞察") }
            }
            item(key = "insights_panel") {
                val metrics =
                    listOf(
                        "累计播放" to vm.history.sumOf { it.play_count }.toString(),
                        "聆听分钟" to (vm.history.sumOf { it.total_listen_ms } / 60000).toString(),
                        "已收录曲目" to
                            (vm.device + vm.files + vm.cached)
                                .distinctBy { it.identity() }
                                .size
                                .toString(),
                        "桌面配对" to if (vm.desktop != null) "已配对" else "未配对",
                    )
                Panel(
                    modifier = Modifier.fillMaxWidth().padding(bottom = 12.dp),
                    contentPadding = PaddingValues(0.dp),
                    spacing = 0.dp,
                ) {
                    metrics.chunked(2).forEachIndexed { rowIdx, pair ->
                        if (rowIdx > 0) HorizontalDivider(color = colors.borderSubtle)
                        Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min)) {
                            pair.forEachIndexed { colIdx, (label, value) ->
                                if (colIdx > 0) {
                                    VerticalDivider(
                                        modifier = Modifier.fillMaxHeight(),
                                        color = colors.borderSubtle,
                                    )
                                }
                                Column(
                                    Modifier.weight(1f).padding(horizontal = 16.dp, vertical = 14.dp),
                                    verticalArrangement = Arrangement.spacedBy(4.dp),
                                ) {
                                    Text(
                                        label,
                                        style = MaterialTheme.typography.labelMedium,
                                        color = colors.secondary,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                    )
                                    Text(
                                        value,
                                        style =
                                            if (value.all(Char::isDigit))
                                                PrunerTypography.number.copy(
                                                    fontSize = 22.sp,
                                                    fontWeight = FontWeight.SemiBold,
                                                )
                                            else MaterialTheme.typography.titleLarge,
                                        color =
                                            if (label == "桌面配对" && vm.desktop != null)
                                                colors.accentText
                                            else colors.text,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                    )
                                }
                            }
                        }
                    }
                }
            }
            item(key = "dusty_header") {
                Box(Modifier.padding(bottom = 10.dp)) {
                    SectionHeader("沉睡宝藏") {
                        Action("均衡随机播放", mix.isNotEmpty(), style = ActionStyle.Primary) {
                            vm.play(mix, mode = PlayMode.shuffle)
                        }
                    }
                }
            }
            if (dusty.isEmpty()) {
                item(key = "dusty_empty") {
                    EmptyStatePanel(
                        icon = Icons.Outlined.AutoAwesome,
                        title = "等待发现沉睡曲目",
                        subtitle = "扫描本机音频或同步桌面曲库后，久未聆听的宝藏歌曲将自动汇聚于此。",
                        modifier = Modifier.padding(bottom = 12.dp),
                        actionLabel = "前往音乐库",
                        onAction = { vm.tab = Tab.LIBRARY },
                    )
                }
            } else {
                item(key = "dusty_row") {
                    LazyRow(
                        modifier = Modifier.padding(bottom = 12.dp),
                        horizontalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        itemsIndexed(
                            items = dusty,
                            key = { _, tr -> "dusty_${tr.identity()}" },
                        ) { i, track ->
                            val isPlaying = track.identity() == activeId
                            val duration = formatTrackDuration(track.durationSeconds())
                            Panel(
                                modifier =
                                    Modifier.width(224.dp).clickable { vm.play(dusty, i) },
                                containerColor =
                                    if (isPlaying) blend(colors.surface, colors.accent, 0.12f)
                                    else colors.surface,
                                contentPadding = PaddingValues(12.dp),
                                spacing = 8.dp,
                            ) {
                                Row(
                                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Cover(track.cover_url, Modifier.size(48.dp), sizeTier = 96)
                                    Column(
                                        Modifier.weight(1f),
                                        verticalArrangement = Arrangement.spacedBy(2.dp),
                                    ) {
                                        Text(
                                            track.title.ifBlank { "未知曲目" },
                                            style = MaterialTheme.typography.titleMedium,
                                            fontSize = 14.sp,
                                            color = if (isPlaying) colors.accentText else colors.text,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis,
                                        )
                                        Text(
                                            track.artist
                                                .ifBlank { track.artists.joinToString(" / ") }
                                                .ifBlank { "未知歌手" },
                                            style = MaterialTheme.typography.bodySmall,
                                            color = colors.secondary,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis,
                                        )
                                        if (duration.isNotEmpty()) {
                                            Text(
                                                duration,
                                                style = PrunerTypography.number.copy(fontSize = 11.5.sp),
                                                color = colors.secondary,
                                            )
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if (vm.user.isNotEmpty()) {
                item(key = "daily_header") {
                    Box(Modifier.padding(bottom = 10.dp)) {
                        SectionHeader("每日推荐") {
                            if (filteredDaily.isNotEmpty()) {
                                Action("播放全部", style = ActionStyle.Secondary) { vm.play(filteredDaily, 0) }
                            }
                            Action("刷新", style = ActionStyle.Ghost) { vm.dailyRecommendations() }
                        }
                    }
                }
                if (filteredDaily.isEmpty() && vm.isRefreshing) {
                    items(4, key = { "daily_skel_$it" }) { idx ->
                        GroupedRowCard(index = idx, count = 4) { TrackRowSkeleton() }
                    }
                } else {
                    // Virtualized LazyColumn items instead of non-lazy forEachIndexed inside single item {}
                    itemsIndexed(
                        items = filteredDaily,
                        key = { _, track -> "daily_${track.identity()}" },
                    ) { i, track ->
                        GroupedRowCard(index = i, count = filteredDaily.size) {
                            TrackRow(
                                track = track,
                                click = { vm.play(filteredDaily, i) },
                                active = track.identity() == activeId,
                            ) {
                                TrackOverflowMenu(
                                    track = track,
                                    onPlayNow = { vm.play(filteredDaily, i) },
                                    onPlayNext = { vm.addNext(track) },
                                    onAddToEnd = { vm.appendToQueue(track) },
                                    onToggleLike = { vm.like(track) },
                                    onCacheOffline = { vm.cacheSingleTrackOffline(track) },
                                    onNotInterested = { vm.markNotInterested(track) },
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}
