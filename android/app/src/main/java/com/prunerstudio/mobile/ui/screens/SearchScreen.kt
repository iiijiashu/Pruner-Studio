package com.prunerstudio.mobile.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.prunerstudio.mobile.core.model.Track
import com.prunerstudio.mobile.core.sources.CompositeAudioSourceResolver
import com.prunerstudio.mobile.ui.state.PrunerViewModel
import com.prunerstudio.mobile.ui.state.matches
import com.prunerstudio.mobile.ui.theme.*

private enum class SearchChannel(val label: String) {
    ALL("全部"),
    DEVICE("本机音频"),
    LAN("桌面曲库"),
    NETEASE("网易云"),
    EXTERNAL("多源 / 外部"),
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SearchScreen(vm: PrunerViewModel, onBack: () -> Unit) {
    var query by rememberSaveable { mutableStateOf("") }
    var channel by rememberSaveable { mutableStateOf(SearchChannel.ALL) }
    val focusRequester = remember { FocusRequester() }
    val activeId = vm.state.current_track?.identity()

    LaunchedEffect(Unit) {
        runCatching { focusRequester.requestFocus() }
    }

    val hasMultiSource = vm.hasMultiSourceSearch
    LaunchedEffect(hasMultiSource) {
        if (!hasMultiSource && channel == SearchChannel.EXTERNAL) {
            channel = SearchChannel.ALL
        }
    }
    val deviceHits =
        remember(query, vm.device) {
            if (query.isBlank()) emptyList() else vm.device.filter { it.matches(query) }
        }
    val sections: List<Pair<String, List<Track>>> =
        remember(
            query,
            channel,
            deviceHits,
            vm.lanResults,
            vm.onlineResults,
            vm.externalResults,
            hasMultiSource,
        ) {
            if (query.isBlank()) emptyList()
            else
                buildList {
                    if (channel == SearchChannel.ALL || channel == SearchChannel.DEVICE) {
                        add("本机音频" to deviceHits)
                    }
                    if (channel == SearchChannel.ALL || channel == SearchChannel.LAN) {
                        add("桌面曲库" to vm.lanResults)
                    }
                    if (channel == SearchChannel.ALL || channel == SearchChannel.NETEASE) {
                        add("网易云音乐" to vm.onlineResults)
                    }
                    if (
                        (channel == SearchChannel.ALL || channel == SearchChannel.EXTERNAL) &&
                            hasMultiSource
                    ) {
                        add("多源 / 外部音源" to vm.externalResults)
                    }
                }
        }

    Column(Modifier.fillMaxSize().background(colors.background)) {
        Row(
            Modifier.fillMaxWidth()
                .background(colors.background)
                .padding(horizontal = 8.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Glyph(Icons.AutoMirrored.Outlined.ArrowBack, "返回") {
                vm.search("")
                onBack()
            }
            OutlinedTextField(
                value = query,
                onValueChange = {
                    query = it
                    vm.search(it)
                },
                modifier = Modifier.weight(1f).focusRequester(focusRequester),
                placeholder = { Text("搜索歌曲、歌手、专辑", color = colors.secondary) },
                singleLine = true,
                shape = controlShape,
                colors = prunerTextFieldColors(),
                trailingIcon = {
                    if (query.isNotEmpty()) {
                        IconButton(
                            onClick = {
                                query = ""
                                vm.search("")
                            }
                        ) {
                            Icon(Icons.Outlined.Close, "清空搜索", tint = colors.secondary)
                        }
                    }
                },
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                keyboardActions =
                    KeyboardActions(
                        onSearch = {
                            if (query.isNotBlank()) {
                                vm.recordSearchQuery(query)
                                vm.search(query)
                            }
                        }
                    ),
            )
            Spacer(Modifier.width(4.dp))
        }

        LazyRow(
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            val channels =
                if (hasMultiSource) SearchChannel.entries
                else SearchChannel.entries.filterNot { it == SearchChannel.EXTERNAL }
            items(channels) { ch ->
                val selected = ch == channel
                Box(
                    Modifier.clip(controlShape)
                        .background(if (selected) colors.primaryActionBg else colors.surface)
                        .border(
                            1.dp,
                            if (selected) colors.primaryActionBg else colors.borderMedium,
                            controlShape,
                        )
                        .clickable { channel = ch }
                        .padding(horizontal = 12.dp, vertical = 7.dp)
                ) {
                    Text(
                        ch.label,
                        style = MaterialTheme.typography.labelLarge,
                        fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
                        color = if (selected) colors.primaryActionFg else colors.text,
                    )
                }
            }
        }
        HorizontalDivider(color = colors.borderSubtle, modifier = Modifier.padding(top = 6.dp))

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(0.dp),
        ) {
            if (query.isBlank()) {
                if (vm.searchHistory.isNotEmpty()) {
                    item(key = "history_header") {
                        Box(Modifier.padding(bottom = 10.dp)) {
                            SectionHeader("搜索历史") {
                                Action("清空", style = ActionStyle.Ghost) { vm.clearSearchHistory() }
                            }
                        }
                    }
                    item(key = "history_chips") {
                        FlowRow(
                            modifier = Modifier.padding(bottom = 12.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            vm.searchHistory.forEach { term ->
                                Row(
                                    Modifier.clip(controlShape)
                                        .background(colors.surface)
                                        .border(1.dp, colors.borderSubtle, controlShape)
                                        .clickable {
                                            query = term
                                            vm.search(term)
                                        }
                                        .padding(start = 10.dp, end = 4.dp, top = 6.dp, bottom = 6.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(4.dp),
                                ) {
                                    Text(
                                        term,
                                        style = MaterialTheme.typography.bodySmall,
                                        color = colors.text,
                                    )
                                    Icon(
                                        Icons.Outlined.Close,
                                        "删除记录",
                                        tint = colors.secondary,
                                        modifier =
                                            Modifier.size(16.dp).clickable {
                                                vm.deleteSearchHistory(term)
                                            },
                                    )
                                }
                            }
                        }
                    }
                } else {
                    item(key = "search_empty_hint") {
                        EmptyStatePanel(
                            icon = Icons.Outlined.Search,
                            title = "跨源聚合搜索",
                            subtitle = "支持同时检索本机音频、已配对桌面曲库、桌面多平台渠道、网易云音乐与已启用的外部音源。点按曲目将插播并保留当前队列。",
                        )
                    }
                }
            } else {
                if (vm.isSearching) {
                    items(3, key = { "skel_$it" }) {
                        GroupedRowCard(index = it, count = 3) { TrackRowSkeleton() }
                    }
                }
                sections.forEach { (sectionName, rows) ->
                    item(key = "hdr_$sectionName") {
                        Row(
                            Modifier.fillMaxWidth().padding(top = 4.dp, bottom = 8.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Heading(sectionName)
                            if (rows.isNotEmpty()) {
                                Meta("${rows.size} 首")
                            }
                        }
                    }
                    if (rows.isEmpty()) {
                        item(key = "empty_$sectionName") {
                            Text(
                                "暂无匹配结果",
                                style = MaterialTheme.typography.bodySmall,
                                color = colors.secondary,
                                modifier = Modifier.padding(start = 4.dp, end = 4.dp, top = 2.dp, bottom = 12.dp),
                            )
                        }
                    } else {
                        itemsIndexed(
                            items = rows,
                            key = { idx, t -> "res_${sectionName}_${t.identity()}_$idx" },
                        ) { i, t ->
                            GroupedRowCard(index = i, count = rows.size) {
                                TrackRow(
                                    track = t,
                                    click = {
                                        vm.recordSearchQuery(query)
                                        vm.playPreservingQueue(t)
                                    },
                                    active = t.identity() == activeId,
                                    reasonTag =
                                        if (
                                            t.effectiveProvider() !in setOf("netease", "device", "local") ||
                                                t.source_id == CompositeAudioSourceResolver.LAN_DESKTOP_SOURCE_ID
                                        )
                                            t.displayProviderLabel()
                                        else null,
                                ) {
                                    TrackOverflowMenu(
                                        track = t,
                                        onPlayNow = {
                                            vm.recordSearchQuery(query)
                                            vm.play(rows, i)
                                        },
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
}
