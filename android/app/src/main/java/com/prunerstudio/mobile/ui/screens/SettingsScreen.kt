package com.prunerstudio.mobile.ui.screens

import android.net.Uri
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.*
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.*
import com.prunerstudio.mobile.core.lan.PairingUri
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*
import java.util.Locale
import java.util.UUID

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(vm: PrunerViewModel, eq: () -> Unit, timer: () -> Unit) {
    var link by remember { mutableStateOf("") }
    var discoveredName by remember { mutableStateOf("") }
    var host by remember { mutableStateOf("") }
    var port by remember { mutableStateOf("") }
    var pin by remember { mutableStateOf("") }
    var fp by remember { mutableStateOf("") }
    var spki by remember { mutableStateOf("") }
    var manual by remember { mutableStateOf(false) }
    var showAddSource by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) { vm.refreshCacheStats() }

    Page {
        item(key = "settings_lan") {
            Panel(Modifier.fillMaxWidth()) {
                Heading("桌面局域网互联")
                val d = vm.desktop
                if (d == null || manual) {
                    Action("扫描局域网桌面", style = ActionStyle.Primary) { vm.discover() }
                    vm.hosts.forEach { a ->
                        Action(
                            a.name + " · " + a.host,
                            enabled = a.pairingActive,
                            style = ActionStyle.Secondary,
                        ) {
                            discoveredName = a.name
                            host = a.host
                            port = a.port.toString()
                            fp = a.fingerprint
                            spki = a.spkiPin.orEmpty()
                            pin = ""
                            manual = true
                        }
                    }
                    Field("配对链接", link) { link = it }
                    Actions {
                        Action("解析链接", style = ActionStyle.Secondary) { vm.parsePair(link) }
                        Action("手动配对", style = ActionStyle.Ghost) { manual = !manual }
                    }
                    if (manual) {
                        Field("主机", host) {
                            host = it
                            discoveredName = ""
                        }
                        Field("端口", port) { port = it }
                        Field("六位配对码", pin) { pin = it }
                        Field("SHA-256 指纹", fp) { fp = it }
                        Field("SPKI 公钥 Pin（可选）", spki) { spki = it }
                        Action("核对配对信息", style = ActionStyle.Primary) {
                            vm.parsePair(
                                Uri.Builder()
                                    .scheme("pruner-lan")
                                    .authority("pair")
                                    .appendQueryParameter("name", discoveredName)
                                    .appendQueryParameter("v", "1")
                                    .appendQueryParameter("tls", "1")
                                    .appendQueryParameter("host", host.trim())
                                    .appendQueryParameter("port", port.trim())
                                    .appendQueryParameter("code", pin.trim())
                                    .appendQueryParameter("fp", fp.replace(":", "").trim())
                                    .apply {
                                        if (spki.isNotBlank()) {
                                            appendQueryParameter("spki", spki.trim())
                                        }
                                    }
                                    .build()
                                    .toString()
                            )
                        }
                    }
                    if (vm.pairError.isNotBlank()) {
                        Text(vm.pairError, style = MaterialTheme.typography.bodySmall, color = colors.prune)
                    }
                }
                if (d != null) {
                    Panel(
                        Modifier.fillMaxWidth(),
                        containerColor = colors.surfaceWell,
                        contentPadding = PaddingValues(12.dp),
                        spacing = 6.dp,
                    ) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            Box(
                                Modifier.size(8.dp)
                                    .clip(CircleShape)
                                    .background(
                                        if (vm.live == "已连接") colors.keep
                                        else colors.secondary
                                    )
                            )
                            Text(
                                d.host + " · " + d.port,
                                style = MaterialTheme.typography.titleMedium,
                                color = colors.text,
                            )
                        }
                        Text(
                            d.fingerprint.chunked(2).joinToString(":"),
                            style = PrunerTypography.number.copy(fontSize = 12.sp),
                            color = colors.secondary,
                        )
                        Text(
                            "实时同步 · " + vm.live,
                            style = MaterialTheme.typography.bodySmall,
                            color = colors.secondary,
                        )
                    }
                    Actions {
                        Action("立即同步", style = ActionStyle.Primary) { vm.sync() }
                        Action("拉取桌面播放", style = ActionStyle.Secondary) {
                            vm.pull()
                            vm.tab = Tab.DISCOVER
                        }
                        Action(
                            "推送到桌面",
                            vm.state.current_track != null,
                            style = ActionStyle.Secondary,
                        ) {
                            vm.push()
                        }
                        if (!manual) Action("更换桌面", style = ActionStyle.Ghost) { manual = true }
                        Action("解除配对", style = ActionStyle.Ghost) { vm.unpair() }
                    }
                }
            }
        }
        item(key = "settings_account") {
            Panel(Modifier.fillMaxWidth()) {
                Heading("网易云音乐账号")
                Text(vm.user.ifBlank { "离线模式" }, color = colors.secondary)
                if (vm.user.isBlank()) {
                    Action("扫码登录", style = ActionStyle.Primary) { vm.login() }
                } else {
                    Action("退出登录", style = ActionStyle.Ghost) { vm.logout() }
                }
            }
        }
        item(key = "settings_sources") {
            Panel(Modifier.fillMaxWidth()) {
                SectionHeader("外部音源与自托管") {
                    Action("添加", style = ActionStyle.Secondary) { showAddSource = true }
                }
                Text(
                    "支持接入 HTTP Resolver v2、OpenSubsonic 与 WebDAV 私人曲库，凭据由 Android Keystore 独立加密保存。",
                    style = MaterialTheme.typography.bodySmall,
                    color = colors.secondary,
                )
                if (vm.externalSources.isEmpty()) {
                    Text(
                        "尚未配置外部音源",
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.secondary,
                    )
                } else {
                    vm.externalSources.forEachIndexed { idx, src ->
                        if (idx > 0) HorizontalDivider(color = colors.borderSubtle)
                        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                            Row(
                                Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.SpaceBetween,
                            ) {
                                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                                    Text(src.name, style = MaterialTheme.typography.titleMedium, color = colors.text)
                                    Text(
                                        "${src.kind.label} · ${src.baseUrl}",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = colors.secondary,
                                    )
                                }
                                Switch(
                                    checked = src.enabled,
                                    onCheckedChange = { vm.toggleExternalSource(src, it) },
                                )
                            }
                            Actions {
                                Action("测试连通性", style = ActionStyle.Secondary) {
                                    vm.testExternalSource(src)
                                }
                                Action("删除", style = ActionStyle.Ghost) {
                                    vm.deleteExternalSource(src.id)
                                }
                            }
                        }
                    }
                }
            }
        }
        item(key = "settings_appearance") {
            Panel(Modifier.fillMaxWidth()) {
                Heading("外观与音质")
                Choice("主题", vm.theme.label, PrunerThemeId.entries.map { it.label }) {
                    vm.preferences(themeId = PrunerThemeId.entries[it])
                }
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text("跟随封面取色", style = MaterialTheme.typography.bodyMedium, color = colors.text)
                        Text(
                            "提取当前播放封面低饱和主色作为主题强调色",
                            style = MaterialTheme.typography.bodySmall,
                            color = colors.secondary,
                        )
                    }
                    Switch(
                        checked = vm.dynamicCoverAccent,
                        onCheckedChange = { vm.preferences(coverAccent = it) },
                    )
                }
                Text(
                    "强调色 · " + vm.accent.label,
                    style = MaterialTheme.typography.bodyMedium,
                    color = colors.secondary,
                )
                Actions {
                    AccentPreset.entries.forEach { p ->
                        val selected = p == vm.accent && !vm.dynamicCoverAccent
                        IconButton(onClick = { vm.preferences(preset = p, coverAccent = false) }) {
                            Box(
                                Modifier.size(34.dp)
                                    .then(
                                        if (selected)
                                            Modifier.border(2.dp, colors.text, CircleShape)
                                                .padding(3.dp)
                                        else Modifier.padding(2.dp)
                                    )
                                    .background(rgb(p.hex), CircleShape),
                                contentAlignment = Alignment.Center,
                            ) {
                                if (selected) {
                                    Icon(
                                        Icons.Outlined.Check,
                                        p.label,
                                        tint = colors.semanticInk,
                                        modifier = Modifier.size(16.dp),
                                    )
                                }
                            }
                        }
                    }
                }
                HorizontalDivider(color = colors.borderSubtle)
                Actions {
                    Choice(
                        "音质",
                        vm.quality.displayLabel(),
                        AudioQualityLevel.entries.map { it.displayLabel() },
                    ) {
                        vm.preferences(level = AudioQualityLevel.entries[it])
                    }
                    val scales = listOf(.9f, 1f, 1.1f, 1.25f)
                    Choice(
                        "字号",
                        (vm.scale * 100).toInt().toString() + "%",
                        scales.map { (it * 100).toInt().toString() + "%" },
                    ) {
                        vm.preferences(font = scales[it])
                    }
                }
                Actions {
                    Action("均衡器", style = ActionStyle.Secondary, onClick = eq)
                    Action("睡眠定时", style = ActionStyle.Secondary, onClick = timer)
                }
            }
        }
        item(key = "settings_cache") {
            Panel(Modifier.fillMaxWidth()) {
                Heading("离线音频缓存")
                val mb = vm.cacheSizeBytes / (1024.0 * 1024.0)
                Text(
                    String.format(Locale.ROOT, "当前占用 %.1f MB（支持边听边缓存与歌单 Wi-Fi 预缓存）", mb),
                    style = MaterialTheme.typography.bodySmall,
                    color = colors.secondary,
                )
                Actions {
                    Action("清理音频缓存", style = ActionStyle.Secondary) { vm.clearAudioCache() }
                }
            }
        }
        item(key = "settings_version") {
            Text(
                "Pruner Studio Mobile 0.4.0",
                style = MaterialTheme.typography.labelMedium,
                color = colors.secondary,
            )
        }
    }

    if (showAddSource) {
        AddExternalSourceSheet(
            onDismiss = { showAddSource = false },
            onSave = { config, secret ->
                vm.saveExternalSource(config, secret)
                showAddSource = false
            },
            onTest = { config, secret ->
                vm.testExternalSource(config, secret)
            },
        )
    }

    if (vm.pairingReady)
        vm.pair?.let { p ->
            PairDialog(p, vm.desktop, vm.pairingClaimInFlight, vm::cancelClaim) { vm.claim(p) }
        }
    if (vm.qrStatus.isNotBlank())
        AlertDialog(
            onDismissRequest = { vm.cancelLogin() },
            containerColor = colors.surface,
            titleContentColor = colors.text,
            textContentColor = colors.text,
            title = { Text("网易云音乐扫码登录") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    vm.qr?.let {
                        LoginQr(it)
                        Text(vm.seconds.toString() + " s", style = PrunerTypography.number)
                    }
                    Text(vm.qrStatus, color = colors.secondary)
                }
            },
            confirmButton = { Action("重新生成", style = ActionStyle.Primary) { vm.login() } },
            dismissButton = { Action("关闭", style = ActionStyle.Ghost) { vm.cancelLogin() } },
        )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddExternalSourceSheet(
    onDismiss: () -> Unit,
    onSave: (ExternalSourceConfig, String?) -> Unit,
    onTest: (ExternalSourceConfig, String?) -> Unit,
) {
    var kind by remember { mutableStateOf(ExternalSourceKind.OPENSUBSONIC) }
    var name by remember { mutableStateOf("") }
    var baseUrl by remember { mutableStateOf("") }
    var username by remember { mutableStateOf("") }
    var secret by remember { mutableStateOf("") }
    var allowLanHttp by remember { mutableStateOf(false) }

    fun buildConfig() =
        ExternalSourceConfig(
            id = UUID.randomUUID().toString().take(12),
            name = name.trim().ifBlank { kind.label },
            kind = kind,
            baseUrl = baseUrl.trim(),
            username = username.trim(),
            enabled = true,
            allowLanHttp = allowLanHttp,
        )

    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = colors.surface) {
        Column(
            Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Heading("添加外部音源")
            Segments(
                labels = ExternalSourceKind.entries.map { it.label },
                selected = ExternalSourceKind.entries.indexOf(kind),
            ) {
                kind = ExternalSourceKind.entries[it]
            }
            Field("显示名称", name) { name = it }
            Field("服务地址 (https://...)", baseUrl) { baseUrl = it }
            if (kind != ExternalSourceKind.RESOLVER_V2) {
                Field("用户名", username) { username = it }
            }
            OutlinedTextField(
                value = secret,
                onValueChange = { secret = it },
                modifier = Modifier.fillMaxWidth(),
                label = {
                    Text(if (kind == ExternalSourceKind.RESOLVER_V2) "Resolver Token / API Key（可选）" else "密码")
                },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                shape = controlShape,
                colors = prunerTextFieldColors(),
            )
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("允许局域网内网 HTTP", style = MaterialTheme.typography.bodySmall, color = colors.secondary)
                Switch(checked = allowLanHttp, onCheckedChange = { allowLanHttp = it })
            }
            Actions {
                Action("测试连通性", enabled = baseUrl.isNotBlank(), style = ActionStyle.Secondary) {
                    onTest(buildConfig(), secret.ifBlank { null })
                }
                Action("保存音源", enabled = baseUrl.isNotBlank(), style = ActionStyle.Primary) {
                    onSave(buildConfig(), secret.ifBlank { null })
                }
            }
            Spacer(Modifier.height(16.dp))
        }
    }
}

