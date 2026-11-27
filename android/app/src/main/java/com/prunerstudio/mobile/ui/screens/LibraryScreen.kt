package com.prunerstudio.mobile.ui.screens

import androidx.activity.compose.BackHandler
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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.*
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LibraryScreen(
    vm: PrunerViewModel,
    scan: () -> Unit,
    onOpenSearch: () -> Unit = {},
) {
    val section = vm.librarySection
    var query by remember { mutableStateOf("") }
    val p = vm.selected
    val activeId = vm.state.current_track?.identity()
    val cacheProgress by vm.cacheProgressFlow.collectAsState()

    BackHandler(p != null) { vm.selected = null }
    LaunchedEffect(section, query) { if (section == 1) vm.browseLan(query) }

    PullToRefreshBox(
        isRefreshing = vm.isRefreshing,
        onRefresh = { vm.pullToRefresh() },
        modifier = Modifier.fillMaxSize(),
    ) {
        Page(verticalSpacing = 0.dp) {
            item(key = "lib_search_bar") {
                Row(
                    Modifier.fillMaxWidth()
                        .padding(bottom = 12.dp)
                        .clip(controlShape)
                        .background(colors.surfaceWell)
                        .border(1.dp, colors.borderSubtle, controlShape)
                        .clickable { onOpenSearch() }
                        .padding(horizontal = 12.dp, vertical = 11.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Icon(Icons.Outlined.Search, null, Modifier.size(18.dp), tint = colors.secondary)
                    Text(
                        "搜索曲库、桌面与在线音乐",
                        style = MaterialTheme.typography.bodyMedium,
                        color = colors.secondary,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            item(key = "lib_segments") {
                Box(Modifier.padding(bottom = 12.dp)) {
                    Segments(listOf("歌单", "桌面曲库", "本机音频"), section) {
                        vm.librarySection = it
                        vm.selected = null
                    }
                }
            }
            when (section) {
                0 ->
                    if (p == null) {
                        if (vm.playlists.isEmpty()) {
                            item(key = "pl_empty") {
                                EmptyStatePanel(
                                    icon = Icons.Outlined.LibraryMusic,
                                    title = "尚未同步任何歌单",
                                    subtitle = "登录网易云音乐账号或与桌面端配对后，歌单与曲库将离线缓存于此。",
                                    actionLabel = "前往设置",
                                    onAction = { vm.tab = Tab.SETTINGS },
                                )
                            }
                        } else {
                            // Real LazyColumn virtualization with grouped card styling
                            itemsIndexed(
                                items = vm.playlists,
                                key = { _, pl -> "pl_${pl.id.content}" },
                            ) { idx, pl ->
                                GroupedRowCard(index = idx, count = vm.playlists.size) {
                                    Row(
                                        Modifier.fillMaxWidth()
                                            .clip(controlShape)
                                            .clickable { vm.openPlaylist(pl) }
                                            .padding(horizontal = 8.dp, vertical = 10.dp),
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                                    ) {
                                        Cover(pl.cover_url, Modifier.size(46.dp), sizeTier = 96)
                                        Column(
                                            Modifier.weight(1f),
                                            verticalArrangement = Arrangement.spacedBy(2.dp),
                                        ) {
                                            Text(
                                                pl.displayName(),
                                                style = MaterialTheme.typography.titleMedium,
                                                color = colors.text,
                                                maxLines = 1,
                                                overflow = TextOverflow.Ellipsis,
                                            )
                                            Text(
                                                "${pl.track_count} 首",
                                                style = PrunerTypography.number.copy(fontSize = 12.sp),
                                                color = colors.secondary,
                                            )
                                        }
                                        Icon(
                                            Icons.Outlined.ChevronRight,
                                            null,
                                            tint = colors.secondary,
                                        )
                                    }
                                }
                            }
                        }
                    } else {
                        item(key = "pl_detail_header") {
                            Column(
                                Modifier.padding(bottom = 10.dp),
                                verticalArrangement = Arrangement.spacedBy(8.dp),
                            ) {
                                Action("返回歌单", style = ActionStyle.Ghost) { vm.selected = null }
                                Heading(p.displayName())
                            }
                        }
                        item(key = "pl_detail_actions") {
                            Box(Modifier.padding(bottom = 12.dp)) {
                                Actions {
                                    Action("播放全部", vm.tracks.isNotEmpty(), style = ActionStyle.Primary) {
                                        vm.play(vm.tracks)
                                    }
                                    Action(
                                        "均衡随机",
                                        vm.tracks.isNotEmpty(),
                                        style = ActionStyle.Secondary,
                                    ) {
                                        vm.play(vm.tracks, mode = PlayMode.shuffle)
                                    }
                                    Action(
                                        "Wi-Fi 离线缓存",
                                        vm.tracks.isNotEmpty() && !cacheProgress.active,
                                        style = ActionStyle.Secondary,
                                    ) {
                                        vm.cachePlaylistOffline(p, vm.tracks)
                                    }
                                    Action("进入整理台", style = ActionStyle.Ghost) {
                                        vm.startDeck(p, vm.tracks)
                                        vm.tab = Tab.DECK
                                    }
                                }
                            }
                        }
                        if (cacheProgress.active && cacheProgress.playlistId == p.id.content) {
                            item(key = "pl_cache_progress") {
                                Panel(
                                    Modifier.fillMaxWidth().padding(bottom = 12.dp),
                                    containerColor = colors.surfaceWell,
                                ) {
                                    val ratio =
                                        if (cacheProgress.total > 0)
                                            cacheProgress.completed.toFloat() / cacheProgress.total.toFloat()
                                        else 0f
                                    Text(
                                        "离线缓存中 (${cacheProgress.completed}/${cacheProgress.total}) · ${cacheProgress.currentTitle}",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = colors.accentText,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                    )
                                    LinearProgressIndicator(
                                        progress = { ratio.coerceIn(0f, 1f) },
                                        modifier = Modifier.fillMaxWidth(),
                                        color = colors.accent,
                                        trackColor = colors.surfaceElevated,
                                    )
                                }
                            }
                        }
                        if (vm.isLoadingPlaylist && vm.tracks.isEmpty()) {
                            items(5, key = { "pl_skel_$it" }) { idx ->
                                GroupedRowCard(index = idx, count = 5) { TrackRowSkeleton() }
                            }
                        } else {
                            itemsIndexed(
                                items = vm.tracks,
                                key = { idx, t -> "pltrk_${t.identity()}_$idx" },
                            ) { i, t ->
                                GroupedRowCard(index = i, count = vm.tracks.size) {
                                    TrackRow(
                                        track = t,
                                        click = { vm.play(vm.tracks, i) },
                                        active = t.identity() == activeId,
                                    ) {
                                        TrackOverflowMenu(
                                            track = t,
                                            onPlayNow = { vm.play(vm.tracks, i) },
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
                1 -> {
                    item(key = "lan_query") {
                        OutlinedTextField(
                            query,
                            { query = it },
                            Modifier.fillMaxWidth().padding(bottom = 12.dp),
                            label = { Text("搜索桌面曲库") },
                            singleLine = true,
                            shape = controlShape,
                            colors = prunerTextFieldColors(),
                            trailingIcon = {
                                if (query.isNotEmpty()) {
                                    IconButton(onClick = { query = "" }) {
                                        Icon(Icons.Outlined.Close, "清空", tint = colors.secondary)
                                    }
                                }
                            },
                        )
                    }
                    if (vm.browse.isEmpty()) {
                        item(key = "lan_empty") {
                            EmptyStatePanel(
                                icon = Icons.Outlined.Computer,
                                title = if (vm.desktop == null) "尚未配对桌面曲库" else "没有匹配的桌面曲目",
                                subtitle =
                                    if (vm.desktop == null) "在同一局域网内配对桌面端，即可直接无损串流电脑本地无损曲库。"
                                    else "尝试更换关键词，或在设置中手动同步桌面曲库。",
                                actionLabel = if (vm.desktop == null) "去设置配对" else null,
                                onAction = if (vm.desktop == null) ({ vm.tab = Tab.SETTINGS }) else null,
                            )
                        }
                    } else {
                        itemsIndexed(
                            items = vm.browse,
                            key = { idx, t -> "lan_${t.identity()}_$idx" },
                        ) { i, t ->
                            GroupedRowCard(index = i, count = vm.browse.size) {
                                TrackRow(
                                    track = t,
                                    click = { vm.play(vm.browse, i) },
                                    active = t.identity() == activeId,
                                ) {
                                    TrackOverflowMenu(
                                        track = t,
                                        onPlayNow = { vm.play(vm.browse, i) },
                                        onPlayNext = { vm.addNext(t) },
                                        onAddToEnd = { vm.appendToQueue(t) },
                                        onCacheOffline = { vm.cacheSingleTrackOffline(t) },
                                    )
                                }
                            }
                        }
                    }
                    if (vm.more) {
                        item(key = "lan_more") {
                            Action("加载更多", style = ActionStyle.Secondary) {
                                vm.browseLan(query, true)
                            }
                        }
                    }
                }
                2 -> {
                    item(key = "device_scan_bar") {
                        Row(
                            Modifier.fillMaxWidth().padding(bottom = 12.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Action(
                                if (vm.isScanning) "正在扫描..." else "扫描本机音频",
                                enabled = !vm.isScanning,
                                style = ActionStyle.Primary,
                                onClick = scan,
                            )
                            if (vm.device.isNotEmpty()) {
                                Meta("${vm.device.size} 首")
                            }
                        }
                    }
                    if (vm.isScanning && vm.device.isEmpty()) {
                        items(4, key = { "dev_skel_$it" }) { idx ->
                            GroupedRowCard(index = idx, count = 4) { TrackRowSkeleton() }
                        }
                    } else if (vm.device.isEmpty()) {
                        item(key = "device_empty") {
                            EmptyStatePanel(
                                icon = Icons.Outlined.AudioFile,
                                title = "本机音频库为空",
                                subtitle = "点击扫描即可索引手机内 FLAC / MP3 / M4A 本地音乐及内嵌封面。",
                                actionLabel = "立即扫描",
                                onAction = scan,
                            )
                        }
                    } else {
                        itemsIndexed(
                            items = vm.device,
                            key = { idx, t -> "dev_${t.identity()}_$idx" },
                        ) { i, t ->
                            GroupedRowCard(index = i, count = vm.device.size) {
                                TrackRow(
                                    track = t,
                                    click = { vm.play(vm.device, i) },
                                    active = t.identity() == activeId,
                                ) {
                                    TrackOverflowMenu(
                                        track = t,
                                        onPlayNow = { vm.play(vm.device, i) },
                                        onPlayNext = { vm.addNext(t) },
                                        onAddToEnd = { vm.appendToQueue(t) },
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun Actions(content: @Composable () -> Unit) {
    FlowRow(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        content()
    }
}
