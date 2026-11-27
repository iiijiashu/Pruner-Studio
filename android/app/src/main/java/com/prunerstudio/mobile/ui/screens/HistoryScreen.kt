package com.prunerstudio.mobile.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.ui.*
import androidx.compose.ui.unit.*
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*
import java.time.*

fun historyGroup(date: String?, today: LocalDate = LocalDate.now()): String {
    val local =
        runCatching { Instant.parse(date).atZone(ZoneId.systemDefault()).toLocalDate() }.getOrNull()
            ?: runCatching { LocalDate.parse(date?.take(10)) }.getOrNull()
    return when (local) {
        today -> "今天"
        today.minusDays(1) -> "昨天"
        else -> "更早"
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HistoryScreen(vm: PrunerViewModel) {
    val q = vm.historyQuery
    val activeId = vm.state.current_track?.identity()
    val groups =
        remember(vm.history, q) {
            vm.history
                .filter { q.isBlank() || it.matches(q) }
                .sortedByDescending { it.last_played_at }
                .groupBy { historyGroup(it.last_played_at) }
        }

    PullToRefreshBox(
        isRefreshing = vm.isRefreshing,
        onRefresh = { vm.pullToRefresh() },
        modifier = Modifier.fillMaxSize(),
    ) {
        Page(verticalSpacing = 0.dp) {
            item(key = "history_filter") {
                OutlinedTextField(
                    q,
                    { vm.historyQuery = it },
                    Modifier.fillMaxWidth().padding(bottom = 12.dp),
                    label = { Text("筛选播放历史") },
                    singleLine = true,
                    shape = controlShape,
                    colors = prunerTextFieldColors(),
                    trailingIcon = {
                        if (q.isNotEmpty()) {
                            IconButton(onClick = { vm.historyQuery = "" }) {
                                Icon(Icons.Outlined.Close, "清空筛选", tint = colors.secondary)
                            }
                        }
                    },
                )
            }
            if (groups.isEmpty()) {
                item(key = "history_empty") {
                    EmptyStatePanel(
                        icon = Icons.Outlined.History,
                        title = if (q.isBlank()) "暂无播放记录" else "没有匹配的播放记录",
                        subtitle =
                            if (q.isBlank()) "聆听超过 30 秒或完整播放一首曲目后，听歌轨迹会自动记录并同步。"
                            else "尝试缩短或清空筛选关键字。",
                        actionLabel = if (q.isNotBlank()) "清空筛选" else "去发现听歌",
                        onAction = {
                            if (q.isNotBlank()) vm.historyQuery = ""
                            else vm.tab = Tab.DISCOVER
                        },
                    )
                }
            }
            listOf("今天", "昨天", "更早").forEach { day ->
                groups[day]?.let { rows ->
                    item(key = "day_hdr_$day") {
                        Row(
                            Modifier.fillMaxWidth().padding(bottom = 8.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Heading(day)
                            Meta("${rows.size} 首")
                        }
                    }
                    // True LazyColumn virtualization
                    itemsIndexed(
                        items = rows,
                        key = { idx, t -> "hist_${day}_${t.identity()}_$idx" },
                    ) { idx, t ->
                        GroupedRowCard(index = idx, count = rows.size) {
                            TrackRow(
                                track = t,
                                click = { vm.playPreservingQueue(t) },
                                active = t.identity() == activeId,
                            ) {
                                TrackOverflowMenu(
                                    track = t,
                                    onPlayNow = { vm.play(rows, idx) },
                                    onPlayNext = { vm.addNext(t) },
                                    onAddToEnd = { vm.appendToQueue(t) },
                                    onToggleLike =
                                        if (vm.user.isNotEmpty() && t.effectiveProvider() == "netease") {
                                            { vm.like(t) }
                                        } else null,
                                    onCacheOffline = { vm.cacheSingleTrackOffline(t) },
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}