@Composable
private fun PairDialog(
    p: PairingUri,
    current: Desktop?,
    busy: Boolean,
    close: () -> Unit,
    claim: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = close,
        containerColor = colors.surface,
        titleContentColor = colors.text,
        textContentColor = colors.text,
        title = { Text("确认桌面身份") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (current == null) {
                    Text(p.name.ifBlank { "桌面主机" })
                    Text(p.host)
                    Text(p.port.toString(), style = PrunerTypography.number)
                } else {
                    Text("当前已配对：${current.name.ifBlank { "桌面主机" }} (${current.host}:${current.port})")
                    Text("替换为：${p.name.ifBlank { "桌面主机" }} (${p.host}:${p.port})")
                }
                Text(p.code, style = PrunerTypography.number)
                Text(p.fingerprint.chunked(2).joinToString(":"), style = PrunerTypography.number)
                p.spkiPin?.let {
                    Text("SPKI Pin: $it", style = PrunerTypography.number.copy(fontSize = 11.sp), color = colors.secondary)
                }
            }
        },
        confirmButton = {
            Action(
                if (current == null) "确认配对" else "替换配对",
                enabled = !busy,
                style = ActionStyle.Primary,
                onClick = claim,
            )
        },
        dismissButton = { Action("取消", style = ActionStyle.Ghost, onClick = close) },
    )
}

@Composable
fun Field(label: String, value: String, change: (String) -> Unit) {
    OutlinedTextField(
        value,
        change,
        Modifier.fillMaxWidth(),
        label = { Text(label) },
        singleLine = true,
        shape = controlShape,
        colors = prunerTextFieldColors(),
    )
}

@Composable
fun Choice(label: String, value: String, items: List<String>, select: (Int) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        Action(label + " · " + value, style = ActionStyle.Secondary) { open = true }
        DropdownMenu(open, { open = false }, containerColor = colors.surface) {
            items.forEachIndexed { i, name ->
                DropdownMenuItem(
                    { Text(name, color = colors.text) },
                    {
                        open = false
                        select(i)
                    },
                )
            }
        }
    }
}
